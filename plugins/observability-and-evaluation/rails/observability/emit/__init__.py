"""Shared emission helpers - ONE copy of what used to be duplicated
across the two JSON handlers (Phase 1 step 4)."""
from .common import safe_text, safe_json_loads, get_nested_attr_or_dict_value
from .spans import parse_otel_time, otel_duration_seconds, parse_otel_console_spans, group_spans_by_name, pick_first_span, pick_last_span, span_identity, extract_agent_name_from_span_name, extract_http_metadata, find_all_http_spans_inside_tool_window
from .extract import extract_prompts_from_first_model_span, get_final_agent_output, build_otel_traces_block
from .tools import make_tool_result, parse_structured_tool_output, summarize_tool_activity_status
from .save import save_single_agent_json

__all__ = ['safe_text', 'safe_json_loads', 'get_nested_attr_or_dict_value', 'parse_otel_time', 'otel_duration_seconds', 'parse_otel_console_spans', 'group_spans_by_name', 'pick_first_span', 'pick_last_span', 'span_identity', 'extract_agent_name_from_span_name', 'extract_http_metadata', 'find_all_http_spans_inside_tool_window', 'extract_prompts_from_first_model_span', 'get_final_agent_output', 'build_otel_traces_block', 'make_tool_result', 'parse_structured_tool_output', 'summarize_tool_activity_status', 'save_single_agent_json']
