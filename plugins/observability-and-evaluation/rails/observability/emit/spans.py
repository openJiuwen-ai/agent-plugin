"""OTel console-span parsing, grouping, picking and timing.

Extracted verbatim from the two JSON handlers (Phase 1 step 4); these were
code-identical copies. Docstrings taken from the sequential copy.
"""
import json
from datetime import datetime

from .common import safe_json_loads


def parse_otel_time(value: str):
    """Convert an OTel ISO timestamp string into a datetime, or None.

   Handles the trailing 'Z' (UTC) that OTel emits by converting it to
   '+00:00' so `datetime.fromisoformat` accepts it. Returns None on anything
   unparseable so callers can branch instead of crashing.
   """
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def otel_duration_seconds(start_time: str, end_time: str) -> float:
    """Return the duration in seconds between two OTel timestamps.

   Returns 0.0 if either timestamp is missing or unparseable. Rounded to 4
   decimals. Used to stamp every span/tool/model block with a duration.
   """
    start = parse_otel_time(start_time)
    end = parse_otel_time(end_time)

    if not start or not end:
        return 0.0

    return round((end - start).total_seconds(), 4)

def parse_otel_console_spans(otel_text: str) -> list[dict]:
    """
   Parse the console exporter's output into a list of spans.

   The exporter prints many JSON objects one after another with no
   separator between them, so a single `json.loads` call would fail.
   Instead, this walks through the text and decodes one object at a
   time. Anything that isn't a real span (i.e. has no "name" field)
   is dropped, and the remaining spans are returned sorted by their
   start_time.
   """
    if not otel_text or not otel_text.strip():
        return []

    spans = []
    decoder = json.JSONDecoder()
    text = otel_text.strip()
    index = 0

    while index < len(text):
        start = text.find("{", index)

        if start == -1:
            break

        try:
            obj, end = decoder.raw_decode(text[start:])
            if isinstance(obj, dict) and "name" in obj:
                    spans.append(obj)

            index = start + end

        except json.JSONDecodeError:
            index = start + 1

    return sorted(spans, key=lambda s: s.get("start_time", ""))

def group_spans_by_name(spans: list[dict], agent_name: str = "") -> dict:
    """Bucket spans by kind using only the span 'name' field.

   Categories: create_agent, invoke_agent, model, tool
   (execute_tool ...), http (GET/POST/...), and other. When `agent_name` is
   given, create/invoke spans are matched exactly ("invoke_agent <name>") so
   one agent's spans aren't mixed with another's. Deliberately does NOT filter
   on trace_id/span_id — grouping is purely name-based. Each bucket is sorted
   by start_time.
   """
    create_agent_spans = []
    invoke_agent_spans = []
    model_spans = []
    tool_spans = []
    http_spans = []
    other_spans = []

    expected_create_name = f"create_agent {agent_name}" if agent_name else ""
    expected_invoke_name = f"invoke_agent {agent_name}" if agent_name else ""

    for span in spans:
        span_name = str(span.get("name", "")).strip()

        if agent_name and span_name == expected_create_name:
            create_agent_spans.append(span)
        elif not agent_name and span_name.startswith("create_agent "):
            create_agent_spans.append(span)
        elif agent_name and span_name == expected_invoke_name:
            invoke_agent_spans.append(span)
        elif not agent_name and span_name.startswith("invoke_agent "):
            invoke_agent_spans.append(span)
        elif span_name == "openai.chat":
            model_spans.append(span)
        elif span_name.startswith("execute_tool "):
            tool_spans.append(span)
        elif span_name in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            http_spans.append(span)
        else:
            other_spans.append(span)

    return {
        "create_agent": sorted(create_agent_spans, key=lambda s: s.get("start_time", "")),
        "invoke_agent": sorted(invoke_agent_spans, key=lambda s: s.get("start_time", "")),
        "model": sorted(model_spans, key=lambda s: s.get("start_time", "")),
        "tool": sorted(tool_spans, key=lambda s: s.get("start_time", "")),
        "http": sorted(http_spans, key=lambda s: s.get("start_time", "")),
        "other": sorted(other_spans, key=lambda s: s.get("start_time", "")),
    }

