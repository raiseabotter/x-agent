"""Auto cookie extraction: open Playwright browser, navigate to x.com, wait for login, extract cookies.

Designed to run in interactive session via PsExec.
Opens a visible browser window on the user's desktop.
If user is already logged in via Google, it should auto-login.
"""
import json
import sys
import time
from pathlib import Path

COOKIE_FILE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/nagi_x_cookies.json")
REQUIRED_COOKIES = {"auth_token", "ct0", "twid"}


def main():
    from playwright.sync_api import sync_playwright

    print("[AUTO] Opening browser for X login...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=30000)

        # Wait for login — check every 5s for up to 5 minutes
        print("[AUTO] Waiting for login (checking for auth cookies)...")
        for attempt in range(60):
            time.sleep(5)
            cookies = context.cookies(["https://x.com", "https://twitter.com"])
            found = {c["name"] for c in cookies} & REQUIRED_COOKIES
            if found == REQUIRED_COOKIES:
                print(f"[AUTO] Login detected! Found: {found}")
                # Save cookies
                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                COOKIE_FILE.write_text(
                    json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"[AUTO] Saved {len(cookies)} cookies to {COOKIE_FILE}")
                browser.close()
                return True

            if attempt % 6 == 5:
                # Try navigating to home to trigger redirect
                try:
                    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                print(f"[AUTO] Still waiting... ({attempt * 5}s, found: {found})")

        print("[AUTO] Timeout — login not detected in 5 minutes")
        browser.close()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
