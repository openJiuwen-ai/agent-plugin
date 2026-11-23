"""Ingest: reading artifacts back OFF DISK and merging/aggregating them -
split from emit/ (live run -> JSON) per §3.4, so merging and aggregation can
work on runs this process did not produce."""
from .discover import load_OrchestratorObj_json_files, extract_run_metadata_from_path
from .aggregate import aggregate_agent_statistics, extract_unique_tools, determine_system_architecture
from .merge import merge_agent_json_files_with_trace_metadata, create_final_merged_json_separate_invocations
