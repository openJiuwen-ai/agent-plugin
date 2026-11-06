"""The trace_metadata fields the two workflow models share (pre-change).

At SCHEMA_VERSION 1.0.0 the sequential and hierarchical blocks differ in six
concepts (§0.1c table); the 14 fields here are the agreeing remainder. The
§0.6.1 batch (step 3) collapses the difference: the two subclasses converge
to one shape and most of sequential.py's extras move up here.
"""
from typing import List, Optional

from pydantic import ConfigDict

from .common import _Frozen


class TraceMetadataBase(_Frozen):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    framework: str            # "OJW" here; "MAF" / "AG" / "LangGraph" in the sibling ports
    scenario: str
    run_nr: int
    model: str
    system_architecture: str  # vocabulary: sequential | shared_context | hierarchical | looping
    system_architecture_description: str
    research_topic: Optional[str] = None   # science/probes runs; null for math/programming
    total_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    number_of_agents: int
    number_of_tools: int
    agent_sequence: List[str]
    status: str               # success | failed | partial | no_agents_executed - see status.py


class UnifiedTraceMetadata(TraceMetadataBase):
    """The POST-BATCH (2.0.0) trace_metadata - ONE shape for both workflow
    models (§0.6.1). Emitted by both merges with identical keys in identical
    order; only system_architecture tells the two workflows apart here."""

    schema_version: str
    run_id_path: str                   # absolute path of the merged trace file
    UC: str
    timestamp_start: Optional[str] = None
    timestamp_end: Optional[str] = None
    duration_seconds: Optional[float] = None
    dataset: Optional[str] = None      # math/programming task source
    index_num: Optional[int] = None    # the task's index within `dataset`
    tool_sequence: List[str]           # sorted distinct tool names used
