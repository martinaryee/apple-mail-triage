"""
Tests for stats.py — RunStats accumulator.
"""

import json
from pathlib import Path

import pytest

from stats import RunStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = {
    "schema_version", "run_id", "started_at", "ended_at", "duration_ms",
    "model", "dry_run", "cap_hit", "max_messages_per_run",
    "counts", "heuristic_dropouts", "llm_ms", "tokens",
    "classification", "per_account", "errors",
}

_COUNTS_KEYS = {
    "fetched_from_mail", "deduped_already_seen", "heuristic_dropped",
    "llm_classified", "actionable", "errors", "backlog_remaining",
}

_HEURISTIC_KEYS = {
    "list_unsubscribe", "no_reply_sender", "junk_flag",
    "auto_submitted", "precedence_bulk",
}


def _make_stats(**kwargs) -> RunStats:
    defaults = dict(model="gemma4:4b", dry_run=False, max_messages_per_run=50)
    defaults.update(kwargs)
    return RunStats(**defaults)


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 1. empty_run
# ---------------------------------------------------------------------------

def test_empty_run(tmp_path):
    log = tmp_path / "runs.ndjson"
    rs = _make_stats()
    rs.write(log)

    lines = _read_lines(log)
    assert len(lines) == 1

    rec = lines[0]

    # All required top-level keys present
    assert _REQUIRED_KEYS <= rec.keys(), f"Missing keys: {_REQUIRED_KEYS - rec.keys()}"

    # counts sub-keys
    assert _COUNTS_KEYS <= rec["counts"].keys()

    # All counts zero
    for k in _COUNTS_KEYS:
        assert rec["counts"][k] == 0, f"counts.{k} should be 0"

    # heuristic dropout keys all present and zero
    assert _HEURISTIC_KEYS == rec["heuristic_dropouts"].keys()
    for k, v in rec["heuristic_dropouts"].items():
        assert v == 0, f"heuristic_dropouts.{k} should be 0"

    # llm_ms all zero
    for k in ("mean", "p50", "p95", "max"):
        assert rec["llm_ms"][k] == 0, f"llm_ms.{k} should be 0"

    # tokens zero
    assert rec["tokens"]["prompt_total"] == 0
    assert rec["tokens"]["eval_total"] == 0

    # urgency zero
    for k in ("low", "medium", "high"):
        assert rec["classification"]["urgency"][k] == 0

    # per_account empty, errors empty
    assert rec["per_account"] == {}
    assert rec["errors"] == []

    # metadata
    assert rec["model"] == "gemma4:4b"
    assert rec["dry_run"] is False
    assert rec["cap_hit"] is False
    assert rec["max_messages_per_run"] == 50
    assert rec["schema_version"] == 1

    # timestamps and duration
    assert rec["started_at"].endswith("Z")
    assert rec["ended_at"].endswith("Z")
    assert rec["duration_ms"] >= 0

    # run_id has the right shape: <timestamp>-<4hexchars>
    parts = rec["run_id"].rsplit("-", 1)
    assert len(parts) == 2
    assert len(parts[1]) == 4
    assert all(c in "0123456789abcdef" for c in parts[1])


# ---------------------------------------------------------------------------
# 2. normal_run — two accounts, varied urgencies
# ---------------------------------------------------------------------------

def test_normal_run(tmp_path):
    log = tmp_path / "runs.ndjson"
    rs = _make_stats()

    # Account A: 3 messages fetched, 2 classified (1 actionable)
    for _ in range(3):
        rs.record_fetched("acct_a@example.com")
    rs.record_deduped_already_seen()
    rs.record_heuristic_dropped("list_unsubscribe")
    rs.record_heuristic_dropped("list_unsubscribe")
    rs.record_heuristic_dropped("junk_flag")
    rs.record_classified("acct_a@example.com", actionable=True,  urgency="high",   llm_ms=1000, prompt_tokens=200, eval_tokens=50)
    rs.record_classified("acct_a@example.com", actionable=False, urgency="low",    llm_ms=800,  prompt_tokens=180, eval_tokens=40)

    # Account B: 2 messages fetched, 1 classified (0 actionable)
    for _ in range(2):
        rs.record_fetched("acct_b@example.com")
    rs.record_classified("acct_b@example.com", actionable=False, urgency="medium", llm_ms=1200, prompt_tokens=220, eval_tokens=60)

    rs.write(log)
    rec = _read_lines(log)[0]

    # counts
    c = rec["counts"]
    assert c["fetched_from_mail"] == 5
    assert c["deduped_already_seen"] == 1
    assert c["heuristic_dropped"] == 3        # derived: sum(heuristic_dropouts)
    assert c["llm_classified"] == 3           # derived: sum(per_account.*.classified)
    assert c["actionable"] == 1              # derived: sum(per_account.*.actionable)
    assert c["errors"] == 0

    # heuristic_dropouts
    hd = rec["heuristic_dropouts"]
    assert hd["list_unsubscribe"] == 2
    assert hd["junk_flag"] == 1
    assert hd["no_reply_sender"] == 0
    assert hd["auto_submitted"] == 0
    assert hd["precedence_bulk"] == 0

    # urgency histogram
    urg = rec["classification"]["urgency"]
    assert urg["low"] == 1
    assert urg["medium"] == 1
    assert urg["high"] == 1

    # tokens
    assert rec["tokens"]["prompt_total"] == 600   # 200+180+220
    assert rec["tokens"]["eval_total"] == 150     # 50+40+60

    # per_account
    pa = rec["per_account"]
    assert pa["acct_a@example.com"] == {"fetched": 3, "classified": 2, "actionable": 1}
    assert pa["acct_b@example.com"] == {"fetched": 2, "classified": 1, "actionable": 0}

    # errors list empty
    assert rec["errors"] == []


