"""The single LLM call surface.

This replaces ~206 lines of AutoGen wrapper (`base_agent.py`). The old code built
an `AssistantAgent` with `tools=[]` and ran a single turn — measured at 7 calls
for 7 categories, no tool loop, no team. That is a plain chat completion.

Output validity is enforced at the API, not by post-hoc repair. Smaller models
asked for one large JSON object lose structural coherence partway through the
response — a measured qwen3-32b zero-shot run emitted well-formed objects for
the first ~10 steps and then degenerated into `"step_id": "4", { ... }`, which
is not valid JSON and not repairable without inventing keys. `response_format`
with a JSON schema removes that failure mode at the decoder instead.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai import AsyncOpenAI


logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


# Judge token usage for this process. The UI worker runs one evaluation per
# process, so these totals belong to exactly one evaluation there.
_USAGE = {"calls": 0, "calls_with_usage": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def reset_usage() -> None:
    for key in _USAGE:
        _USAGE[key] = 0


def usage_snapshot() -> dict:
    """Token totals of the model calls made so far (only calls whose provider reported usage)."""
    return dict(_USAGE)


def _record_usage(response) -> None:
    _USAGE["calls"] += 1
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt = getattr(usage, "prompt_tokens", None) or 0
    completion = getattr(usage, "completion_tokens", None) or 0
    _USAGE["calls_with_usage"] += 1
    _USAGE["input_tokens"] += prompt
    _USAGE["output_tokens"] += completion
    _USAGE["total_tokens"] += getattr(usage, "total_tokens", None) or (prompt + completion)


class ModelOutputError(ValueError):
    """The model returned something that is not the requested JSON object.

    Carries the raw response so the caller can name the artefact that holds it
    instead of re-printing a decoder traceback.
    """

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def json_schema_format(name: str, schema: dict) -> dict:
    """Build the `response_format` value for schema-constrained decoding."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "strict": True, "schema": schema},
    }


async def complete(
    client: AsyncOpenAI,
    model: str,
    system: str,
    user: str,
    *,
    response_format: dict | None = None,
    extra_body: dict | None = None,
    **params,
) -> str:
    """Run one chat completion and return the message text.

    `response_format` is a request, not a requirement: a provider that rejects it
    gets one retry without it, because a degraded call that might still parse
    beats a run that dies on a 400.
    """
    from openai import APIStatusError

    kwargs = dict(params)
    if response_format:
        kwargs["response_format"] = response_format
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
    except APIStatusError as exc:
        if not response_format or exc.status_code not in (400, 404, 422):
            raise
        logger.warning(
            "Model %s rejected schema-constrained output (%s); "
            "retrying unconstrained — malformed JSON becomes possible again",
            model,
            exc.status_code,
        )
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **params,
        )
    _record_usage(response)
    return response.choices[0].message.content or ""


def parse_model_json(text: str) -> dict:
    """Parse a model response that is supposed to be a bare JSON object.

    Tolerant of markdown fences and <think> blocks. Nothing more: a response
    whose structure has broken down cannot be repaired without guessing at the
    keys the model omitted, and a guessed key is a fabricated annotation.
    """
    if not text or not text.strip():
        raise ModelOutputError("empty model response", text or "")

    cleaned = _THINK.sub("", text).strip()
    cleaned = _FENCE.sub("", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: the outermost {...} span.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ModelOutputError(
                f"no JSON object found in model response (first 200 chars): "
                f"{cleaned[:200]!r}",
                text,
            ) from None
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelOutputError(
                f"model response is not valid JSON: {exc.msg} at line {exc.lineno} "
                f"column {exc.colno}. Context: {_around(cleaned[start:], exc.pos)!r}",
                text,
            ) from None


def _around(text: str, pos: int, width: int = 120) -> str:
    """The slice of `text` surrounding `pos`, for a one-line error message."""
    return text[max(0, pos - width) : pos + width]
