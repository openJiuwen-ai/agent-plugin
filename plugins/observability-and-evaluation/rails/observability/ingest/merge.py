"""The two workflow merges: loaded artifacts -> merged trace on disk.

Both emit the UNIFIED trace_metadata shape (§0.6.1, SCHEMA_VERSION 2.0.0) -
same keys, same order; only system_architecture differs.
"""
import json
import re
from pathlib import Path

from ..emit.spans import parse_otel_time
from ..model.version import SCHEMA_VERSION
from .discover import load_OrchestratorObj_json_files, extract_run_metadata_from_path
from .aggregate import aggregate_agent_statistics, extract_unique_tools, determine_system_architecture


def merge_agent_json_files_with_trace_metadata(
    *,
    session_dir: str,
    run_id: int | str,
    model: str | None = None,
    # REQUIRED, no default. This lands in trace_metadata.framework, which is how
    # downstream failure-diagnosis tooling attributes a trajectory once traces
    # from several frameworks are pooled. A wrong value is far worse than a
    # missing one: the trajectory still validates, still merges, and is filed
    # under the wrong producer with nothing raising.
    #
    # The old default was "MagenticOne", which is not a framework at all - it is
    # the name of the hierarchical science scenario (AutoGen's orchestration
    # pattern). So the default was a category error, putting an ARCHITECTURE
    # label in the FRAMEWORK field. Two closed vocabularies that must not mix;
    # see system_architecture below, whose values are sequential / shared_context / hierarchical /
    # looping.
    #
    # All four call sites pass framework=meta["framework"] from the registry.
    framework: str,
    uc: str = "unknown_UC",
    scenario: str | None = None,
    run_nr: int | str | None = None,
    system_architecture: str = "sequential",
    system_architecture_description: str | None = None,
    dataset: str | None = None,
    index_num: int | None = None,
    research_topic: str | None = None,
    output_filename: str | None = None,
) -> str:
    """Merge all agent_*_<run_id>.json files for one run into a workflow JSON
   with a trace_metadata header. Tolerant of corrupt/missing agent files.
   `agents` is a dict keyed by each agent file's stem (collision-safe because
   the agent_<N>_ ordinal is part of the key)."""
    session_path = Path(session_dir).expanduser().resolve()
    run_id_str = str(run_id)

    if output_filename is None:
        output_filename = f"merged_workflow_trace_{run_id_str}.json"
    output_path = session_path / output_filename

    run_suffix = re.compile(rf"_{re.escape(run_id_str)}(?:_|$)")
    order_prefix = re.compile(r"^agent_(\d+)_")

    files = sorted(
        p for p in session_path.glob("agent_*.json")
        if run_suffix.search(p.stem) and p.name != output_filename
    )

    loaded = {}
    for jf in files:
        try:
            with jf.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            if isinstance(data, dict):
                    m = order_prefix.match(jf.name)
                    order = int(m.group(1)) if m else 10**6
                    loaded[jf.stem] = {"order": order, "data": data}
        except Exception as exc:
            print(f"[merge] skipped {jf.name}: {exc}")

    def key(item):
        """Sort loaded agent JSON files by prompt step id and then by filename order."""
        _, entry = item
        data = entry["data"]
        try:
            step = int(data.get("task", {}).get("step_id"))
        except (TypeError, ValueError):
            step = 10**6
        return (step, entry["order"])

    ordered = sorted(loaded.items(), key=key)

    # agents: filename_stem -> agent dict, iteration stays in run order
    agents = {name: entry["data"] for name, entry in ordered}

    def safe_int(v):
        """Convert token/count fields into integers while tolerating missing or malformed values."""
        try:
            return int(v or 0)
        except Exception:
            return 0

    total = total_in = total_out = 0
    tools = set()
    seq, statuses = [], []

    earliest_start = None
    latest_end = None

    for a in agents.values():
        meta = a.get("agent_metadata", {}) or {}

        start_dt = parse_otel_time(meta.get("agent_timestamp_start", ""))
        end_dt = parse_otel_time(meta.get("agent_timestamp_end", ""))

        if start_dt:
            earliest_start = (
                start_dt
                if earliest_start is None
                else min(earliest_start, start_dt)
            )

        if end_dt:
            latest_end = (
                end_dt
                if latest_end is None
                else max(latest_end, end_dt)
            )

        # Keep the rest of your existing aggregation here.
        timestamp_start = earliest_start.isoformat() if earliest_start else None
        timestamp_end = latest_end.isoformat() if latest_end else None

        duration_seconds = (
            round((latest_end - earliest_start).total_seconds(), 4)
            if earliest_start and latest_end
            else None
        )
        seq.append(meta.get("agent_name") or "Unknown_Agent")
        statuses.append(meta.get("status", "unknown"))
        total     += safe_int(meta.get("total_tokens")  or meta.get("Total_tokens"))
        total_in  += safe_int(meta.get("input_tokens")  or meta.get("Total_input_tokens"))
        total_out += safe_int(meta.get("output_tokens") or meta.get("Total_output_tokens"))
        for t in (meta.get("tool_names") or []):
            if t:
                    tools.add(t)

    hard = {"failed", "error", "no_otel_spans", "missing_model_span", "missing_invoke_span"}
    if not agents:
        status = "failed"
    elif any(s in hard for s in statuses):
        status = "failed"
    elif all(s == "success" for s in statuses):
        status = "success"
    else:
        status = "partial"

    # ONE trace_metadata shape for both workflow models (§0.6.1, SCHEMA_VERSION
    # 2.0.0). The hierarchical merge emits these exact keys in this exact
    # order; only system_architecture tells the two apart at this level.
    # tool_sequence (adopted from hierarchical) is the sorted distinct tool
    # names used, matching extract_unique_tools() semantics.
    merged = {
        "trace_metadata": {
            "schema_version": SCHEMA_VERSION,
            "run_id_path": str(output_path),
            "framework": framework,
            "UC": uc,
            "scenario": scenario,
            "timestamp_start": timestamp_start,
            "timestamp_end": timestamp_end,
            "duration_seconds": duration_seconds,
            "run_nr": run_nr if run_nr is not None else run_id,
            "model": model,
            "system_architecture": system_architecture,
            "system_architecture_description": system_architecture_description,
            "dataset": dataset,
            "index_num": index_num,
            "research_topic": research_topic,
            "total_tokens": total,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "number_of_agents": len(agents),
            "number_of_tools": len(tools),
            "agent_sequence": seq,
            "tool_sequence": sorted(tools),
            "status": status,
        },
        "agents": agents,
    }

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, default=str)

    return str(output_path)

