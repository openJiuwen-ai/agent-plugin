"""The tool-result envelope: writer, reader, and the activity-status rollup.

parse_structured_tool_output and summarize_tool_activity_status carry the
CONVERGED cross-port vocabulary (§0.6.2, decision 12, SCHEMA_VERSION 2.0.0):
any failure => failed, no_tool_call_request, unstructured_output. One copy,
both architectures, all ports.
"""
import json

from .common import safe_text


def make_tool_result(
    *,
    tool_called: bool,
    tool_success: bool,
    status: str,
    data=None,
    error: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Serialize a standard tool-result envelope to a JSON string.

   This is the canonical shape every tool function returns, so downstream
   parsing can rely on the keys tool_called / tool_success / status / data /
   error / metadata being present.
   """
    return json.dumps(
        {
            "tool_called": tool_called,
            "tool_success": tool_success,
            "status": status,
            "data": data,
            "error": error,
            "metadata": metadata or {},
        },
        ensure_ascii=False,
        default=str,
    )

def parse_structured_tool_output(raw_output) -> dict:
    """Normalize any tool output back into the standard envelope dict.

   Accepts a dict, a JSON string, or arbitrary text. Missing output -> a
   'missing_output' failure envelope. Unparseable text -> an
   'unstructured_output' envelope that still marks the call as called/success
   so a tool that simply returned plain text isn't reported as a failure.
   """
    if raw_output is None:
        return {
            "tool_called": False,
            "tool_success": False,
            "status": "missing_output",
            "data": None,
            "error": "Tool output missing",
            "metadata": {},
        }

    if isinstance(raw_output, dict):
        return {
            "tool_called": raw_output.get("tool_called", True),
            "tool_success": raw_output.get("tool_success", False),
            "status": raw_output.get("status", "unknown"),
            "data": raw_output.get("data"),
            "error": raw_output.get("error"),
            "metadata": raw_output.get("metadata", {}),
        }

    raw_text = safe_text(raw_output)
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            return {
                "tool_called": parsed.get("tool_called", True),
                "tool_success": parsed.get("tool_success", False),
                "status": parsed.get("status", "unknown"),
                "data": parsed.get("data"),
                "error": parsed.get("error"),
                "metadata": parsed.get("metadata", {}),
            }
    except Exception:
        pass

    return {
        "tool_called": True,
        "tool_success": True,
        "status": "unstructured_output",
        "data": raw_text,
        "error": None,
        "metadata": {},
    }

def summarize_tool_activity_status(tool_call_request: list[dict], tool_names: list[str]) -> str:
    """Roll many tool calls up into one agent-level tool status string.
   Feeds the agent-level status decision.

   CONVERGED VOCABULARY (§0.6.2, decision 12, SCHEMA_VERSION 2.0.0): any
   failure => "failed". This replaced the pre-2.0.0 sequential rule (any
   success => "success") as part of the cross-port §0.6 batch - the same rule
   now applies in both handlers and in all three sibling ports. Do not
   re-diverge the two copies.
   """
    if not tool_names and not tool_call_request:
        return "not_used"

    if tool_names and not tool_call_request:
        return "no_tool_call_request"

    statuses = [call.get("status", "unknown") for call in tool_call_request]
    success_values = [call.get("tool_success") for call in tool_call_request]

    if any(success is False for success in success_values):
        return "failed"

    if any(status in {"failed", "http_error", "timeout", "missing_output"} for status in statuses):
        return "failed"

    if all(success is True for success in success_values):
        return "success"

    return "failed"
