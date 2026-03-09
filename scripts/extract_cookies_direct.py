#!/usr/bin/env python3
"""Direct cookie extraction from Chrome's SQLite database (Windows DPAPI).

Works over SSH without needing to launch Chrome.
Reads encrypted cookies, decrypts via Windows DPAPI, outputs Playwright-format JSON.

Usage:
    python scripts/extract_cookies_direct.py --profile "Default" --cookie-file data/nagi_x_cookies.json
"""
import argparse
import base64
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def decrypt_value(encrypted_value: bytes, key: bytes) -> str:
    """Decrypt a Chrome cookie value."""
    if not encrypted_value:
        return ""

    # v10/v20 encrypted cookies (AES-256-GCM with DPAPI-protected key)
    if encrypted_value[:3] == b"v10" or encrypted_value[:3] == b"v20":
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = encrypted_value[3:15]
            ciphertext = encrypted_value[15:]
            aesgcm = AESGCM(key)
            decrypted = aesgcm.decrypt(nonce, ciphertext, None)
            return decrypted.decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[WARN] AES decrypt failed: {e}", file=sys.stderr)
            return ""

    # Legacy DPAPI-only encryption
    try:
        import ctypes
        import ctypes.wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        blob_in = DATA_BLOB(len(encrypted_value), ctypes.create_string_buffer(encrypted_value))
        blob_out = DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[WARN] DPAPI decrypt failed: {e}", file=sys.stderr)

    return ""


def get_chrome_key(local_state_path: Path) -> bytes:
    """Extract and decrypt the Chrome encryption key from Local State."""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
    encrypted_key_b64 = local_state["os_crypt"]["encrypted_key"]
    encrypted_key = base64.b64decode(encrypted_key_b64)

    # Remove DPAPI prefix
    encrypted_key = encrypted_key[5:]  # strip "DPAPI" prefix

    blob_in = DATA_BLOB(len(encrypted_key), ctypes.create_string_buffer(encrypted_key))
    blob_out = DATA_BLOB()

    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise RuntimeError("Failed to decrypt Chrome key via DPAPI")

    key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return key


def extract_cookies(profile_name: str, cookie_path: Path) -> bool:
    """Extract x.com cookies from Chrome profile."""
    home = Path.home()
    chrome_user_data = home / "AppData" / "Local" / "Google" / "Chrome" / "User Data"

    if not chrome_user_data.exists():
        print(f"[ERROR] Chrome user data not found: {chrome_user_data}")
        return False

    # Get encryption key
    local_state = chrome_user_data / "Local State"
    if not local_state.exists():
        print(f"[ERROR] Local State not found: {local_state}")
        return False

    print("[INFO] Decrypting Chrome master key...")
    try:
        key = get_chrome_key(local_state)
        print(f"[OK] Key decrypted ({len(key)} bytes)")
    except Exception as e:
        print(f"[ERROR] Key decryption failed: {e}")
        return False

    # Copy cookie DB (Chrome locks it while running)
    cookie_db = chrome_user_data / profile_name / "Network" / "Cookies"
    if not cookie_db.exists():
        # Try older Chrome path
        cookie_db = chrome_user_data / profile_name / "Cookies"
    if not cookie_db.exists():
        print(f"[ERROR] Cookie database not found: {cookie_db}")
        print(f"[INFO] Available profiles: {[d.name for d in chrome_user_data.iterdir() if d.is_dir() and (d / 'Network').exists()]}")
        return False

    print(f"[INFO] Reading cookies from: {cookie_db}")
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(str(cookie_db), tmp)

    try:
        conn = sqlite3.connect(tmp)
        cursor = conn.cursor()

        # Query x.com and twitter.com cookies
        cursor.execute(
            "SELECT host_key, name, path, encrypted_value, expires_utc, is_secure, is_httponly, samesite "
            "FROM cookies WHERE host_key LIKE '%x.com' OR host_key LIKE '%twitter.com'"
        )

        cookies = []
        required_found = set()

        for row in cursor.fetchall():
            host, name, path, enc_value, expires, secure, httponly, samesite = row

            value = decrypt_value(enc_value, key)

            if name in ("auth_token", "ct0", "twid"):
                required_found.add(name)
                print(f"[OK] Found: {name} = {value[:20]}...")

            # Convert to Playwright cookie format
            sameSite_map = {-1: "None", 0: "None", 1: "Lax", 2: "Strict"}
            cookie = {
                "name": name,
                "value": value,
                "domain": host,
                "path": path,
                "expires": expires / 1_000_000 - 11_644_473_600 if expires > 0 else -1,
                "httpOnly": bool(httponly),
                "secure": bool(secure),
                "sameSite": sameSite_map.get(samesite, "None"),
            }
            cookies.append(cookie)

        conn.close()

        missing = {"auth_token", "ct0", "twid"} - required_found
        if missing:
            print(f"[ERROR] Missing required cookies: {missing}")
            print("[INFO] Make sure you're logged into X in this Chrome profile")
            return False

        # Save
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[OK] Saved {len(cookies)} cookies to {cookie_path}")
        return True

    finally:
        os.unlink(tmp)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Direct Chrome cookie extraction")
    parser.add_argument("--profile", default="Default", help="Chrome profile name")
    parser.add_argument("--cookie-file", default="data/nagi_x_cookies.json")
    args = parser.parse_args()
    success = extract_cookies(args.profile, Path(args.cookie_file))
    sys.exit(0 if success else 1)
