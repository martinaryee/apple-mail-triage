"""Tests for classify.py — covers live AFM calls, failure paths, and truncation."""

import pytest

import classify as classify_mod
from classify import classify, _truncate_content

RESULT_KEYS = (
    "actionable", "title", "reason", "urgency",
    "llm_ms", "prompt_tokens", "eval_tokens", "error", "permanent",
)


def _afm_available() -> bool:
    """Return True if the on-device Apple foundation model is usable."""
    try:
        import apple_fm_sdk as fm
        ok, _ = fm.SystemLanguageModel().is_available()
        return ok
    except Exception:
        return False


ACTIONABLE_MSG = {
    "subject": "Quick question about the draft",
    "sender": "Alice <alice@example.com>",
    "dateReceived": "2026-05-03T10:00:00Z",
    "content": (
        "Hi Martin, can you confirm by Friday whether you can review my draft? "
        "I need your feedback before I send it to the committee. Please let me know!"
    ),
}

NOT_ACTIONABLE_MSG = {
    "subject": "Your weekly newsletter is here",
    "sender": "newsletter@clouddigest.io",
    "dateReceived": "2026-05-03T08:00:00Z",
    "content": (
        "Your weekly newsletter is here. Read about 5 trends in cloud computing "
        "that are shaping the industry this quarter. Unsubscribe at any time."
    ),
}


@pytest.mark.skipif(not _afm_available(), reason="Apple foundation model not available")
def test_classify_live_actionable():
    result = classify(ACTIONABLE_MSG)
    assert result["actionable"] is True, f"Expected actionable=True, got: {result}"
    assert result["title"] != "", f"Expected non-empty title, got: {result}"
    assert result["error"] is None, f"Expected no error, got: {result['error']}"
    assert result["llm_ms"] > 0, f"Expected llm_ms > 0, got: {result['llm_ms']}"


@pytest.mark.skipif(not _afm_available(), reason="Apple foundation model not available")
def test_classify_live_not_actionable():
    result = classify(NOT_ACTIONABLE_MSG)
    assert result["actionable"] is False, f"Expected actionable=False, got: {result}"
    assert result["error"] is None, f"Expected no error, got: {result['error']}"


def test_classify_transient_failure_returns_safe_default(monkeypatch):
    """A transient failure (e.g. timeout) must return error with permanent=False."""

    async def boom(user_message, timeout_s):
        raise TimeoutError("model call timed out")

    monkeypatch.setattr(classify_mod, "_respond", boom)
    result = classify(ACTIONABLE_MSG)
    assert result["actionable"] is False
    assert result["error"] is not None
    assert result["permanent"] is False
    for key in RESULT_KEYS:
        assert key in result, f"Missing key '{key}' in result"


def test_classify_guardrail_failure_is_permanent(monkeypatch):
    """Guardrail refusals are deterministic; the result must say permanent=True."""
    import apple_fm_sdk as fm

    async def refuse(user_message, timeout_s):
        raise fm.GuardrailViolationError("content flagged")

    monkeypatch.setattr(classify_mod, "_respond", refuse)
    result = classify(ACTIONABLE_MSG)
    assert result["actionable"] is False
    assert result["error"] is not None
    assert result["permanent"] is True
    for key in RESULT_KEYS:
        assert key in result, f"Missing key '{key}' in result"


def test_truncate_bytes_limit():
    """Content of 5000 bytes truncated to 100 bytes must produce UTF-8 length <= 100."""
    long_content = "a" * 5000
    truncated = _truncate_content(long_content, 100)
    assert len(truncated.encode("utf-8")) <= 100


def test_truncate_multibyte_safety():
    """Truncation must not produce invalid UTF-8 by cutting mid-character."""
    # Each '€' is 3 bytes in UTF-8; slicing at 100 bytes might land mid-char
    content = "€" * 200  # 600 bytes total
    truncated = _truncate_content(content, 100)
    # Should be decodable (no exception) and within byte limit
    encoded = truncated.encode("utf-8")
    assert len(encoded) <= 100
    # Confirm it's valid UTF-8 (round-trips cleanly)
    assert encoded.decode("utf-8") == truncated


def test_truncate_short_content_unchanged():
    """Content shorter than the limit must pass through unchanged."""
    short = "Hello, world!"
    result = _truncate_content(short, 4096)
    assert result == short
