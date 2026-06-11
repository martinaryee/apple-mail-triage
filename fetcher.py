"""
fetcher.py — disk-direct Apple Mail fetcher; replaces JXA fetch.

Reads Apple Mail's own Envelope Index SQLite database for the date filter,
then parses the corresponding .emlx file for body, headers, and flags. No
AppleScript runs on the fetch path — the unresponsiveness the old JXA
approach caused is eliminated.

Apple Mail itself maintains the Envelope Index continuously, so there is
no separate index for us to build, sync, or watch.

Setup: the user that runs this needs Full Disk Access (System Settings →
Privacy & Security → Full Disk Access). Without it ~/Library/Mail is
unreadable.
"""

from __future__ import annotations

import email
import email.message
import json
import logging
import plistlib
import re
import sqlite3
import subprocess
import urllib.parse
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Apple's Core Data epoch is 2001-01-01 00:00:00 UTC; offset from Unix epoch.
# Apple Mail stores date_received in the Envelope Index in this format.
_CORE_DATA_EPOCH = 978307200

# Plist footer flag bits in .emlx files (reverse-engineered from Mail.app).
_FLAG_BIT_READ = 1 << 0
_FLAG_BIT_JUNK = 1 << 11

_JXA_LIST_ACCOUNTS = Path(__file__).resolve().parent / "jxa" / "list_accounts.js"


# ── Mail directory & Envelope Index discovery ────────────────────────────────


def find_mail_dir() -> Path:
    """Return the highest-numbered ~/Library/Mail/V<N>/ directory.

    Apple Mail bumps V<N> on schema-breaking changes (V10 since macOS
    Catalina). Picking the highest keeps this working on V11+ in future
    macOS releases without code changes.
    """
    base = Path.home() / "Library" / "Mail"
    if not base.exists():
        raise FileNotFoundError(f"Apple Mail directory not found: {base}")
    candidates = []
    try:
        for entry in base.iterdir():
            if entry.is_dir() and entry.name.startswith("V") and entry.name[1:].isdigit():
                candidates.append((int(entry.name[1:]), entry))
    except PermissionError as e:
        raise PermissionError(
            f"Cannot read {base} — grant Full Disk Access in "
            "System Settings → Privacy & Security → Full Disk Access"
        ) from e
    if not candidates:
        raise FileNotFoundError(f"No V<N> mail directory under {base}")
    candidates.sort()
    return candidates[-1][1]


def find_envelope_index(mail_dir: Path) -> Path:
    # MailData has lived in two places across macOS versions:
    #   modern: ~/Library/Mail/V<N>/MailData/Envelope Index
    #   older:  ~/Library/Mail/MailData/Envelope Index
    # Probe both before giving up.
    for candidate in (mail_dir / "MailData" / "Envelope Index",
                      mail_dir.parent / "MailData" / "Envelope Index"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Envelope Index not found under {mail_dir} or {mail_dir.parent}"
    )


# ── Account UUID → friendly name (one-shot JXA call) ────────────────────────


def load_account_map() -> dict[str, str]:
    """One-shot JXA call returning {uuid: friendly_name}.

    JXA round-trip is short (~0.5s) and reads only metadata. Returns {} on
    failure — callers fall back to UUID-as-name and the flag-setter path
    will log a non-fatal warning for affected messages.
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
        logger.warning("list_accounts JXA exit %d: %s",
                       proc.returncode, proc.stderr.strip()[:200])
        return {}
    try:
        data = json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError as e:
        logger.warning("list_accounts JXA bad JSON: %s", e)
        return {}
    return {
        e["id"]: e["name"]
        for e in data
        if isinstance(e, dict) and "id" in e and "name" in e
    }


# ── Time conversion ──────────────────────────────────────────────────────────
#
# IMPORTANT — Envelope Index epoch:
#   The messages.date_received / date_sent columns in the Envelope Index are
#   plain Unix timestamps (seconds since 1970-01-01 UTC), NOT Core Data
#   timestamps. Use _iso_to_unix / _unix_to_iso for any SQL comparisons.
#
#   The Core Data helpers below are kept because .emlx plist footers and some
#   other Apple data structures do use the Core Data epoch (2001-01-01 UTC),
#   and they are unit-tested separately.


def _iso_to_unix(iso: str) -> float:
    """Convert ISO-8601 to a plain Unix timestamp. Used for Envelope Index SQL."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _unix_to_iso(ts: float | int | None) -> str:
    """Convert a plain Unix timestamp to ISO-8601. Used for Envelope Index dates."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return ""


def _iso_to_core_data(iso: str) -> float:
    """Convert ISO-8601 to Core Data seconds (since 2001-01-01 UTC)."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() - _CORE_DATA_EPOCH


