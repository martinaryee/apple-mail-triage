"""
stats.py — per-run telemetry accumulator for mail-agent.

Creates one NDJSON line per invocation; designed so the user can later load
runs.ndjson into pandas/duckdb to tune max_messages_per_run and compare
model performance.
"""

import datetime
import json
import secrets
import statistics
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with millisecond precision and Z suffix."""
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


_HEURISTIC_REASONS = (
    "list_unsubscribe",
    "no_reply_sender",
    "junk_flag",
    "auto_submitted",
    "precedence_bulk",
)

_URGENCIES = ("low", "medium", "high")


class RunStats:
    """Accumulates per-run telemetry and emits one NDJSON line on write()."""

    def __init__(
        self,
        model: str,
        dry_run: bool,
        max_messages_per_run: int,
        schema_version: int = 1,
    ) -> None:
        self.schema_version = schema_version
        self.model = model
        self.dry_run = dry_run
        self.max_messages_per_run = max_messages_per_run

        self.started_at: str = _now_iso()
        self.run_id: str = f"{self.started_at}-{secrets.token_hex(2)}"

        # Simple counters
        self._fetched_from_mail: int = 0
        self._deduped_already_seen: int = 0

        # Heuristic dropout counters (keyed by reason)
        self._heuristic_dropouts: dict[str, int] = {r: 0 for r in _HEURISTIC_REASONS}

        # LLM timing samples (ms)
        self._llm_ms_samples: list[int] = []

        # Token totals
        self._prompt_total: int = 0
        self._eval_total: int = 0

        self._urgency_counts: dict[str, int] = {u: 0 for u in _URGENCIES}

        # Per-account stats: {account: {fetched, classified, actionable}}
        self._per_account: dict[str, dict[str, int]] = {}

        # Errors list
        self._errors: list[dict] = []

        # Cap
        self._cap_hit: bool = False
        self._backlog_remaining: int = 0

        # Mail app status (schema version 2)
        self._skipped_mail_active: bool = False
        self._waited_seconds: int = 0
        self._forced_run: bool = False

    # ------------------------------------------------------------------
    # Counter methods
    # ------------------------------------------------------------------

    def record_fetched(self, account: str) -> None:
        """Increment fetched_from_mail and per-account fetched."""
        self._fetched_from_mail += 1
        acct = self._per_account.setdefault(account, {"fetched": 0, "classified": 0, "actionable": 0})
        acct["fetched"] += 1

    def record_deduped_already_seen(self) -> None:
        self._deduped_already_seen += 1

    def record_heuristic_dropped(self, reason: str) -> None:
        """reason must be one of the five known heuristic keys."""
        if reason not in self._heuristic_dropouts:
            raise ValueError(f"Unknown heuristic reason: {reason!r}")
        self._heuristic_dropouts[reason] += 1

    def record_classified(
        self,
        account: str,
        actionable: bool,
        urgency: str,
        llm_ms: int,
        prompt_tokens: int,
        eval_tokens: int,
    ) -> None:
        """Record one LLM classification event."""
        if urgency not in _URGENCIES:
            raise ValueError(f"Unknown urgency: {urgency!r}")

        acct = self._per_account.setdefault(account, {"fetched": 0, "classified": 0, "actionable": 0})
        acct["classified"] += 1
        if actionable:
            acct["actionable"] += 1

        self._urgency_counts[urgency] += 1
        self._llm_ms_samples.append(llm_ms)
        self._prompt_total += prompt_tokens
        self._eval_total += eval_tokens

    def record_error(
        self,
        stage: str,
        message_id: Optional[str],
        error: str,
    ) -> None:
        self._errors.append({"stage": stage, "message_id": message_id, "error": error})

    def set_cap_hit(self, hit: bool, backlog_remaining: int) -> None:
        self._cap_hit = hit
        self._backlog_remaining = backlog_remaining

    def set_mail_app_status(
        self, skipped: bool, waited_seconds: int, forced_run: bool
    ) -> None:
        """Record mail app status metrics (schema version 2)."""
        self._skipped_mail_active = skipped
        self._waited_seconds = waited_seconds
        self._forced_run = forced_run

    # ------------------------------------------------------------------
    # write()
    # ------------------------------------------------------------------

    def write(self, path: Path) -> None:
        """Append one NDJSON line to path (creates parent dirs if needed)."""
        ended_at = _now_iso()

        # Compute duration_ms
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        started_dt = datetime.datetime.strptime(self.started_at, fmt).replace(
            tzinfo=datetime.timezone.utc
        )
        ended_dt = datetime.datetime.strptime(ended_at, fmt).replace(
            tzinfo=datetime.timezone.utc
        )
        duration_ms = int((ended_dt - started_dt).total_seconds() * 1000)

        # Derived counts
        heuristic_dropped = sum(self._heuristic_dropouts.values())
        llm_classified = sum(a["classified"] for a in self._per_account.values())
        actionable = sum(a["actionable"] for a in self._per_account.values())

        # llm_ms percentiles
        samples = self._llm_ms_samples
        if len(samples) == 0:
            llm_stats = {"mean": 0, "p50": 0, "p95": 0, "max": 0}
        elif len(samples) < 4:
            m = max(samples)
            llm_stats = {
                "mean": round(sum(samples) / len(samples)),
                "p50": m,
                "p95": m,
                "max": m,
            }
        else:
            # statistics.quantiles(data, n=100) returns 99 cut points (indices 0..98)
            # index 49 ≈ p50, index 94 ≈ p95
            cuts = statistics.quantiles(samples, n=100)
            llm_stats = {
                "mean": round(sum(samples) / len(samples)),
                "p50": int(cuts[49]),
                "p95": int(cuts[94]),
                "max": max(samples),
            }

        # Build the record
        record = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "model": self.model,
            "dry_run": self.dry_run,
            "cap_hit": self._cap_hit,
            "max_messages_per_run": self.max_messages_per_run,
            "counts": {
                "fetched_from_mail": self._fetched_from_mail,
                "deduped_already_seen": self._deduped_already_seen,
                "heuristic_dropped": heuristic_dropped,
                "llm_classified": llm_classified,
                "actionable": actionable,
                "errors": len(self._errors),
                "backlog_remaining": self._backlog_remaining,
            },
            "heuristic_dropouts": dict(self._heuristic_dropouts),
            "llm_ms": llm_stats,
            "tokens": {
                "prompt_total": self._prompt_total,
                "eval_total": self._eval_total,
            },
            "classification": {
                "urgency": {
                    "low": self._urgency_counts["low"],
                    "medium": self._urgency_counts["medium"],
                    "high": self._urgency_counts["high"],
                }
            },
            "per_account": dict(self._per_account),
            "errors": list(self._errors),
            "mail_app_status": {
                "skipped_mail_active": self._skipped_mail_active,
                "waited_seconds": self._waited_seconds,
                "forced_run": self._forced_run,
            } if self.schema_version >= 2 else None,
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
            fh.write("\n")
