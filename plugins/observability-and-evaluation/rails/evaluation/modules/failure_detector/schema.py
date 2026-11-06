"""Output contracts for the failure detector.

TWO contracts, not three: per the design doc, zero-shot emits free-text labels
with no taxonomy codes, while taxonomy-guided emits the code-bound form. Few-shot
reuses the taxonomy-guided contract verbatim.

Three field choices come from measured output rather than taste:

* ``step_id`` is a **string** — real ids include "3.1" and "8.2". An int would
  mangle nested steps, and the spec's "unchanged from input" is load-bearing.
* ``ambiguous`` is a **bool**, not an "[AMBIGUOUS] " prefix on ``failure_type``.
  13 of 58 failures carried the prefix; in-band it splits one taxonomy name into
  two apparent types.
* ``evidence`` is a plain **str**, 1:1 with what the model emits, because
  aggregation does not merge (see aggregate.py).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

AMBIGUOUS_PREFIX = "[AMBIGUOUS]"


def _split_ambiguous(failure_type: str) -> tuple[str, bool]:
    """Strip the prompt's '[AMBIGUOUS] ' prefix into a separate flag."""
    text = (failure_type or "").strip()
    if text.upper().startswith(AMBIGUOUS_PREFIX):
        return text[len(AMBIGUOUS_PREFIX):].strip(), True
    return text, False


class _FailureBase(BaseModel):
    failure_type: str
    ambiguous: bool = False
    evidence: str = ""
    rationale: str = ""

    @model_validator(mode="before")
    @classmethod
    def _lift_ambiguous_prefix(cls, data: Any) -> Any:
        """Move '[AMBIGUOUS] ' out of failure_type and into the boolean field.

        The prompt still asks the model for the prefix; normalising it here keeps
        failure_type a clean taxonomy name so grouping and counting work.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("failure_type")
        if isinstance(raw, str):
            name, flagged = _split_ambiguous(raw)
            data = {**data, "failure_type": name}
            if flagged:
                data["ambiguous"] = True
        return data


class ZeroShotFailure(_FailureBase):
    """Annotator 1: free-text label, no taxonomy binding."""


class TaxonomyFailure(_FailureBase):
    """Annotator 2/3: bound to a taxonomy entry."""

    code: str = ""
    category: str = ""
    sub_category: str = ""
    source_category: str = ""


class ZeroShotStep(BaseModel):
    step_id: str
    failures: list[ZeroShotFailure] = Field(default_factory=list)

    @field_validator("step_id", mode="before")
    @classmethod
    def _as_str(cls, value):
        return str(value)


class TaxonomyStep(BaseModel):
    step_id: str
    failure_codes: list[str] = Field(default_factory=list)
    failures: list[TaxonomyFailure] = Field(default_factory=list)

    @field_validator("step_id", mode="before")
    @classmethod
    def _as_str(cls, value):
        return str(value)


class CategoryResult(BaseModel):
    """One specialist's response, before aggregation."""

    category: str = ""
    annotations: list[TaxonomyStep] = Field(default_factory=list)


class ZeroShotResult(BaseModel):
    annotations: list[ZeroShotStep] = Field(default_factory=list)


def parse_ambiguous(failure_type: str) -> tuple[str, bool]:
    """Public helper so the parser can populate `ambiguous` alongside the name."""
    return _split_ambiguous(failure_type)


# --------------------------------------------------------------------------- #
# Wire schemas                                                                 #
# --------------------------------------------------------------------------- #
#
# What the *model* is asked to emit, for schema-constrained decoding. These are
# deliberately not derived from the Pydantic models above: the models describe
# what we accept after normalisation (`ambiguous` lifted out of the prefix,
# `source_category` stamped during aggregation), while these describe what the
# model writes. They mirror the JSON skeletons in prompts.py field for field.
#
# Written by hand rather than via `model_json_schema()` because strict mode
# forbids `default`, requires every property in `required`, and requires
# `additionalProperties: false` — Pydantic emits none of those three.


def _failure_properties(taxonomy_bound: bool) -> dict:
    fields = {}
    if taxonomy_bound:
        fields = {
            "code": {"type": "string"},
            "category": {"type": "string"},
            "sub_category": {"type": "string"},
        }
    return {
        **fields,
        "failure_type": {"type": "string"},
        "evidence": {"type": "string"},
        "rationale": {"type": "string"},
    }


def _annotations_schema(taxonomy_bound: bool) -> dict:
    failure = _failure_properties(taxonomy_bound)
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["step_id", "failures"],
            "properties": {
                "step_id": {"type": "string"},
                "failures": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(failure),
                        "properties": failure,
                    },
                },
            },
        },
    }


ZERO_SHOT_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["annotations"],
    "properties": {"annotations": _annotations_schema(taxonomy_bound=False)},
}

CATEGORY_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "annotations"],
    "properties": {
        "category": {"type": "string"},
        "annotations": _annotations_schema(taxonomy_bound=True),
    },
}
