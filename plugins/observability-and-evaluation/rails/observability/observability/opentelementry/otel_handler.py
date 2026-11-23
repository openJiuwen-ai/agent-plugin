"""OpenTelemetry setup for openJiuwen runs. It creates the tracer provider, console exporter,
and instrumentations used later to build structured trace JSON."""
import atexit
from opentelemetry import trace
from opentelemetry.instrumentation.openai import OpenAIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, ConsoleSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor


"""
    Set up the global OpenTelemetry tracer. Spans are written to a console
    exporter, captured into an in-memory buffer during each run, and later
    parsed as JSON. It also instruments httpx, the OpenAI client, and the
    requests library so their calls are traced.
"""
class Observability:
    """Owns OpenTelemetry tracing for a workflow run.

    The object keeps the ConsoleSpanExporter available so other helpers can temporarily redirect it into buffers and later parse those exported spans into JSON.
    """
    def __init__(self, ):
        """Create the console exporter and default trace-printing flag."""
        self.console_exporter = ConsoleSpanExporter()
        self.print_traces = True


    # OpenTelemetry setup
    def init_observability(self, ) -> TracerProvider:
        """Configure the global tracer provider and instrument OpenAI, HTTPX, and Requests.

        The console exporter receives every span, and shutdown is registered with atexit so trace resources are cleaned up when the process exits.

        SERVICE.NAME IS NOT A FROZEN PATTERN NAME.
          It was "Autogen", and it is stamped onto the OTel Resource, so it
          reached `resource.attributes` on EVERY span in every trajectory this
          repository produced - while `trace_metadata.framework` in the same
          file said "OJW". The two contradicted each other, and framework is
          the field downstream failure-diagnosis tooling attributes on.

          This is a different category from `MagenticOneGroupChat`,
          `magneticOneOrchestrator_actions_count` and the other AutoGen-derived
          names in the trace, which are deliberately identical across the three
          ports and must stay verbatim. `service.name` is an OpenTelemetry
          semantic-convention field meaning "the service that emitted this
          span" - a fact about the deployment, not a shared pattern name. Here
          that service is openJiuwen.
        """
        self.tracer_provider = TracerProvider(resource=Resource.create({"service.name": "openJiuwen"}))
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.console_exporter))
        trace.set_tracer_provider(self.tracer_provider)

        # Your existing instrumentation
        OpenAIInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()

        #  Add these (covers OpenAI SDK + Serper requests)
        #HTTPXClientInstrumentor().instrument()
        RequestsInstrumentor().instrument()
        atexit.register(self.tracer_provider.shutdown)

    def start_tracer(self, run_id):
        """Create a named top-level span for the current run id."""
        tracer = trace.get_tracer("AutoGen")
        with tracer.start_as_current_span(f"run-{run_id}-span"):
            print(f"[OTEL] span created for run {run_id}")

    def force_flush(self, ):
        """Flush pending spans from the tracer provider into the configured exporter."""
        self.tracer_provider.force_flush()

    def shutdown(self, ):
        """Shut down the tracer provider at the end of all runs."""
        self.tracer_provider.shutdown()
