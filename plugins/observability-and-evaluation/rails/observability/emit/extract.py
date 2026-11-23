"""Prompt, final-output and trace-block extraction shared by both handlers."""
import json

import re

from .common import safe_text, safe_json_loads, get_nested_attr_or_dict_value
from .spans import otel_duration_seconds
from .tools import parse_structured_tool_output


def extract_prompts_from_first_model_span(
    *,
    model_spans: list[dict],
    fallback_system_prompt: str = "",
    fallback_user_prompt: str = "",
) -> tuple[str, str]:
    """Recover the (system, user) prompt from the first openai.chat span.

    Reads gen_ai.prompt.0.content for the system prompt. Reconstructs message
    contents in numerical index order and JSON-encodes contents[1:] as the
    user prompt, preserving all roles' contents and message envelopes.
    Uses the system fallback when its content is absent, and the user fallback
    when fewer than two messages exist. With no model spans, uses both defaults.
    Returns a (system_prompt, user_prompt) tuple of strings.
    """
    if not model_spans:
        return fallback_system_prompt, fallback_user_prompt

    attrs = model_spans[0].get("attributes") or {}
    system_prompt = attrs.get("gen_ai.prompt.0.content", fallback_system_prompt)
    # Reconstruct model-input messages in numerical order.
    indexes = sorted({
        int(match.group(1))
        for key in attrs
        if (match := re.fullmatch(
            r"gen_ai\.prompt\.(\d+)\.(?:role|content)", key
        ))
    })

    contents = [
        attrs.get(f"gen_ai.prompt.{index}.content", "")
        for index in indexes
    ]

    # Keep every message after the first: input[1:].
    user_prompt = (
        json.dumps(contents[1:], ensure_ascii=False)
        if len(contents) > 1
        else fallback_user_prompt
    )

    return system_prompt, user_prompt

def get_final_agent_output(result) -> str:
    """Return the agent's last real text output, skipping tool plumbing messages.

   Iterates messages in reverse, ignoring ToolCallRequest/Execution/Summary
   events and any message whose JSON looks like a tool envelope
   (has "tool_called"/"tool_success"). Returns the first genuine assistant
   text it finds, or "" if none. This is the preferred way to read an agent's
   answer rather than blindly taking messages[-1].
   """
    if result is None:
        return ""

    messages = getattr(result, "messages", []) or []

    for msg in reversed(messages):
        msg_type = type(msg).__name__
        if msg_type in {
            "ToolCallRequestEvent",
            "ToolCallExecutionEvent",
            "ToolCallSummaryMessage",
        }:
            continue

        try:
            text = msg.to_text()
        except Exception:
            text = safe_text(getattr(msg, "content", ""))

        if not text or not text.strip():
            continue

        # Skip messages that are really serialized tool envelopes.
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and ("tool_called" in parsed or "tool_success" in parsed):
                    continue
        except Exception:
            pass

        return text.strip()

    return ""

def build_otel_traces_block(
    *,
    agent_name: str,
    spans: list[dict],
    step_id: str = "",
    step_label: str = "TRACES",
) -> dict:
    """Wrap an agent's raw spans into the OTel_traces block stored in the JSON."""
    return {
        "step_id": step_id,
        "Traces_of_agent": agent_name,
        "Traces": spans,
    }


# ---------------------------------------------------------------------------
# Phase 1 step 5: the four style-drifted extractor pairs, merged to ONE copy
# each - the sequential versions, whose only real difference from the
# hierarchical ones was provisional step-id seeding (the hierarchical copies
# always seeded ""). The sequential signatures are supersets: calling with
# the defaults reproduces the hierarchical behaviour EXACTLY -
# extract_model_requests_from_openai_chat_spans gained an explicit
# "no fallback -> empty step id" branch for that purpose. Provisional ids
# are overwritten at emission time either way.
# ---------------------------------------------------------------------------

