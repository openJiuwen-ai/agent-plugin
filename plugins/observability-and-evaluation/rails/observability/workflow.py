"""Export flat sequential, shared-context, and looping agent trajectories."""

import json
from pathlib import Path
import re

from .emit.spans import parse_otel_time
from .emit.extract import missing_tool_result
from .model.agent import AgentTrace
from .model.sequential import SequentialWorkflowTraceV2
from .model.version import SCHEMA_VERSION


_STEP = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _step_blocks(agent):
    # Only schema-defined blocks: never change step_id keys inside tool data
    # or raw OpenTelemetry attributes.
    for key, block in agent.items():
        if not isinstance(block, dict) or "step_id" not in block:
            continue
        yield block
        if key.startswith("tool_execution_activity_"):
            for name in ("tool_calling", "tool_call_result", "tool_llm_request"):
                if name in block:
                    yield block[name]


def _renumber(agent, offset):
    blocks = list(_step_blocks(agent))
    ids = [block["step_id"] for block in blocks]
    if any(not isinstance(value, str) or not _STEP.fullmatch(value) for value in ids):
        raise ValueError("Agent artifact contains an invalid step_id")
    roots = sorted({int(value.split(".")[0]) for value in ids})
    mapping = {old: offset + index for index, old in enumerate(roots, start=1)}
    for block in blocks:
        head, dot, suffix = block["step_id"].partition(".")
        block["step_id"] = str(mapping[int(head)]) + dot + suffix
    return offset + len(roots)


def merge_workflow(agent_json_files, output_path, run_nr=1, *, scenario_meta=None, run_metadata=None):
    """Merge selected sequential, shared-context, or looping agent JSON paths and return the output Path.

    Read only the supplied files, ordered by invocation start time (ties retain
    input order). Preserve every invocation, even for repeated agent names.
    Renumber schema step IDs in the embedded copies; leave source files intact.
    All input and schema validation completes before the destination is written.
    Optional scenario_meta and run_metadata populate existing domain/scenario,
    description, dataset/index, and topic fields; measured totals remain captured facts.

    The caller selects one workflow's files. This function cannot infer missing
    agents or distinguish unrelated runs supplied together. Nested invocations
    require a hierarchical exporter and are rejected here.
    """
    scenario_meta = scenario_meta or {}
    run_metadata = run_metadata or {}
    if scenario_meta.get("system_architecture", "sequential") not in {"sequential", "shared_context", "looping"}:
        raise ValueError("Flat workflow export requires sequential, shared_context, or looping scenario metadata")
    if scenario_meta.get("framework", "OJW") != "OJW":
        raise ValueError("Observed OpenJiuwen workflows require framework OJW")
    if isinstance(agent_json_files, (str, Path)):
        raise TypeError("Pass a list of agent JSON paths, not a directory or string")
    paths = [Path(path).expanduser().resolve() for path in agent_json_files]
    destination = Path(output_path).expanduser().resolve()
    if not paths:
        raise ValueError("No agent JSON files supplied")
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate agent JSON paths supplied")
    if destination in paths:
        raise ValueError("Workflow output must not overwrite an input agent JSON")

    loaded = []
    span_ids = set()
    invocation_parents = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Older exporters wrote timing-only placeholders when an interrupted
            # tool request had no captured result. Normalize only that exact shape
            # in the merged copy; retain all source files and reject partial payloads.
            timing_keys = {"step_id", "timestamp_start", "timestamp_end",
                           "duration_seconds", "http_attempts"}
            for key, activity in data.items():
                if not key.startswith("tool_execution_activity_") or not isinstance(activity, dict):
                    continue
                result = activity.get("tool_call_result")
                calling = activity.get("tool_calling")
                if (isinstance(result, dict) and set(result) <= timing_keys
                        and isinstance(calling, dict)
                        and "call_id" in calling and "tool_name" in calling):
                    activity["tool_call_result"] = {**missing_tool_result(calling), **result}
            AgentTrace.model_validate(data)
            if data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError(f"Expected agent schema_version {SCHEMA_VERSION}")
            meta = data["agent_metadata"]
            start = parse_otel_time(meta["agent_timestamp_start"])
            end = parse_otel_time(meta["agent_timestamp_end"])
            if start is None or end is None or start.tzinfo is None or end.tzinfo is None or end < start:
                raise ValueError("Agent timestamps must be valid, timezone-aware, and ordered")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Invalid agent artifact {path}: {exc}") from exc
        for span in data["OTel_traces"]["Traces"]:
            context = span.get("context", {})
            span_ids.add((context.get("trace_id"), context.get("span_id")))
            if span.get("name", "").startswith("invoke_agent ") and span.get("parent_id"):
                invocation_parents.append((context.get("trace_id"), span["parent_id"]))
        loaded.append((start, end, path, data))
    if any(parent in span_ids for parent in invocation_parents):
        raise ValueError("Nested agent invocations require a hierarchical workflow exporter")
    loaded.sort(key=lambda entry: entry[0])
    if any(current[0] < previous[1] for previous, current in zip(loaded, loaded[1:])):
        raise ValueError("Overlapping invocations do not form a sequential workflow")

    agents = {}
    offset = 0
    models = set()
    for index, (_, _, path, data) in enumerate(loaded, start=1):
        for block in _step_blocks(data):
            if block.get("model_name"):
                models.add(block["model_name"])
        offset = _renumber(data, offset)
        # Ordinal avoids collisions even for same-named files from two folders.
        agents[f"{index}_{path.stem}"] = data
    metadata = [data["agent_metadata"] for data in agents.values()]
    statuses = [meta["status"] for meta in metadata]
    failed = {"failed", "error", "cancelled", "no_otel_spans", "missing_model_span", "missing_invoke_span"}
    status = "failed" if any(value in failed for value in statuses) else (
        "success" if all(value == "success" for value in statuses) else "partial"
    )
    tools = sorted({name for meta in metadata for name in meta["tool_names"]})
    start, end = loaded[0][0], max(entry[1] for entry in loaded)
    workflow = {"trace_metadata": {
        "schema_version": SCHEMA_VERSION,
        "run_id_path": str(destination), "framework": "OJW", "UC": scenario_meta.get("uc", "unknown_UC"),
        "scenario": scenario_meta.get("scenario", "sequential_workflow"), "run_nr": run_nr,
        "timestamp_start": start.isoformat(), "timestamp_end": end.isoformat(),
        "duration_seconds": round((end - start).total_seconds(), 4),
        "model": ", ".join(sorted(models)), "system_architecture": scenario_meta.get("system_architecture", "sequential"),
        "system_architecture_description": scenario_meta.get(
            "system_architecture_description", "Sequential invocations exported from selected agent JSON files."),
        "dataset": run_metadata.get("dataset"), "index_num": run_metadata.get("index_num"),
        "research_topic": run_metadata.get("research_topic"),
        "total_tokens": sum(meta["total_tokens"] for meta in metadata),
        "total_input_tokens": sum(meta["input_tokens"] for meta in metadata),
        "total_output_tokens": sum(meta["output_tokens"] for meta in metadata),
        "number_of_agents": len(agents), "number_of_tools": len(tools),
        "agent_sequence": [meta["agent_name"] for meta in metadata],
        "tool_sequence": tools, "status": status,
    }, "agents": agents}
    SequentialWorkflowTraceV2.model_validate(workflow)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination
