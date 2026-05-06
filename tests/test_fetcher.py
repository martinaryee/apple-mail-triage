"""Tests for the disk-based fetcher.

We unit-test the pieces that don't require a real Apple Mail directory:
- _iso_to_core_data / _core_data_to_iso (round-trip)
- _truncate_to_bytes (boundary behaviour)
- parse_emlx on synthetic .emlx files (the format Apple Mail writes on disk)
- _parse_mailbox_url
- _emlx_path globbing against a synthetic directory tree
"""

from __future__ import annotations

import plistlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fetcher import (
    _CORE_DATA_EPOCH,
    _core_data_to_iso,
    _emlx_path,
    _iso_to_core_data,
    _parse_mailbox_url,
    _truncate_to_bytes,
    parse_emlx,
)


# ── Time conversion ──────────────────────────────────────────────────────────


def test_iso_to_core_data_z_form():
    # 2001-01-01T00:00:00Z is exactly the Core Data epoch, so the result is 0.
    assert _iso_to_core_data("2001-01-01T00:00:00Z") == 0.0


def test_iso_to_core_data_offset_form():
    assert _iso_to_core_data("2001-01-01T00:00:00+00:00") == 0.0


def test_iso_to_core_data_naive_treated_as_utc():
    assert _iso_to_core_data("2001-01-01T00:00:00") == 0.0


def test_iso_to_core_data_advance_one_day():
    assert _iso_to_core_data("2001-01-02T00:00:00Z") == 86400.0


def test_iso_to_core_data_invalid_raises():
    with pytest.raises(ValueError):
        _iso_to_core_data("not a timestamp")


def test_core_data_to_iso_round_trip():
    iso = "2026-05-01T12:00:00+00:00"
    cd = _iso_to_core_data(iso)
    assert _core_data_to_iso(cd) == iso


def test_core_data_to_iso_none_safe():
    assert _core_data_to_iso(None) == ""


# ── _truncate_to_bytes ──────────────────────────────────────────────────────


def test_truncate_below_limit_unchanged():
    assert _truncate_to_bytes("hello", 100) == "hello"


def test_truncate_at_byte_boundary_appends_marker():
    assert _truncate_to_bytes("a" * 200, 50) == "a" * 50 + "…"


def test_truncate_drops_partial_multibyte():
    # '€' is 3 bytes — cutting at byte 50 lands inside it; it must be dropped.
    s = "a" * 49 + "€"
    out = _truncate_to_bytes(s, 50)
    assert "€" not in out
    assert out.startswith("a" * 49)


# ── parse_emlx ──────────────────────────────────────────────────────────────


def _make_emlx(tmp_path: Path, *, headers: str, body: str, flags: int) -> Path:
    """Write a synthetic .emlx file in the format Apple Mail uses on disk."""
    mime = (headers + "\r\n\r\n" + body).encode("utf-8")
    plist = plistlib.dumps({"flags": flags})
    path = tmp_path / "1234.emlx"
    path.write_bytes(f"{len(mime)}\n".encode() + mime + plist)
    return path


def test_parse_emlx_basic_headers_and_body(tmp_path):
    headers = (
        "From: Alice <alice@example.com>\r\n"
        "To: bob@example.com\r\n"
        "Subject: Hello there\r\n"
        "Message-Id: <abc@example.com>\r\n"
        "Date: Fri, 01 May 2026 12:00:00 +0000"
    )
    p = _make_emlx(tmp_path, headers=headers, body="hello world", flags=0)
    out = parse_emlx(p, truncate_bytes=4096)
    assert out is not None
    assert out["subject"] == "Hello there"
    assert out["sender"] == "Alice <alice@example.com>"
    assert out["messageId"] == "<abc@example.com>"
    assert out["content"] == "hello world"
    assert out["junk"] is False
    assert out["read"] is False
    assert "Subject: Hello there" in out["headers"]


