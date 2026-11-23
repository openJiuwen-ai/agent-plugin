"""
hierarchical_json_handler.py

Standalone module containing ONLY the JSON-construction / JSON-handling
logic extracted from the MagenticOne literature-review pipeline
(scenario_1_magentic.py).

What's included:
   - InvocationStepCounter                    (invocation step-id allocator)
   - Per-agent JSON builders
      * create_agent_json_with_query
      * extract_query_from_otel_spans
      * extract_prompts_from_first_model_span
      * extract_model_requests_from_openai_chat_spans
      * extract_tool_calls_requested_by_model
      * extract_tool_activities_from_result_messages
      * find_tool_span_by_name
      * find_all_http_spans_inside_tool_window
      * build_otel_traces_block
      * summarize_tool_activity_status
      * get_final_agent_output
   - Initialization / skipped-agents JSON
      * create_initialization_json
      * create_agents_skipped_json
      * create_agents_skipped_json_separate_invocations
   - Save / load OrchestratorObj_*.json files
      * save_single_agent_json
      * save_OrchestratorObj_agent_json_files_separate_invocations
      * load_OrchestratorObj_json_files
   - Final merged-workflow JSON builders
      * create_final_merged_json
      * create_final_merged_json_separate_invocations
      * aggregate_agent_statistics
      * extract_unique_tools
      * determine_system_architecture
      * extract_run_metadata_from_path
   - Multi-invocation mapping helpers
      * get_all_invoke_agent_spans
      * filter_otel_text_for_agent_invocation
      * extract_messages_for_agent_invocation
      * map_messages_to_invocations
   - Generic OTel span parsing helpers needed by the above
      * parse_otel_console_spans
      * parse_otel_time
      * otel_duration_seconds
      * group_spans_by_name
      * pick_first_span / pick_last_span
      * extract_agent_name_from_span_name
      * span_identity
      * safe_text / safe_json_loads / safe_http_status_code
      * get_nested_attr_or_dict_value
      * make_tool_result / parse_structured_tool_output
      * extract_http_metadata
      * extract_invoked_agents_from_team_transcript
      * get_invoked_agent_names_from_otel
      * filter_otel_text_for_agent
      * span_windows_for_agent / span_overlaps_any_window
      * extract_messages_for_agent
      * AgentResultView
      * message_source / message_to_text

What's deliberately EXCLUDED (lives in your other modules, e.g.
scenario_1_orchestrator.py / observability setup / tool defs):
   - OpenTelemetry exporter/provider setup (init_observability, CONSOLE_EXPORTER)
   - CoreEventsJsonlHandler / setup_core_events_jsonl / format_core_events_jsonl_to_txt
   - AssistantAgent / MagenticOneGroupChat construction, AGENT_SYSTEM_PROMPTS
   - search_arxiv / search_google_scholar tool implementations
   - TerminalCapture / TeeStream / CaptureAndTeeStream / setup_python_logging
   - extract_final_text / save_transcript / extract_orchestrator_initial_plan
   - main() / __main__ CLI entrypoint

Usage:
   from json_handler import (
      InvocationStepCounter,
      create_agent_json_with_query,
      create_initialization_json,
      save_OrchestratorObj_agent_json_files_separate_invocations,
      create_final_merged_json_separate_invocations,
      create_agents_skipped_json_separate_invocations,
   )
"""

from __future__ import annotations
from ...utils import build_prompt_library
from ...model.version import SCHEMA_VERSION
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from typing import Any
from ..logger.terminal_helper import CaptureAndTeeStream

# Phase 1 step 4: the code-identical helpers now live in ojw_observability.emit -
# ONE copy each. Imported back here so both internal calls and external
# `from <this handler> import X` keep working unchanged.
from ...emit.common import safe_text, safe_json_loads, get_nested_attr_or_dict_value
from ...emit.spans import parse_otel_time, otel_duration_seconds, parse_otel_console_spans, group_spans_by_name, pick_first_span, pick_last_span, span_identity, extract_agent_name_from_span_name, extract_http_metadata, find_all_http_spans_inside_tool_window
from ...emit.extract import extract_prompts_from_first_model_span, get_final_agent_output, build_otel_traces_block
from ...emit.tools import make_tool_result, parse_structured_tool_output, summarize_tool_activity_status
from ...emit.save import save_single_agent_json
from ...emit.spans import find_tool_span_by_name
from ...emit.extract import extract_tool_calls_requested_by_model, extract_model_requests_from_openai_chat_spans, extract_tool_activities_from_result_messages
# Phase 1 step 6: the disk-reading loaders/aggregators/merge live in ingest/ (§3.4).
from ...ingest.discover import load_OrchestratorObj_json_files, extract_run_metadata_from_path
from ...ingest.aggregate import aggregate_agent_statistics, extract_unique_tools, determine_system_architecture
from ...ingest.merge import create_final_merged_json_separate_invocations
# ═════════════════════════════════════════════════════════════════════════════
# StepCounter
# ═════════════════════════════════════════════════════════════════════════════
class InvocationStepCounter:
    """
   Allocates step IDs sequentially as the agent JSON is assembled.

   Replaces the hardcoded WORKFLOW_STEP_MAPS. IDs are handed out in the exact
   order the JSON blocks are built (prompt -> llm/tool -> result -> traces), so
   they stay correct no matter which agent runs, in what order the orchestrator
   delegates, or how many times the same agent is re-invoked.

   Scope: one counter is shared across all agent JSON files in a run. IDs run
   continuously, so if one OrchestratorObj_*.json ends at step 5, the next one
   starts at 6. The counter advances in file-sequence order.
   """

    def __init__(self, start: int = 1) -> None:
        """Initialize the counter so the first call to next() returns the requested start value."""
        self._n = start - 1

    def next(self) -> str:
        """Return the next workflow step id as a string and advance the shared counter."""
        self._n += 1
        return str(self._n)






