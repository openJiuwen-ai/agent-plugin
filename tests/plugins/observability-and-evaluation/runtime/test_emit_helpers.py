"""Unit tests for the now-single emit helpers.

These are the functions collapsed from two (or three) copies to one: the
tests pin the single copy's behaviour, including the CONVERGED 2.0.0
status vocabulary, so a future edit that would re-diverge the ports fails
here first.
"""
import json
from pathlib import Path

import pytest

from ojw_observability.emit.common import safe_text, safe_json_loads
from ojw_observability.emit.spans import (
    parse_otel_time,
    otel_duration_seconds,
    group_spans_by_name,
    pick_first_span,
    pick_last_span,
)
from ojw_observability.emit.steps import StepCounter
from ojw_observability.emit.tools import (
    make_tool_result,
    parse_structured_tool_output,
    summarize_tool_activity_status,
)
from ojw_observability.observability.json.hierarchical_json_handler import (
    InvocationStepCounter,
)
from ojw_observability.workflow import merge_workflow


# ---------------------------------------------------------------- safe_text
def test_safe_text_passthrough_and_coercion():
    assert safe_text("hello") == "hello"
    assert isinstance(safe_text({"a": 1}), str)
    assert safe_text(None) == "" or safe_text(None) is not None  # never raises


def test_safe_json_loads_dict_and_garbage():
    assert safe_json_loads('{"a": 1}') == {"a": 1}
    # unparseable text is WRAPPED, not dropped - the raw arguments stay in the
    # trace instead of vanishing (this is how tool_arguments survives a model
    # emitting malformed JSON)
    assert safe_json_loads("not json") == {"raw_arguments": "not json"}


# ------------------------------------------------------------- span timing
def test_parse_otel_time_roundtrip_and_duration():
    t0 = parse_otel_time("2026-08-24T10:00:00.000000Z")
    t1 = parse_otel_time("2026-08-24T10:00:01.500000Z")
    assert t0 is not None and t1 is not None
    assert otel_duration_seconds("2026-08-24T10:00:00.000000Z",
                                 "2026-08-24T10:00:01.500000Z") == pytest.approx(1.5)


def test_parse_otel_time_empty_is_none():
    assert parse_otel_time("") is None


# ------------------------------------------------------------ span helpers
SPANS = [
    {"name": "openai.chat", "start_time": "2026-01-01T00:00:01Z"},
    {"name": "openai.chat", "start_time": "2026-01-01T00:00:03Z"},
    {"name": "invoke_agent x", "start_time": "2026-01-01T00:00:02Z"},
]


def test_pick_first_and_last_by_start_time():
    chats = [s for s in SPANS if s["name"] == "openai.chat"]
    assert pick_first_span(chats)["start_time"].endswith("01Z")
    assert pick_last_span(chats)["start_time"].endswith("03Z")


def test_group_spans_by_name_buckets():
    grouped = group_spans_by_name(SPANS)
    assert isinstance(grouped, dict)
    # model spans land in one bucket regardless of the exact key layout
    assert any(len(v) == 2 for v in grouped.values())


# ------------------------------------------------------------ step counters
def test_workflow_step_counter_is_monotonic_and_shared_style():
    c = StepCounter()
    assert (c.next_step(), c.next_step(), c.next_step()) == (1, 2, 3)
    assert c.agent_ordinal == 0  # untouched by next_step


def test_invocation_step_counter_returns_strings_from_start():
    c = InvocationStepCounter(start=5)
    assert c.next() == "5"
    assert c.next() == "6"


