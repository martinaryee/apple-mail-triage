"""
Ollama-backed email classifier for the mail-to-todo agent.

Given a dict representing a single email message (with keys: subject, sender,
dateReceived, content, and optionally others), this module POSTs to a local
Ollama instance and asks a small language model to decide whether the email
implies a personal action item. The response is validated and returned as a
structured dict. All errors are caught and returned in-band; this module never
raises on HTTP or parse failures.
"""

import json
import time
from pathlib import Path
from typing import Optional

import requests

# System prompt is loaded from disk so it can be tuned without touching code.
# The file lives in `prompts/classify_system.md` next to this module.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "classify_system.md"


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt not found at {_PROMPT_PATH}. "
            f"Restore it from the repo or write a fresh one."
        )
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = _load_system_prompt()

_VALID_URGENCY = {"low", "medium", "high"}


def _extract_json_object(text: str) -> Optional[str]:
    """Pull the first balanced { ... } object from `text`, ignoring
    surrounding markdown fences, prose, or trailing explanation. Returns
    the JSON substring or None if nothing balanced was found.

    Brace-counting is naive about strings (counts `{`/`}` even inside string
    literals) but adequate for our small classifier outputs which never
    contain braces inside string values."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _truncate_content(content: str, max_bytes: int) -> str:
    """Encode content to UTF-8, slice to max_bytes, decode with errors='ignore'."""
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _default_result(error: Optional[str] = None) -> dict:
    return {
        "actionable": False,
        "title": "",
        "reason": "",
        "urgency": "low",
        "llm_ms": 0,
        "prompt_tokens": 0,
        "eval_tokens": 0,
        "error": error,
    }


def classify(
    msg: dict,
    model: str = "gemma4:e4b",
    ollama_url: str = "http://localhost:11434",
    timeout_s: float = 60.0,
    content_truncate_bytes: int = 4096,
) -> dict:
    """
    Classify a single email. Calls Ollama's /api/chat with format='json'.

    Input msg keys (more may be present):
      - subject: str
      - sender:  str
      - dateReceived: str (ISO8601)
      - content: str (already plaintext-ish from Mail)

    Returns a dict with these keys (always present):
      - actionable: bool
      - title: str          (short imperative todo title; '' if not actionable)
      - reason: str         (one-line justification)
      - urgency: str        ('low' | 'medium' | 'high'; 'low' if not actionable)
      - llm_ms: int         (wall-clock ms for the HTTP call)
      - prompt_tokens: int  (from Ollama 'prompt_eval_count'; 0 if absent)
      - eval_tokens: int    (from Ollama 'eval_count'; 0 if absent)
      - error: str | None   (None on success; error message on parse/HTTP failure)

    On any failure (HTTP non-2xx, JSON parse failure, timeout), return a
    dict with actionable=False, error=<str>, and the rest as defaults; do
    NOT raise.
    """
    subject = str(msg.get("subject", ""))
    sender = str(msg.get("sender", ""))
    date_received = str(msg.get("dateReceived", ""))
    content = str(msg.get("content", ""))

    truncated_content = _truncate_content(content, content_truncate_bytes)

    user_message = (
        f"From: {sender}\n"
        f"Date: {date_received}\n"
        f"Subject: {subject}\n\n"
        f"{truncated_content}"
    )

    payload = {
        "model": model,
        "stream": False,
        # think=false disables reasoning-mode token generation. Gemma 3n's
        # E4B advertises a "thinking" capability; with thinking on, the
        # model spends 100+ tokens on internal reasoning that never reaches
        # message.content, just message.thinking — adding 5-8s of latency
        # for no benefit on a simple classification task.
        "think": False,
        # NOT using format="json" or a JSON schema. Constrained sampling
        # adds ~1-2s of overhead in Ollama (grammar work outside the
        # eval_duration timer); we get equivalent reliability by telling
        # the model in-prompt to emit only a JSON object and parsing the
        # first balanced { ... } from the response ourselves.
        # keep_alive 30m comfortably spans the 5-min agent cadence so we
        # don't pay the ~4s reload penalty every batch and another loaded
        # model can't evict ours under memory pressure.
        "keep_alive": "30m",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        # num_ctx caps the KV-cache size. Gemma's default is 131072 tokens;
        # we never need more than a few thousand for an email + system prompt,
        # and the larger window slows attention math and bloats memory for no
        # benefit. num_predict bounds output so a chatty model can't burn time
        # on a long prose explanation after the JSON object.
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 200},
    }

    start_ns = time.perf_counter_ns()
    try:
        response = requests.post(
            f"{ollama_url}/api/chat",
            json=payload,
            timeout=timeout_s,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000)
        result = _default_result(error=str(exc))
        result["llm_ms"] = elapsed_ms
        return result

    elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000)

    try:
        response_json = response.json()
    except Exception as exc:
        result = _default_result(error=f"Failed to parse Ollama response as JSON: {exc}")
        result["llm_ms"] = elapsed_ms
        return result

    prompt_tokens = int(response_json.get("prompt_eval_count", 0))
    eval_tokens = int(response_json.get("eval_count", 0))

    raw_content = response_json.get("message", {}).get("content", "")
    json_str = _extract_json_object(raw_content)
    if json_str is None:
        result = _default_result(
            error=f"No JSON object found in LLM output. Raw: {raw_content!r}"
        )
        result["llm_ms"] = elapsed_ms
        result["prompt_tokens"] = prompt_tokens
        result["eval_tokens"] = eval_tokens
        return result
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as exc:
        result = _default_result(
            error=f"Failed to parse LLM content as JSON: {exc}. Raw: {raw_content!r}"
        )
        result["llm_ms"] = elapsed_ms
        result["prompt_tokens"] = prompt_tokens
        result["eval_tokens"] = eval_tokens
        return result

    # Validate and coerce fields
    actionable = parsed.get("actionable")
    if not isinstance(actionable, bool):
        # Try to coerce truthy values
        actionable = bool(actionable)

    title = str(parsed.get("title", "")).strip()
    title = title[:80]

    reason = str(parsed.get("reason", "")).strip()

    urgency = str(parsed.get("urgency", "low")).strip().lower()
    if urgency not in _VALID_URGENCY:
        urgency = "low"

    # If not actionable, reset title to empty and urgency to low
    if not actionable:
        title = ""
        urgency = "low"

    return {
        "actionable": actionable,
        "title": title,
        "reason": reason,
        "urgency": urgency,
        "llm_ms": elapsed_ms,
        "prompt_tokens": prompt_tokens,
        "eval_tokens": eval_tokens,
        "error": None,
    }
