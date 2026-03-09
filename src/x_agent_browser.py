"""XAgentBrowser -- X/Twitter browser agent with persistent page state.

Provides human-like autonomous interactions with X (Twitter) via Playwright:
  - Home feed reading
  - Liking, retweeting, replying, posting, quote-tweeting

Unlike BrowserReader (stateless, new page per call), XAgentBrowser maintains
a SINGLE persistent page/tab for all operations.

Sync Playwright API is used internally (matching BrowserReader's approach).
The public interface is async; sync Playwright calls are wrapped with
``asyncio.to_thread()`` so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page  # noqa: F401

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Timing constants (in seconds unless noted)
# ---------------------------------------------------------------------------

# Per-character typing delay range (ms)
_TYPE_DELAY_MIN_MS = 20
_TYPE_DELAY_MAX_MS = 80

# Pause ranges by context (seconds)
_PAUSE_RANGES: dict[str, tuple[float, float]] = {
    "thinking": (2.0, 5.0),
    "reading": (1.0, 3.0),
    "before_post": (3.0, 8.0),
    "between_actions": (1.0, 3.0),
}

# Minimum seconds between consecutive external actions (rate-limit awareness)
_MIN_ACTION_INTERVAL_S: float = 30.0

# Playwright element wait timeout (ms)
_WAIT_TIMEOUT_MS = 10_000

# ---------------------------------------------------------------------------
# DOM selectors
# ---------------------------------------------------------------------------

_SEL_TWEET_ARTICLE = 'article[data-testid="tweet"]'
_SEL_TWEET_TEXT = '[data-testid="tweetText"]'
_SEL_USER_NAME = '[data-testid="User-Name"]'
_SEL_LIKE_BTN = '[data-testid="like"]'
_SEL_UNLIKE_BTN = '[data-testid="unlike"]'
_SEL_RETWEET_BTN = '[data-testid="retweet"]'
_SEL_REPLY_BTN = '[data-testid="reply"]'
_SEL_COMPOSE_AREA = '[data-testid="tweetTextarea_0"]'
_SEL_SUBMIT_BTN = '[data-testid="tweetButtonInline"]'
# Retweet confirmation popup "Repost" button (appears after clicking retweet icon)
_SEL_REPOST_CONFIRM = '[data-testid="retweetConfirm"]'
# Quote-tweet compose area inside the quote modal
_SEL_QUOTE_TWEET_BTN = '[data-testid="quoteTweetBtn"]'


class XAgentBrowser:
    """X-specific browser agent with a persistent page/tab.

    Maintains a single Playwright page across all operations so that
    login state, DOM caches, and navigation history persist between calls.

    Args:
        cookie_file: Path to Playwright-format cookie JSON (array of dicts).
        headless: Whether to run Chromium in headless mode.
        min_action_interval_s: Minimum seconds between actions (rate-limit guard).
        screenshots_dir: Directory for debug screenshots.
    """

    def __init__(
        self,
        cookie_file: Path,
        headless: bool = True,
        min_action_interval_s: float = _MIN_ACTION_INTERVAL_S,
        screenshots_dir: Path | None = None,
    ) -> None:
        self._cookie_file = Path(cookie_file)
        self._headless = headless
        self._min_action_interval_s = min_action_interval_s
        self._screenshots_dir = screenshots_dir or Path("data/screenshots")

        # Internal Playwright objects (populated by start())
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

        # Timestamp of last action for rate-limit enforcement
        self._last_action_ts: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch browser, load cookies, navigate to x.com, verify login."""
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright is required. "
                "Install with: pip install playwright && python -m playwright install chromium"
            )

        self._screenshots_dir.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(self._sync_start)
        logger.info("XAgentBrowser started (headless=%s)", self._headless)

    def _sync_start(self) -> None:
        """Synchronous portion of start() — runs in a thread."""
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless)
        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_timeout(_WAIT_TIMEOUT_MS)

        # Load cookies
        if self._cookie_file.exists():
            try:
                cookies = json.loads(self._cookie_file.read_text(encoding="utf-8"))
                self._context.add_cookies(cookies)
                logger.debug("Loaded %d cookies from %s", len(cookies), self._cookie_file)
            except Exception:
                logger.warning(
                    "Failed to load cookies from %s", self._cookie_file, exc_info=True
                )
        else:
            logger.warning("Cookie file not found: %s", self._cookie_file)

        # Open persistent page and go to x.com home
        self._page = self._context.new_page()
        self._page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)

        # Verify login
        if not self._sync_check_login():
            logger.warning("XAgentBrowser: login check failed — session may be expired")

    async def stop(self) -> None:
        """Close page and browser cleanly."""
        await asyncio.to_thread(self._sync_stop)
        logger.info("XAgentBrowser stopped")

    def _sync_stop(self) -> None:
        """Synchronous portion of stop() — runs in a thread."""
        try:
            if self._page is not None:
                self._page.close()
                self._page = None
            if self._context is not None:
                self._context.close()
                self._context = None
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if self._pw is not None:
                self._pw.stop()
                self._pw = None
        except Exception:
            logger.debug("XAgentBrowser cleanup error", exc_info=True)

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------

    async def read_home_feed(self, max_tweets: int = 20) -> list[dict]:
        """Read home timeline.

        Returns:
            List of dicts with keys:
              tweet_id, author, handle, content, url, timestamp,
              tweet_type, has_media
        """
        return await asyncio.to_thread(self._sync_read_home_feed, max_tweets)

    def _sync_read_home_feed(self, max_tweets: int) -> list[dict]:
        page = self._page
        try:
            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_selector(_SEL_TWEET_ARTICLE, timeout=_WAIT_TIMEOUT_MS)
            except Exception:
                logger.info("read_home_feed: no tweets visible on home timeline")
                return []

            tweets: list[dict] = []
            seen_ids: set[str] = set()
            no_new_count = 0

            while len(tweets) < max_tweets and no_new_count < 3:
                articles = page.query_selector_all(_SEL_TWEET_ARTICLE)
                new_found = False

                for article in articles:
                    if len(tweets) >= max_tweets:
                        break
                    parsed = self._parse_article(article)
                    if parsed is None:
                        continue
                    tid = parsed.get("tweet_id", "")
                    if tid and tid in seen_ids:
                        continue
                    if tid:
                        seen_ids.add(tid)
                    tweets.append(parsed)
                    new_found = True

                if not new_found:
                    no_new_count += 1
                else:
                    no_new_count = 0

                # Scroll down
                page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
                page.wait_for_timeout(int(random.uniform(800, 1500)))

            logger.info("read_home_feed: collected %d tweets", len(tweets))
            return tweets

        except Exception:
            logger.warning("read_home_feed failed", exc_info=True)
            self._sync_screenshot("read_home_feed_error")
            return []

    async def like_tweet(self, tweet_url: str) -> bool:
        """Navigate to tweet, click like button.

        Returns:
            True on success, False on failure.
        """
        return await asyncio.to_thread(self._sync_like_tweet, tweet_url)

    def _sync_like_tweet(self, tweet_url: str) -> bool:
        await_action = self._enforce_rate_limit_sync()
        if await_action > 0:
            time.sleep(await_action)

        try:
            self._page.goto(tweet_url, wait_until="domcontentloaded", timeout=20_000)
            self._page.wait_for_selector(_SEL_TWEET_ARTICLE, timeout=_WAIT_TIMEOUT_MS)

            # Check if already liked
            unlike_btn = self._page.query_selector(_SEL_UNLIKE_BTN)
            if unlike_btn:
                logger.info("like_tweet: tweet already liked — %s", tweet_url)
                return True

            like_btn = self._page.query_selector(_SEL_LIKE_BTN)
            if not like_btn:
                logger.warning("like_tweet: like button not found — %s", tweet_url)
                self._sync_screenshot("like_tweet_no_button")
                return False

            like_btn.click()
            self._page.wait_for_timeout(int(random.uniform(500, 1200)))
            self._last_action_ts = time.monotonic()
            logger.info("like_tweet: liked %s", tweet_url)
            return True

        except Exception:
            logger.warning("like_tweet failed for %s", tweet_url, exc_info=True)
            self._sync_screenshot("like_tweet_error")
            return False

    async def retweet(self, tweet_url: str) -> bool:
        """Navigate to tweet, click retweet (repost) button.

        Returns:
            True on success, False on failure.
        """
        return await asyncio.to_thread(self._sync_retweet, tweet_url)

    def _sync_retweet(self, tweet_url: str) -> bool:
        wait_s = self._enforce_rate_limit_sync()
        if wait_s > 0:
            time.sleep(wait_s)

        try:
            self._page.goto(tweet_url, wait_until="domcontentloaded", timeout=20_000)
            self._page.wait_for_selector(_SEL_TWEET_ARTICLE, timeout=_WAIT_TIMEOUT_MS)

            retweet_btn = self._page.query_selector(_SEL_RETWEET_BTN)
            if not retweet_btn:
                logger.warning("retweet: retweet button not found — %s", tweet_url)
                self._sync_screenshot("retweet_no_button")
                return False

            retweet_btn.click()
            # Wait for confirmation popup
            try:
                self._page.wait_for_selector(_SEL_REPOST_CONFIRM, timeout=5_000)
                confirm_btn = self._page.query_selector(_SEL_REPOST_CONFIRM)
                if confirm_btn:
                    confirm_btn.click()
            except Exception:
                logger.debug("retweet: no confirmation popup found — may have retweeted directly")

            self._page.wait_for_timeout(int(random.uniform(500, 1200)))
            self._last_action_ts = time.monotonic()
            logger.info("retweet: retweeted %s", tweet_url)
            return True

        except Exception:
            logger.warning("retweet failed for %s", tweet_url, exc_info=True)
            self._sync_screenshot("retweet_error")
            return False

    async def reply_to_tweet(self, tweet_url: str, text: str) -> bool:
        """Navigate to tweet, compose and submit a reply.

        Uses human-like typing delays. Returns True on success.
        """
        return await asyncio.to_thread(self._sync_reply_to_tweet, tweet_url, text)

    def _sync_reply_to_tweet(self, tweet_url: str, text: str) -> bool:
        wait_s = self._enforce_rate_limit_sync()
        if wait_s > 0:
            time.sleep(wait_s)

        try:
            self._page.goto(tweet_url, wait_until="domcontentloaded", timeout=20_000)
            self._page.wait_for_selector(_SEL_TWEET_ARTICLE, timeout=_WAIT_TIMEOUT_MS)

            # Pause to "read" the tweet before replying
            self._sync_human_pause_blocking("reading")

            reply_btn = self._page.query_selector(_SEL_REPLY_BTN)
            if not reply_btn:
                logger.warning("reply_to_tweet: reply button not found — %s", tweet_url)
                self._sync_screenshot("reply_no_button")
                return False

            reply_btn.click()
            # Wait for compose area to open
            try:
                self._page.wait_for_selector(_SEL_COMPOSE_AREA, timeout=8_000)
            except Exception:
                logger.warning("reply_to_tweet: compose area did not appear — %s", tweet_url)
                self._sync_screenshot("reply_compose_timeout")
                return False

            self._sync_human_type_blocking(_SEL_COMPOSE_AREA, text)
            self._sync_human_pause_blocking("before_post")

            submit_btn = self._page.query_selector(_SEL_SUBMIT_BTN)
            if not submit_btn:
                logger.warning("reply_to_tweet: submit button not found — %s", tweet_url)
                self._sync_screenshot("reply_no_submit")
                return False

            submit_btn.click()
            self._page.wait_for_timeout(int(random.uniform(1000, 2000)))
            self._last_action_ts = time.monotonic()
            logger.info("reply_to_tweet: replied to %s", tweet_url)
            return True

        except Exception:
            logger.warning("reply_to_tweet failed for %s", tweet_url, exc_info=True)
            self._sync_screenshot("reply_error")
            return False

    async def post_tweet(self, text: str) -> bool:
        """Navigate to compose, type with human-like delays, and submit.

        Returns True on success.
        """
        return await asyncio.to_thread(self._sync_post_tweet, text)

    def _sync_post_tweet(self, text: str) -> bool:
        wait_s = self._enforce_rate_limit_sync()
        if wait_s > 0:
            time.sleep(wait_s)

        try:
            self._page.goto("https://x.com/compose/tweet", wait_until="domcontentloaded", timeout=20_000)
            try:
                self._page.wait_for_selector(_SEL_COMPOSE_AREA, timeout=_WAIT_TIMEOUT_MS)
            except Exception:
                # Fallback: navigate to home and look for compose area
                self._page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=20_000)
                self._page.wait_for_selector(_SEL_COMPOSE_AREA, timeout=_WAIT_TIMEOUT_MS)

            self._sync_human_type_blocking(_SEL_COMPOSE_AREA, text)
            self._sync_human_pause_blocking("before_post")

            submit_btn = self._page.query_selector(_SEL_SUBMIT_BTN)
            if not submit_btn:
                logger.warning("post_tweet: submit button not found")
                self._sync_screenshot("post_no_submit")
                return False

            submit_btn.click()
            self._page.wait_for_timeout(int(random.uniform(1000, 2000)))
            self._last_action_ts = time.monotonic()
            logger.info("post_tweet: posted tweet")
            return True

        except Exception:
            logger.warning("post_tweet failed", exc_info=True)
            self._sync_screenshot("post_tweet_error")
            return False

    async def quote_tweet(self, tweet_url: str, text: str) -> bool:
        """Click quote on tweet, type comment, and submit.

        Returns True on success.
        """
        return await asyncio.to_thread(self._sync_quote_tweet, tweet_url, text)

    def _sync_quote_tweet(self, tweet_url: str, text: str) -> bool:
        wait_s = self._enforce_rate_limit_sync()
        if wait_s > 0:
            time.sleep(wait_s)

        try:
            self._page.goto(tweet_url, wait_until="domcontentloaded", timeout=20_000)
            self._page.wait_for_selector(_SEL_TWEET_ARTICLE, timeout=_WAIT_TIMEOUT_MS)

            self._sync_human_pause_blocking("reading")

            # Click the retweet icon to open options (quote is in the popup)
            retweet_btn = self._page.query_selector(_SEL_RETWEET_BTN)
            if not retweet_btn:
                logger.warning("quote_tweet: retweet button not found — %s", tweet_url)
                self._sync_screenshot("quote_no_retweet_btn")
                return False

            retweet_btn.click()

            # Wait for popup with "Quote" option
            try:
                quote_btn = self._page.wait_for_selector(
                    _SEL_QUOTE_TWEET_BTN, timeout=5_000
                )
            except Exception:
                logger.warning("quote_tweet: quote option did not appear — %s", tweet_url)
                self._sync_screenshot("quote_popup_timeout")
                return False

            quote_btn.click()

            # Wait for quote compose area
            try:
                self._page.wait_for_selector(_SEL_COMPOSE_AREA, timeout=8_000)
            except Exception:
                logger.warning("quote_tweet: compose area did not appear — %s", tweet_url)
                self._sync_screenshot("quote_compose_timeout")
                return False

            self._sync_human_type_blocking(_SEL_COMPOSE_AREA, text)
            self._sync_human_pause_blocking("before_post")

            submit_btn = self._page.query_selector(_SEL_SUBMIT_BTN)
            if not submit_btn:
                logger.warning("quote_tweet: submit button not found — %s", tweet_url)
                self._sync_screenshot("quote_no_submit")
                return False

            submit_btn.click()
            self._page.wait_for_timeout(int(random.uniform(1000, 2000)))
            self._last_action_ts = time.monotonic()
            logger.info("quote_tweet: quoted %s", tweet_url)
            return True

        except Exception:
            logger.warning("quote_tweet failed for %s", tweet_url, exc_info=True)
            self._sync_screenshot("quote_tweet_error")
            return False

    # ------------------------------------------------------------------
    # Async helper interface (thin wrappers around blocking helpers)
    # ------------------------------------------------------------------

    async def _human_type(self, selector: str, text: str) -> None:
        """Type text with human-like delays (20-80ms per char, occasional pauses).

        Uses page.type() with a random per-character delay.
        """
        await asyncio.to_thread(self._sync_human_type_blocking, selector, text)

    async def _human_pause(self, context: str = "thinking") -> None:
        """Random pause.

        contexts: thinking(2-5s), reading(1-3s), before_post(3-8s), between_actions(1-3s)
        """
        await asyncio.to_thread(self._sync_human_pause_blocking, context)

    async def _check_login(self) -> bool:
        """Verify cookies are valid by checking if we're logged in."""
        return await asyncio.to_thread(self._sync_check_login)

    async def _scroll_feed(self, scrolls: int = 3) -> None:
        """Scroll the feed with human-like behavior."""
        await asyncio.to_thread(self._sync_scroll_feed, scrolls)

    async def _take_screenshot(self, name: str) -> Path:
        """Save a debug screenshot to data/screenshots/."""
        return await asyncio.to_thread(self._sync_screenshot, name)

    # ------------------------------------------------------------------
    # Internal synchronous helpers (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _sync_human_type_blocking(self, selector: str, text: str) -> None:
        """Type each character with a random delay via page.type().

        Uses Playwright's built-in per-character delay for the bulk of the
        text, then adds occasional longer pauses after punctuation to
        simulate natural typing rhythm.
        """
        try:
            el = self._page.query_selector(selector)
            if el is None:
                logger.warning("_sync_human_type_blocking: selector not found: %s", selector)
                return
            el.click()
            self._page.wait_for_timeout(int(random.uniform(200, 500)))

            # Type character by character to allow variable delays
            for char in text:
                delay_ms = random.randint(_TYPE_DELAY_MIN_MS, _TYPE_DELAY_MAX_MS)
                # Punctuation → extra pause
                if char in ".!?":
                    delay_ms += random.randint(100, 400)
                elif char in ",;:":
                    delay_ms += random.randint(50, 150)
                elif char == " ":
                    delay_ms += random.randint(10, 60)
                self._page.type(selector, char, delay=delay_ms)

            logger.debug("_sync_human_type_blocking: typed %d chars", len(text))

        except Exception:
            logger.warning("_sync_human_type_blocking failed", exc_info=True)

    def _sync_human_pause_blocking(self, context: str = "thinking") -> None:
        """Block the current thread for a random duration based on context."""
        min_s, max_s = _PAUSE_RANGES.get(context, _PAUSE_RANGES["between_actions"])
        duration_s = random.uniform(min_s, max_s)
        time.sleep(duration_s)

    def _sync_check_login(self) -> bool:
        """Return True if the current page is not a login redirect."""
        try:
            current_url = self._page.url
            if "login" in current_url.lower() and "/status/" not in current_url:
                return False
            # Additional check: look for home feed or user avatar
            avatar = self._page.query_selector('[data-testid="SideNav_AccountSwitcher_Button"]')
            return avatar is not None
        except Exception:
            logger.debug("_sync_check_login: exception during check", exc_info=True)
            return False

    def _sync_scroll_feed(self, scrolls: int = 3) -> None:
        """Scroll the feed with human-like pauses between scrolls."""
        for _ in range(max(1, scrolls)):
            scroll_px = random.randint(300, 700)
            self._page.evaluate(f"window.scrollBy(0, {scroll_px})")
            pause_ms = random.randint(800, 2000)
            self._page.wait_for_timeout(pause_ms)

    def _sync_screenshot(self, name: str) -> Path:
        """Take a debug screenshot; return the path written."""
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        # Sanitize name for use in filename
        safe_name = re.sub(r"[^\w\-]", "_", name)
        path = self._screenshots_dir / f"{safe_name}_{ts}.png"
        try:
            if self._page is not None:
                self._page.screenshot(path=str(path))
                logger.debug("Screenshot saved: %s", path)
        except Exception:
            logger.debug("Screenshot failed for %s", name, exc_info=True)
        return path

    # ------------------------------------------------------------------
    # Article parsing (mirrors x_profile_scraper._parse_article logic)
    # ------------------------------------------------------------------

    def _parse_article(self, article: Any) -> dict | None:
        """Parse a single tweet article element into a dict.

        Returns None if the article cannot be parsed or yields no useful data.
        """
        try:
            # --- URL and tweet_id ---
            tweet_url = ""
            tweet_id = ""
            link_el = article.query_selector('a[href*="/status/"]')
            if link_el:
                href = link_el.get_attribute("href") or ""
                if href:
                    tweet_url = f"https://x.com{href}" if href.startswith("/") else href
                    match = re.search(r"/status/(\d+)", tweet_url)
                    if match:
                        tweet_id = match.group(1)

            # --- Author display name and handle ---
            author = ""
            handle = ""
            # Try div-scoped selector first, then the bare attribute selector as fallback
            author_el = article.query_selector(f"div{_SEL_USER_NAME}")
            if author_el is None:
                author_el = article.query_selector(_SEL_USER_NAME)
            if author_el:
                raw_text = author_el.inner_text() or ""
                lines = [ln.strip() for ln in raw_text.split("\n") if ln.strip()]
                # First line = display name, look for @handle anywhere
                author = lines[0] if lines else ""
                handle_match = re.search(r"@(\w+)", raw_text)
                handle = f"@{handle_match.group(1)}" if handle_match else ""

            # --- Tweet text ---
            text_el = article.query_selector(_SEL_TWEET_TEXT)
            content = text_el.inner_text().strip() if text_el else ""

            if not content and not tweet_id:
                return None

            # --- Timestamp ---
            timestamp = ""
            time_el = article.query_selector("time")
            if time_el:
                timestamp = time_el.get_attribute("datetime") or ""

            # --- Tweet type ---
            tweet_type = self._classify_tweet_type(article)

            # --- Media presence ---
            has_media = bool(
                article.query_selector('div[data-testid="tweetPhoto"]')
                or article.query_selector('div[data-testid="videoComponent"]')
                or article.query_selector('img[src*="pbs.twimg.com/media"]')
            )

            return {
                "tweet_id": tweet_id,
                "author": author,
                "handle": handle,
                "content": content,
                "url": tweet_url,
                "timestamp": timestamp,
                "tweet_type": tweet_type,
                "has_media": has_media,
            }

        except Exception:
            logger.debug("_parse_article: failed to parse article", exc_info=True)
            return None

    def _classify_tweet_type(self, article: Any) -> str:
        """Classify tweet type from DOM indicators.

        Priority order: retweet → reply → quote_tweet → tweet
        """
        try:
            # Retweet: social context indicator
            social_ctx = article.query_selector('[data-testid="socialContext"]')
            if social_ctx:
                ctx_text = (social_ctx.inner_text() or "").lower()
                if "repost" in ctx_text or "リポスト" in ctx_text:
                    return "retweet"

            article_text = ""
            try:
                article_text = (article.inner_text() or "").lower()
            except Exception:
                pass

            # Reply
            if "replying to" in article_text or "返信先" in article_text:
                return "reply"

            # Quote tweet
            if article.query_selector('[data-testid="quoteTweet"]'):
                return "quote"

            # Nested article fallback for quote detection
            inner_articles = article.query_selector_all("article")
            if inner_articles:
                return "quote"

        except Exception:
            logger.debug("_classify_tweet_type: exception", exc_info=True)

        return "tweet"

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _enforce_rate_limit_sync(self) -> float:
        """Return the number of seconds the caller must sleep before acting.

        Does NOT sleep itself — the caller is responsible for sleeping.
        """
        elapsed = time.monotonic() - self._last_action_ts
        if elapsed < self._min_action_interval_s:
            remaining = self._min_action_interval_s - elapsed
            logger.debug(
                "_enforce_rate_limit_sync: waiting %.1fs before next action", remaining
            )
            return remaining
        return 0.0
