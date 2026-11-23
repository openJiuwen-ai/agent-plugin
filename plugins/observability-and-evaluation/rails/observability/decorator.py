"""Observe native OpenJiuwen invoke calls without owning agent construction.

Only pure legacy JSON extraction/model helpers are reused. There is no dependency
on the legacy BaseAgent, pipeline, driver, or global OpenTelemetry setup.
"""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import fcntl
import os
import logging
from pathlib import Path
import re
from uuid import uuid4

from openjiuwen.core.single_agent.rail import AgentRail
from opentelemetry import trace, context as context_api
from opentelemetry.context import Context
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode, SpanKind

from .autogen_shim import (
    _Usage, TaskResult, TextMessage, ToolCallExecutionEvent, ToolCallPayload,
    ToolCallRequestEvent,
)
from .emit.save import save_single_agent_json
from .artifacts import reserve_artifact
from .emit.steps import StepCounter
from .emit.tools import make_tool_result
from .observability.json.sequential_json_handler import (
    extract_agent_json_from_otel,
)

from .otel_bridge import enable_bridge, release
from .model_enrichment import enrich_model_spans

_active = ContextVar("ojw_observation", default=None)
_parent_span = ContextVar("ojw_observation_parent_span", default=None)
_logger = logging.getLogger(__name__)


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _plain(value):
    """Serialize native Pydantic values without inspecting client configuration."""
    if inspect.isclass(value) and hasattr(value, "model_json_schema"):
        return value.model_json_schema()
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _message_details(message):
    # Keep tool linkage and multimodal content; never dump arbitrary metadata.
    return {key: _plain(_value(message, key))
            for key in ("role", "content", "name", "tool_call_id", "tool_calls")
            if _value(message, key) is not None}


def _capture_model_details(span, inputs, response, model_config):
    messages = [_message_details(message) for message in getattr(inputs, "messages", None) or []]
    span.set_attribute("gen_ai.input.messages", _text(messages))
    for index, message in enumerate(messages):
        for key in ("name", "tool_call_id", "tool_calls"):
            if key in message:
                span.set_attribute(f"gen_ai.prompt.{index}.{key}", _text(message[key]))
    tools = getattr(inputs, "tools", None)
    if tools is not None:
        definitions_list = []
        for tool in tools:
            definition = _plain(tool)
            if isinstance(definition, dict) and "function" not in definition and "name" in definition:
                definition = {"type": definition.get("type", "function"), "function": {
                    key: definition[key] for key in ("name", "description", "parameters") if key in definition
                }}
            definitions_list.append(definition)
        definitions = _text(definitions_list)
        span.set_attribute("gen_ai.request.tools", definitions)
        span.set_attribute("gen_ai.tool.definitions", definitions)
    # These are configured parameters, not a claim about transport overrides.
    # SDK spans retain the actual request values independently.
    for key in ("temperature", "top_p", "max_tokens", "stop", "presence_penalty",
                "frequency_penalty", "seed", "reasoning_effort"):
        value = _value(model_config, key)
        if value is not None:
            span.set_attribute(f"gen_ai.request.{key}", value if isinstance(value, (str, int, float, bool)) else _text(value))
    if response is None:
        return
    output = _message_details(response)
    for key in ("finish_reason", "reasoning_content"):
        value = _value(response, key)
        if value is not None:
            output[key] = value
    span.set_attribute("gen_ai.output.messages", _text([output]))
    usage = _value(response, "usage_metadata")
    for target, sources in (
        ("reasoning_tokens", ("reasoning_tokens",)),
        ("cache_read.input_tokens", ("cache_tokens", "cached_tokens")),
    ):
        for source in sources:
            value = _value(usage, source)
            if value is not None:
                span.set_attribute(f"gen_ai.usage.{target}", value)
                break
    actual_model = _value(usage, "model_name")
    if actual_model:
        span.set_attribute("gen_ai.response.model", actual_model)


