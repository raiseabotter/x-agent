#!/usr/bin/env python3
"""Cookie extraction script for X Agent.

Extracts X/Twitter session cookies from Chrome or Edge via CDP, or manual login.

Usage:
    # Auto-detect (tries Chrome, then Edge):
    python scripts/setup_cookies.py --auto --cookie-file data/nagi_x_cookies.json

    # From Chrome:
    python scripts/setup_cookies.py --from-chrome "Default" --cookie-file data/nagi_x_cookies.json

    # From Edge:
    python scripts/setup_cookies.py --from-edge "Default" --cookie-file data/nagi_x_cookies.json

    # Manual login (opens browser window, you log in manually):
    python scripts/setup_cookies.py --manual --cookie-file data/nagi_x_cookies.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REQUIRED_COOKIES = {"auth_token", "ct0", "twid"}


def _find_browser() -> tuple[str, Path, str] | None:
    """Auto-detect Chrome or Edge installation.

    Returns (exe_path, user_data_dir, process_name) or None.
    """
    home = Path.home()

    candidates = [
        # Chrome
        (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            "chrome.exe",
        ),
        (
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            home / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
            "chrome.exe",
        ),
        # Edge (always installed on Windows 10+)
        (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
            "msedge.exe",
        ),
        (
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data",
            "msedge.exe",
        ),
    ]

    for exe, user_data, proc_name in candidates:
        if Path(exe).exists() and user_data.exists():
            return exe, user_data, proc_name

    return None


def extract_via_cdp(
    profile_name: str,
    cookie_path: Path,
    browser_exe: str | None = None,
    user_data_dir: Path | None = None,
    process_name: str | None = None,
) -> bool:
    """Extract cookies from Chrome/Edge via CDP (Chrome DevTools Protocol)."""

    # Auto-detect if not specified
    if browser_exe is None:
        detected = _find_browser()
        if detected is None:
            print("[CDP] No supported browser found (Chrome or Edge)")
            print(f"[CDP] Searched in: C:\\Program Files\\...\\chrome.exe, msedge.exe")
            print(f"[CDP] Home dir: {Path.home()}")
            return False
        browser_exe, user_data_dir, process_name = detected

    browser_name = "Edge" if "edge" in browser_exe.lower() else "Chrome"
    print(f"[CDP] Using {browser_name}: {browser_exe}")
    print(f"[CDP] User data: {user_data_dir}")
    print(f"[CDP] Profile: {profile_name}")

    # Kill existing browser (required for CDP)
    print(f"[CDP] Closing {browser_name}...")
    subprocess.run(["taskkill", "/F", "/IM", process_name], capture_output=True)
    time.sleep(3)

    # Launch with debugging
    print(f"[CDP] Launching {browser_name} with remote debugging on port 9222...")
    proc = subprocess.Popen(
        [
            browser_exe,
            "--remote-debugging-port=9222",
            f"--user-data-dir={user_data_dir}",
            f"--profile-directory={profile_name}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for browser to start
    import urllib.request

    print("[CDP] Waiting for browser to start (up to 60s)...")
    for i in range(60):
        # Check if process died
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            print(f"[CDP] Browser process exited with code {proc.returncode}")
            if stderr:
                print(f"[CDP] stderr: {stderr[:500]}")
            return False

        try:
            urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
            print(f"[CDP] Browser ready (attempt {i + 1})")
            break
        except Exception:
            if i % 10 == 9:
                print(f"[CDP] Still waiting... ({i + 1}s)")
            time.sleep(1)
    else:
        print("[CDP] Browser did not start in time (60s)")
        # Try to get more info
        try:
            import urllib.request
            resp = urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=2)
            print(f"[CDP] /json responded: {resp.read()[:200]}")
        except Exception as e:
            print(f"[CDP] Cannot reach port 9222: {e}")
        proc.terminate()
        return False

    # Connect via Playwright
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9222")
            context = browser.contexts[0]
            cookies = context.cookies(["https://x.com", "https://twitter.com"])

            print(f"[CDP] Found {len(cookies)} cookies total")

            # Check for required cookies
            found = {c["name"] for c in cookies} & REQUIRED_COOKIES
            if found != REQUIRED_COOKIES:
                missing = REQUIRED_COOKIES - found
                print(f"[CDP] Missing required cookies: {missing}")
                print(f"[CDP] Make sure you're logged into X in this {browser_name} profile")
                cookie_names = [c["name"] for c in cookies]
                print(f"[CDP] Available cookie names: {cookie_names[:20]}")
                browser.close()
                return False

            # Save cookies
            cookie_path.parent.mkdir(parents=True, exist_ok=True)
            cookie_path.write_text(
                json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[CDP] Saved {len(cookies)} cookies to {cookie_path}")
            print(f"[CDP] Required cookies found: {found}")
            browser.close()
            return True

    except ImportError:
        print(
            "[CDP] playwright not installed. "
            "Run: pip install playwright && python -m playwright install chromium"
        )
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
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
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
            cookie_path.write_text(
                json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"[Manual] Saved {len(cookies)} cookies to {cookie_path}")
            browser.close()
            return True

    except ImportError:
        print(
            "[Manual] playwright not installed. "
            "Run: pip install playwright && python -m playwright install chromium"
        )
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="X Agent Cookie Setup")
    parser.add_argument(
        "--from-chrome",
        metavar="PROFILE",
        help="Chrome profile name (e.g. 'Default', 'Profile 1')",
    )
    parser.add_argument(
        "--from-edge",
        metavar="PROFILE",
        help="Edge profile name (e.g. 'Default', 'Profile 1')",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-detect Chrome or Edge and extract cookies",
    )
    parser.add_argument("--manual", action="store_true", help="Manual login mode")
    parser.add_argument(
        "--cookie-file",
        default="data/nagi_x_cookies.json",
        help="Output cookie file path",
    )
    args = parser.parse_args()

    cookie_path = Path(args.cookie_file)

    if args.auto:
        # Auto-detect browser, try Default profile
        success = extract_via_cdp("Default", cookie_path)
    elif args.from_chrome:
        home = Path.home()
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        chrome_exe = next((p for p in chrome_paths if Path(p).exists()), None)
        if chrome_exe is None:
            print("[Error] Chrome not found")
            sys.exit(1)
        user_data = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
        success = extract_via_cdp(
            args.from_chrome, cookie_path, chrome_exe, user_data, "chrome.exe"
        )
    elif args.from_edge:
        home = Path.home()
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        edge_exe = next((p for p in edge_paths if Path(p).exists()), None)
        if edge_exe is None:
            print("[Error] Edge not found")
            sys.exit(1)
        user_data = home / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
        success = extract_via_cdp(
            args.from_edge, cookie_path, edge_exe, user_data, "msedge.exe"
        )
    elif args.manual:
        success = manual_login(cookie_path)
    else:
        print("Specify --auto, --from-chrome PROFILE, --from-edge PROFILE, or --manual")
        parser.print_help()
        sys.exit(1)

    sys.exit(0 if success else 1)