# Convert HTTP status values into safe integers for JSON output.
def safe_http_status_code(value) -> int:
    """
   Ensure HTTP status code is always integer.
   Handles: int, str, None, invalid values.
   """
    if isinstance(value, int):
        return value

    if isinstance(value, str):
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    if value is None:
        return 0

    try:
        return int(value)
    except (ValueError, TypeError):
        return 0










# ═════════════════════════════════════════════════════════════════════════════
# Message-source helpers (needed for invocation mapping)
# ═════════════════════════════════════════════════════════════════════════════
# Minimal wrapper that exposes a .messages attribute expected by extraction helpers.
class AgentResultView:
    """Small wrapper so existing extraction helpers can read per-agent messages."""
    def __init__(self, messages):
        """Store a filtered message list while exposing the same .messages shape as an AutoGen result."""
        self.messages = messages or []

# Return the source/sender field from an message.
def message_source(msg) -> str:
    """Return the source/sender name from an AutoGen message, or an empty string if absent."""
    return str(getattr(msg, "source", "") or "")

# Convert an message into readable text with a safe fallback.
def message_to_text(msg) -> str:
    """Convert an message/event to safe text for transcript JSON."""
    try:
        text = msg.to_text()
    except Exception:
        text = safe_text(getattr(msg, "content", ""))

    return text.strip() if isinstance(text, str) else safe_text(text).strip()






















# Read the delegation query that caused an agent to run from the child OpenAI span.
def extract_query_from_otel_spans(
    *,
    otel_spans: list[dict],
    agent_name: str,
) -> str:
    """
   Extract the orchestrator's query for this agent from OTel spans using parent-child relationships.

   Returns the SINGLE query that triggered the agent (from openai.chat span's gen_ai.prompt).
    """

    # Step 1: Find the "invoke_agent AgentName" span
    invoke_span = None
    for span in otel_spans:
        span_name = str(span.get("name", "")).strip()
        if span_name == f"invoke_agent {agent_name}":
            invoke_span = span
            break

    if not invoke_span:
        return ""

    invoke_span_id = invoke_span.get("context", {}).get("span_id")
    if not invoke_span_id:
        return ""

    # Step 2: Find the child "openai.chat" span (has parent_id == invoke_span_id)
    llm_span = None
    for span in otel_spans:
        if (span.get("parent_id") == invoke_span_id and
             span.get("name") == "openai.chat"):
            llm_span = span
            break

    if not llm_span:
        return ""

    # Step 3: Extract the LAST user prompt from gen_ai.prompt attributes
    attributes = llm_span.get("attributes", {})

    prompts = {}  # index → {'role': ..., 'content': ...}
    for key, value in attributes.items():
        if not key.startswith("gen_ai.prompt."):
            continue

        parts = key.split(".")
        if len(parts) < 4:
            continue

        try:
            idx = int(parts[2])
        except ValueError:
            continue

        field = ".".join(parts[3:])  # "role", "content"

        if idx not in prompts:
            prompts[idx] = {}
        prompts[idx][field] = value

    # Find the LAST user prompt (the orchestrator's delegation query)
    last_user_query = None
    for idx in sorted(prompts.keys()):
        prompt = prompts[idx]
        if prompt.get("role") == "user":
            last_user_query = prompt.get("content", "")

    return last_user_query or ""














# ═════════════════════════════════════════════════════════════════════════════
# HELPER 1: Get ALL invoke_agent spans for an agent
# ═════════════════════════════════════════════════════════════════════════════
# Return every OTel invoke_agent span for one agent, including repeated invocations.
def get_all_invoke_agent_spans(spans: list[dict], agent_name: str) -> list[dict]:
    """
   Return ALL invoke_agent spans for an agent in chronological order.

   Unlike get_invoked_agent_names_from_otel(), this returns **all instances**,
   not just unique agents.

   Returns:
      List of invoke_agent spans sorted by start_time
   """
    invoke_spans = []

    for span in spans:
        span_name = str(span.get("name", "")).strip()

        if span_name == f"invoke_agent {agent_name}":
            invoke_spans.append(span)

    # Sort by start_time to maintain chronological order
    return sorted(invoke_spans, key=lambda s: s.get("start_time", ""))


