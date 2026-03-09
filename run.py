#!/usr/bin/env python3
"""Launch script for X Agent.

Usage:
    python run.py --config configs/nagi.yaml
    python run.py --config configs/nagi.yaml --dry-run
    python run.py --status
    python run.py --stop
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def setup_logging(config_name: str) -> None:
    """Setup logging to file and console."""
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"x_agent_{config_name}.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def write_pid(config_name: str) -> None:
    pid_file = ROOT / "data" / f"x_agent_{config_name}.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))


def read_pid(config_name: str) -> int | None:
    pid_file = ROOT / "data" / f"x_agent_{config_name}.pid"
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except ValueError:
            return None
    return None


def remove_pid(config_name: str) -> None:
    pid_file = ROOT / "data" / f"x_agent_{config_name}.pid"
    if pid_file.exists():
        pid_file.unlink()


async def main_loop(config_path: str, dry_run: bool = False) -> None:
    from src.x_agent import XAgent

    config_name = Path(config_path).stem
    setup_logging(config_name)
    logger = logging.getLogger("x_agent.launcher")

    agent = XAgent(config_path=Path(config_path), dry_run=dry_run)

    def shutdown_handler(sig, frame):
        logger.info("Shutdown signal received, stopping agent...")
        asyncio.get_event_loop().create_task(agent.stop())

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    write_pid(config_name)
    try:
        logger.info("Starting X Agent with config: %s (dry_run=%s)", config_path, dry_run)
        await agent.start()
    except Exception as e:
        logger.error("X Agent crashed: %s", e, exc_info=True)
    finally:
        remove_pid(config_name)
        logger.info("X Agent stopped.")


def show_status(config_path: str) -> None:
    config_name = Path(config_path).stem
    pid = read_pid(config_name)

    if pid is None:
        print(f"X Agent ({config_name}): NOT RUNNING (no PID file)")
        return

    import subprocess

    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
    )
    if str(pid) in result.stdout:
        print(f"X Agent ({config_name}): RUNNING (PID {pid})")
    else:
        print(f"X Agent ({config_name}): DEAD (stale PID {pid})")
        remove_pid(config_name)

    log_file = ROOT / "data" / f"{config_name.replace('x_agent_', '')}x_actions.jsonl"
    if not log_file.exists():
        log_file = ROOT / "data" / "nagi_x_actions.jsonl"
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8").strip().split("\n")
        recent = lines[-5:] if len(lines) >= 5 else lines
        print(f"\nRecent actions ({len(lines)} total):")
        for line in recent:
            try:
                entry = json.loads(line)
                print(f"  [{entry.get('timestamp', '?')}] {entry.get('action', '?')}: {'OK' if entry.get('success') else 'FAIL'}")
            except json.JSONDecodeError:
                pass


def stop_agent(config_path: str) -> None:
    config_name = Path(config_path).stem
    pid = read_pid(config_name)

    if pid is None:
        print(f"X Agent ({config_name}): Not running.")
        return

    import subprocess

    # NEVER kill by image name — always by specific PID
    result = subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip() or result.stderr.strip())
    remove_pid(config_name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="X Agent Launcher")
    parser.add_argument("--config", default="configs/nagi.yaml", help="Config file path")
    parser.add_argument("--dry-run", action="store_true", help="Log decisions but don't execute")
    parser.add_argument("--status", action="store_true", help="Show agent status")
    parser.add_argument("--stop", action="store_true", help="Stop running agent")
    args = parser.parse_args()

    if args.status:
        show_status(args.config)
    elif args.stop:
        stop_agent(args.config)
    else:
        asyncio.run(main_loop(args.config, dry_run=args.dry_run))
