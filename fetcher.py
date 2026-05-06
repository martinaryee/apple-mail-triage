"""
fetcher.py — replaces fetch_new_messages.js (JXA) with direct disk reads.

Strategy: read .emlx files via imdinu/apple-mail-mcp's tested parser, and
query its on-disk index for the SINCE-timestamp filter. Apple Mail itself
is never touched on the fetch path — eliminating the unresponsiveness that
the old JXA approach caused.

The agent still uses JXA for ONE thing: setting flag colors after
classification (see jxa/set_flags.js). Account UUIDs from disk are mapped
to friendly names via a one-shot list_accounts.js call so the flag-setter
can do `mail.accounts.byName(...)` unchanged.

First run: requires `apple-mail-mcp index` to build the FTS5 index from
~/Library/Mail/V*/. Terminal needs Full Disk Access. Subsequent runs do
an incremental sync (typically <1s).
"""

from __future__ import annotations

import email
import json
import logging
import plistlib
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Plist footer flag bits — see Mail.app reverse-engineering docs.
_FLAG_BIT_READ = 1 << 0
_FLAG_BIT_JUNK = 1 << 11

_JXA_LIST_ACCOUNTS = Path(__file__).resolve().parent / "jxa" / "list_accounts.js"

# Mailboxes treated as "inbox". Apple Mail's IMAP accounts use "INBOX",
# local accounts use "Inbox"; we match case-insensitively.
_INBOX_NAMES = ("inbox",)


# ── Account UUID → friendly name ─────────────────────────────────────────────


def load_account_map() -> dict[str, str]:
    """One-shot JXA call returning {uuid: friendly_name}.

    The JXA round trip costs ~0.5s and only reads metadata, so it does not
    block Mail.app meaningfully. Returns {} on failure; callers fall back
    to the UUID, which still flows through the rest of the pipeline (the
    flag-setter would just fail for those accounts — logged but non-fatal).
    """
    if not _JXA_LIST_ACCOUNTS.exists():
        return {}
    try:
        proc = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", str(_JXA_LIST_ACCOUNTS)],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("list_accounts JXA failed: %s", e)
        return {}

    if proc.returncode != 0:
        logger.warning("list_accounts JXA exit %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return {}
    try:
        data = json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError as e:
        logger.warning("list_accounts JXA bad JSON: %s", e)
        return {}
    return {entry["id"]: entry["name"] for entry in data
            if isinstance(entry, dict) and "id" in entry and "name" in entry}


# ── .emlx auxiliary parsing (junk flag + raw headers) ────────────────────────
#
# imdinu's parse_emlx returns body, sender, subject, read, flagged, etc. but
# does not expose:
#   • the junk-mail flag (plist bit 11)
#   • the raw RFC-822 header block (needed by prefilter.py to inspect
#     Auto-Submitted, Precedence, List-Unsubscribe)
# We re-read the file once to extract these. The cost is negligible because
# emlx files are small (median ~10 KB) and we process at most ~200/run.


def _emlx_extras(emlx_path: Path) -> dict:
    """Return {'headers': str, 'junk': bool} for an .emlx file.

    On any read/parse error returns sensible defaults rather than raising —
    a partially-parsed message is more useful than dropping it entirely.
    """
    try:
        raw = emlx_path.read_bytes()
    except OSError:
        return {"headers": "", "junk": False}

    nl = raw.find(b"\n")
    if nl < 0:
        return {"headers": "", "junk": False}
    try:
        byte_count = int(raw[:nl].strip())
    except ValueError:
        return {"headers": "", "junk": False}

    mime_start = nl + 1
    mime_end = mime_start + byte_count
    mime_bytes = raw[mime_start:mime_end]
    plist_bytes = raw[mime_end:]

    # Extract the raw header block (everything before the first blank line).
    sep = mime_bytes.find(b"\r\n\r\n")
    if sep < 0:
        sep = mime_bytes.find(b"\n\n")
    headers = mime_bytes[: sep if sep >= 0 else len(mime_bytes)].decode(
        "utf-8", errors="replace"
    )

    junk = False
    if plist_bytes.strip():
        try:
            plist = plistlib.loads(plist_bytes)
            flags = int(plist.get("flags", 0))
            junk = bool(flags & _FLAG_BIT_JUNK)
        except Exception:
            pass

    return {"headers": headers, "junk": junk}


# ── Index management (delegated to apple-mail-mcp) ───────────────────────────


def _ensure_index_ready() -> "IndexManager":  # noqa: F821
    """Return a synced IndexManager, building from disk if needed.

    Building from scratch on a 100k+ mailbox takes a few minutes; subsequent
    syncs are incremental and finish in <1s. The user must have granted Full
    Disk Access to the terminal/launchd binary that runs this.
    """
    # Imported lazily so missing Full Disk Access produces a clean error
    # at the call site rather than at module import time.
    from apple_mail_mcp.index import IndexManager
    from apple_mail_mcp.index.disk import find_mail_directory

    manager = IndexManager.get_instance()

    if not manager.has_index():
        logger.info("Building apple-mail-mcp index from disk (first run)…")
        mail_dir = find_mail_directory()
        manager.build_from_disk(mail_dir)
        logger.info("Index build complete.")
    else:
        manager.sync_updates()
    return manager


# ── Public API ───────────────────────────────────────────────────────────────


def stream_messages_since(
    since_iso: str,
    max_messages: int,
    truncate_bytes: int,
) -> Iterator[tuple[Optional[dict], Optional[str]]]:
    """Yield (msg_dict, None) or (None, error_str) — same contract as the
    JXA-based stream_messages() it replaces.

    Args:
        since_iso: ISO 8601 timestamp; only messages with date_received >=
            since are returned.
        max_messages: Cap on emitted messages. The caller asks for cap+1 so
            it can detect a backlog; we honour LIMIT exactly.
        truncate_bytes: Truncate body content to this many UTF-8 bytes
            before emitting.
    """
    try:
        manager = _ensure_index_ready()
    except (FileNotFoundError, PermissionError) as e:
        yield None, f"index init: {e}"
        return
    except Exception as e:  # noqa: BLE001
        yield None, f"index init failed: {e}"
        return

    account_map = load_account_map()

    # Normalise the since timestamp to imdinu's storage format (UTC isoformat
    # with "+00:00") so the lexical comparison in SQL works correctly. JXA's
    # toISOString() produces the "Z" form, which sorts BEFORE "+00:00" for
    # the same instant — would skip valid messages without normalisation.
    try:
        since_norm = _normalise_iso(since_iso)
    except ValueError as e:
        yield None, f"bad since timestamp {since_iso!r}: {e}"
        return

    # Query the index DB directly. _get_conn() is technically private but
    # the connection is the cleanest entry point; the schema is stable.
    conn = manager._get_conn()
    try:
        cursor = conn.execute(
            """
            SELECT message_id, account, mailbox, subject, sender,
                   content, date_received, emlx_path
            FROM emails
            WHERE date_received >= ?
              AND lower(mailbox) IN ({placeholders})
              AND emlx_path IS NOT NULL
            ORDER BY date_received ASC
            LIMIT ?
            """.format(placeholders=",".join("?" * len(_INBOX_NAMES))),
            (since_norm, *_INBOX_NAMES, max_messages),
        )
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        yield None, f"index query: {e}"
        return

    # Lazy import only inside this function so module-level import cost
    # is bounded.
    from apple_mail_mcp.index.disk import parse_emlx

    for row in rows:
        emlx_path = Path(row["emlx_path"])
        parsed = parse_emlx(emlx_path)
        if parsed is None:
            yield None, f"emlx parse failed: {emlx_path}"
            continue

        extras = _emlx_extras(emlx_path)
        content = parsed.content or row["content"] or ""
        if truncate_bytes > 0:
            content = _truncate_to_bytes(content, truncate_bytes)

        yield {
            "id": row["message_id"],
            "account": account_map.get(row["account"], row["account"]),
            "mailbox": row["mailbox"],
            "subject": parsed.subject or row["subject"] or "",
            "sender": parsed.sender or row["sender"] or "",
            "replyTo": parsed.reply_to or None,
            "messageId": parsed.message_id_header or "",
            "dateReceived": parsed.date_received or row["date_received"] or "",
            "junk": extras["junk"],
            "read": bool(parsed.read) if parsed.read is not None else False,
            "headers": extras["headers"],
            "content": content,
        }, None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _normalise_iso(iso: str) -> str:
    """Parse an ISO timestamp and re-emit in datetime.isoformat() form.

    Handles the trailing 'Z' (UTC) shorthand. Always emits a tz-aware string
    so lexical comparisons against imdinu's index entries are correct.
    """
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    # errors='ignore' drops the dangling multibyte sequence at the cut.
    return enc[:max_bytes].decode("utf-8", errors="ignore") + "…"