# ─────────────────────────────────────────────────────────────────────────────
# HELPER 2: Filter OTel to a specific invocation window
# ─────────────────────────────────────────────────────────────────────────────

# Keep only the OTel spans that belong to one specific agent invocation window.
def filter_otel_text_for_agent_invocation(
    full_otel_text: str,
    agent_name: str,
    invocation_start_time: str,
    invocation_end_time: str,
) -> str:
    """
   Filter OTel traces to a SPECIFIC invocation window of an agent.

   Instead of filtering to all time windows for an agent (multiple invocations),
   this filters to just ONE specific invocation window.

   Args:
      full_otel_text: Raw OTel ConsoleSpanExporter output
      agent_name: Agent name (e.g., "Arxiv_Researcher")
      invocation_start_time: ISO timestamp of this invocation's invoke_agent start
      invocation_end_time: ISO timestamp of this invocation's invoke_agent end

   Returns:
      Filtered OTel text (JSON lines) for just this invocation
   """
    spans = parse_otel_console_spans(full_otel_text)

    # Parse the specific invocation window
    window_start = parse_otel_time(invocation_start_time)
    window_end = parse_otel_time(invocation_end_time)

    if not window_start or not window_end:
        return ""

    filtered = []

    for span in spans:
        span_name = str(span.get("name", "")).strip()

        # Include this invocation's invoke_agent span
        if span_name == f"invoke_agent {agent_name}":
            span_start = parse_otel_time(span.get("start_time", ""))
            if span_start == window_start:  # Match to THIS invocation
                    filtered.append(span)
                    continue

        # Include create_agent (one-time, but include it)
        if span_name == f"create_agent {agent_name}":
            filtered.append(span)
            continue

        # Include any span that overlaps THIS invocation window
        span_start = parse_otel_time(span.get("start_time", ""))
        span_end = parse_otel_time(span.get("end_time", ""))

        if span_start and span_end:
            # Check time overlap: max(start) <= min(end)
            if max(span_start, window_start) <= min(span_end, window_end):
                    filtered.append(span)

    # Return as JSON lines (one span per line)
    filtered = sorted(filtered, key=lambda s: s.get("start_time", ""))
    return "\n".join(json.dumps(span, ensure_ascii=False, default=str) for span in filtered)


# ─────────────────────────────────────────────────────────────────────────────
# HELPER 3: Get agent messages for a specific invocation
# ─────────────────────────────────────────────────────────────────────────────

# Extract only the transcript messages that belong to one specific agent invocation.
def extract_messages_for_agent_invocation(
    result: Any,
    agent_name: str,
    invocation_start_time_str: str,
    invocation_end_time_str: str,
) -> "AgentResultView":
    """
   Extract messages from result that belong to a SPECIFIC agent invocation.

   Uses timestamps to determine which messages belong to this invocation.

   Args:
      result: TeamResult
      agent_name: Agent name
      invocation_start_time_str: ISO timestamp of invoke_agent span start
      invocation_end_time_str: ISO timestamp of invoke_agent span end

   Returns:
      AgentResultView with messages from this invocation only
   """
    messages = []
    invocation_start = parse_otel_time(invocation_start_time_str)
    invocation_end = parse_otel_time(invocation_end_time_str)

    for msg in getattr(result, "messages", []) or []:
        source = message_source(msg)

        # Message must be from this agent
        if source != agent_name:
            continue

        # Try to get message timestamp (if available)
        msg_timestamp = getattr(msg, "timestamp", None)

        if msg_timestamp:
            try:
                    msg_time = datetime.fromisoformat(msg_timestamp.replace("Z", "+00:00"))

                    # Check if message timestamp is within this invocation window
                    if invocation_start and invocation_end:
                        if invocation_start <= msg_time <= invocation_end:
                            messages.append(msg)
                    else:
                        # If no time filtering, include all messages from agent
                        messages.append(msg)
            except Exception:
                    # If timestamp parsing fails, include the message anyway
                    messages.append(msg)
        else:
            # If no timestamp, we can't filter by time; include it
            # This is a fallback when timestamps aren't available
            messages.append(msg)

    return AgentResultView(messages)



# ─────────────────────────────────────────────────────────────────────────────
# HELPER 4: Find which invocation an agent message belongs to
# ─────────────────────────────────────────────────────────────────────────────

# Assign an agent's messages to its repeated invocations using chronological order.
def map_messages_to_invocations(
     result: Any,
     agent_name: str,
     invoke_spans: list[dict],
) -> dict[int, list]:
    """
   Map agent messages to their corresponding invocations.

   Returns dict: { invocation_index → [messages] }

   Args:
      result: TeamResult
      agent_name: Agent name
      invoke_spans: All invoke_agent spans for this agent (in order)

   Returns:
      Dict mapping invocation index (0, 1, 2, ...) to messages
   """
    invocation_messages = {}

    # Get all messages from this agent
    agent_messages = [
        msg for msg in getattr(result, "messages", []) or []
        if message_source(msg) == agent_name
    ]

    if not agent_messages:
        return invocation_messages

    # If only one invocation, all messages belong to it
    if len(invoke_spans) == 1:
        return {0: agent_messages}

    # Multiple invocations: try to assign messages based on order
    # This is a heuristic: assumes messages are in chronological order in the transcript
    messages_per_invocation = len(agent_messages) // len(invoke_spans)

    for inv_idx, invoke_span in enumerate(invoke_spans):
        start_idx = inv_idx * messages_per_invocation

        if inv_idx == len(invoke_spans) - 1:
            # Last invocation gets remaining messages
            end_idx = len(agent_messages)
        else:
            end_idx = (inv_idx + 1) * messages_per_invocation

        invocation_messages[inv_idx] = agent_messages[start_idx:end_idx]

    return invocation_messages



