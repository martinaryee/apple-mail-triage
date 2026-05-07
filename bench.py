"""Quick A/B benchmark: JXA fetch vs disk-direct fetch.

Times the fetch step and the per-message prefilter + classify steps, so
you can see where the new fetcher saves time and how the rest of the
pipeline behaves on the same messages.

Usage:
    .venv/bin/python bench.py --since 2026-05-01T00:00:00Z --max 50

Skip the LLM (faster, isolates fetch + prefilter):
    .venv/bin/python bench.py --since 2026-05-01T00:00:00Z --max 50 --no-classify

Skip one side if disabled:
    --skip-jxa   skip the legacy JXA fetcher (no Mail.app needed)
    --skip-disk  skip the disk-direct fetcher (no Full Disk Access needed)
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import classify as classify_mod
import fetcher as fetcher_mod
from prefilter import filter_message

JXA_SCRIPT = Path(__file__).resolve().parent / "jxa" / "fetch_new_messages.js"
DEFAULT_CONFIG_PATH = Path.home() / ".apple-mail-triage" / "config.toml"


def _load_config():
    if DEFAULT_CONFIG_PATH.exists():
        with DEFAULT_CONFIG_PATH.open("rb") as f:
            return tomllib.load(f)
    return {"ollama_model": "gemma3n:e4b", "content_truncate_bytes": 4096}


def _fetch_jxa(since: str, max_n: int, truncate: int) -> tuple[float, list[dict], list[str]]:
    """Run the legacy JXA fetcher; return (elapsed_s, messages, errors)."""
    cmd = [
        "/usr/bin/osascript", "-l", "JavaScript", str(JXA_SCRIPT),
        "--since", since, "--max", str(max_n), "--truncate-bytes", str(truncate),
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    elapsed = time.perf_counter() - t0
    msgs, errs = [], []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError as e:
            errs.append(f"bad ndjson: {e}")
    if proc.stderr.strip():
        errs.extend(proc.stderr.strip().splitlines())
    return elapsed, msgs, errs


def _fetch_disk(since: str, max_n: int, truncate: int) -> tuple[float, list[dict], list[str]]:
    """Run the new disk-direct fetcher; return (elapsed_s, messages, errors)."""
    t0 = time.perf_counter()
    msgs, errs = [], []
    for msg, err in fetcher_mod.stream_messages_since(since, max_n, truncate):
        if err is not None:
            errs.append(err)
        elif msg is not None:
            msgs.append(msg)
    elapsed = time.perf_counter() - t0
    return elapsed, msgs, errs


def _time_prefilter(messages: list[dict]) -> list[float]:
    times = []
    for m in messages:
        t0 = time.perf_counter()
        filter_message(m)
        times.append(time.perf_counter() - t0)
    return times


def _time_classify(messages: list[dict], model: str, truncate: int) -> list[tuple[float, dict]]:
    """Returns [(wall_seconds, classify_result), ...] for each message."""
    out = []
    for m in messages:
        t0 = time.perf_counter()
        result = classify_mod.classify(m, model=model, content_truncate_bytes=truncate)
        out.append((time.perf_counter() - t0, result))
    return out


def _fmt_ms(seconds: float) -> str:
    return f"{seconds * 1000:8.1f} ms"


def _summary(label: str, times: list[float]) -> str:
    if not times:
        return f"  {label}: (no data)"
    total = sum(times)
    return (
        f"  {label}: total={_fmt_ms(total)}  "
        f"per-msg avg={_fmt_ms(statistics.mean(times))}  "
        f"median={_fmt_ms(statistics.median(times))}  "
        f"max={_fmt_ms(max(times))}  n={len(times)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="ISO8601 timestamp")
    ap.add_argument("--max", type=int, default=50)
    ap.add_argument("--truncate-bytes", type=int, default=4096)
    ap.add_argument("--no-classify", action="store_true",
                    help="Skip Ollama classification step")
    ap.add_argument("--run-jxa", action="store_true",
                    help="Also run the legacy JXA fetcher for comparison (~2 min)")
    ap.add_argument("--skip-disk", action="store_true")
    args = ap.parse_args()
    args.skip_jxa = not args.run_jxa  # JXA is opt-in; disk is default

    cfg = _load_config()
    model = cfg.get("ollama_model", "gemma3n:e4b")
    truncate = args.truncate_bytes or int(cfg.get("content_truncate_bytes", 4096))

    print(f"Benchmark — since={args.since}  max={args.max}  truncate={truncate}B  model={model}")
    print()

    jxa_msgs: list[dict] = []
    disk_msgs: list[dict] = []

    # ── Fetch comparison ────────────────────────────────────────────────────
    if not args.skip_jxa:
        print("=== JXA fetch (jxa/fetch_new_messages.js) ===")
        try:
            t, msgs, errs = _fetch_jxa(args.since, args.max, truncate)
            jxa_msgs = msgs
            print(f"  fetch wall: {_fmt_ms(t)}  msgs={len(msgs)}  errs={len(errs)}")
            if errs:
                for e in errs[:3]:
                    print(f"    ! {e[:160]}")
        except Exception as e:
            print(f"  ! JXA fetch failed: {e}")
        print()

    if not args.skip_disk:
        print("=== Disk fetch (fetcher.stream_messages_since — Envelope Index + .emlx) ===")
        try:
            t, msgs, errs = _fetch_disk(args.since, args.max, truncate)
            disk_msgs = msgs
            print(f"  fetch wall: {_fmt_ms(t)}  msgs={len(msgs)}  errs={len(errs)}")
            if errs:
                for e in errs[:3]:
                    print(f"    ! {e[:160]}")
        except Exception as e:
            print(f"  ! Disk fetch failed: {e}")
        print()

    # ── Per-step timings on the disk-fetched batch (or JXA if disk skipped) ─
    sample = disk_msgs or jxa_msgs
    if not sample:
        print("(no messages — skipping per-step timing)")
        return 0

    print(f"=== Per-step timings on {len(sample)} message(s) ===")

    pre_times = _time_prefilter(sample)
    print(_summary("prefilter", pre_times))

    if not args.no_classify:
        # Only classify messages the prefilter would keep — matches agent.py.
        kept = [m for m in sample if filter_message(m)[0]]
        print(f"  (kept after prefilter: {len(kept)} of {len(sample)})")
        cls_times = [t for t, _ in _time_classify(kept, model, truncate)]
        print(_summary("classify", cls_times))

    return 0


if __name__ == "__main__":
    sys.exit(main())
