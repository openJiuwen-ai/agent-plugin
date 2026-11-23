"""Plugin entry point for the shared evaluation Rail implementation."""

from .evaluation.rail import EvaluationRail as _EvaluationRail


class EvaluationRail(_EvaluationRail):
    """Expose the evaluation lifecycle through the plugin entry point."""
