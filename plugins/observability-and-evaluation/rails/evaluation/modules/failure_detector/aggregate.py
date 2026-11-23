"""Merge the per-category specialist results into one per-step object.

This stage never existed. The old code appended seven raw model responses into a
markdown file separated by '---'; there was no join, and `failure_codes` was
produced nowhere in the repo.

v1 is a PURE JOIN. It deliberately does not deduplicate.
----------------------------------------------------------------------
Measured on a real run: of the 7 (step_id, code) pairs occurring more than once,
all 7 carried *different* evidence quotes and none were identical. They are
distinct occurrences of the same failure type within one step, not one failure
reported twice. Collapsing them would discard 9 real citations, suppress zero
noise, and erase frequency information.

Destructive transforms happen late: keeping everything is reversible, dedup is
not. Whether to collapse is an evaluation-informed decision for the scoring
layer, not a pipeline default — see `dedup()` at the bottom, which is opt-in.
"""
from __future__ import annotations

from collections import OrderedDict

from .schema import CategoryResult, TaxonomyFailure, TaxonomyStep


def aggregate(
    category_results: list[CategoryResult],
    step_order: list[str] | None = None,
) -> list[TaxonomyStep]:
    """Join per-category annotations on step_id, preserving every failure.

    Args:
        category_results: one entry per taxonomy-category specialist.
        step_order: authoritative step ordering from the trajectory. Steps not
            listed are appended in first-seen order, so a model inventing a
            step_id is visible rather than silently dropped.
    """
    merged: OrderedDict[str, list[TaxonomyFailure]] = OrderedDict()
    for step_id in step_order or []:
        merged[step_id] = []

    for result in category_results:
        for step in result.annotations:
            bucket = merged.setdefault(step.step_id, [])
            for failure in step.failures:
                # Record which specialist reported it; a step's failures now mix
                # reports from up to 7 categories.
                if not failure.source_category:
                    failure = failure.model_copy(
                        update={"source_category": result.category}
                    )
                bucket.append(failure)

    return [
        TaxonomyStep(
            step_id=step_id,
            failure_codes=_distinct_codes(failures),
            failures=failures,
        )
        for step_id, failures in merged.items()
    ]


def _distinct_codes(failures: list[TaxonomyFailure]) -> list[str]:
    """Distinct codes, first-seen order.

    A projection, not a deletion — `failures` still holds every occurrence, so
    len(failure_codes) <= len(failures) is expected and correct.
    """
    seen: list[str] = []
    for failure in failures:
        if failure.code and failure.code not in seen:
            seen.append(failure.code)
    return seen


def dedup(steps: list[TaxonomyStep], policy: str = "none") -> list[TaxonomyStep]:
    """Opt-in collapsing, for the scoring layer to experiment with.

    Policies:
        none    - return unchanged (the v1 default)
        exact   - collapse only when code AND evidence match (true repetition)
        by_code - collapse all occurrences sharing a code (LOSSY: discards
                  distinct evidence; measured to remove real citations)
    """
    if policy == "none":
        return steps
    if policy not in {"exact", "by_code"}:
        raise ValueError(f"Unknown dedup policy {policy!r}")

    out: list[TaxonomyStep] = []
    for step in steps:
        kept: list[TaxonomyFailure] = []
        seen: set[tuple] = set()
        for failure in step.failures:
            key = (
                (failure.code, failure.evidence.strip())
                if policy == "exact"
                else (failure.code,)
            )
            if key in seen:
                continue
            seen.add(key)
            kept.append(failure)
        out.append(
            TaxonomyStep(
                step_id=step.step_id,
                failure_codes=_distinct_codes(kept),
                failures=kept,
            )
        )
    return out
