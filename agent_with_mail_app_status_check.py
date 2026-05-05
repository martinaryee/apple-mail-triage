#!/usr/bin/env python3
"""Wrapper that checks mail app status before running the agent.

Only 1 run is allowed at a time. The wrapper owns the lock file
(~/.mail-agent/agent.lock) and prevents overlapping runs.

When mail app is active (running + focused), the wrapper polls every
poll_delay_seconds until mail becomes idle, up to max_wait_minutes.
On timeout, it forces proceeds anyway.
"""

import argparse
import fcntl
import io
import logging
import os
import subprocess
import sys
import tomllib
import time
from pathlib import Path

from stats import RunStats

# Paths
DEFAULT_LOCK_PATH = Path.home() / ".mail-agent" / "agent.lock"
DEFAULT_LOG_DIR = Path.home() / ".mail-agent" / "logs"
DEFAULT_CONFIG_PATH = Path.home() / ".mail-agent" / "config.toml"
AGENT_SCRIPT = Path(__file__).resolve().parent / "agent.py"

# Polling defaults
DEFAULT_POLL_DELAY_SECONDS = 30
DEFAULT_MAX_WAIT_MINUTES = 30


def is_mail_app_active() -> bool:
    """Return True if Mail app is running AND has focus."""
    result = subprocess.run(
        ["pgrep", "-x", "Mail"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    
    # Check if Mail is frontmost (focused)
    script = '''
    tell application "System Events"
        tell process "Mail"
            get frontmost
        end tell
    end tell
    '''
    result = subprocess.run(
        ["osascript"],
        input=script,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # If we can't check, assume not active (conservative)
        return False
    
    return result.stdout.strip().lower() == "true"


def acquire_lock(lock_path: Path) -> "io.TextIOWrapper | None":
    """Try to acquire an exclusive lock. Returns file handle on success."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    
    log = logging.getLogger("lock")
    
    # Check for stale/empty lock file and remove it
    if lock_path.exists():
        try:
            with lock_path.open("r") as f:
                content = f.read().strip()
            if not content:
                # Empty lock file - stale, remove it
                log.info("Found empty lock file - removing stale lock")
                lock_path.unlink(missing_ok=True)
            else:
                # Try to read PID and check if process exists
                pid = int(content)
                os.kill(pid, 0)  # Raises OSError if process doesn't exist
                # Process exists, lock is valid
                log.debug(f"Lock held by PID {pid}, waiting...")
                return None
        except ValueError:
            # PID is invalid - stale lock
            log.info(f"Found invalid PID in lock file - removing stale lock")
            lock_path.unlink(missing_ok=True)
        except OSError:
            # Process doesn't exist - stale lock
            log.info(f"Lock held by nonexistent PID - removing stale lock")
            lock_path.unlink(missing_ok=True)
    
    try:
        fh = lock_path.open("w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        log.debug(f"Lock acquired successfully (PID {os.getpid()})")
        return fh
    except OSError as e:
        log.debug(f"Lock acquisition failed: {e}")
        try:
            fh.close()
        except:
            pass
        try:
            lock_path.unlink(missing_ok=True)
        except:
            pass
        return None


def wait_for_mail_idle(
    poll_delay_seconds: int,
    max_wait_minutes: int,
    log: logging.Logger,
) -> tuple[bool, int]:
    """Wait until mail app is idle. Return (forced_run, waited_seconds)."""
    start_time = time.time()
    max_wait_seconds = max_wait_minutes * 60
    poll_count = 0
    log.info(f"Mail app is active. Waiting for idle (max {max_wait_minutes} min)")
    
    while (time.time() - start_time) < max_wait_seconds:
        if not is_mail_app_active():
            waited = int(time.time() - start_time)
            log.info(f"Mail app is now idle. Proceeding after {waited}s wait")
            return (False, waited)
        
        poll_count += 1
        elapsed = int(time.time() - start_time)
        remaining = max_wait_seconds - elapsed
        log.info(
            f"Mail app still active (poll {poll_count}). "
            f"Waiting {poll_delay_seconds}s... ({remaining}s remaining)"
        )
        time.sleep(poll_delay_seconds)
    
    waited = int(time.time() - start_time)
    log.warning(
        f"Max wait time exceeded ({max_wait_minutes} min). "
        f"Force proceeding after {waited}s with {poll_count} polls"
    )
    return (True, waited)


def load_config(path: Path) -> dict:
    """Load configuration from TOML file."""
    if not path.exists():
        sys.exit(f"Config not found at {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mail-to-todo agent with mail app status checking"
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to config.toml",
    )
    ap.add_argument(
        "--poll-delay",
        type=int,
        default=DEFAULT_POLL_DELAY_SECONDS,
        help="Seconds between mail status checks",
    )
    ap.add_argument(
        "--max-wait",
        type=int,
        default=DEFAULT_MAX_WAIT_MINUTES,
        help="Maximum minutes to wait for mail idle",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--since", help="Override 'since' timestamp")
    ap.add_argument("--reset", action="store_true", help="Reset state files")
    args = ap.parse_args()
    
    cfg = load_config(args.config)
    poll_delay = int(
        cfg.get("mail_app_status_check", {})
        .get("poll_delay_seconds", args.poll_delay)
    )
    max_wait = int(
        cfg.get("mail_app_status_check", {})
        .get("max_wait_minutes", args.max_wait)
    )
    
    DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DEFAULT_LOG_DIR / "agent.log"
    log_level = getattr(
        logging,
        cfg.get("log_level", "INFO").upper(),
        logging.INFO,
    )
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)],
    )
    log = logging.getLogger("mail_app_checker")
    
    lock_fh = acquire_lock(DEFAULT_LOCK_PATH)
    if lock_fh is None:
        log.debug("Another run is active — skipping")
        return 0
    
    try:
        if is_mail_app_active():
            forced_run, waited_seconds = wait_for_mail_idle(
                poll_delay_seconds=poll_delay,
                max_wait_minutes=max_wait,
                log=log,
            )
            
            runs_path = DEFAULT_LOG_DIR / "runs.ndjson"
            model = cfg["ollama_model"]
            cap = int(cfg["max_messages_per_run"])
            
            stats = RunStats(
                model=model,
                dry_run=args.dry_run,
                max_messages_per_run=cap,
                schema_version=2,
            )
            stats.set_mail_app_status(skipped=True, waited_seconds=waited_seconds, forced_run=forced_run)
            stats.write(runs_path)
            
            log.info(f"Skipped run due to mail app being active. Waited {waited_seconds}s")
            
            if forced_run:
                log.info("Force proceeding after timeout")
            else:
                log.info("Proceeding with agent after mail became idle")
        else:
            log.info("Mail app not active. Proceeding with agent")
        
        cmd = [
            sys.executable,
            str(AGENT_SCRIPT),
            "--config", str(args.config),
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        if args.verbose:
            cmd.append("--verbose")
        if args.since:
            cmd.append("--since")
            cmd.append(args.since)
        if args.reset:
            cmd.append("--reset")
        
        result = subprocess.run(cmd)
        return result.returncode
    
    finally:
        if lock_fh:
            lock_fh.close()


if __name__ == "__main__":
    sys.exit(main())
