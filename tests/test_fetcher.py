"""Tests for the disk-based fetcher.

Most of the real work happens in apple-mail-mcp's parse_emlx; here we
exercise the thin pieces this project owns:
- _normalise_iso (Z vs +00:00 handling)
- _truncate_to_bytes (boundary behaviour)
- _emlx_extras (raw header block + junk-flag bit) on synthetic .emlx files
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from fetcher import _emlx_extras, _normalise_iso, _truncate_to_bytes


# ── _normalise_iso ───────────────────────────────────────────────────────────


def test_normalise_iso_z_to_offset():
    # JXA's toISOString() emits the Z form; imdinu's index uses +00:00.
    # Without normalisation, "2026-01-01T00:00:00Z" sorts BEFORE
    # "2026-01-01T00:00:00+00:00" lexically, dropping new mail.
    assert _normalise_iso("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00+00:00"


def test_normalise_iso_already_offset():
    assert _normalise_iso("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"


def test_normalise_iso_naive_treated_as_utc():
    assert _normalise_iso("2026-01-01T00:00:00") == "2026-01-01T00:00:00+00:00"


def test_normalise_iso_invalid_raises():
    with pytest.raises(ValueError):
        _normalise_iso("not a timestamp")


# ── _truncate_to_bytes ──────────────────────────────────────────────────────


def test_truncate_below_limit_unchanged():
    assert _truncate_to_bytes("hello", 100) == "hello"


def test_truncate_at_byte_boundary_appends_marker():
    out = _truncate_to_bytes("a" * 200, 50)
    # 50 bytes of 'a' + the truncation marker
    assert out == "a" * 50 + "…"


def test_truncate_drops_partial_multibyte():
    # A 3-byte UTF-8 character split mid-sequence must not appear in output.
    s = "a" * 49 + "€"  # '€' is 3 bytes; cut at 50 lands inside it
    out = _truncate_to_bytes(s, 50)
    assert "€" not in out
    assert out.startswith("a" * 49)


# ── _emlx_extras ─────────────────────────────────────────────────────────────


def _make_emlx(tmp_path: Path, headers: str, body: str, flags: int) -> Path:
    """Write a synthetic .emlx file in the format Apple Mail uses on disk.

    Format: <byte_count>\n<RFC 822 message><plist footer>
    """
    mime = (headers + "\r\n\r\n" + body).encode("utf-8")
    plist = plistlib.dumps({"flags": flags})
    path = tmp_path / "1234.emlx"
    path.write_bytes(f"{len(mime)}\n".encode() + mime + plist)
    return path


def test_emlx_extras_extracts_headers_block(tmp_path):
    path = _make_emlx(
        tmp_path,
        headers="From: a@b.com\r\nSubject: hi\r\nList-Unsubscribe: <mailto:x>",
        body="ignored",
        flags=0,
    )
    extras = _emlx_extras(path)
    assert "List-Unsubscribe" in extras["headers"]
    assert "ignored" not in extras["headers"]  # body not bled into headers
    assert extras["junk"] is False


def test_emlx_extras_detects_junk_flag(tmp_path):
    # Bit 11 = is junk
    path = _make_emlx(tmp_path, "From: a@b.com", "body", flags=1 << 11)
    assert _emlx_extras(path)["junk"] is True


def test_emlx_extras_missing_file_returns_defaults(tmp_path):
    extras = _emlx_extras(tmp_path / "does-not-exist.emlx")
    assert extras == {"headers": "", "junk": False}


def test_emlx_extras_malformed_byte_count(tmp_path):
    path = tmp_path / "bad.emlx"
    path.write_bytes(b"not-a-number\nFrom: a@b\r\n\r\nbody")
    assert _emlx_extras(path) == {"headers": "", "junk": False}
