#!/usr/bin/env python3
"""Fetch and prefilter messages, saving classifier-bound survivors to disk.

Run once to build a test dataset, then iterate on prompts using
run_classifier.py without touching Apple Mail again.

Output: ~/.mail-agent/candidates.json  (one JSON array of message dicts)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from agent import stream_messages
from prefilter import filter_message

CANDIDATES_PATH = Path.home() / ".mail-agent" / "candidates.json"
# Store more content than production so run_classifier.py can experiment
# with different truncation levels without re-dumping.
_DUMP_TRUNCATE_BYTES = 16_384


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dump prefilter-passing messages to candidates.json."
    )
    ap.add_argument(
        "--since",
        default=None,
        help=(
            "ISO8601 start of fetch window. Bare timestamps (no Z or offset) "
            "are interpreted as local time. Default: 48 hours ago."
        ),
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=CANDIDATES_PATH,
        help=f"Output path (default: {CANDIDATES_PATH})",
    )
    args = ap.parse_args()

    since = args.since or (
        datetime.now() - timedelta(hours=48)
    ).strftime("%Y-%m-%dT%H:%M:%S")

    print(f"Fetching messages since {since} …", flush=True)

    fetched = 0
    candidates = []
    drop_counts: dict[str, int] = {}

    for msg, err in stream_messages(since, 1000, _DUMP_TRUNCATE_BYTES):
        if err:
            print(f"  warning: {err}", file=sys.stderr)
            continue

        fetched += 1
        sender = msg.get("sender", "")
        subject = msg.get("subject", "(no subject)")

        keep, reason = filter_message(msg)
        if not keep:
            drop_counts[reason or "unknown"] = drop_counts.get(reason or "unknown", 0) + 1
            print(f"  DROP [{reason:20s}] {sender} | {subject}")
            continue

        candidates.append(msg)
        print(f"  KEEP                       {sender} | {subject}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(candidates, indent=2, default=str), encoding="utf-8")

    dropped = fetched - len(candidates)
    print(f"\nFetched {fetched} messages — dropped {dropped}, saved {len(candidates)} candidates")
    if drop_counts:
        for reason, n in sorted(drop_counts.items(), key=lambda x: -x[1]):
            print(f"  {n:3d}  {reason}")
    print(f"\nCandidates written to {args.out}")
    print("Run  uv run python run_classifier.py  to classify them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
