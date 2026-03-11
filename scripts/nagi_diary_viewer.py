"""Nagi Diary Viewer — View Nagi's daily diaries and observations.

Usage:
    python scripts/nagi_diary_viewer.py                    # Show all diaries
    python scripts/nagi_diary_viewer.py --today             # Show today's observations
    python scripts/nagi_diary_viewer.py --date 2026-03-11   # Show specific date
    python scripts/nagi_diary_viewer.py --remote             # Read from RemotePC via SSH
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_JST = timezone(timedelta(hours=9))


def today_jst() -> date:
    return datetime.now(_JST).date()


def read_local_diary(data_dir: Path, target_date: date | None = None) -> None:
    """Read diary entries from local data directory."""
    diary_dir = data_dir / "diary"
    if not diary_dir.exists():
        print("No diary directory found.")
        return

    files = sorted(diary_dir.glob("nagi_*.md"))
    if not files:
        print("No diary entries yet. Nagi needs a few days to start writing.")
        return

    for f in files:
        d = f.stem.replace("nagi_", "")
        if target_date and d != target_date.isoformat():
            continue
        print(f.read_text(encoding="utf-8"))
        print()


def read_local_observations(data_dir: Path, target_date: date) -> None:
    """Read raw observations for a specific date."""
    obs_file = data_dir / "memory" / f"nagi_obs_{target_date.isoformat()}.jsonl"
    if not obs_file.exists():
        print(f"No observations for {target_date}.")
        return

    print(f"=== Nagi's observations — {target_date} ===\n")
    with open(obs_file, encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line.strip())
            ts = entry.get("ts", "")[:19]
            obs_type = entry.get("type", "?")
            handle = entry.get("handle", "")
            snippet = entry.get("text_snippet", "")
            reasoning = entry.get("reasoning", "")
            conf = entry.get("confidence", 0)
            tags = entry.get("topic_tags", [])

            icon = {"liked": "♥", "posted": "✎", "saw": "👁", "replied": "↩"}.get(obs_type, "·")
            print(f"  {icon} [{ts}] {obs_type}", end="")
            if handle:
                print(f" @{handle}", end="")
            if conf:
                print(f" (conf={conf})", end="")
            print()
            if snippet:
                print(f"    「{snippet[:80]}」")
            if reasoning:
                print(f"    → {reasoning}")
            if tags:
                print(f"    tags: {', '.join(tags)}")
            print()


def read_remote(target_date: date | None = None, show_obs: bool = False) -> None:
    """Read from RemotePC via SSH."""
    try:
        import paramiko
    except ImportError:
        print("paramiko not installed. Use --local or install paramiko.")
        return

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect("100.74.211.55", username="USER", password="Password123!")

    base_path = "C:/Users/USER/x-agent/data"

    if show_obs:
        d = target_date or today_jst()
        obs_path = f"{base_path}/memory/nagi_obs_{d.isoformat()}.jsonl"
        _remote_read_b64(ssh, obs_path, f"Nagi's observations — {d}")
    else:
        # List and read diary files
        si, so, se = ssh.exec_command(
            f'powershell -Command "Get-ChildItem {base_path}/diary/nagi_*.md -Name"'
        )
        files = so.read().decode("utf-8", errors="replace").strip().split("\n")
        files = [f.strip() for f in files if f.strip()]

        if not files:
            print("No diary entries yet.")
            ssh.close()
            return

        for fname in sorted(files):
            d_str = fname.replace("nagi_", "").replace(".md", "")
            if target_date and d_str != target_date.isoformat():
                continue
            _remote_read_b64(ssh, f"{base_path}/diary/{fname}", None)
            print()

    ssh.close()


def _remote_read_b64(ssh, filepath: str, label: str | None) -> None:
    """Read a file from remote via base64 transport."""
    reader = f'import base64; data=open(r"{filepath}","rb").read(); print(base64.b64encode(data).decode())'

    # Write temp script
    ssh.exec_command(f'echo {reader} > C:\\Users\\USER\\_tmp_read.py')
    time.sleep(0.5)
    si, so, se = ssh.exec_command(r"python C:\Users\USER\_tmp_read.py")
    b64 = so.read().decode("ascii", errors="replace").strip()
    err = se.read().decode("utf-8", errors="replace")

    if err and "No such file" in err:
        if label:
            print(f"{label}: (no data yet)")
        return

    if b64:
        text = base64.b64decode(b64).decode("utf-8")
        if label:
            print(f"=== {label} ===")
        print(text)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Nagi Diary Viewer")
    parser.add_argument("--today", action="store_true", help="Show today's observations")
    parser.add_argument("--date", type=str, help="Show specific date (YYYY-MM-DD)")
    parser.add_argument("--remote", action="store_true", help="Read from RemotePC via SSH")
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="Local data directory",
    )
    args = parser.parse_args()

    target = None
    if args.date:
        target = date.fromisoformat(args.date)

    if args.remote:
        read_remote(target_date=target, show_obs=args.today)
    elif args.today:
        read_local_observations(Path(args.data_dir), target or today_jst())
    else:
        read_local_diary(Path(args.data_dir), target_date=target)


if __name__ == "__main__":
    main()
