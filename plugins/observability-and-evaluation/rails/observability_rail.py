"""Plugin entry point for the shared observation Rail implementation."""

from .observability.rail import ObservabilityRail as _ObservabilityRail


class ObservabilityRail(_ObservabilityRail):
    """Expose the observation lifecycle through the plugin entry point."""
