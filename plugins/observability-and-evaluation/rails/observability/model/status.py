"""Status vocabularies of the trace data model.

PRE-CHANGE STATE (SCHEMA_VERSION 1.0.0 - what this model validates):
status fields are typed as plain `str`. The pre-change vocabularies are
deliberately NOT enforced as enums, because they are per-field, partly
tool-author-defined (make_tool_result callers write their own status into
parsed_output.status), and split between the two handlers:

  workflow status (trace_metadata.status):
      success | failed | partial | no_agents_executed(hier only)
  tool-phase summary (summarize_tool_activity_status):
      sequential:   any success => success;  no-details => no_tool_call_request
      hierarchical: any failure => failed;   no-details => detected_without_call_details
  plain-text tool output (parse_structured_tool_output):
      sequential: unstructured_output      hierarchical: success

POST-BATCH STATE (2.0.0, applied in Phase 1 step 3 - §0.6.2, decision 12):
one ToolStatus vocabulary for all ports:
      any failure => failed;  no-details => no_tool_call_request;
      plain text => unstructured_output
at which point the enum below becomes the enforced field type. It is defined
now so step 3 is an edit to the field annotations, not a design session.
"""
from enum import Enum


class ToolStatus(str, Enum):
    """The converged post-batch tool-status vocabulary (§0.6.2). NOT yet
    enforced - see the module docstring."""

    SUCCESS = "success"
    FAILED = "failed"
    OK = "ok"
    MISSING_OUTPUT = "missing_output"
    UNSTRUCTURED_OUTPUT = "unstructured_output"
    NO_TOOL_CALL_REQUEST = "no_tool_call_request"


class WorkflowStatus(str, Enum):
    """Workflow-level status. Shared by both models pre- and post-batch."""

    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    NO_AGENTS_EXECUTED = "no_agents_executed"