class _InvocationRail(AgentRail):
    """A collector owned by one invocation, with explicit span parenting.

    ContextVar filtering prevents another invocation's registered rail from
    collecting these callbacks. SDK spans are routed separately by their invocation owner.
    """

    priority = 90

    def __init__(self, agent, inputs):
        self.agent = agent
        self.name = agent.card.name
        self.final_output = ""
        self.stop_reason = None
        self.usage = None
        self.native_spans = []
        self.closed = False
        self.handoff = None
        self.naming = None
        self.context_tokens = {}
        self.query = inputs.get("query", "") if isinstance(inputs, dict) else inputs
        self.messages = []
        self.events = []
        self.open_spans = {}
        self.tool_tokens = {}
        self.exporter = InMemorySpanExporter()
        enable_bridge(lambda: _active.get())
        self.tracer = trace.get_tracer("ojw.decorator.prototype")
        parent = _parent_span.get()
        parent_context = trace.set_span_in_context(parent, Context()) if parent is not None else context_api.get_current()
        owner_token = _active.set(self)
        try:
            self.root = self.tracer.start_span(
                f"invoke_agent {self.name}", context=parent_context, kind=SpanKind.CLIENT,
                attributes={"gen_ai.operation.name": "invoke_agent",
                            "gen_ai.system": "openjiuwen",
                            "gen_ai.agent.description": getattr(agent.card, "description", "") or "",
                            "gen_ai.agent.name": self.name,
                            "gen_ai.agent.id": agent.card.id,
                            "observability.source": "openjiuwen.rail"},
            )
        finally:
            _active.reset(owner_token)
        self.parent = trace.set_span_in_context(self.root, Context())
        self.event("agent_start", query=self.query)

    def event(self, kind, **data):
        self.events.append({"timestamp": datetime.now(timezone.utc).isoformat(),
                            "agent": self.name, "event": kind, **data})

    async def after_invoke(self, ctx):
        if _active.get() is self:
            # Priority 90 runs before EvaluationRail clears the invocation
            # binding. Retain its file address until the outer wrapper saves
            # the trace, including when observing a standalone ReActAgent.
            variable = getattr(self.agent, "_ojw_trace_handoff", None)
            self.handoff = variable.get() if variable is not None else None
            naming = getattr(self.agent, '_ojw_trace_naming', None)
            if naming is not None:
                self.naming = naming.get() or {'kind': 'unlinked'}

    def start(self, kind, ctx, name):
        self.open_spans[(kind, id(ctx))] = self.tracer.start_span(
            name, context=self.parent,
            attributes={"observability.source": "openjiuwen.rail"},
        )

        self.context_tokens[(kind, id(ctx))] = context_api.attach(trace.set_span_in_context(self.open_spans[(kind, id(ctx))]))

    def end(self, kind, ctx):
        span = self.open_spans.pop((kind, id(ctx)), None)
        if span:
            if ctx.exception is not None:
                span.record_exception(ctx.exception)
                span.set_status(Status(StatusCode.ERROR, str(ctx.exception)))
            span.end()
        token = self.context_tokens.pop((kind, id(ctx)), None)
        if token is not None:
            context_api.detach(token)

    async def before_model_call(self, ctx):
        if _active.get() is self:
            # Legacy extractor recognizes this name. This measures the native
            # model lifecycle, not an SDK/HTTP request; provenance is explicit.
            self.start("model", ctx, "openai.chat")
            self.event("model_start")

    async def after_model_call(self, ctx):
        if _active.get() is not self:
            return
        span = self.open_spans.get(("model", id(ctx)))
        if span is None:
            return
        response = getattr(ctx.inputs, "response", None)
        for i, message in enumerate(getattr(ctx.inputs, "messages", None) or []):
            span.set_attribute(f"gen_ai.prompt.{i}.role", _value(message, "role", ""))
            span.set_attribute(f"gen_ai.prompt.{i}.content", _text(_value(message, "content", "")))
        config = getattr(self.agent, "_config", None)
        model_config = getattr(config, "model_config_obj", None)
        _capture_model_details(span, ctx.inputs, response, model_config)
        span.set_attribute("gen_ai.request.model", getattr(model_config, "model_name", "") or getattr(config, "model_name", ""))
        client_config = getattr(config, "model_client_config", None)
        provider = getattr(client_config, "client_provider", None) or getattr(config, "model_provider", "") or "unknown"
        span.set_attribute("gen_ai.system", getattr(provider, "value", provider))
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            incoming = int(_value(usage, "input_tokens", 0) or 0)
            outgoing = int(_value(usage, "output_tokens", 0) or 0)
            total = int(_value(usage, "total_tokens", 0) or incoming + outgoing)
            for key, value in (("input_tokens", incoming), ("output_tokens", outgoing), ("total_tokens", total)):
                span.set_attribute(f"gen_ai.usage.{key}", value)
            self.usage = _Usage(incoming + (self.usage.prompt_tokens if self.usage else 0),
                                outgoing + (self.usage.completion_tokens if self.usage else 0))
        reasoning = _value(response, "reasoning_content")
        if reasoning:
            span.set_attribute("gen_ai.completion.0.reasoning_content", str(reasoning))
        span.set_attribute("observability.usage_available", usage is not None)
        calls = getattr(response, "tool_calls", None) or []
        finish_reason = getattr(response, "finish_reason", None)
        if ctx.exception is not None:
            finish_reason = "error"
        elif finish_reason in (None, "", "null"):
            finish_reason = "tool_calls" if calls else "stop"
        finish_reason = {"tool_call": "tool_calls", "function_call": "tool_calls"}.get(finish_reason, finish_reason)
        span.set_attribute("gen_ai.completion.0.finish_reason", finish_reason)
        span.set_attribute("gen_ai.completion.0.content", _text(getattr(response, "content", "")))
        payloads = []
        for i, call in enumerate(calls):
            for key in ("id", "name", "arguments"):
                span.set_attribute(f"gen_ai.completion.0.tool_calls.{i}.{key}", _text(_value(call, key, "{}" if key == "arguments" else "") or ("{}" if key == "arguments" else "")))
            payloads.append(ToolCallPayload(id=_value(call, "id", "") or "", name=_value(call, "name", "") or "", arguments=_text(_value(call, "arguments", "{}") or "{}")))
        if payloads:
            content = str(_value(response, "content", "") or "")
            content = re.sub(r"</?(?:tool_call|think)>|/?ool_call>", "", content).strip()
            if content:
                self.messages.append(TextMessage(source=self.name, content=content))
            self.messages.append(ToolCallRequestEvent(source=self.name, content=payloads))
        self.event("model_end", response=_value(response, "content"),
                   tool_calls=[{"id": p.id, "name": p.name, "arguments": p.arguments} for p in payloads],
                   error=str(ctx.exception) if ctx.exception else None)
        self.end("model", ctx)

    def tool_identity(self, ctx):
        call = getattr(ctx.inputs, "tool_call", None)
        return (getattr(ctx.inputs, "tool_name", "") or _value(call, "name", "") or "unknown_tool",
                _value(call, "id", "") or "")

    async def before_tool_call(self, ctx):
        if _active.get() is self:
            name, call_id = self.tool_identity(ctx)
            self.start("tool", ctx, f"execute_tool {name}")
            span = self.open_spans[("tool", id(ctx))]
            span.set_attributes({"gen_ai.operation.name": "execute_tool",
                                 "gen_ai.system": "openjiuwen", "gen_ai.tool.name": name,
                                 "gen_ai.tool.call.id": call_id})
            self.tool_tokens[id(ctx)] = _parent_span.set(span)
            self.event("tool_start", tool=name, arguments=getattr(ctx.inputs, "tool_args", None))

    async def after_tool_call(self, ctx):
        if _active.get() is not self:
            return
        name, call_id = self.tool_identity(ctx)
        raw = getattr(ctx.inputs, "tool_result", None)
        if raw is None:
            raw = getattr(getattr(ctx.inputs, "tool_msg", None), "content", "")
        error = str(ctx.exception) if ctx.exception is not None else None
        parsed = raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                pass
        if isinstance(parsed, dict) and "tool_success" in parsed:
            # Preserve the application's envelope and extensions; never mutate it.
            envelope = dict(parsed)
            if error is not None:
                envelope.update(tool_success=False, status="failed", error=error)
            failed = envelope.get("tool_success") is False or envelope.get("status") in {"failed", "http_error", "timeout", "missing_output"}
            if failed:
                envelope["tool_success"] = False
            content = _text(envelope)
        else:
            failed = error is not None or (isinstance(parsed, dict) and parsed.get("result_type") == "error")
            content = make_tool_result(tool_called=True, tool_success=not failed,
                                       status="failed" if failed else "success", data=raw,
                                       error=error or (str(parsed.get("output", "")) if failed else None))
        self.messages.append(ToolCallExecutionEvent(source=self.name, content=[
            ToolCallPayload(id=call_id, name=name, content=content, is_error=failed)
        ]))
        self.event("tool_end", tool=name, result=raw, error=error, tool_success=not failed)
        try:
            span = self.open_spans.get(("tool", id(ctx)))
            if failed and span:
                span.set_status(Status(StatusCode.ERROR, error or "Tool returned failure"))
            self.end("tool", ctx)
        finally:
            token = self.tool_tokens.pop(id(ctx), None)
            if token is not None:
                _parent_span.reset(token)

    def finish(self, result, error, output_dir):
        try:
            for span in self.open_spans.values():
                if error is not None:
                    span.record_exception(error)
                    span.set_status(Status(StatusCode.ERROR, str(error)))
                span.end()
            self.open_spans.clear()
            failed = error is not None or (isinstance(result, dict) and result.get("result_type") == "error")
            if failed:
                self.root.set_status(Status(StatusCode.ERROR, str(error or result)))
                if error is not None:
                    self.root.record_exception(error)
            output = result.get("output", "") if isinstance(result, dict) else result
            self.messages.append(TextMessage(source=self.name, content="" if output is None else _text(output), models_usage=self.usage))
            self.final_output = "" if output is None else _text(output)
            self.stop_reason = result.get("result_type") if isinstance(result, dict) else None
            self.root.end()
            self.closed = True
            local_spans = list(self.exporter.get_finished_spans())
            raw_local = [json.loads(span.to_json()) for span in local_spans]
            raw_native = [json.loads(span.to_json()) for span in self.native_spans]
            extraction_spans = enrich_model_spans(raw_local, raw_native)
            spans = "\n".join(json.dumps(span) for span in extraction_spans)
            model_attrs = [span.get("attributes", {}) for span in extraction_spans
                           if span.get("name") == "openai.chat"]
            if any(attrs.get("observability.usage_available") for attrs in model_attrs):
                self.usage = _Usage(
                    sum(attrs.get("gen_ai.usage.input_tokens", 0) for attrs in model_attrs),
                    sum(attrs.get("gen_ai.usage.output_tokens", 0) for attrs in model_attrs),
                )
                self.messages[-1].models_usage = self.usage
            artifact = extract_agent_json_from_otel(
                otel_traces=spans, counter=StepCounter(),
                result=TaskResult(self.messages, stop_reason=self.stop_reason), agent_name=self.name,
                fallback_user_prompt=_text(self.query),
            )
            # SDK model spans are raw evidence, not additional model calls.
            artifact["OTel_traces"]["Traces"] = raw_local + raw_native
            artifact["OTel_traces"]["Traces"].sort(key=lambda s: s["start_time"])
            # Agent-local identity, not a workflow ordinal invented by an observer.
            artifact["agent_metadata"]["agent_id"] = self.agent.card.id
            if failed:
                artifact["agent_metadata"]["status"] = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            self.event("agent_end", result=result, error=str(error) if error is not None else None)
            directory = Path(output_dir).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9_-]", "_", self.name) or "agent"
            if self.naming is not None:
                reserved = reserve_artifact(directory, name, self.naming)
                stem = reserved.stem
            else:
                stem = f"agent_{name}_{uuid4().hex}"
            saved_path = save_single_agent_json(artifact, str(directory / f"{stem}.json"))
            handoff = self.handoff
            if handoff is not None:
                # Shared state is a per-invocation file in the session workspace.
                # The ContextVar transports only its address, not another Rail.
                try:
                    handoff.parent.mkdir(parents=True, exist_ok=True)
                    with handoff.with_suffix(".lock").open("a") as lock:
                        os.chmod(lock.name, 0o600)
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        temporary = handoff.with_name(handoff.name + "." + uuid4().hex + ".tmp")
                        record = json.loads(handoff.read_text(encoding="utf-8")) if handoff.is_file() else {}
                        paths = record.get("source_paths") or ([record["source_path"]] if record.get("source_path") else [])
                        if saved_path not in paths:
                            paths.append(saved_path)
                        record.update(source_path=saved_path, source_paths=paths)
                        temporary.touch(mode=0o600)
                        try:
                            temporary.write_text(json.dumps(record), encoding="utf-8")
                            temporary.replace(handoff)
                        finally:
                            temporary.unlink(missing_ok=True)
                except OSError:
                    _logger.warning("Could not publish evaluation trace reference")
            transcript = {"source": self.name,
                          "final_output": self.final_output, "stop_reason": self.stop_reason,
                          "messages": [{"type": type(m).__name__, **vars(m)} for m in self.messages]}
            with (directory / f"{stem}.transcript.jsonl").open("x", encoding="utf-8") as stream:
                json.dump(transcript, stream, ensure_ascii=False, default=lambda value: vars(value))
                stream.write("\n")
            with (directory / f"{stem}.events.jsonl").open("x", encoding="utf-8") as stream:
                for event in self.events:
                    stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        finally:
            self.closed = True
            release(self)
            self.exporter.shutdown()


