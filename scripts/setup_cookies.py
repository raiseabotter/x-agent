#!/usr/bin/env python3
"""Cookie extraction script for X Agent.

Extracts X/Twitter session cookies from Chrome via CDP or manual login.

Usage:
    # From Chrome (recommended — uses existing login, no bot detection):
    python scripts/setup_cookies.py --from-chrome "Default" --cookie-file data/nagi_x_cookies.json

    # Manual login (fallback):
    python scripts/setup_cookies.py --manual --cookie-file data/nagi_x_cookies.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REQUIRED_COOKIES = {"auth_token", "ct0", "twid"}


def extract_via_cdp(profile_name: str, cookie_path: Path) -> bool:
    """Extract cookies from Chrome via CDP (Chrome DevTools Protocol).

    1. Kills existing Chrome processes
    2. Launches Chrome with remote debugging
    3. Connects via Playwright CDP
    4. Extracts x.com cookies
    """
    print(f"[CDP] Extracting cookies from Chrome profile: {profile_name}")

    # Kill existing Chrome (required for CDP)
    print("[CDP] Closing Chrome...")
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(2)

    # Find Chrome user data dir
    local_app = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    if not local_app.exists():
        print(f"[CDP] Chrome user data not found: {local_app}")
        return False

    # Launch Chrome with debugging
    chrome_exe = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not Path(chrome_exe).exists():
        chrome_exe = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    if not Path(chrome_exe).exists():
        print("[CDP] Chrome not found")
        return False

    print("[CDP] Launching Chrome with remote debugging on port 9222...")
    subprocess.Popen(
        [
            chrome_exe,
            f"--remote-debugging-port=9222",
            f"--user-data-dir={local_app}",
            f"--profile-directory={profile_name}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for Chrome to start
    import urllib.request
    for i in range(40):
        try:
            urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
            print(f"[CDP] Chrome ready (attempt {i + 1})")
            break
        except Exception:
            time.sleep(1)
    else:
        print("[CDP] Chrome did not start in time")
        return False

    # Connect via Playwright
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            cookies = context.cookies(["https://x.com", "https://twitter.com"])

            # Check for required cookies
            found = {c["name"] for c in cookies} & REQUIRED_COOKIES
            if found != REQUIRED_COOKIES:
                missing = REQUIRED_COOKIES - found
                print(f"[CDP] Missing cookies: {missing}")
                print("[CDP] Make sure you're logged into X in this Chrome profile")
                browser.close()
                return False

            # Save cookies
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[CDP] Saved {len(cookies)} cookies to {cookie_path}")
            print(f"[CDP] Required cookies found: {found}")
            browser.close()
            return True

    except ImportError:
        print("[CDP] playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        return False
    except Exception as e:
        print(f"[CDP] Error: {e}")
        return False


def manual_login(cookie_path: Path) -> bool:
    """Open browser for manual X login, then extract cookies."""
    try:
        from playwright.sync_api import sync_playwright

        print("[Manual] Opening browser for X login...")
        print("[Manual] Please log in, then press Enter in this terminal.")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://x.com/login", wait_until="domcontentloaded")

            input("\n>>> Press Enter after you've logged in successfully... ")

            # Navigate to verify login
            page.goto("https://x.com/home", wait_until="domcontentloaded")
            time.sleep(3)

            cookies = context.cookies(["https://x.com", "https://twitter.com"])
            found = {c["name"] for c in cookies} & REQUIRED_COOKIES
            if found != REQUIRED_COOKIES:
                missing = REQUIRED_COOKIES - found
                print(f"[Manual] Missing cookies: {missing}")
                browser.close()
                return False

            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[Manual] Saved {len(cookies)} cookies to {cookie_path}")
            browser.close()
            return True

    except ImportError:
        print("[Manual] playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="X Agent Cookie Setup")
    parser.add_argument("--from-chrome", metavar="PROFILE", help="Chrome profile name (e.g. 'Default', 'Profile 1')")
    parser.add_argument("--manual", action="store_true", help="Manual login mode")
    parser.add_argument("--cookie-file", default="data/nagi_x_cookies.json", help="Output cookie file path")
    args = parser.parse_args()

    cookie_path = Path(args.cookie_file)

    if args.from_chrome:
        success = extract_via_cdp(args.from_chrome, cookie_path)
    elif args.manual:
        success = manual_login(cookie_path)
    else:
        print("Specify --from-chrome PROFILE or --manual")
        parser.print_help()
        sys.exit(1)

    sys.exit(0 if success else 1)
