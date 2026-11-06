"""The sequential workflow trace: merged_workflow_trace_<run>.json.

Pinned by tests/golden/references/science_scenario6_sequential.json (the AG
reference; contains Scenario_1 despite the filename) - identical at every
level (§0.1c).
"""
from typing import Dict, Optional

from .agent import AgentTrace
from .common import _Frozen
from .workflow_base import TraceMetadataBase, UnifiedTraceMetadata


class SequentialTraceMetadata(TraceMetadataBase):
    """Pre-change sequential block: base + the six concepts that still differ
    from hierarchical (§0.6.1 unifies them in step 3, mostly in this block's
    favour)."""

    run_id_path: str          # ABSOLUTE path of the run directory - the live cross-port contract (§0.1c)
    UC: str                   # "Science" | "Programming" | "Math" | "Observability_Probes"
    timestamp_start: str
    timestamp_end: str
    duration_seconds: float
    dataset: Optional[str] = None      # math/programming: "GSM8K" | "GSM-Plus" | dataset of the task
    index_num: Optional[int] = None    # the task's index within `dataset`


class SequentialWorkflowTrace(_Frozen):
    """PRE-BATCH (no schema_version field). Validates the committed 1.0.0
    corpus and the AG reference."""

    trace_metadata: SequentialTraceMetadata
    # Keyed by agent name - with the attempt suffix for looping runs
    # ("Planner_Agent_attempt2"), which is how one merged trace holds every
    # pass of a QA-retry loop without collision.
    agents: Dict[str, AgentTrace]


class SequentialWorkflowTraceV2(_Frozen):
    """Flat sequential, shared_context, or looping workflow with v2 metadata."""

    trace_metadata: UnifiedTraceMetadata
    agents: Dict[str, AgentTrace]
