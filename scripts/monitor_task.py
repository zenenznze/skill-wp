#!/usr/bin/env python3
"""Monitor one delegated task attempt: artifact growth, Goal state, stalls.

Emits one timeline line per poll: <time> <attempt> <bytes> <files> <newest>
<goal-status>. With --watch it polls every --interval seconds and warns when
the attempt directory stops growing for --stall-after consecutive polls.

Usage:
    python3 scripts/monitor_task.py --repo <execution-root> --task-id <id> --once
    python3 scripts/monitor_task.py --repo <execution-root> --task-id <id> --watch
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from protocol import task_relative_paths


def attempt_dirs(run_root: Path) -> list[Path]:
    if not run_root.is_dir():
        return []
    attempts = sorted(
        (p for p in run_root.iterdir() if p.is_dir() and p.name.startswith("attempt-")),
        key=lambda p: p.name,
    )
    return attempts


def snapshot(attempt: Path) -> dict:
    total = 0
    newest = 0.0
    count = 0
    for path in attempt.rglob("*"):
        if not path.is_file():
            continue
        count += 1
        total += path.stat().st_size
        newest = max(newest, path.stat().st_mtime)
    goal_status = "n/a"
    goal_path = attempt / "goal.json"
    if goal_path.is_file():
        try:
            goal = json.loads(goal_path.read_text(encoding="utf-8"))
            goal_status = str(goal.get("status", "unknown"))
        except (OSError, ValueError):
            goal_status = "unreadable"
    return {"bytes": total, "files": count, "newest": newest, "goal_status": goal_status}


def format_line(attempt: Path, snap: dict, warn: str = "") -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(snap["newest"]))
    parts = [
        time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        attempt.name,
        f"{snap['bytes']}B",
        f"{snap['files']}f",
        f"newest={stamp}",
        f"goal={snap['goal_status']}",
    ]
    if warn:
        parts.append(warn)
    return " ".join(parts)


def monitor(repo: Path, task_id: str, once: bool, interval: int, stall_after: int) -> int:
    paths = task_relative_paths(task_id, repo)
    run_root = repo / paths["run_root"]
    frozen = 0
    previous: dict | None = None
    while True:
        attempts = attempt_dirs(run_root)
        if not attempts:
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} no attempts yet")
        else:
            attempt = attempts[-1]
            snap = snapshot(attempt)
            warn = ""
            if previous is not None and previous == snap:
                frozen += 1
                if frozen >= stall_after:
                    warn = "STALLED"
            else:
                frozen = 0
            previous = snap
            print(format_line(attempt, snap, warn), flush=True)
        if once:
            return 0
        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="execution Git root")
    parser.add_argument("--task-id", required=True, help="yyyymmdd-lowercase-slug")
    parser.add_argument("--once", action="store_true", help="single snapshot, then exit")
    parser.add_argument("--watch", action="store_true", help="poll until interrupted")
    parser.add_argument("--interval", type=int, default=90, help="poll interval seconds")
    parser.add_argument(
        "--stall-after",
        type=int,
        default=3,
        help="consecutive unchanged polls before STALLED warning",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.once and not args.watch:
        print("monitor_task: pass --once or --watch", file=sys.stderr)
        return 2
    try:
        return monitor(args.repo, args.task_id, args.once, args.interval, args.stall_after)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"monitor_task: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
