"""The agent-level trace - ONE model for both architectures.

The agent artifact is the shared level of the data model: the sequential
per-step files (agent_N_*.json), the hierarchical per-invocation files
(OrchestratorObj_N_*.json, inside their `execution` wrapper), and the agent
blocks embedded in both merged traces all carry this exact shape - measured
byte-identical across the two handlers (29 keys, §3.2) and conformant with
the AG reference agent artifact (§0.1b, zero divergence).

The artifact mixes FIXED keys (agent_metadata, task, result, OTel_traces)
with DYNAMIC ones (llm_request_N, tool_execution_activity_N,
tool_llm_request_N). Pydantic models the fixed keys as fields and validates
every extra key against the dynamic patterns - any key matching neither is a
schema violation.
"""
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from .common import (
    LLMRequest,
    ToolCallDuration,
    ToolCalling,
    ToolCallResult,
    ToolLLMRequest,
    _Frozen,
)

_LLM_REQUEST = re.compile(r"^llm_request_\d+$")
_TOOL_LLM_REQUEST = re.compile(r"^tool_llm_request_\d+$")
_TOOL_ACTIVITY = re.compile(r"^tool_execution_activity_\d+$")


class AgentMetadata(_Frozen):
    agent_name: str
    agent_id: str
    system_prompt: str
    agent_timestamp_start: str
    agent_timestamp_end: str
    duration_seconds: float
    tool_names: List[str]
    tool_call_count: int
    tool_called: bool
    llm_call_count: int
    total_tokens: int
    input_tokens: int
    output_tokens: int
    status: str


class TaskBlock(_Frozen):
    step_id: str
    user_prompt: str
    input_tokens: int


class QueryBlock(_Frozen):
    """The hierarchical per-invocation input block: `recent_query` where the
    sequential/agent-level `task` block has `user_prompt`.

    DECIDED (decision vii, 2026-08-24): the canonical key is lowercase
    **`query`**, emitted by all ports after the §0.6 batch. The AG reference
    carries it capitalized (`Query`) and our pre-batch hierarchical files
    carry `task` instead - both remain valid 1.0.0 inputs; see AgentTrace."""

    step_id: str
    recent_query: str
    input_tokens: int


class ResultBlock(_Frozen):
    step_id: str
    agent_output: str
    output_tokens: int


class OTelTracesBlock(_Frozen):
    """Raw captured spans. Span internals are OpenTelemetry's schema, not
    ours - they pass through as dicts and are not further constrained."""

    step_id: str
    Traces_of_agent: str
    Traces: List[Dict[str, Any]]


class ToolActivity(_Frozen):
    """`tool_execution_activity_N`: one tool call - request, result, timing,
    and (Case 1 only) the nested reflection on its output."""

    step_id: str
    tool_names: List[str]
    tool_call_count: int
    tool_call_duration: ToolCallDuration
    tool_calling: ToolCalling
    tool_call_result: ToolCallResult
    # Case 1: the reflection nests INSIDE the activity, bare key, step
    # <activity>.3, no reflects_on_activities. Cases 2/3 reflect top-level
    # instead (see AgentTrace).
    tool_llm_request: Optional[ToolLLMRequest] = None


class AgentTrace(BaseModel):
    """One agent's complete trace. Fixed blocks as fields; the numbered
    llm_request / tool_execution_activity / tool_llm_request blocks validated
    from model_extra.

    CROSS-PORT DIVERGENCE at the hierarchical per-invocation level, found
    during model transcription — **DECIDED 2026-08-24 (decision vii, §2.8.1),
    applied cross-port in the §0.6 batch:**

      | concept                | this port (OJW)      | AG reference          | DECIDED |
      |------------------------|----------------------|-----------------------|---------|
      | input block            | `task`/`user_prompt` | `Query`/`recent_query`| **`query`/`recent_query`** (lowercase) |
      | first top reflection   | `tool_llm_request_1` | `tool_llm_request_0`  | **`_1`** (OJW) |
      | extra reflection field | -                    | `reflects_on_latest_batch` | **dropped** |
      | tool_invocation_index  | emitted              | absent                | **emitted** |

    Until the batch lands, artifacts on disk carry the pre-change shapes, so
    this 1.0.0 model accepts exactly one of `task` (our files, and the shared
    agent level everywhere) / `Query` (the AG reference) / `query` (the
    post-batch hierarchical shape, accepted early so step 3 only changes the
    emitter).
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    # Stamped at save time from 2.0.0 on; absent in pre-batch files and in
    # the in-memory extractor output (the extractor stays pure - see
    # save_single_agent_json).
    schema_version: Optional[str] = None
    agent_metadata: AgentMetadata
    task: Optional[TaskBlock] = None
    Query: Optional[QueryBlock] = None
    query: Optional[QueryBlock] = None
    result: ResultBlock
    OTel_traces: OTelTracesBlock

    @model_validator(mode="after")
    def _validate_dynamic_blocks(self):
        input_blocks = [b for b in (self.task, self.Query, self.query) if b is not None]
        if len(input_blocks) != 1:
            raise ValueError(
                "exactly one input block must be present: task (shared agent "
                "level / pre-batch OJW hierarchical), Query (AG reference), "
                "or query (post-batch hierarchical)"
            )
        for key, value in (self.model_extra or {}).items():
            if _LLM_REQUEST.match(key):
                LLMRequest.model_validate(value)
            elif _TOOL_LLM_REQUEST.match(key):
                block = ToolLLMRequest.model_validate(value)
                if block.reflects_on_activities is None:
                    raise ValueError(
                        f"{key}: top-level reflection must carry "
                        "reflects_on_activities (§0.1b Cases 2/3)"
                    )
            elif _TOOL_ACTIVITY.match(key):
                ToolActivity.model_validate(value)
            else:
                raise ValueError(f"unexpected key in agent trace: {key!r}")
        return self

    def llm_requests(self) -> Dict[str, LLMRequest]:
        return {k: LLMRequest.model_validate(v)
                for k, v in (self.model_extra or {}).items()
                if _LLM_REQUEST.match(k)}

    def tool_activities(self) -> Dict[str, ToolActivity]:
        return {k: ToolActivity.model_validate(v)
                for k, v in (self.model_extra or {}).items()
                if _TOOL_ACTIVITY.match(k)}

    def top_level_reflections(self) -> Dict[str, ToolLLMRequest]:
        return {k: ToolLLMRequest.model_validate(v)
                for k, v in (self.model_extra or {}).items()
                if _TOOL_LLM_REQUEST.match(k)}
