#!/usr/bin/env python3
"""Mail-to-todo orchestrator. Run via launchd every N minutes.

Pipeline per run: fetch new messages from Apple Mail (JXA) -> prefilter
heuristics -> classify with local Ollama -> append actionable items to a
markdown review queue in Obsidian -> record outcome in SQLite cache and
runs.ndjson.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import classify as classify_mod
from prefilter import filter_message
from state import State
from stats import RunStats
from triage_queue import append_block, existing_message_ids

DEFAULT_CONFIG_PATH = Path.home() / ".mail-agent" / "config.toml"
DEFAULT_DB_PATH = Path.home() / ".mail-agent" / "state.db"
DEFAULT_LOG_DIR = Path.home() / ".mail-agent" / "logs"
JXA_SCRIPT = Path(__file__).resolve().parent / "jxa" / "fetch_new_messages.js"


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"Config not found at {path}. "
            f"Copy config.toml.example to {path} and edit it."
        )
    with path.open("rb") as f:
        return tomllib.load(f)


def compute_since(db_path: Path, start_date: str) -> str:
    """Earliest date the JXA fetcher should look back to.

    Returns max(start_date, max over accounts of MAX(date_received)). Using
    the global max (not min-per-account) prevents the slowest account from
    dragging `since` backward, which would re-fetch already-processed
    messages every batch and stall progress. Trade-off: messages older than
    the leading account's high_water in lagging accounts are skipped — the
    proper fix is per-account since plumbed into JXA, deferred to v2.
    """
    if not db_path.exists():
        return start_date
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(date_received) FROM processed"
        ).fetchone()
    high = row[0] if row and row[0] else None
    if not high:
        return start_date
    return high if high > start_date else start_date


def fetch_messages(
    since_iso: str, max_n: int, truncate_bytes: int
) -> tuple[list[dict], list[str]]:
    """Run the JXA fetcher and parse NDJSON. Returns (messages, errors)."""
    cmd = [
        "/usr/bin/osascript",
        "-l", "JavaScript",
        str(JXA_SCRIPT),
        "--since", since_iso,
        "--max", str(max_n),
        "--truncate-bytes", str(truncate_bytes),
    ]
    errors: list[str] = []
    msgs: list[dict] = []
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        return msgs, [f"jxa timeout after 300s (since={since_iso})"]
    except FileNotFoundError as e:
        return msgs, [f"osascript not found: {e}"]

    if proc.returncode != 0:
        errors.append(
            f"jxa exit {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
        return msgs, errors

    # Split only on '\n' — NOT splitlines(), which also breaks on U+2028/U+2029
    # that JXA's JSON.stringify leaves unescaped (legal JSON, but ambiguous as
    # NDJSON line separators). Each emit ends with '\n' from writeStdout.
    for line in proc.stdout.split("\n"):
        line = line.strip("\r")
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError as e:
            errors.append(f"bad jxa ndjson: {e}: {line[:200]}")

    if proc.stderr.strip():
        for ln in proc.stderr.strip().splitlines():
            errors.append(f"jxa stderr: {ln}")
    return msgs, errors


def reset_state(queue_path: Path, db_path: Path) -> None:
    print("This will delete:")
    print(f"  {queue_path}  ({'exists' if queue_path.exists() else 'absent'})")
    print(f"  {db_path}  ({'exists' if db_path.exists() else 'absent'})")
    ans = input("Type 'yes' to confirm: ").strip()
    if ans != "yes":
        print("Aborted.")
        return
    if queue_path.exists():
        queue_path.unlink()
        print(f"Deleted {queue_path}")
    if db_path.exists():
        db_path.unlink()
        print(f"Deleted {db_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mail-to-todo agent.")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true",
                    help="classify but do not write to queue, state, or runs.ndjson")
    ap.add_argument("--reset", action="store_true",
                    help="delete the queue file and state.db (with confirmation)")
    ap.add_argument("--since", help="override since (ISO8601) for testing")
    args = ap.parse_args()

    cfg = load_config(args.config)
    queue_path = Path(cfg["vault_path"]) / cfg["queue_file"]
    db_path = DEFAULT_DB_PATH

    if args.reset:
        reset_state(queue_path, db_path)
        return 0

    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEFAULT_LOG_DIR / "agent.log"
    runs_path = DEFAULT_LOG_DIR / "runs.ndjson"
    log_level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)],
    )
    log = logging.getLogger("agent")

    model = cfg["ollama_model"]
    cap = int(cfg["max_messages_per_run"])
    truncate = int(cfg["content_truncate_bytes"])
    start_date = str(cfg["start_date"])
    since = args.since or compute_since(db_path, start_date)

    log.info(
        "run start: model=%s since=%s cap=%d dry=%s queue=%s",
        model, since, cap, args.dry_run, queue_path,
    )

    stats = RunStats(model=model, dry_run=args.dry_run, max_messages_per_run=cap)

    # Ask JXA for cap+1 so we can detect overflow.
    msgs, fetch_errors = fetch_messages(since, cap + 1, truncate)
    for e in fetch_errors:
        stats.record_error("fetch", None, e)
        log.warning("fetch: %s", e)

    cap_hit = len(msgs) > cap
    backlog = len(msgs) - cap if cap_hit else 0
    if cap_hit:
        msgs.sort(key=lambda m: m.get("dateReceived") or "")
        msgs = msgs[:cap]
    else:
        msgs.sort(key=lambda m: m.get("dateReceived") or "")
    stats.set_cap_hit(cap_hit, backlog)

    # Safety belt: existing queue ids prevent duplicate writes if state.db
    # is fresh but queue.md was kept (or vice versa).
    existing_ids = existing_message_ids(queue_path) if queue_path.exists() else set()

    with State(db_path) as state:
        for msg in msgs:
            account = msg.get("account") or "unknown"
            mid = msg.get("messageId") or ""
            stats.record_fetched(account)

            if not mid:
                # Modern email always has Message-Id; without one we cannot
                # dedupe across runs, so skip and log.
                stats.record_error("fetch", None, "missing messageId; skipping")
                log.warning("missing Message-Id from %s; skipping", account)
                continue

            if state.is_processed(mid):
                stats.record_deduped_already_seen()
                continue

            keep, drop_reason = filter_message(msg)
            if not keep:
                stats.record_heuristic_dropped(drop_reason or "unknown")
                if not args.dry_run:
                    state.mark_processed(mid, account, msg.get("dateReceived", ""), False)
                continue

            try:
                result = classify_mod.classify(
                    msg, model=model, content_truncate_bytes=truncate,
                )
            except Exception as e:  # noqa: BLE001
                stats.record_error("classify", mid, str(e))
                log.exception("classify crashed for %s", mid)
                continue

            if result.get("error"):
                stats.record_error("classify", mid, result["error"])
                log.warning("classify error for %s: %s", mid, result["error"])

            actionable = bool(result.get("actionable", False))
            urgency = result.get("urgency", "low")
            if urgency not in ("low", "medium", "high"):
                urgency = "low"
            stats.record_classified(
                account, actionable, urgency,
                int(result.get("llm_ms", 0)),
                int(result.get("prompt_tokens", 0)),
                int(result.get("eval_tokens", 0)),
            )

            if actionable:
                if mid in existing_ids:
                    log.debug("queue dedupe (file): %s", mid)
                else:
                    entry = {
                        "title": result.get("title") or "(no title)",
                        "messageId": mid,
                        "urgency": urgency,
                        "sender": msg.get("sender", ""),
                        "dateReceived": msg.get("dateReceived", ""),
                        "reason": result.get("reason", ""),
                        "account": account,
                    }
                    if not args.dry_run:
                        append_block(queue_path, entry)
                        existing_ids.add(mid)
                    log.info(
                        "%s [%s] %s | %s",
                        "QUEUED" if not args.dry_run else "WOULD-QUEUE",
                        urgency, account, entry["title"],
                    )

            if not args.dry_run:
                state.mark_processed(
                    mid, account, msg.get("dateReceived", ""), actionable
                )

    if args.dry_run:
        log.info("[dry-run] not writing runs.ndjson")
    else:
        stats.write(runs_path)
    log.info("run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
