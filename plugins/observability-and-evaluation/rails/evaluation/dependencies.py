"""Check evaluator dependencies at use time, before launching work or writing state."""

from importlib import import_module


class EvaluationDependencyError(RuntimeError):
    """The evaluator cannot run in the current Python environment."""


def require_evaluation_dependencies():
    for name in ("openai", "pydantic", "opentelemetry.sdk.trace",
                 "opentelemetry.instrumentation.openai", "opentelemetry.instrumentation.httpx"):
        try:
            import_module(name)
        except ImportError as exc:
            raise EvaluationDependencyError(
                f"Evaluation unavailable: dependency {exc.name or name} could not load. "
                "Install the plugin requirements.txt in the agent's Python environment."
            ) from exc
