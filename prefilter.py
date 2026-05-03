"""
prefilter.py — heuristic prefilter for the mail-to-todo agent.

Pure function, no I/O, no external dependencies.
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Local-parts that indicate automated / no-reply senders.
# Order doesn't matter here — we build a set for fast membership tests.
_NOREPLY_EXACT: frozenset[str] = frozenset(
    {
        "noreply",
        "no-reply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "mailer-daemon",
        "notifications",
        "notification",
        "mail-noreply",
        "bounces",
    }
)

# A prefix match is valid when the character immediately after the prefix is
# a hyphen or an underscore (e.g. "noreply-svc" or "notifications_2024").
_NOREPLY_PREFIX_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(p) for p in sorted(_NOREPLY_EXACT, key=len, reverse=True))
    + r")[-_]",
    re.IGNORECASE,
)

# Extracts the email address from formats like:
#   "Alice <alice@x.com>"  ->  "alice@x.com"
#   "<alice@x.com>"        ->  "alice@x.com"
#   "alice@x.com"          ->  "alice@x.com"  (fallback)
_ANGLE_BRACKET_RE = re.compile(r"<([^>]+@[^>]+)>")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_headers(raw: str) -> dict[str, str]:
    """
    Parse a raw RFC-822 header block into a lowercase-keyed dict.

    Rules applied:
    - Folded headers (continuation lines starting with whitespace) are
      unfolded by joining them to the preceding header value with a single
      space.
    - Only the *first* occurrence of each header name is kept (sufficient
      for our filtering purposes).
    - Does NOT use the stdlib ``email`` module.
    """
    headers: dict[str, str] = {}
    current_name: Optional[str] = None
    current_value_parts: list[str] = []

    def _flush() -> None:
        if current_name and current_name not in headers:
            headers[current_name] = " ".join(current_value_parts).strip()

    for line in raw.splitlines():
        if not line:
            # Blank line signals end of headers (RFC 822 §3.1).
            break
        if line[0] in (" ", "\t"):
            # Continuation / folded line.
            if current_name is not None:
                current_value_parts.append(line.strip())
        else:
            # New header — flush the previous one first.
            _flush()
            if ":" in line:
                name, _, value = line.partition(":")
                current_name = name.strip().lower()
                current_value_parts = [value.strip()]
            else:
                # Malformed line; ignore.
                current_name = None
                current_value_parts = []

    _flush()
    return headers


def _extract_email(sender: str) -> str:
    """Return just the email address from a sender string."""
    m = _ANGLE_BRACKET_RE.search(sender)
    if m:
        return m.group(1).strip()
    return sender.strip()


def _is_noreply_local_part(local: str) -> bool:
    """Return True if *local* is an exact match or a bounded-prefix match."""
    if local.lower() in _NOREPLY_EXACT:
        return True
    return bool(_NOREPLY_PREFIX_RE.match(local))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def filter_message(msg: dict) -> tuple[bool, Optional[str]]:
    """
    Pure function. Decide whether to keep this message for LLM classification.

    Input *msg* must contain at least:
      - subject: str
      - sender:  str   (e.g. "Alice <alice@x.com>")
      - junk:    bool  (Apple Mail's spam flag)
      - headers: str   (full raw header block, multi-line)

    Returns ``(keep, drop_reason)``:
      keep == True  → proceed to LLM; drop_reason is None.
      keep == False → skip; drop_reason is one of:
        "junk_flag", "auto_submitted", "precedence_bulk",
        "list_unsubscribe", "no_reply_sender".
    """
    # --- Rule 1: junk flag --------------------------------------------------
    if msg.get("junk") is True:
        return False, "junk_flag"

    # Parse headers once; used by the remaining rules.
    parsed = _parse_headers(msg.get("headers", ""))

    # --- Rule 2: Auto-Submitted ---------------------------------------------
    auto_sub = parsed.get("auto-submitted", "")
    if auto_sub.lower().startswith("auto-"):
        return False, "auto_submitted"

    # --- Rule 3: Precedence: bulk | list | junk -----------------------------
    precedence = parsed.get("precedence", "")
    if re.match(r"^(bulk|list|junk)$", precedence.strip(), re.IGNORECASE):
        return False, "precedence_bulk"

    # --- Rule 4: List-Unsubscribe -------------------------------------------
    if "list-unsubscribe" in parsed:
        return False, "list_unsubscribe"

    # --- Rule 5: no-reply sender --------------------------------------------
    email_addr = _extract_email(msg.get("sender", ""))
    if "@" in email_addr:
        local_part = email_addr.split("@", 1)[0]
        if _is_noreply_local_part(local_part):
            return False, "no_reply_sender"

    return True, None
