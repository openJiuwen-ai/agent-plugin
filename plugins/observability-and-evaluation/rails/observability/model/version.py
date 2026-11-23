"""Schema version of the agentic trace data model.

2.0.0 is the POST-BATCH shape (§0.6, decisions 12 and 13): one unified
trace_metadata for both workflow models, one converged status vocabulary
(any failure => failed; no_tool_call_request; unstructured_output), and the
hierarchical invocation-level keys converged (query/recent_query,
tool_llm_request_1, no reflects_on_latest_batch, tool_invocation_index
emitted). It is the first version stamped INTO artifacts - as the first key
of every standalone file and of trace_metadata in the merged traces.

Files WITHOUT a schema_version field are the 1.0.0 pre-batch shape: the
sequential/hierarchical trace_metadata key split, the divergent status
vocabularies, task/user_prompt at the hierarchical invocation level. The
committed pre-batch trajectories keep that shape forever (§0.3.1 principle:
artifacts are never rewritten), and the model validates both vintages.

⚠ Cross-port contract (decision 11): the 2.0.0 changes ship to MAF, AutoGen
and LangGraph together. This port emitting 2.0.0 while the siblings still
emit the pre-batch shape is the transitional state the version field exists
to make visible.
"""

SCHEMA_VERSION = "2.0.0"