# ---------------------------------------------------------------------------
# 3. percentiles — 10 known samples
# ---------------------------------------------------------------------------

def test_percentiles(tmp_path):
    log = tmp_path / "runs.ndjson"
    rs = _make_stats()

    samples = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    for ms in samples:
        rs.record_classified("x@x.com", actionable=False, urgency="low",
                             llm_ms=ms, prompt_tokens=10, eval_tokens=5)

    rs.write(log)
    rec = _read_lines(log)[0]
    lms = rec["llm_ms"]

    assert lms["max"] == 1000
    assert 400 <= lms["p50"] <= 600, f"p50={lms['p50']} not in [400,600]"
    assert lms["p95"] >= 900, f"p95={lms['p95']} < 900"
    assert lms["mean"] == round(sum(samples) / len(samples))  # 550


# ---------------------------------------------------------------------------
# 4. percentiles_few_samples — 2 samples
# ---------------------------------------------------------------------------

def test_percentiles_few_samples(tmp_path):
    log = tmp_path / "runs.ndjson"
    rs = _make_stats()

    rs.record_classified("x@x.com", actionable=False, urgency="low",
                         llm_ms=300, prompt_tokens=10, eval_tokens=5)
    rs.record_classified("x@x.com", actionable=False, urgency="low",
                         llm_ms=700, prompt_tokens=10, eval_tokens=5)

    rs.write(log)
    rec = _read_lines(log)[0]
    lms = rec["llm_ms"]

    assert lms["p50"] == 700
    assert lms["p95"] == 700
    assert lms["max"] == 700
    assert lms["mean"] == round((300 + 700) / 2)   # 500


# ---------------------------------------------------------------------------
# 5. append_appends — second write produces two lines
# ---------------------------------------------------------------------------

def test_append_appends(tmp_path):
    log = tmp_path / "subdir" / "runs.ndjson"

    rs1 = _make_stats(model="gemma4:4b")
    rs1.write(log)

    rs2 = _make_stats(model="gemma4:26b")
    rs2.write(log)

    lines = _read_lines(log)
    assert len(lines) == 2

    # Both parse as valid JSON with required keys
    for rec in lines:
        assert _REQUIRED_KEYS <= rec.keys()

    # Different run_ids
    assert lines[0]["run_id"] != lines[1]["run_id"]

    # Models preserved
    assert lines[0]["model"] == "gemma4:4b"
    assert lines[1]["model"] == "gemma4:26b"


# ---------------------------------------------------------------------------
# 6. error_recording
# ---------------------------------------------------------------------------

def test_error_recording(tmp_path):
    log = tmp_path / "runs.ndjson"
    rs = _make_stats()

    rs.record_error(stage="classify", message_id="msg-001", error="JSON parse failure")
    rs.record_error(stage="queue_write", message_id=None, error="File not found")

    rs.write(log)
    rec = _read_lines(log)[0]

    assert rec["counts"]["errors"] == 2
    errs = rec["errors"]
    assert len(errs) == 2

    assert errs[0] == {"stage": "classify", "message_id": "msg-001", "error": "JSON parse failure"}
    assert errs[1] == {"stage": "queue_write", "message_id": None, "error": "File not found"}


# ---------------------------------------------------------------------------
# 7. cap_hit
# ---------------------------------------------------------------------------

def test_cap_hit(tmp_path):
    log = tmp_path / "runs.ndjson"
    rs = _make_stats()

    rs.set_cap_hit(True, 12)
    rs.write(log)

    rec = _read_lines(log)[0]
    assert rec["cap_hit"] is True
    assert rec["counts"]["backlog_remaining"] == 12
