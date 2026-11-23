"""Evaluation modules over agentic structured trajectories."""


def __getattr__(name):
    if name == "EvaluationRail":
        from .rail import EvaluationRail
        return EvaluationRail
    if name == "open_evaluation_ui":
        from .workflow import open_evaluation_ui
        return open_evaluation_ui
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