def _core_data_to_iso(ts: float | int | None) -> str:
    if ts is None:
        return ""
    try:
        unix_ts = float(ts) + _CORE_DATA_EPOCH
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return ""


# ── .emlx parser ─────────────────────────────────────────────────────────────


def parse_emlx(path: Path, truncate_bytes: int) -> Optional[dict]:
    r"""Parse a single .emlx file end-to-end.

    Format:
        <byte_count>\n
        <RFC 822 message of exactly byte_count bytes>
        <plist footer with Apple metadata (flags, etc.)>

    Returns a dict with all fields the downstream pipeline needs, or None
    on read/parse failure. Best-effort: a partial parse is more useful
    than dropping the message entirely.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    nl = raw.find(b"\n")
    if nl < 0:
        return None
    try:
        byte_count = int(raw[:nl].strip())
    except ValueError:
        return None

    mime_start = nl + 1
    mime_end = mime_start + byte_count
    mime_bytes = raw[mime_start:mime_end]
    plist_bytes = raw[mime_end:]

    msg = email.message_from_bytes(mime_bytes)

    # Raw header block — prefilter.py inspects this for Auto-Submitted,
    # Precedence, List-Unsubscribe.
    sep = mime_bytes.find(b"\r\n\r\n")
    if sep < 0:
        sep = mime_bytes.find(b"\n\n")
    headers_text = mime_bytes[: sep if sep >= 0 else len(mime_bytes)].decode(
        "utf-8", errors="replace"
    )

    junk = read = False
    if plist_bytes.strip():
        try:
            flags = int(plistlib.loads(plist_bytes).get("flags", 0))
            read = bool(flags & _FLAG_BIT_READ)
            junk = bool(flags & _FLAG_BIT_JUNK)
        except Exception:
            pass

    subject = _decode_header(msg.get("Subject"))
    sender = _decode_header(msg.get("From"))
    reply_to = _decode_header(msg.get("Reply-To")) or None
    message_id = (msg.get("Message-Id") or msg.get("Message-ID") or "").strip()

    # Date received: prefer the Received header (delivery time), fall back
    # to Date header (composition time). Matches Apple Mail's own display.
    date_received = ""
    rec = msg.get("Received")
    if rec:
        i = rec.rfind(";")
        if i >= 0:
            try:
                date_received = parsedate_to_datetime(rec[i + 1:].strip()).isoformat()
            except (ValueError, TypeError):
                pass
    if not date_received and msg.get("Date"):
        try:
            date_received = parsedate_to_datetime(msg["Date"]).isoformat()
        except (ValueError, TypeError):
            pass

    content = _extract_body(msg)
    if truncate_bytes > 0:
        content = _truncate_to_bytes(content, truncate_bytes)

    return {
        "subject": subject,
        "sender": sender,
        "replyTo": reply_to,
        "messageId": message_id,
        "dateReceived": date_received,
        "junk": junk,
        "read": read,
        "headers": headers_text,
        "content": content,
    }


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return value


def _extract_body(msg: email.message.Message) -> str:
    """Plain-text body, preferring text/plain over text/html."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or "utf-8"
                    return payload.decode(cs, errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    cs = part.get_content_charset() or "utf-8"
                    return _strip_html(payload.decode(cs, errors="replace"))
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        cs = msg.get_content_charset() or "utf-8"
        text = payload.decode(cs, errors="replace")
        if msg.get_content_type() == "text/html":
            return _strip_html(text)
        return text
    return ""


def _strip_html(html: str) -> str:
    """Naive HTML strip — adequate for body text fed into an LLM."""
    no_scripts = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE
    )
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", no_scripts)).strip()


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    enc = s.encode("utf-8")
    if len(enc) <= max_bytes:
        return s
    # errors='ignore' drops a partial multibyte sequence at the cut point.
    return enc[:max_bytes].decode("utf-8", errors="ignore") + "…"


# ── .emlx path resolution ───────────────────────────────────────────────────