# ═════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS FOR OrchestratorObj PIPELINE
# ═════════════════════════════════════════════════════════════════════════════
# Use the team transcript as the source of truth for which agents were actually invoked.
def extract_invoked_agents_from_team_transcript(
    *,
    result: Any,
    known_agent_names: list[str],
) -> list[str]:
    """
   Use result.messages / Team_Transcript as the authority.
   """
    known_agents = set(known_agent_names or [])
    invoked_agents = []

    for msg in getattr(result, "messages", []) or []:
        source = message_source(msg)

        if source in known_agents and source not in invoked_agents:
            invoked_agents.append(source)

    return invoked_agents

# Create a lightweight result view containing only messages from one agent.
def extract_messages_for_agent(result: Any, agent_name: str):
    """Return a result-like object containing only messages emitted by one agent."""
    messages = []

    for msg in getattr(result, "messages", []) or []:
        source = message_source(msg)
        if source == agent_name or agent_name in source:
            messages.append(msg)

    return AgentResultView(messages)





# Find all start/end time windows where one agent was invoked.
def span_windows_for_agent(spans: list[dict], agent_name: str) -> list[tuple[Any, Any]]:
    """Find invoke_agent windows for a specific participant."""
    windows = []
    expected_name = f"invoke_agent {agent_name}"

    for span in spans:
        if str(span.get("name", "")).strip() != expected_name:
            continue

        start = parse_otel_time(span.get("start_time", ""))
        end = parse_otel_time(span.get("end_time", ""))

        if start and end:
            windows.append((start, end))

    return windows


# Check whether a span overlaps any of the known invocation time windows.
def span_overlaps_any_window(span: dict, windows: list[tuple[Any, Any]]) -> bool:
    """Return True when a span overlaps at least one agent invocation time window."""
    span_start = parse_otel_time(span.get("start_time", ""))
    span_end = parse_otel_time(span.get("end_time", ""))

    if not span_start or not span_end:
        return False

    for window_start, window_end in windows:
        if max(span_start, window_start) <= min(span_end, window_end):
            return True

    return False



# Legacy per-agent OTel filter that groups all invocations of the same agent together.
def filter_otel_text_for_agent(full_otel_text: str, agent_name: str) -> str:
    """
   Split one Magentic-One OTEL stream into a per-agent OTEL stream.
   """
    spans = parse_otel_console_spans(full_otel_text)
    windows = span_windows_for_agent(spans, agent_name)

    if not windows:
        filtered = [
            span for span in spans
            if agent_name in str(span.get("name", ""))
            or agent_name in safe_text(span.get("attributes", {}))
        ]
    else:
        filtered = []
        for span in spans:
            span_name = str(span.get("name", "")).strip()

            if span_name in {f"create_agent {agent_name}", f"invoke_agent {agent_name}"}:
                    filtered.append(span)
                    continue

            if span_overlaps_any_window(span, windows):
                    filtered.append(span)

    filtered = sorted(filtered, key=lambda s: s.get("start_time", ""))
    return "\n".join(json.dumps(span, ensure_ascii=False, default=str) for span in filtered)


