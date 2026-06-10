"""
Apple Foundation Models email classifier for the mail-to-todo agent.

Given a dict representing a single email message (with keys: subject, sender,
dateReceived, content, and optionally others), this module asks the on-device
Apple Intelligence foundation model (via the apple-fm-sdk Python bindings)
whether the email implies a personal action item. Guided generation guarantees
a structurally valid response, so there is no JSON extraction or repair here.
All errors are caught and returned in-band; this module never raises.

Error semantics: results with error != None also carry a `permanent` flag.
Permanent errors (guardrail refusals, unsupported language, oversized content)
are deterministic — the same message will fail the same way on every retry —
so the caller should record them and move on. Transient errors (model
unavailable, rate limiting, timeouts) should halt the run and be retried on
the next cycle, matching the agent's watermark-safety behavior.
"""

import asyncio
import time
from pathlib import Path
from typing import Optional

import apple_fm_sdk as fm

# Model identifier recorded in runs.ndjson telemetry.
MODEL_NAME = "apple-foundation-model"

# CONTENT_TAGGING outperforms the default GENERAL use case for this triage
# task (higher specificity at comparable sensitivity in benchmarking).
_MODEL = fm.SystemLanguageModel(use_case=fm.SystemLanguageModelUseCase.CONTENT_TAGGING)

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

# Deterministic failures: retrying the same message yields the same error.
_PERMANENT_ERRORS = (
    fm.GuardrailViolationError,
    fm.RefusalError,
    fm.UnsupportedLanguageOrLocaleError,
    fm.DecodingFailureError,
    fm.InvalidGenerationSchemaError,
    fm.UnsupportedGuideError,
)


@fm.generable
class _EmailTriage:
    actionable: bool = fm.guide(
        "Whether the email implies a personal action the recipient must take"
    )
    title: str = fm.guide(
        "Short imperative todo title under 80 chars; empty string if not actionable"
    )
    reason: str = fm.guide("Brief one-sentence justification")
    urgency: str = fm.guide(
        "Urgency of the action; low if not actionable",
        anyOf=["low", "medium", "high"],
    )


def _truncate_content(content: str, max_bytes: int) -> str:
    """Encode content to UTF-8, slice to max_bytes, decode with errors='ignore'."""
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _default_result(error: Optional[str] = None, permanent: bool = False) -> dict:
    return {
        "actionable": False,
        "title": "",
        "reason": "",
        "urgency": "low",
        "llm_ms": 0,
        "prompt_tokens": 0,
        "eval_tokens": 0,
        "error": error,
        "permanent": permanent,
    }


async def _respond(user_message: str, timeout_s: float) -> _EmailTriage:
    session = fm.LanguageModelSession(instructions=SYSTEM_PROMPT, model=_MODEL)
    return await asyncio.wait_for(
        session.respond(
            user_message,
            generating=_EmailTriage,
            # Greedy (deterministic) decoding: a classification verdict should
            # not change between runs on identical input. Random sampling
            # (temperature>0) was producing ±15-30pt swings in benchmark
            # sensitivity/specificity on the same messages.
            options=fm.GenerationOptions(
                sampling=fm.SamplingMode.greedy(), maximum_response_tokens=200
            ),
        ),
        timeout=timeout_s,
    )


def classify(
    msg: dict,
    timeout_s: float = 60.0,
    content_truncate_bytes: int = 4096,
) -> dict:
    """
    Classify a single email with the on-device Apple foundation model.

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
      - llm_ms: int         (wall-clock ms for the model call)
      - prompt_tokens: int  (always 0; apple-fm-sdk does not expose token counts)
      - eval_tokens: int    (always 0; apple-fm-sdk does not expose token counts)
      - error: str | None   (None on success; error message on failure)
      - permanent: bool     (True when retrying this message cannot succeed)

    On any failure (model unavailable, guardrail refusal, timeout), return a
    dict with actionable=False, error=<str>, and the rest as defaults; do
    NOT raise.
    """
    available, reason = _MODEL.is_available()
    if not available:
        # Unavailability (Apple Intelligence off, model assets still
        # downloading, battery saver) is transient from the agent's view.
        return _default_result(error=f"Foundation model unavailable: {reason}")

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

    start_ns = time.perf_counter_ns()
    try:
        try:
            parsed = asyncio.run(_respond(user_message, timeout_s))
        except fm.ExceededContextWindowSizeError:
            # The on-device model has a small (~4k token) context window.
            # Retry once with the body cut to 1024 bytes before giving up.
            user_message = (
                f"From: {sender}\n"
                f"Date: {date_received}\n"
                f"Subject: {subject}\n\n"
                f"{_truncate_content(content, 1024)}"
            )
            parsed = asyncio.run(_respond(user_message, timeout_s))
    except _PERMANENT_ERRORS as exc:
        elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000)
        result = _default_result(
            error=f"{type(exc).__name__}: {exc}", permanent=True
        )
        result["llm_ms"] = elapsed_ms
        return result
    except fm.ExceededContextWindowSizeError as exc:
        # Still too large at 1024 bytes — deterministic, do not retry.
        elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000)
        result = _default_result(
            error=f"{type(exc).__name__}: {exc}", permanent=True
        )
        result["llm_ms"] = elapsed_ms
        return result
    except Exception as exc:  # noqa: BLE001 — timeouts, rate limits, SDK bugs
        elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000)
        result = _default_result(error=f"{type(exc).__name__}: {exc}")
        result["llm_ms"] = elapsed_ms
        return result

    elapsed_ms = round((time.perf_counter_ns() - start_ns) / 1_000_000)

    # Guided generation enforces types and the urgency enum; validation below
    # is belt-and-suspenders against SDK decoding quirks.
    actionable = bool(getattr(parsed, "actionable", False))

    title = str(getattr(parsed, "title", "")).strip()
    title = title[:80]

    reason = str(getattr(parsed, "reason", "")).strip()

    urgency = str(getattr(parsed, "urgency", "low")).strip().lower()
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
        "prompt_tokens": 0,
        "eval_tokens": 0,
        "error": None,
        "permanent": False,
    }
