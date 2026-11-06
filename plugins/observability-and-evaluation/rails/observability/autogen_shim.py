# src/AG/autogen_shim.py
"""Shims that give openJiuwen results an autogen_agentchat.base.TaskResult surface.

Class names match AutoGen's on purpose

Contract expected by the untouched observability code
-----------------------------------------------------
result.messages[i] must expose:
    .source          str   agent name; must equal an entry of `agent_names`
                           for leaf agents, and contain "MagenticOneOrchestrator"
                           for the supervisor (extract_orchestrator_initial_plan).
    .content         str for TextMessage, list[ToolCallPayload] for the two
                     tool events.
    .to_text()       str
    .timestamp       ISO-8601 str, used by extract_messages_for_agent_invocation
                     to bucket a message into one invoke_agent window.
    .models_usage    object with .prompt_tokens / .completion_tokens (or None).
"""

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Timestamp in the same format as the OTel span times we synthesize."""
    return datetime.now(timezone.utc).isoformat()


class _Usage:
    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class TextMessage:
    def __init__(self, source, content, models_usage=None, timestamp=None):
        self.source = source
        self.content = content
        self.models_usage = models_usage
        self.timestamp = timestamp or utc_now_iso()

    def to_text(self):
        return self.content if isinstance(self.content, str) else str(self.content)


class ToolCallPayload:
    """One entry inside ToolCallRequestEvent.content / ToolCallExecutionEvent.content.

    get_nested_attr_or_dict_value() reads these by attribute name, so the field
    names below are part of the observability contract.
    """

    def __init__(self, *, id="", name="", arguments="{}", content="", is_error=False):
        self.id = id
        self.call_id = id
        self.name = name
        self.arguments = arguments
        self.content = content
        self.is_error = is_error


class ToolCallRequestEvent(TextMessage):
    """content is a list[ToolCallPayload] carrying id/name/arguments."""

    def to_text(self):
        calls = self.content if isinstance(self.content, list) else []
        return "\n".join(f"{c.name}({c.arguments})" for c in calls)


class ToolCallExecutionEvent(TextMessage):
    """content is a list[ToolCallPayload] carrying call_id/name/content/is_error."""

    def to_text(self):
        calls = self.content if isinstance(self.content, list) else []
        return "\n".join(str(c.content) for c in calls)


class TaskResult:
    def __init__(self, messages, stop_reason=None):
        self.messages = messages
        self.stop_reason = stop_reason
