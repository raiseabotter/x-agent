"""X Agent — Autonomous X/Twitter agent loop.

Ties together:
- XAgentBrowser (browser operations) — src/x_agent_browser.py
- Claude API (content generation) — anthropic SDK
- Persona config (YAML files) — voice.yaml, value_matrix.yaml, SOUL.md

Loop: read timeline → filter by interests → decide action (LLM) → execute → log
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Project root is one level up from this file (src/x_agent.py → x-agent/)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    """Load a YAML file and return its contents."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class XAgent:
    """Autonomous X agent loop.

    Reads the home timeline, filters tweets by persona interest keywords,
    asks an LLM to decide an action for each relevant tweet, and executes
    approved actions via XAgentBrowser.
    """

    def __init__(self, config_path: Path, dry_run: bool = False) -> None:
        """Load config YAML and initialise components.

        Args:
            config_path: Path to the agent config YAML (e.g. configs/nagi.yaml).
            dry_run: If True, log decisions but do not execute browser actions.
        """
        config_path = Path(config_path)
        self._dry_run = dry_run
        self._config_path = config_path
        self._config: dict = _load_yaml(config_path)

        # Resolve persona assets
        persona_dir = _PROJECT_ROOT / self._config.get("persona_dir", "personas/nagi")
        self._voice: dict = _load_yaml(persona_dir / "voice.yaml")
        self._value_matrix: dict = _load_yaml(persona_dir / "value_matrix.yaml")
        soul_path = persona_dir / "SOUL.md"
        self._soul_summary: str = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""

        # Browser (imported lazily to avoid hard dependency if not available)
        cookie_file = _PROJECT_ROOT / self._config.get("cookie_file", "data/x_cookies.json")
        browser_cfg = self._config.get("browser", {})
        try:
            from src.x_agent_browser import XAgentBrowser  # type: ignore[import]
            self._browser: Any = XAgentBrowser(
                cookie_file=cookie_file,
                headless=browser_cfg.get("headless", True),
                timeout_ms=browser_cfg.get("timeout_ms", 15000),
                screenshot_on_error=browser_cfg.get("screenshot_on_error", True),
            )
        except ImportError:
            logger.warning("src.x_agent_browser not found — browser operations will fail")
            self._browser = None

        # Anthropic LLM client (uses ANTHROPIC_API_KEY env var)
        try:
            import anthropic  # type: ignore[import]
            self._anthropic = anthropic.Anthropic()
        except ImportError:
            logger.warning("anthropic package not installed — LLM decisions unavailable")
            self._anthropic = None

        # State
        self._running = False
        self._paused = False
        self._cycle_actions: list[dict] = []  # actions taken in current cycle

        # Persistent logs
        self._log_file = _PROJECT_ROOT / self._config.get("log_file", "data/x_actions.jsonl")
        self._pending_file = _PROJECT_ROOT / self._config.get(
            "pending_file", "data/x_pending.json"
        )
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        self._pending_file.parent.mkdir(parents=True, exist_ok=True)

        # Ensure pending file exists
        if not self._pending_file.exists():
            self._pending_file.write_text("[]", encoding="utf-8")

        # Interest keyword sets (flat, lower-cased for fast matching)
        kw = self._value_matrix.get("keywords", {})
        self._high_interest_kw: list[str] = [k.lower() for k in kw.get("high_interest", [])]
        self._moderate_interest_kw: list[str] = [k.lower() for k in kw.get("moderate_interest", [])]
        self._ignore_kw: list[str] = [k.lower() for k in kw.get("ignore", [])]

    # ──────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the autonomous loop. Runs until stop() is called."""
        if self._running:
            logger.warning("XAgent already running")
            return

        if self._browser is None:
            raise RuntimeError("XAgentBrowser not available — cannot start")

        logger.info("XAgent starting (codename=%s)", self._config.get("codename", "?"))
        await self._browser.start()
        self._running = True

        timing = self._config.get("timing", {})
        min_interval = timing.get("cycle_interval_min_minutes", 30) * 60
        max_interval = timing.get("cycle_interval_max_minutes", 90) * 60

        try:
            while self._running:
                if not self._paused and self._is_active_hour():
                    try:
                        result = await self._run_cycle()
                        logger.info("Cycle complete: %s", result.get("status"))
                    except Exception:
                        logger.exception("Cycle raised an unhandled error")

                sleep_seconds = random.uniform(min_interval, max_interval)
                logger.debug("Sleeping %.0fs until next cycle", sleep_seconds)
                await asyncio.sleep(sleep_seconds)
        finally:
            await self._browser.stop()
            self._running = False
            logger.info("XAgent stopped")

    async def stop(self) -> None:
        """Gracefully stop the loop and close browser."""
        logger.info("XAgent stop requested")
        self._running = False

    async def pause(self) -> None:
        """Pause the loop (skip cycles but stay alive)."""
        logger.info("XAgent paused")
        self._paused = True

    async def resume(self) -> None:
        """Resume after pause."""
        logger.info("XAgent resumed")
        self._paused = False

    # ──────────────────────────────────────────
    # Core cycle
    # ──────────────────────────────────────────

    async def _run_cycle(self) -> dict:
        """One cycle: read → filter → decide → act → log.

        Returns:
            dict with keys: status, actions_taken (int)
        """
        self._cycle_actions = []

        # 1. Read home timeline
        tweets = await self._read_timeline()
        if not tweets:
            return {"status": "no_tweets"}

        # 2. Filter by interest keywords
        relevant = self._filter_by_interest(tweets)
        if not relevant:
            return {"status": "no_relevant_tweets"}

        # 3. Check daily action budget
        if self._daily_actions_remaining() <= 0:
            return {"status": "budget_exhausted"}

        interest_cfg = self._config.get("interest", {})
        max_per_cycle = interest_cfg.get("max_tweets_per_cycle", 5)

        # 4. Decide and act on top-N relevant tweets
        for tweet in relevant[:max_per_cycle]:
            if self._daily_actions_remaining() <= 0:
                break

            decision = await self._decide_action(tweet)

            if decision.get("action") == "ignore":
                continue

            # 5. Autonomy gate
            if self._needs_approval(decision):
                self._queue_for_approval(decision)
                continue

            # 6. Execute (skip in dry-run mode)
            if self._dry_run:
                logger.info("[DRY-RUN] Would execute: %s on %s", decision.get("action"), decision.get("tweet_url", "")[:60])
                self._log_action(decision, success=True, dry_run=True)
                self._cycle_actions.append({"decision": decision, "success": True, "dry_run": True})
                continue
            success = await self._execute_action(decision)
            self._log_action(decision, success)
            self._cycle_actions.append({"decision": decision, "success": success})

            # 7. Human-like pause between actions
            timing = self._config.get("timing", {})
            min_delay = timing.get("min_delay_between_actions_seconds", 30)
            await asyncio.sleep(random.uniform(min_delay, min_delay * 4))

        return {"status": "ok", "actions_taken": len(self._cycle_actions)}

    # ──────────────────────────────────────────
    # Timeline
    # ──────────────────────────────────────────

    async def _read_timeline(self) -> list[dict]:
        """Read home timeline via XAgentBrowser."""
        if self._browser is None:
            logger.error("No browser available")
            return []
        try:
            return await self._browser.read_home_feed(max_tweets=20)
        except Exception:
            logger.exception("Failed to read timeline")
            return []

    # ──────────────────────────────────────────
    # Interest filtering
    # ──────────────────────────────────────────

    def _filter_by_interest(self, tweets: list[dict]) -> list[dict]:
        """Filter and rank tweets by persona interest keywords.

        Each tweet dict is expected to have at least a 'text' field.
        Scoring:
        - High-interest keyword match: +1.0 per keyword
        - Moderate-interest keyword match: +0.5 per keyword
        - Ignore keyword match: score clamped to 0.0

        Returns tweets sorted by score descending, above min_relevance_score.
        """
        interest_cfg = self._config.get("interest", {})
        min_score = interest_cfg.get("min_relevance_score", 0.3)

        scored: list[tuple[float, dict]] = []
        for tweet in tweets:
            text = (tweet.get("text", "") + " " + tweet.get("author", "")).lower()
            score = self._score_text(text)
            if score >= min_score:
                scored.append((score, tweet))

        scored.sort(key=lambda t: t[0], reverse=True)
        return [t for _, t in scored]

    def _score_text(self, text_lower: str) -> float:
        """Compute a relevance score for a lower-cased text string."""
        # Ignore signals override everything
        for kw in self._ignore_kw:
            if kw in text_lower:
                return 0.0

        score = 0.0
        for kw in self._high_interest_kw:
            if kw in text_lower:
                score += 1.0
        for kw in self._moderate_interest_kw:
            if kw in text_lower:
                score += 0.5

        # Normalise to [0, 1] roughly: cap at 3.0 high-interest matches
        return min(score / 3.0, 1.0) if score > 0 else 0.0

    # ──────────────────────────────────────────
    # LLM decision
    # ──────────────────────────────────────────

    async def _decide_action(self, tweet: dict) -> dict:
        """Ask the LLM to decide what action to take on a tweet.

        Returns:
            dict with keys: action, content, confidence, reasoning, tweet_url, tweet_id
        """
        if self._anthropic is None:
            return {"action": "ignore", "content": "", "confidence": 0.0,
                    "reasoning": "LLM unavailable"}

        autonomy_cfg = self._config.get("autonomy", {})
        allowed_actions = autonomy_cfg.get("allowed_actions", ["like"])
        require_approval = autonomy_cfg.get("require_approval", ["post", "reply", "quote"])

        system_prompt = self._build_system_prompt(allowed_actions, require_approval)
        user_prompt = self._build_user_prompt(tweet)

        llm_cfg = self._config.get("llm", {})
        model = llm_cfg.get("model", "claude-sonnet-4-20250514")
        max_tokens = llm_cfg.get("max_tokens_per_decision", 500)

        try:
            response = self._anthropic.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text if response.content else ""
            decision = self._parse_llm_decision(raw)
        except Exception:
            logger.exception("LLM decision call failed")
            decision = {"action": "ignore", "content": "", "confidence": 0.0,
                        "reasoning": "LLM call failed"}

        # Attach tweet metadata
        decision["tweet_url"] = tweet.get("url", "")
        decision["tweet_id"] = tweet.get("id", "")
        return decision

    def _build_system_prompt(
        self, allowed_actions: list[str], require_approval: list[str]
    ) -> str:
        """Build the LLM system prompt from persona assets and autonomy rules."""
        voice_x = (
            self._voice.get("voice", {})
            .get("platform_adaptation", {})
            .get("x", {})
        )
        tone = voice_x.get("tone", "warm, experience-driven")
        style_guide = voice_x.get("format", "")

        # Flatten interest keywords for the prompt
        kw_high = ", ".join(self._high_interest_kw[:10])

        soul_excerpt = self._soul_summary[:600].strip() if self._soul_summary else ""

        return f"""You are an autonomous social media agent acting as the persona described below.

--- PERSONA SUMMARY ---
{soul_excerpt}

--- VOICE STYLE ---
Tone: {tone}
Format guidance: {style_guide}

--- PRIMARY INTEREST KEYWORDS ---
{kw_high}

--- AUTONOMY RULES ---
You may autonomously perform these actions (no approval needed): {', '.join(allowed_actions)}
These actions require human approval before execution: {', '.join(require_approval)}

Your role is to evaluate a tweet and decide whether and how to engage.
You must respond ONLY with a valid JSON object — no prose, no markdown fences."""

    def _build_user_prompt(self, tweet: dict) -> str:
        """Build the LLM user prompt for a specific tweet."""
        author = tweet.get("author", "Unknown")
        handle = tweet.get("handle", "")
        text = tweet.get("text", "")

        return f"""Evaluate this tweet and decide on an action.

TWEET:
Author: {author} (@{handle})
Text: {text}

AVAILABLE ACTIONS:
- "like"     — appreciate the tweet
- "retweet"  — share without comment
- "reply"    — respond with a short message (provide content)
- "quote"    — quote-tweet with your own comment (provide content)
- "post"     — post a new tweet inspired by this (provide content)
- "ignore"   — do nothing

Respond with exactly this JSON structure:
{{
  "action": "<action>",
  "content": "<text for reply/quote/post, or empty string>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one sentence>"
}}"""

    def _parse_llm_decision(self, raw: str) -> dict:
        """Parse a JSON decision from LLM output.

        Attempts to find the first JSON object in the response. Falls back to
        ignore on parse failure.
        """
        # Try to extract a JSON object from the response
        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                action = str(data.get("action", "ignore")).lower()
                return {
                    "action": action,
                    "content": str(data.get("content", "")),
                    "confidence": float(data.get("confidence", 0.5)),
                    "reasoning": str(data.get("reasoning", "")),
                }
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        logger.warning("Could not parse LLM decision from: %s", raw[:200])
        return {"action": "ignore", "content": "", "confidence": 0.0,
                "reasoning": "parse_failed"}

    # ──────────────────────────────────────────
    # Autonomy / approval
    # ──────────────────────────────────────────

    def _needs_approval(self, decision: dict) -> bool:
        """Check if this action needs manual approval.

        autonomy.level:
          'manual'  — all actions need approval
          'semi'    — like/retweet auto, rest need approval
          'full'    — all auto if confidence >= confidence_threshold
        autonomy.require_approval overrides level for specific action types.
        """
        autonomy_cfg = self._config.get("autonomy", {})
        level = autonomy_cfg.get("level", "manual")
        action = decision.get("action", "ignore")
        confidence = float(decision.get("confidence", 0.0))
        require_approval: list[str] = autonomy_cfg.get("require_approval", [])
        allowed_actions: list[str] = autonomy_cfg.get("allowed_actions", [])

        # Action not in the allowed list at all → always needs approval
        if action not in allowed_actions and action not in ("ignore",):
            return True

        # Explicitly listed as always-requiring approval
        if action in require_approval:
            return True

        if level == "manual":
            return True

        if level == "semi":
            # like and retweet are automatic; everything else needs approval
            return action not in ("like", "retweet")

        if level == "full":
            threshold = float(autonomy_cfg.get("confidence_threshold", 0.7))
            return confidence < threshold

        # Unknown level — require approval
        return True

    def _queue_for_approval(self, decision: dict) -> None:
        """Queue decision for manual approval. Saves to pending_actions.json."""
        pending = self._load_pending()
        entry = {
            "id": str(uuid.uuid4()),
            "queued_at": _now_iso(),
            "decision": decision,
            "status": "pending",
        }
        pending.append(entry)
        self._save_pending(pending)
        logger.info(
            "Queued for approval: action=%s tweet=%s",
            decision.get("action"),
            decision.get("tweet_url", "")[:60],
        )

    # ──────────────────────────────────────────
    # Execution
    # ──────────────────────────────────────────

    async def _execute_action(self, decision: dict) -> bool:
        """Execute the decided action via XAgentBrowser.

        Returns True on success, False on failure.
        """
        if self._browser is None:
            logger.error("Cannot execute action — no browser")
            return False

        action = decision.get("action", "ignore")
        tweet_url = decision.get("tweet_url", "")
        content = decision.get("content", "")

        try:
            if action == "like":
                return await self._browser.like_tweet(tweet_url)
            elif action == "retweet":
                return await self._browser.retweet(tweet_url)
            elif action == "reply":
                return await self._browser.reply_to_tweet(tweet_url, content)
            elif action == "post":
                return await self._browser.post_tweet(content)
            elif action == "quote":
                return await self._browser.quote_tweet(tweet_url, content)
            else:
                logger.warning("Unknown action type: %s", action)
                return False
        except Exception:
            logger.exception("Failed to execute action=%s", action)
            return False

    # ──────────────────────────────────────────
    # Content generation
    # ──────────────────────────────────────────

    async def _generate_content(self, context: str, action_type: str) -> str:
        """Generate tweet content using Claude API with persona voice guidance.

        Args:
            context: The context or seed for the content (e.g. source tweet text).
            action_type: The type of action ('reply', 'quote', 'post').

        Returns:
            Generated text string, or empty string on failure.
        """
        if self._anthropic is None:
            return ""

        voice_x = (
            self._voice.get("voice", {})
            .get("platform_adaptation", {})
            .get("x", {})
        )
        tone = voice_x.get("tone", "warm, experience-driven")
        example = voice_x.get("example_ja", "")
        forbidden = "\n".join(
            f"- {item}"
            for item in self._voice.get("voice", {}).get("forbidden", [])
        )

        soul_excerpt = self._soul_summary[:400].strip() if self._soul_summary else ""

        system_prompt = f"""You are writing as the following persona for X (Twitter).

--- PERSONA ---
{soul_excerpt}

--- TONE ---
{tone}

--- EXAMPLE POST ---
{example}

--- FORBIDDEN ---
{forbidden}

Write in Japanese (primary). Max 280 characters. No markdown.
Output ONLY the tweet text — nothing else."""

        user_prompt = f"Action type: {action_type}\nContext:\n{context}"

        llm_cfg = self._config.get("llm", {})
        model = llm_cfg.get("model", "claude-sonnet-4-20250514")
        max_tokens = llm_cfg.get("max_tokens_per_generation", 1000)

        try:
            response = self._anthropic.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text.strip() if response.content else ""
        except Exception:
            logger.exception("Content generation failed")
            return ""

    # ──────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────

    def _log_action(self, decision: dict, success: bool, dry_run: bool = False) -> None:
        """Append a single action to the JSONL action log."""
        entry = {
            "timestamp": _now_iso(),
            "action": decision.get("action"),
            "tweet_url": decision.get("tweet_url", ""),
            "content": decision.get("content", ""),
            "confidence": decision.get("confidence", 0.0),
            "reasoning": decision.get("reasoning", ""),
            "success": success,
            "dry_run": dry_run,
        }
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ──────────────────────────────────────────
    # Budget tracking
    # ──────────────────────────────────────────

    def _daily_actions_remaining(self) -> int:
        """Return how many actions are left for today based on config max_actions_per_day."""
        max_actions = int(
            self._config.get("autonomy", {}).get("max_actions_per_day", 5)
        )
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        count = 0

        if not self._log_file.exists():
            return max_actions

        with open(self._log_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", "").startswith(today) and entry.get("success"):
                        count += 1
                except (json.JSONDecodeError, TypeError):
                    continue

        return max(0, max_actions - count)

    def _is_active_hour(self) -> bool:
        """Return True if current JST hour is within the configured active window."""
        import zoneinfo  # Python 3.9+

        timing = self._config.get("timing", {})
        start = int(timing.get("active_hours_start", 8))
        end = int(timing.get("active_hours_end", 24))

        try:
            jst = zoneinfo.ZoneInfo("Asia/Tokyo")
            now_jst = datetime.now(jst)
        except Exception:
            # Fallback: assume UTC+9
            from datetime import timedelta
            now_jst = datetime.now(timezone.utc) + timedelta(hours=9)

        hour = now_jst.hour
        return start <= hour < end

    # ──────────────────────────────────────────
    # Pending actions helpers
    # ──────────────────────────────────────────

    def _load_pending(self) -> list[dict]:
        """Load pending actions from disk."""
        try:
            return json.loads(self._pending_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save_pending(self, pending: list[dict]) -> None:
        """Save pending actions to disk."""
        self._pending_file.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ──────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current agent status for API / Discord."""
        return {
            "codename": self._config.get("codename", "?"),
            "running": self._running,
            "paused": self._paused,
            "daily_actions_remaining": self._daily_actions_remaining(),
            "autonomy_level": self._config.get("autonomy", {}).get("level", "manual"),
            "pending_count": len(self.get_pending_actions()),
        }

    def get_log(self, n: int = 20) -> list[dict]:
        """Return last n logged actions."""
        if not self._log_file.exists():
            return []

        lines: list[str] = []
        with open(self._log_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(line.strip())

        entries: list[dict] = []
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def get_pending_actions(self) -> list[dict]:
        """Return all pending-approval actions."""
        return [e for e in self._load_pending() if e.get("status") == "pending"]

    def approve_action(self, action_id: str) -> bool:
        """Approve a pending action by ID.

        Schedules the action for execution on the next cycle by marking it
        'approved'. Returns True if the ID was found, False otherwise.
        """
        pending = self._load_pending()
        found = False
        for entry in pending:
            if entry.get("id") == action_id and entry.get("status") == "pending":
                entry["status"] = "approved"
                entry["approved_at"] = _now_iso()
                found = True
                break
        if found:
            self._save_pending(pending)
            # Schedule execution immediately in background
            asyncio.ensure_future(self._execute_approved(action_id))
        return found

    async def _execute_approved(self, action_id: str) -> None:
        """Execute a previously approved action."""
        pending = self._load_pending()
        for entry in pending:
            if entry.get("id") == action_id and entry.get("status") == "approved":
                decision = entry.get("decision", {})
                success = await self._execute_action(decision)
                self._log_action(decision, success)
                entry["status"] = "executed" if success else "failed"
                entry["executed_at"] = _now_iso()
                break
        self._save_pending(pending)

    def reject_action(self, action_id: str) -> bool:
        """Reject a pending action by ID. Returns True if found."""
        pending = self._load_pending()
        found = False
        for entry in pending:
            if entry.get("id") == action_id and entry.get("status") == "pending":
                entry["status"] = "rejected"
                entry["rejected_at"] = _now_iso()
                found = True
                break
        if found:
            self._save_pending(pending)
        return found

    def update_config(self, key: str, value: Any) -> None:
        """Update a config value at runtime and persist to disk.

        Supports dot-notation for nested keys, e.g. 'autonomy.level'.
        """
        keys = key.split(".")
        target = self._config
        for k in keys[:-1]:
            if k not in target or not isinstance(target[k], dict):
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value

        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)

        logger.info("Config updated: %s = %r", key, value)