# Build the complete JSON object for one agent invocation from result messages and OTel spans.
def create_agent_json_with_query(
    *,
    otel_traces: str,
    result=None,
    include_otel_traces: bool = True,
    sequence_number: int,
    agent_name: str,
    fallback_system_prompt: str = "",
    fallback_query_prompt: str = "",
    recent_agent_query: str = "",
    counter: "InvocationStepCounter | None" = None,
) -> dict:
    """
   Extract and structure agent JSON with conditional tool blocks.

   Step IDs are allocated iteratively (see StepCounter) in JSON-assembly order:
   prompt -> per-model-span llm / tool-execution flow -> result -> OTel traces.
   Pass a shared `counter` to continue numbering across agent files; if none is
   given, a fresh counter starting at 1 is used for this invocation only.
   """
    # Parse raw ConsoleSpanExporter text once, then group spans by the role
    # they play in this agent invocation.
    spans = parse_otel_console_spans(otel_traces)
    grouped = group_spans_by_name(spans=spans, agent_name=agent_name)

    if counter is None:
        counter = InvocationStepCounter(start=1)
    prompt_step_id = counter.next()

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

    agent_id = f"agent_{sequence_number}"

    run_start = invoke_span.get("start_time", "")
    run_end = invoke_span.get("end_time", "")
    run_duration = otel_duration_seconds(run_start, run_end)

    # Model spans contain token counts, response IDs, finish reasons, and any
    # tool calls requested by the LLM.
    model_requests, token_usage, requested_tool_calls = (
        extract_model_requests_from_openai_chat_spans(
            model_spans=grouped["model"],
        )
    )

    system_prompt, user_prompt = extract_prompts_from_first_model_span(
        model_spans=grouped["model"],
        fallback_system_prompt=fallback_system_prompt,
        fallback_user_prompt=fallback_query_prompt,
    )

    # result messages contain the logical tool request/result pairs.
    # OTel spans contain timing and HTTP details. Later code joins both sources.
    raw_tool_activities = extract_tool_activities_from_result_messages(
        result,
    )

    has_tool_calls = len(raw_tool_activities) > 0 and any(
        activity.get("tool_calling", {}).get("tool_called", False)
        for activity in raw_tool_activities
    )

    activity_by_call_id: dict[str, dict] = {}

    for activity in raw_tool_activities:
        call_id = (
            activity.get("tool_calling", {}).get("call_id")
            or activity.get("tool_call_result", {}).get("call_id")
            or ""
        )

        if call_id:
            activity_by_call_id[call_id] = activity

    # Track matched OTel tool spans so repeated or parallel calls to the same
    # tool do not all receive the same HTTP timing information.
    used_tool_span_keys: set[tuple] = set()

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

        # used_tool_span_keys prevents two different tool_call activities
        # (e.g. parallel calls to the same tool with no usable call_id match)
        # from being matched to the SAME OTel tool span — which previously
        # caused the same http_attempts list to be duplicated onto every
        # tool_execution_activity in a batch instead of each activity getting
        # only the HTTP attempt(s) that belong to its own call.
        matching_tool_span = find_tool_span_by_name(
            tool_spans=grouped["tool"],
            tool_name=tool_name,
            call_id=call_id,
            used_span_keys=used_tool_span_keys,
        )

        if matching_tool_span:
            used_tool_span_keys.add(span_identity(matching_tool_span))

        all_http_spans = find_all_http_spans_inside_tool_window(
            tool_span=matching_tool_span,
            http_spans=grouped["http"],
        )

        http_attempts = []

        for attempt_num, http_span in enumerate(all_http_spans, start=1):
            attrs = http_span.get("attributes", {})
            status_code = safe_http_status_code(attrs.get("http.status_code"))

            http_attempts.append(
            {
                "attempt": attempt_num,
                "http_url": attrs.get("http.url", ""),
                "http_method": attrs.get("http.method", ""),
                "http_status_code": status_code,
                "status": "success" if status_code == 200 else "failed",
                "timestamp_start": http_span.get("start_time", ""),
                "timestamp_end": http_span.get("end_time", ""),
                "duration_seconds": otel_duration_seconds(
                    http_span.get("start_time", ""),
                    http_span.get("end_time", ""),
                ),
            }
            )

        tool_start = matching_tool_span.get("start_time", "")
        tool_end = matching_tool_span.get("end_time", "")

        tool_call_result["timestamp_start"] = tool_start
        tool_call_result["timestamp_end"] = tool_end
        tool_call_result["duration_seconds"] = otel_duration_seconds(tool_start, tool_end)


        tool_call_result["http_attempts"] = http_attempts
        tool_calling["http_attempt_count"] = len(http_attempts)

        if http_attempts:
            tool_calling["http_url"] = http_attempts[0]["http_url"]
            tool_calling["http_method"] = http_attempts[0]["http_method"]
            tool_calling["http_status_code"] = http_attempts[0]["http_status_code"]
        else:
            tool_calling["http_url"] = ""
            tool_calling["http_method"] = ""
            tool_calling["http_status_code"] = None

        activity["tool_calling"] = tool_calling
        activity["tool_call_result"] = tool_call_result

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

    initial_query = (
    recent_agent_query
    or fallback_query_prompt
    )

    final_output = get_final_agent_output(result)


    # Start the per-agent JSON with metadata and the query that triggered this
    # specific invocation. Additional LLM/tool/result blocks are added below.
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
        # CONVERGED (§0.6.3, decision 13, SCHEMA_VERSION 2.0.0): the hierarchical
        # per-invocation input block is `query`/`recent_query` - AG's concept,
        # lowercase casing. Was `task`/`user_prompt` (the sequential/agent-level
        # shape, which is NOT renamed - only this invocation-level block).
        "query": {
            "step_id": prompt_step_id,
            "recent_query": initial_query,
            "input_tokens": token_usage["input_tokens"],
        },
    }

    # Tool-using agents need a richer flow:
    # LLM request -> one or more tool execution activities -> optional reflection.
    if has_tool_calls:
        llm_counter = 0
        tool_counter = 0
        reflection_counter = 0
        # Accumulates all tool activities that have happened since the last
        # reflection/synthesis LLM call.
        pending_tool_activity_indices: list[int] = []
        model_request_by_index = {index: request for index, request in enumerate(model_requests)}
        consumed_as_tool_llm: set[int] = set()

        # Walk model calls in chronological order so generated step IDs match
        # the actual execution flow shown in OTel.
        for model_index, _model_span in enumerate(grouped["model"]):
            tool_calls_from_this_span = [
                tool_call
                for tool_call in requested_tool_calls
                if tool_call.get("model_span_index") == model_index
            ]

            if tool_calls_from_this_span:
                # 1) The LLM call that requested this batch of tools
                llm_counter += 1
                llm_request = model_request_by_index[model_index]
                llm_request["step_id"] = counter.next()
                agent_json[f"llm_request_{llm_counter}"] = llm_request

                is_parallel_batch = len(tool_calls_from_this_span) > 1
                # 2) One activity per tool in the batch (NO per-tool reflection)
                batch_activity_indices: list[int] = []
                emitted_activities: list[tuple[str, dict]] = []

                for tool_call in tool_calls_from_this_span:
                    call_id = tool_call.get("call_id", "")
                    activity = activity_by_call_id.get(call_id)
                    if activity is None:
                        continue

                    tool_counter += 1

                    # Current batch only
                    batch_activity_indices.append(tool_counter)

                    # All tools since the last reflection/synthesis
                    pending_tool_activity_indices.append(tool_counter)

                    flow_step_id = counter.next()

                    tool_calling = activity.get("tool_calling", {}) or {}
                    tool_call_result = activity.get("tool_call_result", {}) or {}
                    tool_calling["step_id"] = f"{flow_step_id}.1"
                    tool_call_result["step_id"] = f"{flow_step_id}.2"

                    activity_tool_name = (
                        tool_calling.get("tool_name")
                        or tool_call_result.get("tool_name")
                        or ""
                    )

                    http_attempts = tool_call_result.get("http_attempts", [])

                    tool_execution_block = {
                        "step_id": flow_step_id,
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
                    emitted_activities.append((flow_step_id, tool_execution_block))

                # 3) ONE reflection LLM call for the whole batch, placed AFTER the
                #    activities. The next model span is the reflection ONLY if it
                #    isn't itself requesting more tools.
                follow_index = model_index + 1
                if (follow_index < len(grouped["model"])
                        and follow_index not in consumed_as_tool_llm):
                    follow_tool_calls = [
                        item for item in requested_tool_calls
                        if item.get("model_span_index") == follow_index
                    ]

                    if not follow_tool_calls:
                        tool_llm_request = dict(model_request_by_index[follow_index])
                        consumed_as_tool_llm.add(follow_index)

                        # Case 1: one tool, immediately answered, nothing pending
                        # from an earlier batch -> nest as <activity>.3. The nested
                        # form draws no step id from the shared counter and carries
                        # no reflects_on_* fields: the nesting is the relationship.
                        is_single_immediate_reflection = (
                            not is_parallel_batch
                            and len(pending_tool_activity_indices) == 1
                            and bool(emitted_activities)
                        )

                        if is_single_immediate_reflection:
                            activity_step, block = emitted_activities[-1]
                            tool_llm_request["step_id"] = f"{activity_step}.3"
                            block["tool_llm_request"] = tool_llm_request

                        else:
                            # Case 2 (parallel batch) and Case 3 (chain): top level.
                            tool_llm_request["step_id"] = counter.next()
                            tool_llm_request["reflects_on_activities"] = list(
                                pending_tool_activity_indices
                            )

                            reflection_counter += 1
                            agent_json[f"tool_llm_request_{reflection_counter}"] = tool_llm_request

                        pending_tool_activity_indices.clear()

            else:
                if model_index not in consumed_as_tool_llm:
                    llm_counter += 1
                    llm_request = model_request_by_index[model_index]
                    llm_request["step_id"] = counter.next()
                    agent_json[f"llm_request_{llm_counter}"] = llm_request

    else:
        for llm_counter, model_request in enumerate(model_requests, start=1):
            model_request["step_id"] = counter.next()
            agent_json[f"llm_request_{llm_counter}"] = model_request


    result_step_id = counter.next()
    agent_json["result"] = {
        "step_id": result_step_id,
        "agent_output": final_output if final_output else "?",
        "output_tokens": token_usage["output_tokens"],
    }

    if include_otel_traces:
        traces_step_id = counter.next()
        agent_json["OTel_traces"] = build_otel_traces_block(
            agent_name=detected_agent_name,
            spans=spans,
            step_id=traces_step_id,
        )
    return agent_json


# ═════════════════════════════════════════════════════════════════════════════
# Save / load OrchestratorObj_.json files
# ═════════════════════════════════════════════════════════════════════════════
# Return the last non-empty text message from a team result.
def extract_final_text(result: Any) -> str:
    """Return the last useful text message from an team result."""
    messages = getattr(result, "messages", []) or []

    for msg in reversed(messages):
        try:
            text = msg.to_text()
        except Exception:
            text = safe_text(getattr(msg, "content", ""))

        if text and text.strip():
            return text.strip()

    return ""

# Write the full team conversation to a transcript file.
def save_transcript(result: Any, path: str) -> None:
    """Save the full team transcript so Magentic-One output is auditable."""
    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        for i, msg in enumerate(getattr(result, "messages", []) or [], start=1):
            source = message_source(msg) or "unknown"
            f.write(f"[{i}] source={source} type={type(msg).__name__}\n")
            try:
                    f.write(msg.to_text())
            except Exception:
                    f.write(safe_text(getattr(msg, "content", "")))
            f.write("\n" + "-" * 100 + "\n\n")



# ═════════════════════════════════════════════════════════════════════════════
# MAIN: Save separate JSON per agent invocation
# ═════════════════════════════════════════════════════════════════════════════
# Save one OrchestratorObj JSON file per real agent invocation, preserving global order.
def save_OrchestratorObj_agent_json_files_separate_invocations(
    *,
    result: Any,
    full_otel_text: str,
    session_dir: str,
    run_id: int | str,
    agent_names: list[str],
    fallback_query_prompt: str,
    prompt_library: dict,
) -> list[dict]:
    """
   Save separate JSON files for EACH agent invocation.

   Pattern: OrchestratorObj_{sequence_number}_{agent_name}_{run_id}.json

   Invocations are sequenced by true wall-clock order (OTel invoke_agent
   start_time) across ALL agents, so an agent re-invoked later keeps its
   real position (e.g. Arxiv → Google → Arxiv → seq 1, 2, 3).

   Returns:
      List of saved file records with sequence numbers
   """

    saved_records = []
    sequence_number = 1                  # increments for EACH invocation
    step_counter = InvocationStepCounter(start=1)  # shared across all files: IDs run continuously

    # Authority for which agents ran (transcript), in first-appearance order.
    transcript_invoked_agents = extract_invoked_agents_from_team_transcript(
        result=result,
        known_agent_names=agent_names,
    )

    all_otel_spans = parse_otel_console_spans(full_otel_text)

    unique_agents = []
    for agent_name in transcript_invoked_agents:
        if agent_name not in unique_agents:
            unique_agents.append(agent_name)

    # ─────────────────────────────────────────────────────────────────────────
    # Build ONE globally-chronological list of every invocation across every
    # agent. This is the fix: an agent re-invoked later (e.g. Arxiv after
    # Google) keeps its true position instead of being grouped with its
    # earlier call.
    # ─────────────────────────────────────────────────────────────────────────
    total_invocations_by_agent: dict[str, int] = {}
    flat_invocations: list[tuple[str, dict]] = []

    for agent_name in unique_agents:
        spans = get_all_invoke_agent_spans(all_otel_spans, agent_name)
        if not spans:
            print(f"Skipping {agent_name}: no invoke_agent spans in OTEL.")
            continue
        total_invocations_by_agent[agent_name] = len(spans)
        for span in spans:
            flat_invocations.append((agent_name, span))

    # Sort by parsed start_time so cross-agent ordering is correct even if
    # some timestamps use 'Z' and others '+00:00' (string sort would break that).
    flat_invocations.sort(
        key=lambda item: parse_otel_time(item[1].get("start_time", ""))
        or datetime.min.replace(tzinfo=timezone.utc)
    )

    per_agent_seen: dict[str, int] = {}

    for agent_name, invoke_span in flat_invocations:
        per_agent_seen[agent_name] = per_agent_seen.get(agent_name, 0) + 1
        invocation_num = per_agent_seen[agent_name]
        total_for_agent = total_invocations_by_agent.get(agent_name, 1)

        print(f"\n-> [seq {sequence_number}] {agent_name} "
                f"(invocation {invocation_num}/{total_for_agent})")

        invoke_start = invoke_span.get("start_time", "")
        invoke_end = invoke_span.get("end_time", "")

        # Filter OTel to THIS invocation window only
        agent_otel_text = filter_otel_text_for_agent_invocation(
            full_otel_text,
            agent_name,
            invoke_start,
            invoke_end,
        )

        if not agent_otel_text.strip():
            print("No filtered OTEL trace for this invocation")
            continue

        # Extract agent result for THIS invocation
        agent_result = extract_messages_for_agent_invocation(
            result,
            agent_name,
            invoke_start,
            invoke_end,
        )

        # Extract the query for THIS invocation
        agent_otel_spans = parse_otel_console_spans(agent_otel_text)
        recent_query = extract_query_from_otel_spans(
            otel_spans=agent_otel_spans,
            agent_name=agent_name,
        )

        if not recent_query:
            recent_query = fallback_query_prompt

        print(f" Query: {recent_query[:60]}...")

        prompts = prompt_library[agent_name.lower()]
        system_prompt = prompts.system

        # Create JSON for THIS invocation
        agent_json = create_agent_json_with_query(
            otel_traces=agent_otel_text,
            result=agent_result,
            sequence_number=sequence_number,
            agent_name=agent_name,
            fallback_system_prompt=system_prompt,
            fallback_query_prompt=fallback_query_prompt,
            recent_agent_query=recent_query,
            counter=step_counter,
        )

        # Save with sequence number (increments per invocation)
        output_path = os.path.join(
            session_dir,
            f"OrchestratorObj_{sequence_number}_{agent_name}_{run_id}.json",
        )

        saved_path = save_single_agent_json(agent_json, output_path)

        saved_records.append({
            "sequence_number": sequence_number,
            "agent_name": agent_name,
            "invocation_number": invocation_num,
            "total_invocations_of_agent": total_for_agent,
            "path": saved_path,
            "agent_json_file": os.path.basename(saved_path),
            "query": recent_query[:100],  # Preview
        })

        print(f"Saved: OrchestratorObj_{sequence_number}_{agent_name}_{run_id}.json")

        sequence_number += 1  # increment for EACH invocation

    # ═════════════════════════════════════════════════════════════════════════
    # Report unused agents
    # ═════════════════════════════════════════════════════════════════════════
    unused_agents = [
        agent_name
        for agent_name in agent_names
        if agent_name not in unique_agents
    ]

    if unused_agents:
        print("\n Unused agents (not invoked by orchestrator):")
        for agent_name in unused_agents:
            print(f"   - {agent_name}")

    return saved_records




# ═════════════════════════════════════════════════════════════════════════════
# Initialization / skipped-agents JSON
# ═════════════════════════════════════════════════════════════════════════════
# Extract the orchestrator's first planning message for the initialisation JSON.
def extract_orchestrator_initial_plan(result: Any) -> str:
    """
   Extract the very first planning message from the orchestrator.
   """
    messages = getattr(result, "messages", []) or []  # ← Get all messages

    for msg in messages:  # ← Loop through messages in order
        source = message_source(msg)  # ← Get who sent this message
        msg_type = type(msg).__name__  # ← Get message type

        # ← Look for orchestrator's TEXT messages (not tool calls)
        if "MagenticOneOrchestrator" in source and msg_type == "TextMessage":
            try:
                    text = msg.to_text()
            except Exception:
                    text = safe_text(getattr(msg, "content", ""))

            if text and text.strip():
                    # ← Return the FIRST one (the initial plan!)
                    return text.strip()

    return ""


# Save the run initialisation JSON that describes the task, model, and team setup.
def create_initialization_json(
    *,
    session_dir: str,
    run_id: int | str,
    agent_names: list[str],
    orchestrator_task: str,
    orchestrator_plan: str,
    selected_model: str,
) -> str:
    """
   Create OrchestratorObj_1_initialization.json with orchestrator setup and response.
   """
    initialization_json = {
        "schema_version": SCHEMA_VERSION,
        "initialization_metadata": {
            "run_id": str(run_id),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "team_type": "MagenticOneGroupChat",
            "model": selected_model,
        },
        "orchestrator_setup": {
            "task": orchestrator_task,
            "model_used": selected_model,
        },
        "team_composition": {
            "total_agents": len(agent_names),
            "agent_names": agent_names,
        },
        "orchestrator_response": orchestrator_plan,
    }

    output_path = Path(session_dir).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    init_file = output_path / f"OrchestratorObj_0_initialization_{run_id}.json"

    with init_file.open("w", encoding="utf-8") as f:
        json.dump(initialization_json, f, ensure_ascii=False, indent=2, default=str)

    print(f"Created initialization file: {init_file.name}")
    return str(init_file)


# ─────────────────────────────────────────────────────────────────────────────
# Create skipped agents JSON (now includes unused invocations)
# ─────────────────────────────────────────────────────────────────────────────
# Create a small JSON file documenting agents that Magentic-One never called.
def create_agents_skipped_json_separate_invocations(
    *,
    all_agent_names: list[str],
    invoked_agents_from_records: list[str],
    session_dir: str,
    run_id: int | str,
) -> str:
    """
   Document why each agent was NOT invoked.

   Works with the separate-invocations version.

   Args:
      all_agent_names: All available agents
      invoked_agents_from_records: Agents that were actually invoked (from saved records)
      session_dir: Output directory
      run_id: Run identifier

   Returns:
      Path to skipped agents JSON, or empty string if no agents skipped
   """
    skipped = []

    for agent_name in all_agent_names:
        if agent_name not in invoked_agents_from_records:
            skipped.append({
                    "agent_name": agent_name,
                    "reason": "orchestrator_decision",
                    "note": "Not invoked by Magentic-One orchestrator in this run",
            })

    if not skipped:
        return ""  # No agents skipped

    skipped_json = {
        "schema_version": SCHEMA_VERSION,
        "skipped_agents_metadata": {
            "total_agents_provided": len(all_agent_names),
            "total_agents_invoked": len(invoked_agents_from_records),
            "total_agents_skipped": len(skipped),
            "agents": skipped,
        }
    }

    output_path = Path(session_dir).resolve()
    skipped_file = output_path / f"OrchestratorObj_skipped_agents_{run_id}.json"

    with skipped_file.open("w", encoding="utf-8") as f:
        json.dump(skipped_json, f, ensure_ascii=False, indent=2, default=str)

    print(f"Created skipped agents documentation: {skipped_file.name}")
    return str(skipped_file)
