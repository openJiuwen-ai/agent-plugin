"""The agentic trace data model, as pydantic - transcribed from the
reference artifacts (§0.1b-c), not designed.

Two workflow models (hierarchical adds team_configuration and
workflow_summary), one shared agent level. SCHEMA_VERSION 1.0.0 is the
pre-change shape; the §0.6 cross-port batch bumps it.
"""
from .agent import AgentTrace
from .hierarchical import (
    HierarchicalWorkflowTrace,
    HierarchicalWorkflowTraceV2,
    InitializationExecution,
    SkippedAgentsArtifact,
)
from .sequential import (
    SequentialWorkflowTrace,
    SequentialWorkflowTraceV2,
)
from .status import ToolStatus, WorkflowStatus
from .version import SCHEMA_VERSION

__all__ = [
    "AgentTrace",
    "SequentialWorkflowTrace",
    "SequentialWorkflowTraceV2",
    "HierarchicalWorkflowTrace",
    "HierarchicalWorkflowTraceV2",
    "InitializationExecution",
    "SkippedAgentsArtifact",
    "ToolStatus",
    "WorkflowStatus",
    "SCHEMA_VERSION",
]
