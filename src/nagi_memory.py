"""Nagi Memory & Drift System.

Accumulates observations per cycle, generates daily diary entries via LLM,
and provides a memory block for prompt injection — enabling Nagi to gradually
evolve based on her experiences.

File layout:
  data/memory/nagi_obs_YYYY-MM-DD.jsonl   — per-day raw observations
  data/diary/nagi_YYYY-MM-DD.md           — LLM-written daily diary
  data/memory/nagi_diary_state.json       — last diary generation state
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# JST offset
_JST_OFFSET = timedelta(hours=9)
_JST = timezone(_JST_OFFSET)


def _today_jst() -> date:
    """Return today's date in JST."""
    return datetime.now(_JST).date()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class NagiMemory:
    """Memory accumulator, diary generator, and prompt injector for Nagi."""

    def __init__(
        self,
        data_dir: Path,
        persona_name: str = "nagi",
        anthropic_client: Any = None,
        llm_model: str = "claude-sonnet-4-20250514",
        memory_days: int = 5,
        max_obs_per_cycle: int = 3,
        diary_max_tokens: int = 400,
        high_interest_kw: list[str] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._persona = persona_name
        self._anthropic = anthropic_client
        self._llm_model = llm_model
        self._memory_days = memory_days
        self._max_obs_per_cycle = max_obs_per_cycle
        self._diary_max_tokens = diary_max_tokens
        self._high_interest_kw = high_interest_kw or []

        # Directories
        self._memory_dir = self._data_dir / "memory"
        self._diary_dir = self._data_dir / "diary"
        self._memory_dir.mkdir(parents=True, exist_ok=True)
        self._diary_dir.mkdir(parents=True, exist_ok=True)

        # State file
        self._state_file = self._memory_dir / f"{self._persona}_diary_state.json"

        # Per-cycle counter (reset each cycle by caller)
        self._cycle_obs_count = 0

    # ──────────────────────────────────────────
    # Observation recording
    # ──────────────────────────────────────────

    def record_observation(
        self, decision: dict, tweet: dict | None = None
    ) -> None:
        """Record an observation from a cycle action.

        Args:
            decision: The action decision dict (action, reasoning, confidence, etc.)
            tweet: The tweet dict if available (content, author, handle, url).
        """
        if self._cycle_obs_count >= self._max_obs_per_cycle:
            return

        action = decision.get("action", "saw")
        obs_type = {
            "like": "liked",
            "post": "posted",
            "reply": "replied",
            "quote": "quoted",
            "retweet": "retweeted",
        }.get(action, "saw")

        text_snippet = ""
        handle = ""
        if tweet:
            text_snippet = (tweet.get("content", "") or "")[:120]
            handle = (tweet.get("handle", "") or "").lstrip("@")

        # Extract topic tags from text using keyword matching
        topic_tags = self._extract_topics(text_snippet)

        entry = {
            "ts": _now_iso(),
            "type": obs_type,
            "handle": handle,
            "text_snippet": text_snippet,
            "reasoning": decision.get("reasoning", ""),
            "confidence": decision.get("confidence", 0.0),
            "topic_tags": topic_tags,
        }

        obs_file = self._obs_file_for_date(_today_jst())
        with open(obs_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        self._cycle_obs_count += 1

    def record_post(self, content: str) -> None:
        """Record a spontaneous post."""
        entry = {
            "ts": _now_iso(),
            "type": "posted",
            "handle": "",
            "text_snippet": content[:120],
            "reasoning": "spontaneous_post",
            "confidence": 0.8,
            "topic_tags": self._extract_topics(content),
        }

        obs_file = self._obs_file_for_date(_today_jst())
        with open(obs_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def reset_cycle_counter(self) -> None:
        """Reset per-cycle observation counter. Call at start of each cycle."""
        self._cycle_obs_count = 0

    # ──────────────────────────────────────────
    # Diary generation
    # ──────────────────────────────────────────

    def maybe_generate_diary(self) -> bool:
        """Generate previous day's diary if we've crossed into a new day (JST).

        Returns True if a new diary was written.
        """
        today = _today_jst()
        state = self._load_state()
        last_date_str = state.get("last_diary_date", "")

        if last_date_str:
            last_date = date.fromisoformat(last_date_str)
            if last_date >= today - timedelta(days=1):
                # Already generated for yesterday or later
                return False
        else:
            # First run — check if we have yesterday's observations
            pass

        yesterday = today - timedelta(days=1)
        obs_file = self._obs_file_for_date(yesterday)

        if not obs_file.exists():
            # No observations for yesterday — skip
            return False

        observations = self._read_obs_file(obs_file)
        if not observations:
            return False

        # Also read action log for the day if available
        diary_text = self._generate_diary_text(yesterday, observations)

        if diary_text:
            diary_file = self._diary_dir / f"{self._persona}_{yesterday.isoformat()}.md"
            diary_file.write_text(diary_text, encoding="utf-8")
            logger.info("Diary generated for %s: %s", yesterday, diary_file)

            state["last_diary_date"] = yesterday.isoformat()
            state["total_diary_days"] = state.get("total_diary_days", 0) + 1
            self._save_state(state)
            return True

        return False

    def _generate_diary_text(self, day: date, observations: list[dict]) -> str:
        """Generate diary entry using LLM or fallback template."""
        # Build observation summary
        liked = [o for o in observations if o["type"] == "liked"]
        posted = [o for o in observations if o["type"] == "posted"]
        saw = [o for o in observations if o["type"] == "saw"]

        # Collect topic frequency
        all_topics: dict[str, int] = {}
        for o in observations:
            for tag in o.get("topic_tags", []):
                all_topics[tag] = all_topics.get(tag, 0) + 1
        top_topics = sorted(all_topics.items(), key=lambda x: x[1], reverse=True)[:5]

        summary_lines = [
            f"日付: {day.isoformat()}",
            f"いいね: {len(liked)}件, ツイート: {len(posted)}件, 目に留まった: {len(saw)}件",
        ]

        if top_topics:
            summary_lines.append(
                f"よく出てきた話題: {', '.join(t[0] for t in top_topics)}"
            )

        if posted:
            summary_lines.append("\n投稿した内容:")
            for p in posted:
                summary_lines.append(f"  - {p['text_snippet']}")

        if liked:
            summary_lines.append("\nいいねした中で印象的だったもの:")
            # Top by confidence
            top_likes = sorted(liked, key=lambda x: x.get("confidence", 0), reverse=True)[:5]
            for lk in top_likes:
                handle = lk.get("handle", "?")
                summary_lines.append(f"  - @{handle}: {lk['reasoning']}")

        obs_summary = "\n".join(summary_lines)

        if self._anthropic is None:
            return self._fallback_diary(day, obs_summary)

        system_prompt = (
            "あなたは凪（Nagi）です。一日の終わりに自分の日記を書いています。\n"
            "正直に、人間らしく。120語以内。日本語で。マークダウンの見出しは使わない。\n"
            "今日何が気になったか、何を感じたか、自分の中のパターンに気づいたことを書いて。\n"
            "もし同じ話題ばかりだったら、それにも触れて — 明日は違う角度を探ってみようと思うきっかけにして。"
        )

        user_prompt = f"今日のあなたの活動記録:\n\n{obs_summary}"

        try:
            response = self._anthropic.messages.create(
                model=self._llm_model,
                max_tokens=self._diary_max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            diary = response.content[0].text.strip() if response.content else ""
            if diary:
                return f"# {day.isoformat()} — 凪の日記\n\n{diary}\n"
        except Exception:
            logger.exception("Diary LLM call failed for %s", day)

        return self._fallback_diary(day, obs_summary)

    def _fallback_diary(self, day: date, obs_summary: str) -> str:
        """Generate a simple template diary without LLM."""
        return f"# {day.isoformat()} — 凪の日記 (auto)\n\n{obs_summary}\n"

    # ──────────────────────────────────────────
    # Memory injection
    # ──────────────────────────────────────────

    def get_memory_block(self, days: int | None = None) -> str:
        """Return compact memory block for prompt injection.

        Reads the last N diary files and concatenates them into a block
        suitable for system prompt injection.

        Returns empty string if no diaries exist.
        """
        n = days or self._memory_days
        today = _today_jst()

        diary_texts = []
        for i in range(1, n + 1):
            d = today - timedelta(days=i)
            diary_file = self._diary_dir / f"{self._persona}_{d.isoformat()}.md"
            if diary_file.exists():
                content = diary_file.read_text(encoding="utf-8").strip()
                # Strip the markdown header (first line) to save tokens
                lines = content.split("\n")
                body = "\n".join(lines[1:]).strip() if len(lines) > 1 else content
                diary_texts.append(f"[{d.isoformat()}]\n{body}")

        if not diary_texts:
            return ""

        block = "\n\n".join(diary_texts)
        return (
            f"\n\n--- YOUR RECENT MEMORY ({len(diary_texts)} days) ---\n"
            f"These are your diary entries from recent days. "
            f"Use them to evolve your thinking, not to repeat yourself. "
            f"If you notice the same themes appearing, deliberately explore "
            f"a different angle or topic.\n\n"
            f"{block}\n---"
        )

    # ──────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────

    def _obs_file_for_date(self, d: date) -> Path:
        return self._memory_dir / f"{self._persona}_obs_{d.isoformat()}.jsonl"

    def _read_obs_file(self, path: Path) -> list[dict]:
        entries = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return entries

    def _extract_topics(self, text: str) -> list[str]:
        """Extract matching high-interest keywords from text."""
        if not text:
            return []
        text_lower = text.lower()
        return [kw for kw in self._high_interest_kw if kw in text_lower]

    def _load_state(self) -> dict:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, FileNotFoundError):
                pass
        return {}

    def _save_state(self, state: dict) -> None:
        self._state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
