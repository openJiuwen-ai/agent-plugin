"""Minimal structured reporting for completed screening results."""

from __future__ import annotations

from typing import Any

from screening.domain import ScreeningResult


def generate_screening_report(result: ScreeningResult) -> dict[str, Any]:
    """Return the validated result as a JSON-compatible report payload."""

    return result.model_dump(mode="json")