def pick_first_span(spans: list[dict]) -> dict:
    """Return the first span in a (pre-sorted) list, or {} if empty."""
    return spans[0] if spans else {}

def pick_last_span(spans: list[dict]) -> dict:
    """Return the last span in a (pre-sorted) list, or {} if empty.

   Used to grab the final invoke_agent span when an agent is invoked more than
   once, so the recorded run window covers the whole agent activity.
   """
    return spans[-1] if spans else {}

def span_identity(span: dict) -> tuple:
    """Build a hashable identity tuple for a span (trace_id, span_id, name, start).

   Used to track which tool/HTTP spans have already been consumed during
   matching, so the same span isn't attributed to two different tool calls.
   """
    context = span.get("context") or {}
    return (
        context.get("trace_id", ""),
        context.get("span_id", ""),
        span.get("name", ""),
        span.get("start_time", ""),
    )

def extract_agent_name_from_span_name(span: dict, prefix: str) -> str:
    """Strip a known prefix off a span name to recover the agent name.

   e.g. span name "invoke_agent Arxiv_Researcher" with prefix "invoke_agent "
   returns "Arxiv_Researcher". Returns "" if the prefix doesn't match.
   """
    name = str(span.get("name", ""))

    if name.startswith(prefix):
        return name.replace(prefix, "", 1).strip()

    return ""

def extract_http_metadata(http_span: dict) -> dict:
    """Pull url/method/status_code out of an HTTP span's attributes."""
    attrs = http_span.get("attributes") or {}
    return {
        "http_url": attrs.get("http.url", ""),
        "http_method": attrs.get("http.method", ""),
        "http_status_code": attrs.get("http.status_code", ""),
    }

def find_all_http_spans_inside_tool_window(
    *,
    tool_span: dict,
    http_spans: list[dict],
) -> list[dict]:
    """Return every HTTP span whose time window overlaps the tool span window.

   LEGACY helper (time overlap only). The main builder now uses
   find_all_http_spans_for_tool_span, which prefers exact parent-child
   attribution and consumes spans so parallel tool calls never share the
   same HTTP attempt. Kept for any external callers.
   """
    if not tool_span:
        return []

    tool_start = parse_otel_time(tool_span.get("start_time", ""))
    tool_end = parse_otel_time(tool_span.get("end_time", ""))

    if not tool_start or not tool_end:
        return []

    candidates = []

    for http_span in http_spans:
        http_start = parse_otel_time(http_span.get("start_time", ""))
        http_end = parse_otel_time(http_span.get("end_time", ""))

        if not http_start or not http_end:
            continue

        # Two intervals overlap iff max(starts) <= min(ends).
        if max(tool_start, http_start) <= min(tool_end, http_end):
            candidates.append(http_span)

    return sorted(candidates, key=lambda s: s.get("start_time", ""))

def find_tool_span_by_name(
    *,
    tool_spans: list[dict],
    tool_name: str,
    call_id: str = "",
    used_span_keys: set[tuple] | None = None,
) -> dict:
    """Find the execute_tool span matching a given tool name (and ideally call_id).

   Prefers an unused span whose attribute 'gen_ai.tool.call.id' matches
   `call_id`; otherwise returns the first unused name match; otherwise falls
   back to the last name match. `used_span_keys` lets the caller exclude spans
   already attributed to earlier tool calls.

   NOTE: the final fallback can return an already-used span when every match
   is consumed — for an agent calling the same tool twice this can mis-attribute
   the HTTP window. Returning {} there would fail louder.
   """
    used_span_keys = used_span_keys or set()
    expected_name = f"execute_tool {tool_name}"

    name_matches = sorted(
        [s for s in tool_spans if str(s.get("name", "")).strip() == expected_name],
        key=lambda s: s.get("start_time", ""),
    )

    if not name_matches:
        return {}

    if call_id:
        for span in name_matches:
            if span_identity(span) in used_span_keys:
                    continue
            attrs = span.get("attributes") or {}
            if attrs.get("gen_ai.tool.call.id") == call_id:
                    return span

    for span in name_matches:
        if span_identity(span) not in used_span_keys:
            return span

    return name_matches[-1]