def test_parse_emlx_extracts_junk_and_read_flags(tmp_path):
    p = _make_emlx(
        tmp_path,
        headers="From: a@b.com\r\nSubject: x",
        body="body",
        flags=(1 << 0) | (1 << 11),  # bit 0 = read, bit 11 = junk
    )
    out = parse_emlx(p, truncate_bytes=4096)
    assert out is not None
    assert out["read"] is True
    assert out["junk"] is True


def test_parse_emlx_keeps_list_unsubscribe_in_headers(tmp_path):
    """prefilter.py inspects the raw header block for List-Unsubscribe."""
    headers = (
        "From: noreply@bigco.com\r\n"
        "Subject: Newsletter\r\n"
        "List-Unsubscribe: <mailto:unsub@bigco.com>"
    )
    p = _make_emlx(tmp_path, headers=headers, body="ad copy", flags=0)
    out = parse_emlx(p, truncate_bytes=4096)
    assert out is not None
    assert "List-Unsubscribe" in out["headers"]
    # Body must NOT bleed into the header block.
    assert "ad copy" not in out["headers"]


def test_parse_emlx_decodes_rfc2047_subject(tmp_path):
    # RFC 2047 encoded-word: "=?UTF-8?B?<base64>?=" — Apple Mail commonly
    # stores subjects this way for non-ASCII characters.
    headers = "From: a@b.com\r\nSubject: =?UTF-8?B?SGVsbG8gd29ybGQ=?="
    p = _make_emlx(tmp_path, headers=headers, body="body", flags=0)
    out = parse_emlx(p, truncate_bytes=4096)
    assert out is not None
    assert out["subject"] == "Hello world"


def test_parse_emlx_missing_file_returns_none(tmp_path):
    assert parse_emlx(tmp_path / "missing.emlx", truncate_bytes=4096) is None


def test_parse_emlx_malformed_byte_count(tmp_path):
    p = tmp_path / "bad.emlx"
    p.write_bytes(b"not-a-number\nFrom: a@b\r\n\r\nbody")
    assert parse_emlx(p, truncate_bytes=4096) is None


def test_parse_emlx_truncates_long_body(tmp_path):
    long_body = "x" * 10000
    p = _make_emlx(tmp_path, headers="From: a@b\r\nSubject: x", body=long_body, flags=0)
    out = parse_emlx(p, truncate_bytes=100)
    assert out is not None
    assert len(out["content"].encode("utf-8")) <= 100 + len("…".encode("utf-8"))
    assert out["content"].endswith("…")


# ── _parse_mailbox_url ──────────────────────────────────────────────────────


def test_parse_mailbox_url_simple():
    assert _parse_mailbox_url("mailbox://ABC-123/INBOX") == ("ABC-123", "INBOX")


def test_parse_mailbox_url_nested():
    assert _parse_mailbox_url("mailbox://UUID/Work/Projects") == ("UUID", "Work/Projects")


def test_parse_mailbox_url_empty():
    assert _parse_mailbox_url("") == ("", "")
    assert _parse_mailbox_url(None) == ("", "")


# ── _emlx_path ──────────────────────────────────────────────────────────────


def test_emlx_path_locates_file_in_bucket(tmp_path):
    # Build the directory layout Apple Mail uses on disk.
    mbox = tmp_path / "UUID-1" / "INBOX.mbox" / "Data" / "9" / "4" / "Messages"
    mbox.mkdir(parents=True)
    target = mbox / "12345.emlx"
    target.write_bytes(b"placeholder")
    found = _emlx_path(tmp_path, "UUID-1", "INBOX", 12345)
    assert found == target


def test_emlx_path_handles_partial_emlx(tmp_path):
    mbox = tmp_path / "UUID-1" / "INBOX.mbox" / "Data" / "0" / "1" / "Messages"
    mbox.mkdir(parents=True)
    target = mbox / "777.partial.emlx"
    target.write_bytes(b"placeholder")
    assert _emlx_path(tmp_path, "UUID-1", "INBOX", 777) == target


def test_emlx_path_returns_none_when_missing(tmp_path):
    (tmp_path / "UUID-1" / "INBOX.mbox").mkdir(parents=True)
    assert _emlx_path(tmp_path, "UUID-1", "INBOX", 42) is None
