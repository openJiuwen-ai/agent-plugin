"""

All functions responsible for turning agent runs + OpenTelemetry
(ConsoleSpanExporter) output into structured JSON.

AUTOGEN-ONLY: result access is `result.messages` with
ToolCallRequestEvent / ToolCallExecutionEvent. No LangChain/LangGraph
message handling in this file.

STEP IDS ARE ASSIGNED AT EMISSION TIME from the shared StepCounter, so
ids stay strictly sequential even when model calls, tool activities and
reflection calls interleave. This is what makes all three patterns below
number correctly:

Case 1 — SINGLE TOOL + IMMEDIATE REFLECTION (sequential, "Option 2"):

      LLM -> Tool -> LLM

      llm_request_1              step 2   (post_condition: tool_call)
      tool_execution_activity_1  step 3
         tool_calling           step 3.1
         tool_call_result       step 3.2
         tool_llm_request       step 3.3  <- reflection NESTED inside,
                                             FULL model-request JSON

Case 2 — PARALLEL BATCH + ONE SHARED REFLECTION ("Option 1"):

      LLM -> Tool, Tool, Tool -> LLM      (one span requested them all)

      llm_request_1              step 2
      tool_execution_activity_1  step 3   (3.1 / 3.2)
      tool_execution_activity_2  step 4   (4.1 / 4.2)
      tool_execution_activity_3  step 5   (5.1 / 5.2)
      tool_llm_request_1         step 6   <- reflection TOP-LEVEL,
                                             reflects_on_activities: [1,2,3]

Case 3 — TOOL CHAIN (the next LLM asks for ANOTHER tool):

      LLM -> Tool -> LLM -> Tool -> LLM -> Tool -> LLM

      llm_request_1              step 2
      tool_execution_activity_1  step 3   (3.1 / 3.2, NO reflection: the
                                          next model span requested a
                                          tool, so it is llm_request_2)
      llm_request_2              step 4
      tool_execution_activity_2  step 5   (5.1 / 5.2)
      llm_request_3              step 6
      tool_execution_activity_3  step 7   (7.1 / 7.2)
      tool_llm_request_1         step 8   <- reflection TOP-LEVEL,
                                             reflects_on_activities: [1,2,3]

      Case 3 reflects TOP-LEVEL, like Case 2 - NOT nested. An earlier
      revision of this header (and the Case 3 panel of
      data_model_files/llm&tool_execution_json_structure.png) showed it
      nesting as step 7.3; that is wrong, and the code has always emitted
      step 8. Only Case 1 nests.

      Case 2 and Case 3 stay distinguishable despite the identical
      reflection shape: a batch has ONE llm_request and N adjacent
      activities, a chain has N llm_requests interleaved with N
      activities. Count llm_request_* to tell them apart.

Detection uses the span's tool-call count (gen_ai.completion.0.tool_calls.*)
AND how many activities are still awaiting a reflection. Nesting requires
BOTH: batch size == 1 and exactly one pending activity -> Case 1 nests.
Anything else -> top level: batch size > 1 is Case 2, and more than one
pending activity is Case 3, because a chain leaves every earlier activity
pending until the terminal model span arrives. No placeholder reflection is
ever fabricated: if no reflection model span follows, nothing is emitted.

`tool_llm_request` (nested or top-level) now carries the FULL model
request JSON: step_id, model_name, model_provider, timestamps, duration,
response_id, tokens, llm_call_post_condition and http_request.

`tool_call_result` now includes the `http_attempts` list (every HTTP
attempt made during that tool execution, retries included).

The flow is:

   OTel console text  ->  parse_otel_console_spans()      -> list[span dict]
                     ->  group_spans_by_name()           -> spans bucketed by kind
                     ->  extract_*_from_*()               -> structured sub-blocks
                     ->  extract_agent_json_from_otel()   -> ONE agent's JSON
                     ->  save_single_agent_json()         -> agent_<n>.json on disk

   All agent_<n>.json -> merge_agent_json_files_with_trace_metadata()
                     -> merged_workflow_trace_<run>.json

Everything here is pure data-shaping. There are no agent calls and no
network calls, so these functions can be unit-tested in isolation by feeding
them captured OTel text and a fake `result` object exposing `.messages`.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from ...model.version import SCHEMA_VERSION
from dataclasses import dataclass

# Phase 1 step 4: the code-identical helpers now live in ojw_observability.emit -
# ONE copy each. Imported back here so both internal calls and external
# `from <this handler> import X` keep working unchanged.
from ...emit.steps import StepCounter
from ...emit.common import safe_text, safe_json_loads, get_nested_attr_or_dict_value
from ...emit.spans import parse_otel_time, otel_duration_seconds, parse_otel_console_spans, group_spans_by_name, pick_first_span, pick_last_span, span_identity, extract_agent_name_from_span_name, extract_http_metadata, find_all_http_spans_inside_tool_window
from ...emit.extract import extract_prompts_from_first_model_span, get_final_agent_output, build_otel_traces_block
from ...emit.tools import make_tool_result, parse_structured_tool_output, summarize_tool_activity_status
from ...emit.save import save_single_agent_json
from ...emit.spans import find_tool_span_by_name
from ...emit.extract import extract_tool_calls_requested_by_model, extract_model_requests_from_openai_chat_spans, extract_tool_activities_from_result_messages
# Phase 1 step 6: the disk-reading merge lives in ingest/ now (§3.4).
from ...ingest.merge import merge_agent_json_files_with_trace_metadata














# =============================================================================
# SPAN PARSING + GROUPING
# =============================================================================
# The ConsoleSpanExporter prints spans as bare JSON objects concatenated in the
# captured text. These functions turn that text into structured, bucketed spans.


















# Attribute HTTP spans to exactly ONE tool span — needed for parallel tool calls.
def find_all_http_spans_for_tool_span(
    *,
    tool_span: dict,
    http_spans: list[dict],
    used_http_span_keys: set[tuple] | None = None,
) -> list[dict]:
    """Attribute HTTP spans to exactly ONE tool span (one query).

   Preferred: parent-child matching — an HTTP span whose parent_id equals
   the tool span's span_id (same trace) belongs to that tool call. This is
   exact even when parallel tool calls have overlapping time windows
   (Case 2), where pure time-overlap matching would attribute the same
   HTTP attempt to several activities.

   Fallback: time-window overlap, but restricted to spans not already
   consumed by another activity, so attempts are never shared.

   Every returned span is marked in `used_http_span_keys` so subsequent
   activities can't claim it again.
   """
    if not tool_span:
        return []

    if used_http_span_keys is None:
        used_http_span_keys = set()

    tool_context = tool_span.get("context") or {}
    tool_span_id = tool_context.get("span_id", "")
    tool_trace_id = tool_context.get("trace_id", "")

    matches = []

    # ── 1) exact attribution via parent-child relationship ────────────────
    if tool_span_id:
        for http_span in http_spans:
            if span_identity(http_span) in used_http_span_keys:
                    continue

            http_context = http_span.get("context") or {}

            same_trace = (
                    not tool_trace_id
                    or http_context.get("trace_id", "") == tool_trace_id
            )

            if http_span.get("parent_id", "") == tool_span_id and same_trace:
                    matches.append(http_span)

    # ── 2) fallback: window overlap, unused spans only ─────────────────────
    if not matches:
        tool_start = parse_otel_time(tool_span.get("start_time", ""))
        tool_end = parse_otel_time(tool_span.get("end_time", ""))

        if tool_start and tool_end:
            for http_span in http_spans:
                    if span_identity(http_span) in used_http_span_keys:
                        continue

                    http_start = parse_otel_time(http_span.get("start_time", ""))
                    http_end = parse_otel_time(http_span.get("end_time", ""))

                    if not http_start or not http_end:
                        continue

                    if max(tool_start, http_start) <= min(tool_end, http_end):
                        matches.append(http_span)

    matches = sorted(matches, key=lambda s: s.get("start_time", ""))

    # consume — one HTTP span belongs to one activity, ever
    for http_span in matches:
        used_http_span_keys.add(span_identity(http_span))

    return matches




# =============================================================================
# MODEL (openai.chat) SPAN EXTRACTION
# =============================================================================



















# =============================================================================
# STEP MAP CONFIGURATION
# =============================================================================
# Create the step-number layout for one agent.
def build_step_map(
    *,
    counter: "StepCounter",
    model_span_count: int,
    has_tools: bool,
    result_label: str = "Agent Output",
) -> dict:
    """Reserve step ids differently for normal agents and tool-using agents.

   NON-TOOL agents: the sequence is fully known up front, so prompt, one id
   per model span, result and traces are all pre-allocated here.

   TOOL agents: model calls, tool activities and reflection calls interleave
   in an order only known while emitting the JSON (it depends on which model
   span requested tools and how many — Cases 1/2/3). Only agent-order +
   prompt are allocated here; every body/result/traces id is drawn from the
   SAME counter at emission time so ids stay strictly sequential.
   """
    counter.agent_ordinal += 1
    agent_order_step_id = counter.agent_ordinal

    prompt_step_id = counter.next_step()          # PROMPT

    if has_tools:
        return {
            "agent_order_step_id": str(agent_order_step_id),
            "prompt_step_id": str(prompt_step_id),
            "prompt_label": "PROMPT",
            "model_request_step_ids": [],
            "result_label": result_label,
            "traces_label": "TRACES",
        }

    model_request_step_ids = []
    for _ in range(max(model_span_count, 1)):
        model_request_step_ids.append(str(counter.next_step()))

    result_step_id = counter.next_step()
    traces_step_id = counter.next_step()

    return {
        "agent_order_step_id": str(agent_order_step_id),
        "prompt_step_id": str(prompt_step_id),
        "prompt_label": "PROMPT",
        "model_request_step_ids": model_request_step_ids,
        "result_step_id": str(result_step_id),
        "result_label": result_label,
        "traces_step_id": str(traces_step_id),
        "traces_label": "TRACES",
    }




# =============================================================================
#  THE MAIN PER-AGENT JSON BUILDER
# =============================================================================
# Main trace-normalization function: combines OTEL spans, AutoGen messages,
# tool activity, and final output into one agent JSON block.
def extract_agent_json_from_otel(
    *,
    otel_traces: str,
    counter: "StepCounter",
    result=None,
    include_otel_traces: bool = True,
    agent_name: str,
    fallback_system_prompt: str = "",
    fallback_user_prompt: str = "",
    result_label: str = "Agent Output",
) -> dict:
    """Extract and structure one agent's JSON with adaptive tool blocks.

   Reflection placement is decided PER MODEL SPAN by batch size:

   * batch size == 1  (Case 1 / Case 3 tail):
      the reflection LLM call is nested inside that activity block as
      `tool_llm_request` with step id `<activity>.3` — carrying the FULL
      model request JSON (model_name, tokens, http_request, ...).

   * batch size  > 1  (Case 2, parallel batch):
      ONE shared reflection is emitted as a TOP-LEVEL
      `tool_llm_request_{n}` with its own sequential step id and a
      `reflects_on_activities` list naming the activities it covers.

   * Case 3 chains: if the NEXT model span itself requested tools, it is
      NOT a reflection — it becomes the next `llm_request_{n}` and the
      current activity gets no `tool_llm_request` at all.

   In every case the reflection model span is consumed (never re-emitted as
   a plain llm_request). If no reflection span follows a tool batch, nothing
   is emitted — no empty placeholder.
   """
    spans = parse_otel_console_spans(otel_traces)
    grouped = group_spans_by_name(spans=spans, agent_name=agent_name)

    has_tools = bool(grouped["tool"])
    step_map = build_step_map(
        counter=counter,
        model_span_count=len(grouped["model"]),
        has_tools=has_tools,
        result_label=result_label,
    )

    create_span = pick_first_span(grouped["create_agent"])
    invoke_span = pick_last_span(grouped["invoke_agent"])

    create_attrs = create_span.get("attributes") or {}
    invoke_attrs = invoke_span.get("attributes") or {}

    detected_agent_name = (
        extract_agent_name_from_span_name(invoke_span, "invoke_agent ")
        or extract_agent_name_from_span_name(create_span, "create_agent ")
        or invoke_attrs.get("gen_ai.agent.name", "")
        or create_attrs.get("gen_ai.agent.name", "")
        or agent_name
    )

    otel_traces_block = (
        build_otel_traces_block(
            agent_name=detected_agent_name,
            spans=spans,
            step_id=step_map.get("traces_step_id", ""),
        )
        if include_otel_traces
        else None
    )

    agent_id = f"agent_{step_map['agent_order_step_id']}"

    run_start = invoke_span.get("start_time", "")
    run_end = invoke_span.get("end_time", "")
    run_duration = otel_duration_seconds(run_start, run_end)

    model_requests, token_usage, requested_tool_calls = (
        extract_model_requests_from_openai_chat_spans(
            model_spans=grouped["model"],
            model_request_step_ids=step_map.get("model_request_step_ids", []),
            fallback_step_id=step_map["prompt_step_id"],
        )
    )

    system_prompt, user_prompt = extract_prompts_from_first_model_span(
        model_spans=grouped["model"],
        fallback_system_prompt=fallback_system_prompt,
        fallback_user_prompt=fallback_user_prompt,
    )

    raw_tool_activities = extract_tool_activities_from_result_messages(
        result,
        step_map=step_map,
    )

    has_tool_calls = len(raw_tool_activities) > 0 and any(
        activity.get("tool_calling", {}).get("tool_called", False)
        for activity in raw_tool_activities
    )

    activity_by_call_id: dict[str, dict] = {}
    activity_by_sequence: list[dict] = []

    used_tool_span_keys: set[tuple] = set()
    used_http_span_keys: set[tuple] = set()

    # ── enrich every activity with its OTel tool span + HTTP attempts ──────
    for activity_index, activity in enumerate(raw_tool_activities, start=1):
        tool_calling = activity.get("tool_calling", {}) or {}
        tool_call_result = activity.get("tool_call_result", {}) or {}

        # independent tool call number, not retry number
        tool_calling["tool_invocation_index"] = activity_index

        tool_name = (
            tool_call_result.get("tool_name")
            or tool_calling.get("tool_name")
            or ""
        )

        call_id = (
            tool_call_result.get("call_id")
            or tool_calling.get("call_id")
            or ""
        )

        matching_tool_span = find_tool_span_by_name(
            tool_spans=grouped["tool"],
            tool_name=tool_name,
            call_id=call_id,
            used_span_keys=used_tool_span_keys,
        )

        if matching_tool_span:
            used_tool_span_keys.add(span_identity(matching_tool_span))

        all_http_spans = find_all_http_spans_for_tool_span(
            tool_span=matching_tool_span,
            http_spans=grouped["http"],
            used_http_span_keys=used_http_span_keys,
        )

        http_attempts = []

        for attempt_num, http_span in enumerate(all_http_spans, start=1):
            attrs = http_span.get("attributes", {})
            status_code = attrs.get("http.status_code")
            status_code_int = int(status_code) if status_code else None

            http_attempts.append({
                    "attempt": attempt_num,
                    "http_url": attrs.get("http.url", ""),
                    "http_method": attrs.get("http.method", ""),
                    "http_status_code": status_code_int,
                    "status": "success" if status_code_int == 200 else "failed",
                    "timestamp_start": http_span.get("start_time", ""),
                    "timestamp_end": http_span.get("end_time", ""),
                    "duration_seconds": otel_duration_seconds(
                        http_span.get("start_time", ""),
                        http_span.get("end_time", ""),
                    ),
            })

        tool_start = matching_tool_span.get("start_time", "") if matching_tool_span else ""
        tool_end = matching_tool_span.get("end_time", "") if matching_tool_span else ""

        tool_call_result["timestamp_start"] = tool_start
        tool_call_result["timestamp_end"] = tool_end
        tool_call_result["duration_seconds"] = otel_duration_seconds(tool_start, tool_end)

        # HTTP attempts are now part of the persisted JSON (was commented out).
        tool_call_result["http_attempts"] = http_attempts
        tool_calling["http_attempt_count"] = len(http_attempts)

        if http_attempts:
            tool_calling["http_url"] = http_attempts[0]["http_url"]
            tool_calling["http_method"] = http_attempts[0]["http_method"]
            tool_calling["http_status_code"] = http_attempts[0]["http_status_code"]  # guaranteed int
        else:
            tool_calling["http_url"] = ""
            tool_calling["http_method"] = ""
            tool_calling["http_status_code"] = None

        activity["tool_calling"] = tool_calling
        activity["tool_call_result"] = tool_call_result

        if call_id:
            activity_by_call_id[call_id] = activity

        activity_by_sequence.append(activity)

    # ── tool names ──────────────────────────────────────────────────────────
    tool_names = []

    for activity in raw_tool_activities:
        for block in (
            activity.get("tool_calling", {}),
            activity.get("tool_call_result", {}),
        ):
            name = (block or {}).get("tool_name", "")

            if name and name not in tool_names:
                    tool_names.append(name)

    if not tool_names:
        for tool_span in grouped["tool"]:
            span_name = str(tool_span.get("name", ""))

            if span_name.startswith("execute_tool "):
                    name = span_name.replace("execute_tool ", "", 1).strip()

                    if name and name not in tool_names:
                        tool_names.append(name)

    # ── agent status ────────────────────────────────────────────────────────
    tool_activity_status_blocks = [
        {
            "status": (activity.get("tool_calling") or {}).get("status", "unknown"),
            "tool_success": (activity.get("tool_calling") or {}).get("tool_success", False),
        }
        for activity in raw_tool_activities
        if activity.get("tool_call_result")
    ]

    tool_status = summarize_tool_activity_status(
        tool_call_request=tool_activity_status_blocks,
        tool_names=tool_names,
    )

    if not spans:
        agent_status = "no_otel_spans"
    elif not invoke_span:
        agent_status = "missing_invoke_span"
    elif token_usage["model_call_count"] == 0:
        agent_status = "missing_model_span"
    elif tool_status == "failed":
        agent_status = "failed"
    else:
        agent_status = "success"

    final_output = get_final_agent_output(result)

    agent_json = {
        "agent_metadata": {
            "agent_name": detected_agent_name,
            "agent_id": agent_id,
            "system_prompt": system_prompt,
            "agent_timestamp_start": run_start,
            "agent_timestamp_end": run_end,
            "duration_seconds": run_duration,
            "tool_names": tool_names,
            "tool_call_count": len(raw_tool_activities),
            "tool_called": has_tool_calls,
            "llm_call_count": token_usage["model_call_count"],
            "total_tokens": token_usage["total_tokens"],
            "input_tokens": token_usage["input_tokens"],
            "output_tokens": token_usage["output_tokens"],
            "status": agent_status,
        },
        "task": {
            "step_id": step_map["prompt_step_id"],
            "user_prompt": user_prompt,
            "input_tokens": token_usage["input_tokens"],
        },
    }

    # ========================================================================
    # BODY EMISSION — adaptive Case 1 / Case 2 / Case 3 handling
    # ========================================================================
    if has_tool_calls:
        tool_counter = 0
        llm_counter = 0
        tool_llm_counter = 0

        model_request_by_index = {
            index: request for index, request in enumerate(model_requests)
        }

        consumed_as_tool_llm: set[int] = set()
        pending_reflection_activity_indices: list[int] = []

        for model_index, model_span in enumerate(grouped["model"]):
            # This span was already emitted as a reflection (nested or
            # top-level) for a previous tool batch — skip it.
            if model_index in consumed_as_tool_llm:
                    continue

            tool_calls_from_this_span = [
                    tool_call
                    for tool_call in requested_tool_calls
                    if tool_call.get("model_span_index") == model_index
            ]

            if tool_calls_from_this_span:
                    # ── the model call that REQUESTED the tools ────────────────
                    llm_counter += 1
                    llm_request = model_request_by_index[model_index]
                    llm_request["step_id"] = str(counter.next_step())
                    agent_json[f"llm_request_{llm_counter}"] = llm_request

                    # batch size decides Case 2 vs Case 1/3 nesting
                    is_parallel_batch = len(tool_calls_from_this_span) > 1

                    batch_activity_indices: list[int] = []
                    emitted_activities: list[tuple[int, dict]] = []  # (step, block)

                    # ── one tool_execution_activity per call in the batch ──────
                    for tool_call in tool_calls_from_this_span:
                        call_id = tool_call.get("call_id", "")

                        activity = activity_by_call_id.get(call_id)

                        # Fallback for cases where call_id is missing or not
                        # matched, so independent tool calls aren't skipped.
                        if activity is None and tool_counter < len(activity_by_sequence):
                            activity = activity_by_sequence[tool_counter]

                        if activity is None:
                            continue

                        tool_counter += 1
                        batch_activity_indices.append(tool_counter)
                        pending_reflection_activity_indices.append(tool_counter)

                        tool_calling = activity.get("tool_calling", {}) or {}
                        tool_call_result = activity.get("tool_call_result", {}) or {}

                        activity_tool_name = (
                            tool_calling.get("tool_name")
                            or tool_call_result.get("tool_name")
                            or ""
                        )

                        # Each activity gets its own sequential step id, with
                        # call / result as sub-steps (e.g. 3, 3.1, 3.2).
                        activity_step = counter.next_step()
                        tool_calling["step_id"] = f"{activity_step}.1"
                        tool_call_result["step_id"] = f"{activity_step}.2"

                        http_attempts = tool_call_result.get("http_attempts", [])
                        tool_execution_block = {
                            "step_id": str(activity_step),
                            "tool_names": [activity_tool_name] if activity_tool_name else [],
                            "tool_call_count": len(http_attempts) if http_attempts else (1 if activity_tool_name else 0),
                            "tool_call_duration": {
                                    "timestamp_start": tool_call_result.get("timestamp_start", ""),
                                    "timestamp_end": tool_call_result.get("timestamp_end", ""),
                                    "duration_seconds": tool_call_result.get("duration_seconds", 0.0),
                            },
                            "tool_calling": tool_calling,
                            "tool_call_result": tool_call_result,
                        }

                        agent_json[f"tool_execution_activity_{tool_counter}"] = tool_execution_block
                        emitted_activities.append((activity_step, tool_execution_block))

                    # ── locate the reflection: the NEXT model span, but only if
                    #    that span itself requested no tools (Case 3: a span that
                    #    requested another tool is the next llm_request, NOT a
                    #    reflection) ──────────────────────────────────────────────
                    follow_index = model_index + 1
                    reflection_request = None

                    if (
                        emitted_activities
                        and follow_index < len(grouped["model"])
                        and follow_index not in consumed_as_tool_llm
                    ):
                        follow_tool_calls = [
                            item
                            for item in requested_tool_calls
                            if item.get("model_span_index") == follow_index
                        ]

                        if not follow_tool_calls:
                            # FULL model-request JSON (model_name, provider,
                            # timestamps, tokens, http_request, ...) — no
                            # abbreviated placeholder.
                            reflection_request = dict(model_request_by_index[follow_index])
                            consumed_as_tool_llm.add(follow_index)

                    # ── place the reflection according to the detected pattern ─────────────
                    if reflection_request is not None:
                        # Case 1:
                        # One tool followed immediately by a final LLM response.
                        # Keep this nested as N.3.
                        is_single_immediate_reflection = (
                            not is_parallel_batch
                            and len(pending_reflection_activity_indices) == 1
                        )

                        if is_single_immediate_reflection:
                            activity_step, block = emitted_activities[-1]
                            reflection_request["step_id"] = f"{activity_step}.3"
                            block["tool_llm_request"] = reflection_request

                        else:
                            # Case 2:
                            # One model span requested several parallel tools.
                            #
                            # Case 3:
                            # Several sequential LLM → tool iterations were followed by
                            # one final model response.
                            #
                            # In both cases, emit the final reflection at the top level.
                            reflection_request["step_id"] = str(counter.next_step())
                            reflection_request["reflects_on_activities"] = list(
                                    pending_reflection_activity_indices
                            )

                            tool_llm_counter += 1
                            agent_json[
                                    f"tool_llm_request_{tool_llm_counter}"
                            ] = reflection_request

                        # The pending chain has now been summarized by this reflection.
                        pending_reflection_activity_indices.clear()

            else:
                    # plain model call: no tools requested, not a reflection
                    llm_counter += 1
                    llm_request = model_request_by_index[model_index]
                    llm_request["step_id"] = str(counter.next_step())
                    agent_json[f"llm_request_{llm_counter}"] = llm_request

    else:
        for llm_counter, model_request in enumerate(model_requests, start=1):
            agent_json[f"llm_request_{llm_counter}"] = model_request

    # ── result + traces (ids pre-allocated for non-tool agents, drawn from
    #    the counter now for tool agents) ─────────────────────────────────────
    result_step_id = step_map.get("result_step_id") or str(counter.next_step())
    traces_step_id = step_map.get("traces_step_id") or str(counter.next_step())

    agent_json["result"] = {
        "step_id": result_step_id,
        "agent_output": final_output,
        "output_tokens": token_usage["output_tokens"],
    }

    if include_otel_traces:
        if otel_traces_block is not None:
            otel_traces_block["step_id"] = traces_step_id
        agent_json["OTel_traces"] = otel_traces_block

    return agent_json
