"""Tests for CAPTCHA / challenge detection and auto-pause."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.x_agent_browser import XAgentBrowser, _CHALLENGE_URL_PATTERNS


# ---------------------------------------------------------------------------
# XAgentBrowser.check_for_challenge
# ---------------------------------------------------------------------------


class TestBrowserChallengeDetection:
    """Test _sync_check_for_challenge via different page URL states."""

    def _make_browser(self) -> XAgentBrowser:
        """Create an XAgentBrowser with mocked internals (no real browser)."""
        b = XAgentBrowser.__new__(XAgentBrowser)
        b._page = MagicMock()
        b._screenshots_dir = Path("data/screenshots")
        return b

    def test_no_challenge_on_home(self):
        b = self._make_browser()
        b._page.url = "https://x.com/home"
        b._page.query_selector.return_value = None
        result = b._sync_check_for_challenge()
        assert result["challenged"] is False
        assert result["challenge_type"] == "none"

    @pytest.mark.parametrize("pattern", _CHALLENGE_URL_PATTERNS)
    def test_url_redirect_challenge(self, pattern: str):
        b = self._make_browser()
        b._page.url = f"https://x.com/{pattern}?foo=bar"
        result = b._sync_check_for_challenge()
        assert result["challenged"] is True
        assert result["challenge_type"] == "url_redirect"

    def test_captcha_element_challenge(self):
        b = self._make_browser()
        b._page.url = "https://x.com/home"

        # First 3 selectors return None, the arkoseFrame one returns an element
        def mock_query(sel):
            if "arkoseFrame" in sel:
                return MagicMock()  # found
            return None

        b._page.query_selector.side_effect = mock_query
        result = b._sync_check_for_challenge()
        assert result["challenged"] is True
        assert result["challenge_type"] == "captcha_element"

    def test_no_page_returns_safe(self):
        b = self._make_browser()
        b._page = None
        result = b._sync_check_for_challenge()
        assert result["challenged"] is False


# ---------------------------------------------------------------------------
# XAgent auto-pause on consecutive challenges
# ---------------------------------------------------------------------------


class TestAgentAutoChallengePause:
    """Test that XAgent auto-pauses after consecutive challenge detections."""

    def _make_agent(self):
        """Create an XAgent with mocked config (no real files)."""
        from src.x_agent import XAgent

        with patch.object(XAgent, "__init__", lambda self, *a, **kw: None):
            agent = XAgent.__new__(XAgent)
        agent._running = True
        agent._paused = False
        agent._consecutive_challenges = 0
        agent._challenge_pause_threshold = 2
        agent._browser = MagicMock()
        return agent

    def test_single_challenge_does_not_pause(self):
        agent = self._make_agent()
        agent._browser.check_for_challenge = AsyncMock(
            return_value={"challenged": True, "challenge_type": "url_redirect", "url": "https://x.com/account/access"}
        )
        asyncio.run(agent._check_challenge_state({"status": "no_tweets"}))
        assert agent._consecutive_challenges == 1
        assert agent._paused is False

    def test_consecutive_challenges_trigger_pause(self):
        agent = self._make_agent()
        agent._browser.check_for_challenge = AsyncMock(
            return_value={"challenged": True, "challenge_type": "url_redirect", "url": "https://x.com/account/access"}
        )
        # First challenge
        asyncio.run(agent._check_challenge_state({"status": "no_tweets"}))
        assert agent._paused is False
        # Second challenge — should auto-pause
        asyncio.run(agent._check_challenge_state({"status": "no_tweets"}))
        assert agent._consecutive_challenges == 2
        assert agent._paused is True

    def test_successful_cycle_resets_counter(self):
        agent = self._make_agent()
        agent._consecutive_challenges = 1
        agent._browser.check_for_challenge = AsyncMock(
            return_value={"challenged": False, "challenge_type": "none", "url": "https://x.com/home"}
        )
        asyncio.run(agent._check_challenge_state({"status": "ok"}))
        assert agent._consecutive_challenges == 0
        assert agent._paused is False

    def test_resume_resets_challenge_counter(self):
        agent = self._make_agent()
        agent._consecutive_challenges = 5
        agent._paused = True
        asyncio.run(agent.resume())
        assert agent._consecutive_challenges == 0
        assert agent._paused is False

    def test_no_browser_is_safe(self):
        agent = self._make_agent()
        agent._browser = None
        asyncio.run(agent._check_challenge_state({"status": "ok"}))
        assert agent._consecutive_challenges == 0

    def test_status_includes_challenge_count(self):
        agent = self._make_agent()
        agent._consecutive_challenges = 3
        agent._config = {"codename": "test", "autonomy": {"level": "full"}}
        agent._log_file = Path("/tmp/fake.jsonl")
        agent._pending_file = Path("/tmp/fake_pending.json")
        # Mock the methods that get_status calls
        with patch.object(type(agent), "_daily_actions_remaining", return_value=50), \
             patch.object(type(agent), "get_pending_actions", return_value=[]):
            status = agent.get_status()
        assert status["consecutive_challenges"] == 3
