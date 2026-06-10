#!/usr/bin/env python3
"""Classify saved candidates against the current prompt. No Mail access needed.

Workflow:
  1. Edit prompts/classify_system.md
  2. uv run python run_classifier.py
  3. Review results, go to 1.

The prompt is loaded fresh each run, so edits take effect immediately.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import classify as classify_mod

CANDIDATES_PATH = Path.home() / ".apple-mail-triage" / "candidates.json"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Classify candidates.json with the current prompt."
    )
    ap.add_argument(
        "--candidates",
        type=Path,
        default=CANDIDATES_PATH,
        help=f"Path to candidates JSON (default: {CANDIDATES_PATH})",
    )
    ap.add_argument(
        "--truncate-bytes",
        type=int,
        default=4096,
        help="Truncate message body to this many bytes before sending to LLM "
             "(default: 4096, matching production)",
    )
    args = ap.parse_args()

    if not args.candidates.exists():
        sys.exit(
            f"Candidates file not found: {args.candidates}\n"
            "Run  uv run python dump_candidates.py  first."
        )

    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    if not candidates:
        sys.exit("No candidates in file — nothing to classify.")

    model = classify_mod.MODEL_NAME
    total = len(candidates)

    print(f"Model:  {model}")
    print(f"Prompt: {classify_mod._PROMPT_PATH}")
    print(f"Corpus: {args.candidates}  ({total} message{'s' if total != 1 else ''})")
    print()

    actionable_count = 0
    total_ms = 0

    for i, msg in enumerate(candidates, 1):
        sender = msg.get("sender", "")
        subject = msg.get("subject", "(no subject)")
        print(f"[{i}/{total}] {sender} | {subject}")

        result = classify_mod.classify(
            msg,
            content_truncate_bytes=args.truncate_bytes,
        )

        ms = result.get("llm_ms", 0)
        pt = result.get("prompt_tokens", 0)
        et = result.get("eval_tokens", 0)
        total_ms += ms

        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            print()
            continue

        actionable = result.get("actionable", False)
        urgency = result.get("urgency", "low")
        title = result.get("title", "")
        reason = result.get("reason", "")

        if actionable:
            actionable_count += 1
            print(f"  ACTIONABLE [{urgency}] — {title}")
        else:
            print(f"  not actionable")
        if reason:
            print(f"  Why: {reason}")
        print(f"  {ms}ms  ({pt} prompt + {et} eval tokens)")
        print()

    print("─" * 60)
    print(f"Actionable: {actionable_count}/{total}")
    if total > 0:
        print(f"Total time: {total_ms / 1000:.1f}s  ({total_ms // total}ms avg)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
