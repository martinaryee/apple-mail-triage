#!/usr/bin/env python3
"""Mail-to-todo orchestrator. Run via launchd every N minutes.

Pipeline per run: fetch new messages from Apple Mail (JXA) -> prefilter
heuristics -> classify with local Ollama -> append actionable items to a
markdown review queue in Obsidian -> record outcome in SQLite cache and
runs.ndjson.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
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
DEFAULT_LOCK_PATH = Path.home() / ".mail-agent" / "agent.lock"
JXA_SCRIPT     = Path(__file__).resolve().parent / "jxa" / "fetch_new_messages.js"
JXA_SET_FLAGS  = Path(__file__).resolve().parent / "jxa" / "set_flags.js"

_FLAG_GRAY   = 6  # processed, no action needed (grey — works on Gmail + Exchange)
_FLAG_YELLOW = 2  # low urgency actionable
_FLAG_ORANGE = 1  # medium urgency actionable
_FLAG_RED    = 0  # high urgency actionable
_URGENCY_TO_FLAG = {"high": _FLAG_RED, "medium": _FLAG_ORANGE, "low": _FLAG_YELLOW}


def acquire_run_lock(lock_path: Path) -> "io.TextIOWrapper | None":
    """Try to acquire an exclusive lock. Returns the open file handle on success,
    None if another run is already active.

    Uses fcntl.flock so the lock is released automatically by the OS if this
    process dies — no stale lock files possible.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


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


