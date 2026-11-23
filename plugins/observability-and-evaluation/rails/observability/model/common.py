"""Blocks shared by every level of the trace data model.

TRANSCRIBED, NOT DESIGNED. Every field below is read off the three reference
artifacts in tests/golden/references/ and cross-checked against all committed
trajectories (§0.1b-c). extra="forbid" everywhere: an unexpected key is a
schema violation, not something to silently carry (acceptance criterion 1).

`model_name` / `model_provider` are wire names from the artifacts; pydantic's
protected `model_` namespace is disabled where they appear.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class TokenUsage(_Frozen):
    input: int
    output: int
    total: int


class HttpRequest(_Frozen):
    """The one HTTP round-trip an LLM request made."""

    http_url: str
    timestamp_start: str
    timestamp_end: str
    duration_seconds: float


class ToolCallDuration(_Frozen):
    timestamp_start: str
    timestamp_end: str
    duration_seconds: float


class LLMRequest(_Frozen):
    """`llm_request_N`: one model call, from the OpenAI-instrumented span."""

    step_id: str
    model_name: str
    model_provider: str
    timestamp_start: str
    timestamp_end: str
    duration_seconds: float
    response_id: str
    tokens: TokenUsage
    llm_call_post_condition: str
    http_request: HttpRequest


class ToolLLMRequest(LLMRequest):
    """A reflection LLM call on tool output.

    Two positions, one shape (§0.1b):
    - nested inside an activity as key `tool_llm_request`, step `<activity>.3`,
      NO `reflects_on_activities` (Case 1);
    - top-level as `tool_llm_request_N` WITH `reflects_on_activities`
      (Case 2 batch / Case 3 chain).
    The positional encoding is part of the interface - do not replace it with
    an explicit scope field (§0.1, withdrawn proposal).

    `reflects_on_latest_batch` appears ONLY in the AG reference's hierarchical
    invocations (same value as reflects_on_activities); our port never emits
    it. DECIDED (decision vii): the field is **dropped** by the §0.6 batch -
    it stays accepted here only so the pre-batch AG reference validates.
    """

    reflects_on_activities: Optional[List[int]] = None
    reflects_on_latest_batch: Optional[List[int]] = None


class ToolCalling(_Frozen):
    """The model's request to call a tool, plus transport facts."""

    step_id: str
    tool_call_request_count: int
    call_id: str
    tool_name: str
    tool_arguments: Dict[str, Any]
    status: str
    tool_called: bool
    tool_success: bool
    # Emitted by BOTH of our handlers; ABSENT from the AG reference's
    # hierarchical invocations. DECIDED (decision vii): **emitted**, in all
    # ports, per the §0.6 batch. Optional only so the pre-batch AG reference
    # still validates.
    tool_invocation_index: Optional[int] = None
    http_attempt_count: int
    http_url: str
    http_method: str
    http_status_code: Optional[int] = None  # null for tools with no HTTP call


class ParsedOutput(_Frozen):
    """The parsed make_tool_result envelope - or the parser's classification
    of output that was not an envelope. `status` is partly tool-author-defined
    (whatever the tool wrote into make_tool_result), so it stays `str` in the
    pre-change model - see status.py."""

    status: str
    data: Any = None
    error: Optional[Any] = None
    metadata: Any = None


class ToolCallResult(_Frozen):
    step_id: str
    tool_call_result_count: int
    call_id: str
    tool_name: str
    raw_output: str
    result_preview: str
    error: Optional[str] = None
    parsed_output: ParsedOutput
    timestamp_start: str
    timestamp_end: str
    duration_seconds: float
    http_attempts: List[Any] = Field(default_factory=list)
