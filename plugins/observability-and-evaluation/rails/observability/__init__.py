"""Rail-based observation and sequential/hierarchical workflow exports."""

from importlib import import_module

__all__ = ["ObservabilityRail", "observe", "unobserve", "merge_workflow", "merge_hierarchical_workflow"]


def __getattr__(name):
    modules = {"ObservabilityRail": ".rail", "observe": ".decorator", "unobserve": ".decorator",
               "merge_workflow": ".workflow", "merge_hierarchical_workflow": ".hierarchical_workflow"}
    if name in modules:
        return getattr(import_module(modules[name], __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
