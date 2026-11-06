"""OpenTelemetry setup.

`opentelemetry-instrumentation-openai` instruments the OpenAI SDK directly, so
tracing is unaffected by removing AutoGen.
"""
from __future__ import annotations

import atexit
import io

from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.openai import OpenAIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor


class _NullStream:
    """Discards span output. Keeps the exporter working without printing."""

    def write(self, data):  # noqa: D102
        return len(data)

    def flush(self):  # noqa: D102
        return None


class Observability:
    """Owns the tracer provider and the console exporter used for span capture."""

    def __init__(self, print_traces: bool = False):
        self.console_exporter = ConsoleSpanExporter()
        self.print_traces = print_traces
        self.tracer_provider: TracerProvider | None = None

    def init(self) -> None:
        # Spans go nowhere unless explicitly requested. The exporter defaults to
        # stdout, which TerminalCapture tees into the run log — that is how the
        # old pipeline produced a 2.1 MB log for 49 KB of useful output.
        if not self.print_traces:
            self.console_exporter.out = _NullStream()

        self.tracer_provider = TracerProvider(
            resource=Resource.create({"service.name": "ojw_evaluation"})
        )
        self.tracer_provider.add_span_processor(
            SimpleSpanProcessor(self.console_exporter)
        )
        trace.set_tracer_provider(self.tracer_provider)

        OpenAIInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        atexit.register(self.tracer_provider.shutdown)

    def run_span(self, run_id: int):
        """Parent span for a whole run.

        Used as a context manager so it actually wraps the work — the old
        `start_tracer` opened and closed the span immediately, wrapping nothing
        but a print.
        """
        tracer = trace.get_tracer("ojw_evaluation")
        return tracer.start_as_current_span(f"run-{run_id}")

    def capture(self):
        """Redirect exporter output into a buffer for the duration of a block."""
        return _ExporterCapture(self)

    def force_flush(self) -> None:
        if self.tracer_provider:
            self.tracer_provider.force_flush()

    def shutdown(self) -> None:
        if self.tracer_provider:
            self.tracer_provider.shutdown()


class _ExporterCapture:
    def __init__(self, obs: Observability):
        self._obs = obs
        self._buf = io.StringIO()
        self._prev = None

    def __enter__(self) -> io.StringIO:
        self._prev = self._obs.console_exporter.out
        self._obs.console_exporter.out = self._buf
        return self._buf

    def __exit__(self, *exc):
        self._obs.force_flush()
        self._obs.console_exporter.out = self._prev
        return False
