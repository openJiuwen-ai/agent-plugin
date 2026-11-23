"""The hierarchical (Magentic-One) workflow trace and its side artifacts:
merged_workflow_<run>.json, OrchestratorObj_0_initialization_<run>.json,
OrchestratorObj_skipped_agents_<run>.json.

Pinned by tests/golden/references/science_scenario6_merged.json - identical
at every level including team_configuration and workflow_summary (§0.1c).

The AutoGen-derived strings here are INTERFACE (§0.3): "MagenticOneGroupChat"
names the pattern all three ports implement, and
`magneticOneOrchestrator_actions_count` - misspelling included - is the wire
key. The Literal pins the former so an edit fails at construction instead of
silently de-aligning this port.
"""
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import ConfigDict, model_validator

from .agent import AgentTrace
from .common import TokenUsage, _Frozen
from .workflow_base import TraceMetadataBase, UnifiedTraceMetadata


class HierarchicalTraceMetadata(TraceMetadataBase):
    """Pre-change hierarchical block: base + the six concepts that still
    differ from sequential. §0.6.1 (step 3) renames/replaces five of them and
    keeps tool_sequence."""

    run_id: str                       # holds the run NUMBER as a string - a duplicate of run_nr,
                                      # never a path despite the name; dropped by §0.6.1
    uc: str                           # lowercase pre-change; becomes UC in §0.6.1
    run_timestamp_start: Optional[str] = None
    run_timestamp_end: Optional[str] = None
    task_index: Optional[str] = None  # only ever observed null; replaced by dataset+index_num in §0.6.1
    tool_sequence: List[str]          # distinct tool names used - the one concept sequential ADOPTS


class TeamConfiguration(_Frozen):
    team: Literal["MagenticOneGroupChat"]
    provided_agent_names: List[str]
    invoked_agent_names: List[str]
    agent_invocation_count: int


class WorkflowSummary(_Frozen):
    orchestrator_task: str
    magneticOneOrchestrator_actions_count: int   # frozen wire key, misspelling included (decision 8)
    agent_execution_order: List[str]
    llm_call_count: int
    tool_call_count: int
    tokens: TokenUsage


# --- the initialization artifact (OrchestratorObj_0_...) --------------------

class InitializationMetadata(_Frozen):
    run_id: str
    timestamp_utc: str
    team_type: Literal["MagenticOneGroupChat"]
    model: str


class OrchestratorSetup(_Frozen):
    task: str
    model_used: str


class TeamComposition(_Frozen):
    total_agents: int
    agent_names: List[str]


class InitializationExecution(_Frozen):
    schema_version: Optional[str] = None   # stamped from 2.0.0 on
    initialization_metadata: InitializationMetadata
    orchestrator_setup: OrchestratorSetup
    team_composition: TeamComposition
    orchestrator_response: str


# --- the skipped-agents artifact --------------------------------------------

class SkippedAgent(_Frozen):
    agent_name: str
    reason: str
    note: str


class SkippedAgentsMetadata(_Frozen):
    total_agents_provided: int
    total_agents_invoked: int
    total_agents_skipped: int
    agents: List[SkippedAgent]


class SkippedAgentsArtifact(_Frozen):
    schema_version: Optional[str] = None   # stamped from 2.0.0 on
    skipped_agents_metadata: SkippedAgentsMetadata


# --- the merged trace -------------------------------------------------------

class InvocationEntry(_Frozen):
    """One entry of the merged trace's `agents` dict, keyed by the source
    filename. Sequence 0 wraps the initialization artifact; every later
    sequence wraps one agent invocation (the same shared AgentTrace shape the
    standalone OrchestratorObj files carry)."""

    sequence: int
    agent_name: str
    invocation_info: Dict[str, Any]
    execution: Union[InitializationExecution, AgentTrace]

    @model_validator(mode="after")
    def _sequence_matches_execution(self):
        is_init = isinstance(self.execution, InitializationExecution)
        if is_init != (self.sequence == 0):
            raise ValueError(
                "sequence 0 must wrap the initialization execution and only it"
            )
        return self


class HierarchicalWorkflowTrace(_Frozen):
    """PRE-BATCH (no schema_version field). Validates the committed 1.0.0
    corpus and the AG reference."""

    trace_metadata: HierarchicalTraceMetadata
    team_configuration: TeamConfiguration
    workflow_summary: WorkflowSummary
    agents: Dict[str, InvocationEntry]


class HierarchicalWorkflowTraceV2(_Frozen):
    """POST-BATCH (2.0.0): unified trace_metadata (§0.6.1); the invocation
    executions inside carry query/recent_query and schema_version stamps
    (§0.6.3), which AgentTrace already accepts."""

    trace_metadata: UnifiedTraceMetadata
    team_configuration: TeamConfiguration
    workflow_summary: WorkflowSummary
    agents: Dict[str, InvocationEntry]