def extract_tool_calls_requested_by_model(model_span: dict) -> list[dict]:
    """Extract the tool calls a model requested inside one openai.chat span.

   OTel stores them as indexed attributes:
      gen_ai.completion.0.tool_calls.<i>.id / .name / .arguments
   This discovers each index via regex and returns a list of
   {call_id, tool_name, arguments} dicts. Empty list if the model made no
   tool calls in this span.
   """
    attrs = model_span.get("attributes") or {}
    tool_call_indexes = set()

    for key in attrs.keys():
        match = re.match(r"gen_ai\.completion\.0\.tool_calls\.(\d+)\.", key)
        if match:
            tool_call_indexes.add(int(match.group(1)))

    tool_calls = []

    for index in sorted(tool_call_indexes):
        prefix = f"gen_ai.completion.0.tool_calls.{index}"
        tool_calls.append(
            {
                    "call_id": attrs.get(f"{prefix}.id", ""),
                    "tool_name": attrs.get(f"{prefix}.name", ""),
                    "arguments": safe_json_loads(attrs.get(f"{prefix}.arguments", "{}")),
            }
        )

    return tool_calls

def extract_model_requests_from_openai_chat_spans(
    *,
    model_spans: list[dict],
    model_request_step_ids: list[str] | None = None,
    fallback_step_id: str = "",
) -> tuple[list[dict], dict, list[dict]]:
    """Turn every openai.chat span into a structured model-request record.

   For each model span this computes:
   - model name/provider (trying several attribute keys),
   - per-call token counts (input/output/total) and running totals,
   - finish_reason -> llm_call_post_condition mapping
      (tool_calls -> "tool_call", stop -> "operation_completed", else raw),
   - a PROVISIONAL step_id (non-tool agents keep it; tool agents get a
      fresh sequential id at emission time in extract_agent_json_from_otel),
   - timing + a minimal http_request block,
   - any tool calls the model requested (tagged with model_span_index).

   Returns a 3-tuple:
   (model_requests, token_usage_summary, requested_tool_calls)

   NOTE: `messages` and `raw_tools` are computed below but not currently
   attached to the output dict — wire them in if you want prompts/tool schemas
   persisted per request.
   """
    model_requests = []
    requested_tool_calls = []

    total_tokens  = 0
    input_tokens  = 0
    output_tokens = 0

    for index, span in enumerate(model_spans):
        attrs = span.get("attributes") or {}

        model_name = (
            attrs.get("gen_ai.response.model")
            or attrs.get("gen_ai.request.model")
            or attrs.get("llm.request.model")
            or ""
        )

        model_provider = (
            attrs.get("gen_ai.system")
            or attrs.get("llm.system")
            or ""
        )

        # ── per-call token counts ──────────────────────────────────────────
        call_total  = int(attrs.get("llm.usage.total_tokens")
                            or attrs.get("gen_ai.usage.total_tokens") or 0)
        call_input  = int(attrs.get("gen_ai.usage.input_tokens")
                            or attrs.get("llm.usage.prompt_tokens")
                            or attrs.get("gen_ai.usage.prompt_tokens") or 0)
        call_output = int(attrs.get("gen_ai.usage.output_tokens")
                            or attrs.get("llm.usage.completion_tokens")
                            or attrs.get("gen_ai.usage.completion_tokens") or 0)

        total_tokens  += call_total
        input_tokens  += call_input
        output_tokens += call_output

        # ── finish_reason + llm_call_post_condition ────────────────────────
        finish_reason = attrs.get("gen_ai.completion.0.finish_reason", "")
        if finish_reason in ("tool_calls", "tool_use"):
            llm_call_post_condition = "tool_call"
        elif finish_reason == "stop":
            llm_call_post_condition = "operation_completed"
        else:
            llm_call_post_condition = finish_reason or "unknown"

        # ── step_id assignment (provisional for tool agents) ──────────────
        # No ids and no fallback (the hierarchical caller) -> "" exactly, as
        # its own pre-merge copy always seeded; real ids are assigned at
        # emission time either way.
        if model_request_step_ids and index < len(model_request_step_ids):
            current_step_id = model_request_step_ids[index]
        elif fallback_step_id:
            current_step_id = f"{fallback_step_id}.{index + 1}"
        else:
            current_step_id = ""

        # ── timing ────────────────────────────────────────────────────────
        span_start = span.get("start_time", "")
        span_end   = span.get("end_time", "")

        # ── http_request: pull messages from OTel prompt attributes ───────
        # OTel OpenAI instrumentation stores prompts as:
        # gen_ai.prompt.0.content = system message
        # gen_ai.prompt.1.content = user message
        # gen_ai.prompt.N.role    = role of message N
        messages = []
        prompt_index = 0
        while True:
            prefix = f"gen_ai.prompt.{prompt_index}"
            content = attrs.get(f"{prefix}.content")
            if content is None:
                    break
            role = attrs.get(f"{prefix}.role", "user" if prompt_index > 0 else "system")
            messages.append({
                    "role":    role,
                    "content": content,
            })
            prompt_index += 1

        # ── tools from OTel attributes ─────────────────────────────────────
        # stored as gen_ai.request.tools (JSON string) or llm.request.tools
        raw_tools = (
            attrs.get("gen_ai.request.tools")
            or attrs.get("llm.request.tools")
            or []
        )
        if isinstance(raw_tools, str):
            try:
                    raw_tools = json.loads(raw_tools)
            except Exception:
                    raw_tools = []

        http_request = {
            "http_url": (
                    attrs.get("server.address", "")
                    or attrs.get("http.url", "")
                    or attrs.get("gen_ai.openai.api_base", "")
            ),
            "timestamp_start":  span_start,
            "timestamp_end":    span_end,
            "duration_seconds": otel_duration_seconds(span_start, span_end),
        }

        model_requests.append({
            "step_id":                 current_step_id,
            "model_name":              model_name,
            "model_provider":          model_provider,
            "timestamp_start":         span_start,
            "timestamp_end":           span_end,
            "duration_seconds":        otel_duration_seconds(span_start, span_end),
            "response_id":             attrs.get("gen_ai.response.id", ""),
            "tokens": {
                    "input":  call_input,
                    "output": call_output,
                    "total":  call_total,
            },
            "llm_call_post_condition": llm_call_post_condition,
            "http_request":            http_request,
        })

        for tool_call in extract_tool_calls_requested_by_model(span):
            tool_call["model_span_index"] = index
            requested_tool_calls.append(tool_call)

    token_usage = {
        "total_tokens":     total_tokens,
        "input_tokens":     input_tokens,
        "output_tokens":    output_tokens,
        "model_call_count": len(model_spans),
    }

    return model_requests, token_usage, requested_tool_calls

