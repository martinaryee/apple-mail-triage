"""Queue writer for the mail-to-todo agent.

Appends actionable-email blocks to a single markdown file (the user's
review queue inside an Obsidian vault). Safely append-only and deduplicated
against itself via embedded HTML-comment markers.
"""

import fcntl
import os
import re
import time
import urllib.parse
from pathlib import Path

# Matches <!-- mid:<MESSAGE_ID> urgency:<urgency> --> markers written by
# append_block.  MESSAGE_ID may contain any non-newline characters (including
# angle brackets, @, dots, slashes, etc.).
_MARKER_RE = re.compile(r'<!-- mid:(.*?) urgency:[a-z]+ -->')

_FILE_HEADER = """\
# Mail Triage
<!-- Mail-to-Todo agent appends candidate action items below.
     Tick the box and (optionally) cut/paste into your real task list.
     To redo the triage from scratch, delete THIS FILE *and*
     ~/.apple-mail-triage/state.db. -->
"""


def existing_message_ids(path: Path) -> set[str]:
    """Return the set of Message-Id values already recorded in the queue file.

    Extracted from ``<!-- mid:... urgency:... -->`` markers.  Returns an empty
    set if the file does not exist.
    """
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    return set(_MARKER_RE.findall(text))


def append_block(path: Path, entry: dict) -> None:
    """Append one block to the queue file.

    Creates the file (and any parent directories) with a one-line header if
    missing.  Idempotent: if *entry['messageId']* is already recorded in the
    file, this function does nothing.

    A ``fcntl.LOCK_EX`` file lock is held around the read-existing/append
    sequence so concurrent ``agent.py`` runs cannot produce duplicate entries.

    Empty or missing *messageId*: a synthetic ``no-message-id-{epoch_ms}``
    value is generated so deduplication bookkeeping still works.  Note that
    two calls with ``messageId=''`` will each get a *different* synthetic id
    and will therefore both be appended — this is intentional fallback
    behaviour for malformed messages where no stable id is available.
    """
    message_id: str = entry.get("messageId") or ""
    if not message_id:
        epoch_ms = int(time.time() * 1000)
        message_id = f"no-message-id-{epoch_ms}"

    title = entry["title"]
    urgency = entry["urgency"]
    sender = entry["sender"]
    date_received = entry["dateReceived"]
    reason = entry["reason"]
    account = entry["account"]

    encoded_id = urllib.parse.quote(message_id, safe="")

    block = (
        f"\n"
        f"- [ ] {title}  <!-- mid:{message_id} urgency:{urgency} -->\n"
        f"  - From: {sender} | {date_received}\n"
        f"  - Why: {reason}\n"
        f"  - [Open in Mail](message://{encoded_id})\n"
        f"  - Account: {account}\n"
    )

    # Ensure parent directories exist before we try to open the file.
    path.parent.mkdir(parents=True, exist_ok=True)

    # Open for reading+writing if the file already exists, otherwise create it.
    # We use the queue file itself as the lock target so a single fd covers
    # both the lock and the I/O.
    open_mode = "r+" if path.exists() else "w+"
    with open(path, open_mode, encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

        # --- critical section start ---

        # If the file is new (empty after open), write the header first.
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(_FILE_HEADER)

        # Re-read current ids now that we hold the lock (another process may
        # have appended between our earlier check and lock acquisition).
        fh.seek(0)
        existing_text = fh.read()
        recorded_ids = set(_MARKER_RE.findall(existing_text))

        if message_id in recorded_ids:
            # Already present — nothing to do.
            return

        # Append the block at the end.
        fh.seek(0, os.SEEK_END)
        fh.write(block)

        # Flush to disk before releasing the lock.
        fh.flush()
        os.fsync(fh.fileno())

        # --- critical section end (lock released on context-manager exit) ---
