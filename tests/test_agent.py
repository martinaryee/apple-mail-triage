"""Tests for agent.py orchestration behavior."""

import sys

import agent
from state import State


def _message(message_id: str, subject: str) -> dict:
    return {
        "id": f"mail-{message_id}",
        "messageId": message_id,
        "account": "test@example.com",
        "mailbox": "INBOX",
        "subject": subject,
        "sender": "Sender <sender@example.com>",
        "dateReceived": "2026-05-03T10:00:00Z",
        "content": "Please review this.",
    }


def _result(**overrides) -> dict:
    base = {
        "actionable": True,
        "title": "Queued follow-up",
        "reason": "asks for review",
        "urgency": "medium",
        "llm_ms": 10,
        "prompt_tokens": 0,
        "eval_tokens": 0,
        "error": None,
        "permanent": False,
    }
    base.update(overrides)
    return base


def _patch_common(monkeypatch, tmp_path, messages, fake_classify, applied_flags):
    vault_path = tmp_path / "vault"
    monkeypatch.setattr(sys, "argv", ["agent.py"])
    monkeypatch.setattr(agent, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(agent, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(agent, "DEFAULT_LOCK_PATH", tmp_path / "agent.lock")
    monkeypatch.setattr(
        agent,
        "load_config",
        lambda path: {
            "enable_triage_queue": True,
            "vault_path": str(vault_path),
            "queue_file": "Inbox/Mail Triage.md",
            "max_messages_per_run": 10,
            "content_truncate_bytes": 4096,
            "start_date": "2026-05-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        agent, "stream_messages",
        lambda since, max_n, truncate: ((m, None) for m in messages),
    )
    monkeypatch.setattr(agent.classify_mod, "classify", fake_classify)
    monkeypatch.setattr(agent, "filter_message", lambda msg: (True, None))
    monkeypatch.setattr(
        agent, "_apply_flags",
        lambda assignments, log: applied_flags.extend(assignments),
    )
    return vault_path / "Inbox" / "Mail Triage.md"


def test_classification_errors_stop_run_without_marking_processed(tmp_path, monkeypatch):
    """Errored classifications should preserve the date watermark for retry."""
    applied_flags = []
    messages = [
        _message("err-msg", "Transient model outage"),
        _message("ok-msg", "Actionable follow-up"),
    ]

    def fake_classify(msg, *, content_truncate_bytes):
        if msg["messageId"] == "err-msg":
            return _result(
                title="Should not be queued",
                reason="classification failed after partial output",
                urgency="high",
                llm_ms=0,
                error="foundation model unavailable",
            )
        return _result()

    queue_path = _patch_common(
        monkeypatch, tmp_path, messages, fake_classify, applied_flags
    )

    assert agent.main() == 0

    with State(tmp_path / "state.db") as state:
        assert state.is_processed("err-msg") is False
        assert state.is_processed("ok-msg") is False

    assert not queue_path.exists()
    assert applied_flags == []


def test_permanent_classification_errors_skip_message_and_continue(tmp_path, monkeypatch):
    """Permanent errors (e.g. AFM guardrail refusal) must not wedge the run:
    the message is marked processed (not actionable) and later mail still
    gets classified and queued."""
    applied_flags = []
    messages = [
        _message("refused-msg", "Guardrail-refused content"),
        _message("ok-msg", "Actionable follow-up"),
    ]

    def fake_classify(msg, *, content_truncate_bytes):
        if msg["messageId"] == "refused-msg":
            return _result(
                actionable=False,
                title="",
                reason="",
                urgency="low",
                llm_ms=0,
                error="GuardrailViolationError: content flagged",
                permanent=True,
            )
        return _result()

    queue_path = _patch_common(
        monkeypatch, tmp_path, messages, fake_classify, applied_flags
    )

    assert agent.main() == 0

    with State(tmp_path / "state.db") as state:
        assert state.is_processed("refused-msg") is True
        assert state.is_processed("ok-msg") is True

    assert queue_path.exists()
    queue_text = queue_path.read_text(encoding="utf-8")
    assert "ok-msg" in queue_text
    assert "refused-msg" not in queue_text