async def _run_observed(agent, original, output_dir, *args, **kwargs):
    inputs = args[0] if args else kwargs.get("inputs")
    rail = _InvocationRail(agent, inputs)
    token = _active.set(rail)
    parent_token = _parent_span.set(rail.root)
    otel_token = context_api.attach(trace.set_span_in_context(rail.root))
    result = error = None
    registered = False
    try:
        await agent.register_rail(rail)
        registered = True
        result = await original(*args, **kwargs)
        return result
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            if registered:
                await agent.unregister_rail(rail)
        except Exception:
            _logger.exception("Could not unregister observation rail for %s", agent.card.name)
        finally:
            context_api.detach(otel_token)
            _parent_span.reset(parent_token)
            _active.reset(token)
            try:
                rail.finish(result, error, output_dir)
            except Exception:
                _logger.exception("Could not save observation for %s", agent.card.name)


def observe(agent, output_dir=None, force_non_streaming=False):
    """Observe an existing agent in place and return that same instance.

    output_dir is required. Usage: observe(agent, output_dir="outputs").
    No await or assignment is required.
    Only this instance's invoke method is wrapped; its class, configuration,
    tools, and native return objects stay intact. Repeating with the same output
    directory and options is a no-op; conflicting settings raise ValueError.

    Transcripts use the agent card name. Tool calls always emit tool spans.

    force_non_streaming=True overrides the invocation's _streaming flag with
    False. The default leaves the caller's flag unchanged. This option targets
    agents supporting the internal _streaming keyword, such as ReActAgent.

    Scope: async invoke(), not stream(). SDK/HTTP instrumentation is enabled once, preserving an existing SDK provider.
    """
    if output_dir is None:
        raise ValueError("observe requires an explicit output_dir; use ObservabilityRail for session workspace defaults")
    directory = Path(output_dir).expanduser().resolve()

    if inspect.isclass(agent):
        raise TypeError("observe(agent) requires an agent instance, not a class")

    original = getattr(agent, "invoke", None)
    if not inspect.iscoroutinefunction(original):
        raise TypeError("observe(agent) requires an instance with async invoke()")
    if getattr(original, "__ojw_observed__", False):
        if (
            original.__ojw_output_dir__ != directory
            or getattr(original, "__ojw_force_non_streaming__", False) != force_non_streaming
        ):
            raise ValueError("Agent is already observed with a different output directory or observation options")
        return agent
    for method in ("register_rail", "unregister_rail"):
        if not inspect.iscoroutinefunction(getattr(agent, method, None)):
            raise TypeError(f"observe(agent) requires async {method}()")
    if getattr(agent, "card", None) is None:
        raise TypeError("observe(agent) requires an agent card")

    enable_bridge(lambda: _active.get())

    had_instance_invoke = "invoke" in vars(agent)
    instance_invoke = vars(agent).get("invoke")
    enabled = True

    @wraps(original)
    async def invoke(*args, **kwargs):
        if not enabled:
            return await original(*args, **kwargs)
        if force_non_streaming:
            kwargs["_streaming"] = False
        return await _run_observed(agent, original, directory, *args, **kwargs)

    invoke.__ojw_observed__ = True
    invoke.__ojw_output_dir__ = directory
    invoke.__ojw_force_non_streaming__ = force_non_streaming

    def detach():
        nonlocal enabled
        if agent.invoke is not invoke:
            raise ValueError("Cannot unobserve: another wrapper has replaced agent.invoke")
        if had_instance_invoke:
            agent.invoke = instance_invoke
        else:
            del agent.invoke
        enabled = False

    invoke.__ojw_unobserve__ = detach
    # This closure wraps the already bound method. Assigning on the instance
    # leaves its class and all sibling instances untouched.
    agent.invoke = invoke
    return agent


def unobserve(agent):
    """Restore invoke() and return the same agent; repeated calls are a no-op.

    Already running observations finish normally, including artifact export.
    Retained references to the detached wrapper call the original method without
    observing or forcing non-streaming. Shared OpenTelemetry instrumentation is
    left installed because other agents and the host may still depend on it.

    Refuse to remove an observation hidden beneath another wrapper rather than
    overwrite that wrapper. Its owner must remove it first.
    """
    if inspect.isclass(agent):
        raise TypeError("unobserve(agent) requires an agent instance, not a class")
    current = getattr(agent, "invoke", None)
    if not getattr(current, "__ojw_observed__", False):
        return agent
    detach = getattr(current, "__ojw_unobserve__", None)
    if detach is None:
        raise ValueError("Observation wrapper does not support unobserve; recreate the agent")
    detach()
    return agent