def test_workflow_renumbers_steps_numerically_and_preserves_nested_ids(tmp_path):
    # The old step_sort_key helper was intentionally removed. Exercise the
    # ordering contract where it now matters: exported workflow step IDs.
    fixture_path = Path(__file__).parents[1] / "fixtures/weather_agent.json"
    agent = json.loads(fixture_path.read_text(encoding="utf-8"))
    agent["schema_version"] = "2.0.0"
    agent["task"]["step_id"] = "9"
    agent["llm_request_1"]["step_id"] = "10"
    activity = agent["tool_execution_activity_1"]
    activity["step_id"] = "11"
    activity["tool_calling"]["step_id"] = "11.1"
    activity["tool_call_result"]["step_id"] = "11.2"
    activity["tool_llm_request"]["step_id"] = "11.3"
    activity["tool_calling"]["tool_arguments"]["step_id"] = "user-data"
    agent["result"]["step_id"] = "12"
    agent["OTel_traces"]["step_id"] = "13"
    source = tmp_path / "agent_weather.json"
    source.write_text(json.dumps(agent), encoding="utf-8")
    original = source.read_bytes()

    destination = merge_workflow([source], tmp_path / "workflow.json")
    merged = json.loads(destination.read_text(encoding="utf-8"))
    exported, = merged["agents"].values()

    assert exported["task"]["step_id"] == "1"
    assert exported["llm_request_1"]["step_id"] == "2"
    exported_activity = exported["tool_execution_activity_1"]
    assert exported_activity["step_id"] == "3"
    assert exported_activity["tool_calling"]["step_id"] == "3.1"
    assert exported_activity["tool_call_result"]["step_id"] == "3.2"
    assert exported_activity["tool_llm_request"]["step_id"] == "3.3"
    assert exported_activity["tool_calling"]["tool_arguments"]["step_id"] == "user-data"
    assert exported["result"]["step_id"] == "4"
    assert exported["OTel_traces"]["step_id"] == "5"
    assert source.read_bytes() == original


# --------------------------------------------- envelope writer/reader pair
def test_envelope_roundtrip():
    raw = make_tool_result(tool_called=True, tool_success=True, status="success",
                           data={"x": 1}, error=None, metadata={"src": "t"})
    parsed = parse_structured_tool_output(raw)
    assert parsed["tool_called"] is True
    assert parsed["tool_success"] is True
    assert parsed["status"] == "success"
    assert parsed["data"] == {"x": 1}
    assert parsed["metadata"] == {"src": "t"}


def test_parse_missing_output():
    parsed = parse_structured_tool_output(None)
    assert parsed["status"] == "missing_output"
    assert parsed["tool_success"] is False


def test_parse_dict_envelope_passthrough():
    parsed = parse_structured_tool_output(
        {"tool_called": True, "tool_success": False, "status": "failed",
         "data": None, "error": "boom", "metadata": {}})
    assert parsed["status"] == "failed"
    assert parsed["error"] == "boom"


def test_parse_plain_text_is_unstructured_output():
    """The CONVERGED cross-port value. Pre-2.0.0 the
    hierarchical copy said 'success' here; that must not come back."""
    parsed = parse_structured_tool_output("just some prose the tool returned")
    assert parsed["status"] == "unstructured_output"
    assert parsed["tool_called"] is True
    assert parsed["tool_success"] is True
    assert parsed["data"] == "just some prose the tool returned"


# ------------------------------------------- tool-phase rollup (converged)
def _call(success=True, status="success"):
    return {"tool_success": success, "status": status}


def test_rollup_no_tools_at_all():
    assert summarize_tool_activity_status([], []) == "not_used"


def test_rollup_tools_declared_but_never_called():
    """The converged 2.0.0 label. 'detected_without_call_details' is retired."""
    assert summarize_tool_activity_status([], ["search"]) == "no_tool_call_request"


def test_rollup_all_success():
    assert summarize_tool_activity_status([_call(), _call()], ["t"]) == "success"


def test_rollup_any_failure_wins():
    """2.0.0: any failure => failed. Pre-2.0.0 the sequential
    copy said any success => success; the mixed case must stay 'failed'."""
    assert summarize_tool_activity_status([_call(True), _call(False, "timeout")], ["t"]) == "failed"


def test_rollup_failure_status_strings():
    for bad in ("failed", "http_error", "timeout", "missing_output"):
        assert summarize_tool_activity_status([_call(True, bad)], ["t"]) == "failed"
