"""Tests for state.py — SQLite-backed processed-message store."""

import sqlite3

import pytest

from state import State


# ---------------------------------------------------------------------------
# 1. Schema creation
# ---------------------------------------------------------------------------

def test_schema_creation(tmp_path):
    """Opening a fresh db creates the expected tables; reopening doesn't error."""
    db_path = tmp_path / "state.db"

    # First open — creates tables
    with State(db_path) as st:
        conn = st._conn
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "processed" in tables
        assert "schema_version" in tables

        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == 1

    # Second open — idempotent, no error
    with State(db_path) as st2:
        version2 = st2._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        assert version2 == 1


# ---------------------------------------------------------------------------
# 2. mark_and_check
# ---------------------------------------------------------------------------

def test_mark_and_check(tmp_path):
    """mark_processed(...) then is_processed returns True; unknown id False."""
    db_path = tmp_path / "state.db"
    with State(db_path) as st:
        assert st.is_processed("msg-001") is False

        st.mark_processed(
            message_id="msg-001",
            account="alice@example.com",
            date_received="2026-05-01T09:00:00+00:00",
            actionable=True,
        )

        assert st.is_processed("msg-001") is True
        assert st.is_processed("msg-999") is False


# ---------------------------------------------------------------------------
# 3. Idempotent insert
# ---------------------------------------------------------------------------

def test_idempotent_insert(tmp_path):
    """Calling mark_processed twice with the same message_id inserts only one row."""
    db_path = tmp_path / "state.db"
    with State(db_path) as st:
        for _ in range(2):
            st.mark_processed(
                message_id="msg-dup",
                account="alice@example.com",
                date_received="2026-05-01T09:00:00+00:00",
                actionable=False,
            )

        count = st._conn.execute(
            "SELECT COUNT(*) FROM processed WHERE message_id = 'msg-dup'"
        ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# 4. high_water per account
# ---------------------------------------------------------------------------

def test_high_water_per_account(tmp_path):
    """high_water returns the correct MAX date per account; None for unknown."""
    db_path = tmp_path / "state.db"
    with State(db_path) as st:
        # Account A: two messages
        st.mark_processed("a1", "alice@example.com", "2026-05-01T08:00:00+00:00", False)
        st.mark_processed("a2", "alice@example.com", "2026-05-03T12:00:00+00:00", True)

        # Account B: one message with an earlier date than A's max
        st.mark_processed("b1", "bob@example.com", "2026-05-02T10:00:00+00:00", False)

        assert st.high_water("alice@example.com") == "2026-05-03T12:00:00+00:00"
        assert st.high_water("bob@example.com") == "2026-05-02T10:00:00+00:00"
        assert st.high_water("nobody@example.com") is None


# ---------------------------------------------------------------------------
# 5. high_water with real ISO strings (including UTC-offset)
# ---------------------------------------------------------------------------

def test_high_water_with_real_iso_strings(tmp_path):
    """String comparison of ISO8601 dates with a *consistent* UTC offset works.

    NOTE: If messages for the same account arrive with *mixed* UTC offsets
    (e.g. -04:00 vs +00:00) the MAX string comparison will give wrong results
    because '2026-05-03T11:00:00+00:00' > '2026-05-03T11:00:00-04:00' even
    though the -04:00 timestamp is actually 4 hours later in absolute time.
    For v1 this is acceptable because Apple Mail returns consistent timezone
    offsets per account; a future fix would normalise to UTC before storing.
    """
    db_path = tmp_path / "state.db"
    with State(db_path) as st:
        earlier = "2026-05-03T10:58:58-04:00"
        later   = "2026-05-03T11:00:00-04:00"

        st.mark_processed("x1", "test@example.com", earlier, False)
        st.mark_processed("x2", "test@example.com", later, True)

        # String-ordered MAX should correctly pick the later timestamp
        # because both have the same offset (-04:00) and ISO8601 is
        # lexicographically monotone when timezone offset is constant.
        assert st.high_water("test@example.com") == later


# ---------------------------------------------------------------------------
# 6. Context manager
# ---------------------------------------------------------------------------

def test_context_manager(tmp_path):
    """`with State(path) as st:` works and closes cleanly; db is reusable."""
    db_path = tmp_path / "state.db"

    with State(db_path) as st:
        st.mark_processed("cm-1", "user@example.com", "2026-05-01T00:00:00+00:00", True)
        assert st.is_processed("cm-1") is True

    # Connection should be closed after __exit__; direct use should raise
    with pytest.raises(Exception):
        st._conn.execute("SELECT 1")

    # db file persists and is usable in a new State instance
    with State(db_path) as st2:
        assert st2.is_processed("cm-1") is True
        assert st2.is_processed("cm-never") is False