def missing_tool_result(tool_calling):
    """Represent a request with no captured execution result, without inventing output."""
    return {
        "step_id": "",
        "tool_call_result_count": 0,
        "call_id": tool_calling["call_id"],
        "tool_name": tool_calling["tool_name"],
        "raw_output": "",
        "result_preview": "",
        "error": None,
        "parsed_output": {
            "status": "unknown", "data": None, "error": None,
            "metadata": {"result_captured": False},
        },
    }


def extract_tool_activities_from_result_messages(result, step_map: dict | None = None) -> list[dict]:
    """Pair tool-call requests with their execution results from result.messages.

   AUTOGEN: walks the messages, collecting ToolCallRequestEvent items (the
   intent: call_id, tool_name, arguments) and ToolCallExecutionEvent items
   (the outcome: parsed output, success, status). It keys both sides by
   call_id (falling back to positional keys) and zips them into a list of
   {"tool_calling": {...}, "tool_call_result": {...}} activities, one per call.

   Step IDs from `step_map` are PROVISIONAL and overwritten at emission time
   by extract_agent_json_from_otel (per-activity N.1 / N.2 numbering).
   """
    step_map = step_map or {}

    if result is None:
        return []

    messages = getattr(result, "messages", []) or []

    tool_calling_by_id = {}
    tool_result_by_id = {}
    tool_activity_status_by_id = {}

    request_count = 0
    result_count = 0

    for msg in messages:
        msg_type = type(msg).__name__
        content = getattr(msg, "content", None)

        if msg_type == "ToolCallRequestEvent" and isinstance(content, list):
            for call in content:
                    request_count += 1

                    call_id = get_nested_attr_or_dict_value(
                        call, "id", "call_id", default=""
                    )

                    tool_name = get_nested_attr_or_dict_value(
                        call, "name", default=""
                    )

                    arguments_raw = get_nested_attr_or_dict_value(
                        call, "arguments", default="{}"
                    )

                    call_key = call_id or f"tool_call_{request_count}"

                    tool_calling_by_id[call_key] = {
                        "step_id": step_map.get("tool_calling_step_id", ""),
                        "tool_call_request_count": request_count,
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "tool_arguments": safe_json_loads(arguments_raw),
                    }

        elif msg_type == "ToolCallExecutionEvent" and isinstance(content, list):
            for call in content:
                    result_count += 1

                    call_id = get_nested_attr_or_dict_value(
                        call, "call_id", "id", default=""
                    )

                    tool_name = get_nested_attr_or_dict_value(
                        call, "name", default=""
                    )

                    raw_output = get_nested_attr_or_dict_value(
                        call, "content", "result", "output", default=""
                    )

                    is_error = get_nested_attr_or_dict_value(
                        call, "is_error", default=False
                    )

                    parsed_tool_output = parse_structured_tool_output(raw_output)

                    call_key = call_id or f"tool_call_{result_count}"

                    tool_called = parsed_tool_output.get("tool_called", True)
                    tool_success = False if is_error else parsed_tool_output.get(
                        "tool_success", False
                    )
                    tool_status = parsed_tool_output.get(
                        "status",
                        "failed" if is_error else "unknown",
                    )

                    # remove these from the nested tool_call_result payload
                    clean_parsed_output = dict(parsed_tool_output)
                    clean_parsed_output.pop("tool_called", None)
                    clean_parsed_output.pop("tool_success", None)

                    tool_activity_status_by_id[call_key] = {
                        "tool_called": tool_called,
                        "tool_success": tool_success,
                        "status": tool_status,
                    }

                    tool_result_by_id[call_key] = {
                        "step_id": step_map.get("tool_result_step_id", ""),
                        "tool_call_result_count": result_count,
                        "call_id": call_id,
                        "tool_name": tool_name,

                        "raw_output": safe_text(clean_parsed_output, max_chars=5000),
                        "result_preview": safe_text(
                            clean_parsed_output.get("data"),
                            max_chars=500,
                        ),
                        "error": clean_parsed_output.get("error"),
                        "parsed_output": clean_parsed_output
                        or (safe_text(raw_output, max_chars=500) if is_error else None),
                    }

    activities = []

    all_call_keys = list(
        dict.fromkeys(
            list(tool_calling_by_id.keys()) + list(tool_result_by_id.keys())
        )
    )

    for call_key in all_call_keys:
        activity_status = tool_activity_status_by_id.get(call_key, {})

        tool_calling = dict(tool_calling_by_id.get(call_key, {}))

        tool_calling["status"] = activity_status.get("status", "unknown")
        tool_calling["tool_called"] = activity_status.get(
            "tool_called",
            bool(tool_calling_by_id.get(call_key)),
        )
        tool_calling["tool_success"] = activity_status.get("tool_success", False)

        activities.append(
            {
                    "tool_calling": tool_calling,
                    "tool_call_result": (tool_result_by_id[call_key] if call_key in tool_result_by_id
                                         else missing_tool_result(tool_calling)),
            }
        )

    return activities
