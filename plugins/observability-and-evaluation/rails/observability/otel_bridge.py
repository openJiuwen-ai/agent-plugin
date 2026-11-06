"""Route instrumented SDK spans to their owning invocation without replacing exporters."""
from threading import RLock
from weakref import ref

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.resources import Resource

_lock = RLock()
_provider = None


class InvocationSpanProcessor(SpanProcessor):
    def __init__(self, current):
        self.current = current
        self.owners = {}
        self.lock = RLock()

    def on_start(self, span, parent_context=None):
        owner = self.current()
        if owner is not None and not owner.closed:
            with self.lock:
                self.owners[span.context.span_id] = ref(owner)

    def on_end(self, span):
        with self.lock:
            owner_ref = self.owners.pop(span.context.span_id, None)
        owner = owner_ref() if owner_ref else None
        if owner is not None and not owner.closed:
            if span.instrumentation_scope.name == "ojw.decorator.prototype":
                owner.exporter.export((span,))
            else:
                owner.native_spans.append(span)

    def release(self, owner):
        with self.lock:
            self.owners = {key: value for key, value in self.owners.items()
                           if value() is not None and value() is not owner}

    def shutdown(self):
        with self.lock:
            self.owners.clear()

    def force_flush(self, timeout_millis=30000):
        return True


_processor = None


def enable_bridge(current):
    """Enable installed adapters once. Never replace an application's SDK provider."""
    global _provider, _processor
    # Resolve optional adapters before changing the application's provider.
    from opentelemetry.instrumentation.openai import OpenAIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    with _lock:
        provider = trace.get_tracer_provider()
        if provider is _provider:
            return
        if isinstance(provider, trace.ProxyTracerProvider):
            trace.set_tracer_provider(TracerProvider(resource=Resource.create({"service.name": "openJiuwen"})))
            provider = trace.get_tracer_provider()
        if not hasattr(provider, "add_span_processor"):
            raise TypeError("observe requires an OpenTelemetry provider supporting add_span_processor")
        _processor = InvocationSpanProcessor(current)
        provider.add_span_processor(_processor)
        for adapter in (OpenAIInstrumentor(), HTTPXClientInstrumentor(), RequestsInstrumentor()):
            if not adapter.is_instrumented_by_opentelemetry:
                adapter.instrument(tracer_provider=provider)
        _provider = provider


def release(owner):
    if _processor is not None:
        _processor.release(owner)
