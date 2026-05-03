"""Tests for classify.py — covers live Ollama calls, failure paths, and truncation."""

import pytest
import requests

from classify import classify, _truncate_content


def _ollama_available() -> bool:
    """Return True if Ollama is reachable at localhost:11434."""
    try:
        requests.get("http://localhost:11434/api/tags", timeout=2)
        return True
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


@pytest.mark.skipif(not _ollama_available(), reason="Ollama not reachable at localhost:11434")
def test_classify_live_actionable():
    result = classify(ACTIONABLE_MSG)
    assert result["actionable"] is True, f"Expected actionable=True, got: {result}"
    assert result["title"] != "", f"Expected non-empty title, got: {result}"
    assert result["error"] is None, f"Expected no error, got: {result['error']}"
    assert result["llm_ms"] > 0, f"Expected llm_ms > 0, got: {result['llm_ms']}"
    assert result["prompt_tokens"] > 0, f"Expected prompt_tokens > 0, got: {result['prompt_tokens']}"


@pytest.mark.skipif(not _ollama_available(), reason="Ollama not reachable at localhost:11434")
def test_classify_live_not_actionable():
    result = classify(NOT_ACTIONABLE_MSG)
    assert result["actionable"] is False, f"Expected actionable=False, got: {result}"
    assert result["error"] is None, f"Expected no error, got: {result['error']}"


def test_classify_handles_bad_url():
    """Passing an unreachable URL must return a safe default dict, not raise."""
    result = classify(ACTIONABLE_MSG, ollama_url="http://localhost:1")
    assert result["actionable"] is False
    assert result["error"] is not None
    assert isinstance(result["error"], str)
    assert len(result["error"]) > 0
    # Verify all expected keys are present
    for key in ("actionable", "title", "reason", "urgency", "llm_ms", "prompt_tokens", "eval_tokens", "error"):
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