def create_final_merged_json_separate_invocations(
    *,
    session_dir: str,
    run_id: int | str,
    agent_names: list[str],
    model_name: str,
    saved_agent_json_records: list[dict],
    scenario_meta: dict | None = None,   # ← output of get_scenario_metadata()
    run_metadata: dict | None = None,    # ← RunSession.run_metadata: research_topic / dataset / index_num
) -> str:
    """
   Create final merged JSON for separate-invocation pipeline.

   scenario_meta (optional): dict from get_scenario_metadata(). When provided,
   its fields override the path-derived fallbacks for uc/scenario/framework/
   system_architecture.

   run_metadata (optional): the per-run metadata dict the scenario seeded on
   the RunSession - the same one the sequential merge splats. Carries
   research_topic (science), dataset + index_num (math). Added by the §0.6.1
   batch, which replaced the always-null task_index with dataset + index_num.
   """
    scenario_meta = scenario_meta or {}
    run_metadata = run_metadata or {}

    OrchestratorObj_files = load_OrchestratorObj_json_files(session_dir, run_id)

    if not OrchestratorObj_files:
        print(f"No OrchestratorObj_ JSON files found in {session_dir}")
        return ""

    init_data = None
    for idx, (seq, name, data) in enumerate(OrchestratorObj_files):
        if name == "initialization":
            init_data = data
            break

    orchestrator_task = None
    orchestrator_response = None
    if init_data:
        orchestrator_task = init_data.get("orchestrator_setup", {}).get("task")
        orchestrator_response = init_data.get("orchestrator_response")

    # Path-derived values kept only as fallback when scenario_meta is absent
    path_metadata = extract_run_metadata_from_path(session_dir, run_id)

    stats = aggregate_agent_statistics(OrchestratorObj_files)
    unique_tools = extract_unique_tools(OrchestratorObj_files)

    invoked_agents = stats["agent_sequence"]

    all_agent_statuses = []
    for seq, agent_name, data in OrchestratorObj_files:
        if agent_name != "initialization":
            metadata = data.get("agent_metadata", {}) or {}
            all_agent_statuses.append(metadata.get("status", "unknown"))

    # The empty case MUST be checked first: all() over an empty list is True,
    # so with the success branch first, a run in which the orchestrator
    # invoked no agent at all reported "success". The no_agents_executed
    # branch was unreachable - found live on 2026-08-24, when a math run's
    # orchestrator answered the task itself and skipped both agents. Same
    # branch-order bug presumably exists in the sibling ports (copied code) -
    # part of the §0.6 batch notes for them.
    if len(all_agent_statuses) == 0:
        overall_status = "no_agents_executed"
    elif all(status == "success" for status in all_agent_statuses):
        overall_status = "success"
    elif any(status == "failed" for status in all_agent_statuses):
        overall_status = "failed"
    else:
        overall_status = "partial"

    # ── §0.6.1 (SCHEMA_VERSION 2.0.0): ONE trace_metadata shape, shared with
    # the sequential merge - same keys, same order. What changed here:
    #   uc -> UC;  run_timestamp_* -> timestamp_*;  run_id (a string duplicate
    #   of run_nr, never a path) dropped for run_id_path (the merged file's
    #   absolute path, the field's designed meaning);  task_index (always
    #   null) dropped for dataset + index_num from run_metadata;
    #   duration_seconds added;  research_topic now comes from run_metadata
    #   (its old fallback-to-scenario-name produced junk like "Scenario_2"
    #   for math runs, where sequential correctly emits null).
    timestamp_start = stats.get("run_timestamp_start")
    timestamp_end = stats.get("run_timestamp_end")
    start_dt = parse_otel_time(timestamp_start or "")
    end_dt = parse_otel_time(timestamp_end or "")
    duration_seconds = (
        round((end_dt - start_dt).total_seconds(), 4)
        if start_dt and end_dt
        else None
    )

    merged_output_path = Path(session_dir).expanduser().resolve() / f"merged_workflow_{run_id}.json"

    trace_metadata = {
        "schema_version": SCHEMA_VERSION,
        "run_id_path": str(merged_output_path),
        # Fallback was "MAF" (Microsoft Agent Framework), left over from when this
        # layer served three frameworks. Unreachable today - every entry in
        # all_scenario_Information.json carries "framework": "OJW" - but a wrong
        # value here mislabels the trajectory silently for downstream diagnosis.
        # Phase 1 should make this required and validated, as the sequential
        # merge now is.
        "framework": scenario_meta.get("framework") or "OJW",
        "UC": scenario_meta.get("uc")
                or path_metadata.get("uc", "unknown_UC"),
        "scenario": scenario_meta.get("scenario")
                        or path_metadata.get("scenario", f"Scenario{run_id}"),
        "timestamp_start": timestamp_start,
        "timestamp_end": timestamp_end,
        "duration_seconds": duration_seconds,
        "run_nr": path_metadata.get("run_nr", int(str(run_id))),
        "model": model_name,
        "system_architecture": scenario_meta.get("system_architecture")
                                      or determine_system_architecture(),
        "system_architecture_description": scenario_meta.get(
            "system_architecture_description", ""
        ),
        "dataset": run_metadata.get("dataset"),
        "index_num": run_metadata.get("index_num"),
        "research_topic": run_metadata.get("research_topic"),
        "total_tokens": stats.get("total_tokens", 0),
        "total_input_tokens": stats.get("input_tokens", 0),
        "total_output_tokens": stats.get("output_tokens", 0),
        "number_of_agents": len(invoked_agents),
        "number_of_tools": len(unique_tools),
        "agent_sequence": invoked_agents,
        "tool_sequence": unique_tools,
        "status": overall_status,
    }

    team_configuration = {
        "team": "MagenticOneGroupChat",
        "provided_agent_names": agent_names,
        "invoked_agent_names": list(dict.fromkeys(invoked_agents)),
        "agent_invocation_count": len(invoked_agents),
    }

    workflow_summary = {
        "orchestrator_task": orchestrator_task,
        "magneticOneOrchestrator_actions_count": len(invoked_agents),
        "agent_execution_order": invoked_agents,
        "llm_call_count": stats.get("llm_call_count", 0),
        "tool_call_count": stats.get("tool_call_count", 0),
        "tokens": {
            "input": stats.get("input_tokens", 0),
            "output": stats.get("output_tokens", 0),
            "total": stats.get("total_tokens", 0),
        },
    }

    # Agents keyed by their source JSON filename.
    # The initialization file (seq 0) is inserted FIRST so the dict opens with
    # OrchestratorObj_0_initialization_<run_id>.json, then the invocations follow
    # in orchestrator-sequence order. OrchestratorObj_files is already sorted by
    # sequence, so seq 0 lands first on its own.
    agents = {}
    for seq, agent_name, data in OrchestratorObj_files:
        if agent_name == "initialization":
            init_file_key = f"OrchestratorObj_{seq}_{agent_name}_{run_id}.json"
            agents[init_file_key] = {
                "sequence": seq,
                "agent_name": "initialization",
                "invocation_info": {},
                "execution": data,
            }
            continue

        saved_record = next(
                (r for r in saved_agent_json_records if r["sequence_number"] == seq),
                None
        )

        invocation_info = {}
        if saved_record:
                invocation_info = {
                    "invocation_number": saved_record.get("invocation_number"),
                    "total_invocations_of_agent": saved_record.get("total_invocations_of_agent"),
                    "query_preview": saved_record.get("query"),
                }

        file_key = (
                saved_record.get("agent_json_file")
                if saved_record
                else f"OrchestratorObj_{seq}_{agent_name}_{run_id}.json"
        )

        agents[file_key] = {
                "sequence": seq,
                "agent_name": agent_name,
                "invocation_info": invocation_info,
                "execution": data,
        }

    merged_json = {
        "trace_metadata": trace_metadata,
        "team_configuration": team_configuration,
        "workflow_summary": workflow_summary,
        "agents": agents,
    }

    #if orchestrator_response:
    #   merged_json["orchestrator_final_response"] = orchestrator_response

    output_path = Path(session_dir).resolve()
    merged_file = output_path / f"merged_workflow_{run_id}.json"

    with merged_file.open("w", encoding="utf-8") as f:
        json.dump(merged_json, f, ensure_ascii=False, indent=2, default=str)

    print(f"\nCreated final merged workflow JSON: {merged_file.name}")
    print(f"  - Total invocations: {len(invoked_agents)}")
    print(f"  - Unique agents: {len(team_configuration['invoked_agent_names'])}")
    print(f"  - Total LLM calls: {workflow_summary['llm_call_count']}")
    print(f"  - Total tool calls: {workflow_summary['tool_call_count']}")
    print(f"  - Total tokens: {trace_metadata['total_tokens']}")
    print(f"  - Status: {overall_status}")

    return str(merged_file)