def _emlx_bucket_dirs(msg_id: int) -> list[str]:
    """Apple Mail's bucket directory names for a message id.

    Mail files each .emlx under directories named by the digits of
    (msg_id // 1000) in REVERSE order, then a Messages/ leaf:
        id 597914 -> 597914 // 1000 = 597 -> "597" reversed -> 7/9/5
        full path: Data/7/9/5/Messages/597914.emlx
        id  89346 ->  89346 // 1000 =  89 ->  "89" reversed -> 9/8  (2 levels)
        id    742 ->    742 // 1000 =   0 ->   "0"          -> 0    (1 level)
    The number of levels therefore scales with id magnitude. Verified to hold
    with zero mismatches across 9k+ messages on IMAP (Gmail) and Exchange
    (DFCI/HSPH) accounts. Returns the ordered bucket directory names.
    """
    return list(reversed(str(msg_id // 1000)))


def _emlx_path(mail_dir: Path, account_uuid: str, mailbox_name: str, msg_id: int) -> Optional[Path]:
    """Find the .emlx file for a (account, mailbox, msg_id) tuple.

    Apple Mail's on-disk layout has evolved across macOS versions:

      Older layout (no sub-UUID):
        V*/<UUID>/<mailbox>.mbox/Data/<a>/<b>/Messages/<id>.emlx

      Modern layout (with sub-UUID; all known current installs):
        V*/<UUID>/<mailbox>.mbox/<sub-UUID>/Data/<a>/<b>/<c>/Messages/<id>.emlx

    The bucket directories are derivable directly from the msg_id (see
    _emlx_bucket_dirs), so the common case is a single exists() check. A
    bounded glob over observed bucket depths (0–3) remains as a fallback for
    any layout the deterministic rule doesn't cover.

    Nested mailbox paths (e.g. "[Gmail]/All Mail") map to nested .mbox
    directories on disk: each path segment gets its own .mbox suffix.
        "[Gmail]/All Mail" → [Gmail].mbox/All Mail.mbox/
        "Inbox"            → Inbox.mbox/
    mailbox_name must already be URL-decoded (handled by _parse_mailbox_url).
    """
    # Build the .mbox directory path: each "/" in the mailbox name means a
    # nested .mbox directory on disk.
    mbox_dir = mail_dir / account_uuid
    for segment in mailbox_name.split("/"):
        mbox_dir = mbox_dir / f"{segment}.mbox"
    if not mbox_dir.is_dir():
        return None

    # Collect candidate Data/ directories: directly under mbox_dir (older
    # layout) and under any sub-UUID child directory (modern layout).
    data_dirs: list[Path] = []
    if (mbox_dir / "Data").is_dir():
        data_dirs.append(mbox_dir / "Data")
    try:
        for child in mbox_dir.iterdir():
            if child.is_dir() and (child / "Data").is_dir():
                data_dirs.append(child / "Data")
    except OSError:
        pass

    # Fast path: jump straight to the deterministic bucket directory. This
    # turns a per-message lookup from a full-mailbox scandir storm (Gmail's
    # "All Mail" is ~220k files across ~7400 dirs — tens of seconds per glob
    # when the VFS cache is cold or Mail is concurrently writing the tree)
    # into a single stat().
    bucket = _emlx_bucket_dirs(msg_id)
    for ext in (".emlx", ".partial.emlx"):
        for data_dir in data_dirs:
            candidate = data_dir
            for b in bucket:
                candidate = candidate / b
            candidate = candidate / "Messages" / f"{msg_id}{ext}"
            if candidate.is_file():
                return candidate

    # Fallback: deterministic path missed (unexpected layout / future macOS
    # change). Fall back to the original bounded glob so findability never
    # regresses — at worst this is as slow as the old behavior.
    for ext in (".emlx", ".partial.emlx"):
        for data_dir in data_dirs:
            for bucket_pattern in (
                f"Messages/{msg_id}{ext}",          # 0-level bucket
                f"*/Messages/{msg_id}{ext}",         # 1-level bucket
                f"*/*/Messages/{msg_id}{ext}",       # 2-level bucket
                f"*/*/*/Messages/{msg_id}{ext}",     # 3-level bucket (Exchange)
            ):
                for hit in data_dir.glob(bucket_pattern):
                    return hit
    return None


# ── Public API ───────────────────────────────────────────────────────────────


def stream_messages_since(
    since_iso: str,
    max_messages: int,
    truncate_bytes: int,
) -> Iterator[tuple[Optional[dict], Optional[str]]]:
    """Yield (msg_dict, None) or (None, error_str). Drop-in replacement for
    the JXA-backed stream_messages() — same dict shape, oldest-first sort,
    same cap semantics.
    """
    try:
        mail_dir = find_mail_dir()
        envelope = find_envelope_index(mail_dir)
    except (FileNotFoundError, PermissionError) as e:
        yield None, f"mail dir: {e}"
        return

    try:
        since_unix = _iso_to_unix(since_iso)  # Envelope Index uses plain Unix timestamps
    except (ValueError, TypeError) as e:
        yield None, f"bad since timestamp {since_iso!r}: {e}"
        return

    account_map = load_account_map()

    # Open the Envelope Index in immutable read-only mode so we don't
    # interfere with Mail.app's own writers (or compete for the lock).
    try:
        conn = sqlite3.connect(f"file:{envelope}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        yield None, f"open envelope index: {e}"
        return

    # Modern Mail.app stores subject/sender as foreign keys into separate
    # tables; we LEFT JOIN them as fallbacks for cases where the .emlx is
    # missing or unreadable. parse_emlx remains the primary source.
    #
    # We fetch from ALL mailboxes except known noise folders (Spam, Trash,
    # Deleted Items, Drafts, Outbox) rather than restricting to inbox. This
    # makes the query uniform across account types: Gmail stores inbox messages
    # in [Gmail]/All Mail (not a dedicated INBOX folder), and some mail rules
    # sort messages into custom folders. Our prefilter and LLM handle content-
    # based triage; the only structural exclusion needed is "clearly not received
    # mail". Sent mail that slips through will not be classified as actionable.
    try:
        cursor = conn.execute(
            """
            SELECT
                m.ROWID         AS msg_id,
                m.date_received AS date_received,
                s.subject       AS subject,
                a.address       AS sender_addr,
                a.comment       AS sender_name,
                mb.url          AS mailbox_url
            FROM messages m
            LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
            LEFT JOIN subjects  s  ON m.subject = s.ROWID
            LEFT JOIN addresses a  ON m.sender  = a.ROWID
            WHERE m.date_received >= ?
              AND lower(mb.url) NOT LIKE '%spam%'
              AND lower(mb.url) NOT LIKE '%junk%'
              AND lower(mb.url) NOT LIKE '%trash%'
              AND lower(mb.url) NOT LIKE '%deleted%'
              AND lower(mb.url) NOT LIKE '%draft%'
              AND lower(mb.url) NOT LIKE '%outbox%'
              AND lower(mb.url) NOT LIKE '%sent%'
            ORDER BY m.date_received ASC
            LIMIT ?
            """,
            (since_unix, max_messages),
        )
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        conn.close()
        yield None, f"envelope query: {e}"
        return
    finally:
        conn.close()

    for row in rows:
        account_uuid, mailbox_name = _parse_mailbox_url(row["mailbox_url"])
        emlx_path = _emlx_path(mail_dir, account_uuid, mailbox_name, row["msg_id"])
        if emlx_path is None:
            yield None, f"emlx not found for {account_uuid}/{mailbox_name}/{row['msg_id']}"
            continue

        parsed = parse_emlx(emlx_path, truncate_bytes)
        if parsed is None:
            yield None, f"emlx parse failed: {emlx_path}"
            continue

        sender = parsed["sender"]
        if not sender and row["sender_addr"]:
            sender = (
                f"{row['sender_name']} <{row['sender_addr']}>"
                if row["sender_name"] else row["sender_addr"]
            )

        yield {
            "id": row["msg_id"],
            "account": account_map.get(account_uuid, account_uuid),
            "mailbox": mailbox_name,
            "subject": parsed["subject"] or row["subject"] or "",
            "sender": sender or "",
            "replyTo": parsed["replyTo"],
            "messageId": parsed["messageId"],
            "dateReceived": parsed["dateReceived"] or _unix_to_iso(row["date_received"]),
            "junk": parsed["junk"],
            "read": parsed["read"],
            "headers": parsed["headers"],
            "content": parsed["content"],
        }, None


def _parse_mailbox_url(url: Optional[str]) -> tuple[str, str]:
    """<scheme>://<UUID>/<mailbox-path> → (uuid, url-decoded mailbox-path).

    Apple Mail uses different schemes per account type:
        mailbox:// for IMAP/POP
        ews://     for Exchange Web Services
        imap://, pop3:// also seen on some macOS versions
    The on-disk layout under ~/Library/Mail/V*/<UUID>/ is uniform
    regardless of scheme, so we strip whatever scheme is present.

    The mailbox path is URL-decoded so callers work with plain names:
        imap://UUID/%5BGmail%5D/All%20Mail → ("[Gmail]/All Mail")
        ews://UUID/Inbox                  → ("Inbox")
    """
    if not url:
        return "", ""
    after_scheme = url.split("://", 1)
    body = after_scheme[1] if len(after_scheme) == 2 else after_scheme[0]
    parts = body.split("/", 1)
    if len(parts) >= 2:
        return parts[0], urllib.parse.unquote(parts[1])
    return parts[0], ""
