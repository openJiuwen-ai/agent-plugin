"""Aggregation over loaded artifacts (ingest side, §3.4)."""
from datetime import datetime


def aggregate_agent_statistics(OrchestratorObj_files: list[tuple[int, str, dict]]) -> dict:
    """
   Aggregate statistics from all loaded OrchestratorObj_ JSON files.
   """
    agent_names = []
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    llm_calls = 0
    tool_calls = 0

    earliest_start = None
    latest_end = None

    for sequence, agent_or_init, data in OrchestratorObj_files:
        if agent_or_init == "initialization":
            continue

        agent_names.append(agent_or_init)

        metadata = data.get("agent_metadata", {}) or {}

        tokens = metadata.get("total_tokens", 0) or 0
        in_tokens = metadata.get("input_tokens", 0) or 0
        out_tokens = metadata.get("output_tokens", 0) or 0

        total_tokens += tokens
        input_tokens += in_tokens
        output_tokens += out_tokens

        llm_calls += metadata.get("llm_call_count", 0) or 0
        tool_calls += metadata.get("tool_call_count", 0) or 0

        start_time = metadata.get("agent_timestamp_start")
        end_time = metadata.get("agent_timestamp_end")

        if start_time:
            try:
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                if earliest_start is None or start_dt < earliest_start:
                    earliest_start = start_dt
            except Exception:
                pass

        if end_time:
            try:
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                if latest_end is None or end_dt > latest_end:
                    latest_end = end_dt
            except Exception:
                pass

    run_start = earliest_start.isoformat() if earliest_start else None
    run_end = latest_end.isoformat() if latest_end else None

    return {
        "agent_sequence": agent_names,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "llm_call_count": llm_calls,
        "tool_call_count": tool_calls,
        "run_timestamp_start": run_start,
        "run_timestamp_end": run_end,
    }

def extract_unique_tools(OrchestratorObj_files: list[tuple[int, str, dict]]) -> list[str]:
    """Extract unique tool names from all agent executions."""
    tools = set()

    for sequence, agent_or_init, data in OrchestratorObj_files:
        if agent_or_init == "initialization":
            continue

        metadata = data.get("agent_metadata", {}) or {}
        tool_names = metadata.get("tool_names", []) or []

        for tool_name in tool_names:
            if tool_name:
                tools.add(tool_name)

    return sorted(list(tools))

def determine_system_architecture() -> str:
    """
   Determine system architecture from agent sequence.
   """
    return "sequential"
