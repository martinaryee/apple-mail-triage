"""Tests for triage_queue.py — the append-and-deduplicate markdown queue writer."""

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_entry(
    title="Review PR",
    message_id="<abc123@mail.example.com>",
    urgency="medium",
    sender="Alice <alice@example.com>",
    date_received="2026-05-03 09:00",
    reason="asks for code review",
    account="martin.aryee@gmail.com",
) -> dict:
    return {
        "title": title,
        "messageId": message_id,
        "urgency": urgency,
        "sender": sender,
        "dateReceived": date_received,
        "reason": reason,
        "account": account,
    }


from triage_queue import append_block, existing_message_ids


# ---------------------------------------------------------------------------
# 1. create_from_empty
# ---------------------------------------------------------------------------

def test_create_from_empty(tmp_path: Path) -> None:
    """append_block to a non-existent path creates the file with header and block."""
    queue_file = tmp_path / "vault" / "Inbox" / "Mail Triage.md"
    entry = make_entry()

    append_block(queue_file, entry)

    assert queue_file.exists(), "queue file should be created"
    text = queue_file.read_text(encoding="utf-8")

    # Header present
    assert "# Mail Triage" in text
    assert "Mail-to-Todo agent" in text

    # Checkbox syntax
    assert "- [ ] Review PR" in text

    # Marker present
    assert "<!-- mid:<abc123@mail.example.com> urgency:medium -->" in text

    # Nested bullet lines
    assert "  - From: Alice <alice@example.com> | 2026-05-03 09:00" in text
    assert "  - Why: asks for code review" in text
    assert "  - Account: martin.aryee@gmail.com" in text


# ---------------------------------------------------------------------------
# 2. dedup — same messageId appended twice
# ---------------------------------------------------------------------------

def test_dedup(tmp_path: Path) -> None:
    """Calling append_block twice with the same messageId produces one block."""
    queue_file = tmp_path / "Mail Triage.md"
    entry = make_entry(message_id="<dedup@example.com>")

    append_block(queue_file, entry)
    append_block(queue_file, entry)  # second call should be a no-op

    text = queue_file.read_text(encoding="utf-8")

    # Exactly one marker for this id
    markers = re.findall(r'<!-- mid:<dedup@example\.com> urgency:[a-z]+ -->', text)
    assert len(markers) == 1, f"expected 1 marker, found {len(markers)}"

    # existing_message_ids returns the single id
    ids = existing_message_ids(queue_file)
    assert ids == {"<dedup@example.com>"}


# ---------------------------------------------------------------------------
# 3. multiple_distinct — three different entries all appear
# ---------------------------------------------------------------------------

def test_multiple_distinct(tmp_path: Path) -> None:
    """Three different entries all appear, each with its own marker."""
    queue_file = tmp_path / "Mail Triage.md"

    entries = [
        make_entry(title="Task A", message_id="<aaa@example.com>", urgency="low"),
        make_entry(title="Task B", message_id="<bbb@example.com>", urgency="medium"),
        make_entry(title="Task C", message_id="<ccc@example.com>", urgency="high"),
    ]

    for e in entries:
        append_block(queue_file, e)

    text = queue_file.read_text(encoding="utf-8")

    for e in entries:
        mid = e["messageId"]
        assert f"<!-- mid:{mid} urgency:{e['urgency']} -->" in text, (
            f"marker for {mid} not found"
        )
        assert f"- [ ] {e['title']}" in text

    ids = existing_message_ids(queue_file)
    assert ids == {"<aaa@example.com>", "<bbb@example.com>", "<ccc@example.com>"}


# ---------------------------------------------------------------------------
# 4. url_encoding — angle brackets and @ are percent-encoded in message:// link
# ---------------------------------------------------------------------------

def test_url_encoding(tmp_path: Path) -> None:
    """messageId '<abc@example.com>' is correctly percent-encoded in the link."""
    queue_file = tmp_path / "Mail Triage.md"
    entry = make_entry(message_id="<abc@example.com>")

    append_block(queue_file, entry)

    text = queue_file.read_text(encoding="utf-8")

    # The raw id should appear in the HTML-comment marker
    assert "<!-- mid:<abc@example.com> urgency:" in text

    # The message:// link should use percent-encoding
    assert "message://%3Cabc%40example.com%3E" in text


# ---------------------------------------------------------------------------
# 5. empty_message_id — synthetic id generated; second empty call appends again
# ---------------------------------------------------------------------------

def test_empty_message_id(tmp_path: Path) -> None:
    """Entry with messageId='' gets a synthetic id; two such calls both append.

    Two calls with messageId='' each generate a *different* synthetic id
    (timestamped to millisecond resolution).  This means both blocks are
    written — intentional fallback behaviour documented in append_block's
    docstring.  We verify:
      - A synthetic 'no-message-id-...' marker is present after the first call.
      - After the second call a second block with a distinct synthetic id exists.
    """
    import time

    queue_file = tmp_path / "Mail Triage.md"

    entry1 = make_entry(title="Empty ID email 1", message_id="")

    append_block(queue_file, entry1)

    text_after_first = queue_file.read_text(encoding="utf-8")
    # Count only marker lines (not the message:// link which also contains the id).
    markers_after_first = re.findall(
        r'<!-- mid:(no-message-id-\d+) urgency:[a-z]+ -->', text_after_first
    )
    assert len(markers_after_first) == 1, (
        "expected exactly one synthetic-id block after first call"
    )

    # Sleep a tiny bit so the millisecond timestamp differs for the second call.
    time.sleep(0.005)

    entry2 = make_entry(title="Empty ID email 2", message_id="")

    # Second call should append a NEW block (different synthetic id).
    append_block(queue_file, entry2)

    text_after_second = queue_file.read_text(encoding="utf-8")
    # Count only marker lines for the same reason as above.
    markers_after_second = re.findall(
        r'<!-- mid:(no-message-id-\d+) urgency:[a-z]+ -->', text_after_second
    )
    # Both blocks present, with different ids.
    assert len(markers_after_second) == 2, (
        "expected two synthetic-id blocks after second call"
    )
    assert markers_after_second[0] != markers_after_second[1], (
        "synthetic ids should differ between the two calls"
    )
