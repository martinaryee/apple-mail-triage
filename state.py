"""SQLite-backed state store for the mail-to-todo agent.

Remembers every message that has been classified so the LLM doesn't
re-classify the same email on every poll.  The user can force a full
reprocess by deleting the database file (and the queue markdown file).
"""

import datetime
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
  message_id    TEXT PRIMARY KEY,
  account       TEXT NOT NULL,
  date_received TEXT NOT NULL,    -- ISO8601 from Mail
  processed_at  TEXT NOT NULL,    -- ISO8601 UTC, set by us
  actionable    INTEGER NOT NULL  -- 0 or 1
);
CREATE INDEX IF NOT EXISTS idx_account_date
  ON processed(account, date_received);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version(version) VALUES (1);
"""


class State:
    """Persistent record of every message the agent has classified."""

    def __init__(self, db_path: Path) -> None:
        """Open or create the SQLite db at *db_path*.

        Runs schema migrations idempotently.  Parent directories are
        created automatically if they do not exist.
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → autocommit; explicit transactions used below.
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._apply_schema()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        """Create tables and index if they don't exist yet (idempotent)."""
        self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_processed(
        self,
        message_id: str,
        account: str,
        date_received: str,
        actionable: bool,
    ) -> None:
        """Record that *message_id* has been classified.

        Uses INSERT OR IGNORE so calling this twice with the same
        *message_id* is a no-op (the first write wins).
        *processed_at* is set to the current UTC time in ISO8601 format.
        """
        processed_at = datetime.datetime.now(datetime.UTC).isoformat()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO processed
              (message_id, account, date_received, processed_at, actionable)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, account, date_received, processed_at, int(actionable)),
        )

    def is_processed(self, message_id: str) -> bool:
        """Return True iff *message_id* has a row in the processed table."""
        row = self._conn.execute(
            "SELECT 1 FROM processed WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None

    def high_water(self, account: str) -> str | None:
        """Return the latest *date_received* seen for *account* (ISO8601).

        Returns None if no messages have been processed for this account yet.
        Used as the lower bound for the next AppleScript fetch so we only
        pull genuinely new messages.

        The index on (account, date_received) makes this O(log n).
        """
        row = self._conn.execute(
            "SELECT MAX(date_received) FROM processed WHERE account = ?",
            (account,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return row[0]

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