def stream_messages(
    since_iso: str, max_n: int, truncate_bytes: int
):
    """Yield (msg_dict, None) or (None, error_str) as JXA emits each line.

    Uses Popen so the caller sees each message the moment JXA writes it,
    rather than waiting for the entire fetch to complete. JXA emits
    oldest-first and caps at max_n, so no re-sorting is needed.

    Note on stderr: we read it after stdout is exhausted. JXA writes only
    short warning lines to stderr, so the pipe buffer will not fill and
    deadlock.
    """
    cmd = [
        "/usr/bin/osascript",
        "-l", "JavaScript",
        str(JXA_SCRIPT),
        "--since", since_iso,
        "--max", str(max_n),
        "--truncate-bytes", str(truncate_bytes),
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError as e:
        yield None, f"osascript not found: {e}"
        return

    assert proc.stdout is not None
    assert proc.stderr is not None

    # Read stdout line-by-line — strip only \r to preserve the splitlines()
    # avoidance documented in HANDOFF.md (U+2028/U+2029 in JSON strings).
    for raw_line in proc.stdout:
        line = raw_line.strip("\r\n")
        if not line:
            continue
        try:
            yield json.loads(line), None
        except json.JSONDecodeError as e:
            yield None, f"bad jxa ndjson: {e}: {line[:200]}"

    proc.stdout.close()
    stderr_text = proc.stderr.read()
    proc.stderr.close()
    proc.wait()

    if proc.returncode != 0:
        yield None, f"jxa exit {proc.returncode}: {stderr_text.strip()[:500]}"
        return
    if stderr_text.strip():
        for ln in stderr_text.strip().splitlines():
            yield None, f"jxa stderr: {ln}"


def _apply_flags(assignments: list[dict], log: logging.Logger) -> None:
    """Set Apple Mail flag colors for a batch of messages via JXA."""
    if not assignments:
        return
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(assignments, f)
        tmp_path = f.name
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", str(JXA_SET_FLAGS),
             "--input", tmp_path],
            capture_output=True, text=True, timeout=600,
        )
        if proc.stderr.strip():
            log.info("set_flags: %s", proc.stderr.strip())
        if proc.returncode != 0:
            log.warning("set_flags exited %d", proc.returncode)
    except subprocess.TimeoutExpired:
        log.warning("set_flags timed out")
    finally:
        Path(tmp_path).unlink(missing_ok=True)


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
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="print one-line status for every message to stderr")
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

    lock_fh: io.TextIOWrapper | None = None
    if not args.dry_run:
        lock_fh = acquire_run_lock(DEFAULT_LOCK_PATH)
        if lock_fh is None:
            log.info("another run is active — skipping")
            return 0

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

    # Safety belt: existing queue ids prevent duplicate writes if state.db
    # is fresh but queue.md was kept (or vice versa).
    existing_ids = existing_message_ids(queue_path) if queue_path.exists() else set()

    def vprint(n: int, tag: str, detail: str) -> None:
        if args.verbose:
            print(f"[{n}] {tag:30s} {detail}", file=sys.stderr, flush=True)

    if args.verbose:
        print(f"Fetching messages since {since} …", file=sys.stderr, flush=True)

    # Ask JXA for cap+1 so we can detect overflow. JXA emits oldest-first and
    # applies the cap internally, so no re-sort is needed here.
    n = 0
    cap_hit = False
    backlog = 0
    flag_assignments: list[dict] = []

    def _flag(msg: dict, flag_index: int) -> None:
        mail_id = msg.get("id")
        if mail_id and msg.get("account") and msg.get("mailbox"):
            flag_assignments.append({
                "account": msg["account"],
                "mailbox": msg["mailbox"],
                "id": mail_id,
                "flagIndex": flag_index,
            })

    with State(db_path) as state:
        for msg, err in stream_messages(since, cap + 1, truncate):
            if err:
                stats.record_error("fetch", None, err)
                log.warning("fetch: %s", err)
                continue

            # If JXA sent cap+1 messages the queue has a backlog; count extras
            # but don't process them.
            if n >= cap:
                cap_hit = True
                backlog += 1
                continue

            n += 1
            account = msg.get("account") or "unknown"
            mid = msg.get("messageId") or ""
            subject = msg.get("subject") or "(no subject)"
            sender = msg.get("sender") or ""
            stats.record_fetched(account)

            if not mid:
                # Modern email always has Message-Id; without one we cannot
                # dedupe across runs, so skip and log.
                stats.record_error("fetch", None, "missing messageId; skipping")
                log.warning("missing Message-Id from %s; skipping", account)
                vprint(n, "SKIP no-message-id", f"{sender} | {subject}")
                continue

            if state.is_processed(mid):
                stats.record_deduped_already_seen()
                vprint(n, "SKIP already-seen", f"{sender} | {subject}")
                continue

            keep, drop_reason = filter_message(msg)
            if not keep:
                stats.record_heuristic_dropped(drop_reason or "unknown")
                vprint(n, f"DROP {drop_reason}", f"{sender} | {subject}")
                _flag(msg, _FLAG_GRAY)
                if not args.dry_run:
                    state.mark_processed(mid, account, msg.get("dateReceived", ""), False)
                continue

            vprint(n, "CLASSIFYING...", f"{sender} | {subject}")
            try:
                result = classify_mod.classify(
                    msg, model=model, content_truncate_bytes=truncate,
                )
            except Exception as e:  # noqa: BLE001
                stats.record_error("classify", mid, str(e))
                log.exception("classify crashed for %s", mid)
                vprint(n, "ERROR classify-crash", f"{sender} | {subject}")
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
                _flag(msg, _URGENCY_TO_FLAG.get(urgency, _FLAG_YELLOW))
                if mid in existing_ids:
                    log.debug("queue dedupe (file): %s", mid)
                    vprint(n, f"ACTIONABLE [{urgency}] dup", f"{sender} | {subject}")
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
                    vprint(n, f"ACTIONABLE [{urgency}]", f"{sender} | {subject}")
                    log.info(
                        "%s [%s] %s | %s",
                        "QUEUED" if not args.dry_run else "WOULD-QUEUE",
                        urgency, account, entry["title"],
                    )
            else:
                _flag(msg, _FLAG_GRAY)
                vprint(n, "not actionable", f"{sender} | {subject}")

            if not args.dry_run:
                state.mark_processed(
                    mid, account, msg.get("dateReceived", ""), actionable
                )

    stats.set_cap_hit(cap_hit, backlog)

    if not args.dry_run:
        _apply_flags(flag_assignments, log)
    elif flag_assignments and args.verbose:
        print(f"[dry-run] would flag {len(flag_assignments)} messages", file=sys.stderr, flush=True)

    if args.verbose:
        print(
            f"Done: {n} message(s) processed{f', {backlog} in backlog' if cap_hit else ''}",
            file=sys.stderr, flush=True,
        )

    if args.dry_run:
        log.info("[dry-run] not writing runs.ndjson")
    else:
        stats.write(runs_path)
    log.info("run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
