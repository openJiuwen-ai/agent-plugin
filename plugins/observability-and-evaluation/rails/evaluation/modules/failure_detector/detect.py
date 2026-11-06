"""Failure-detection strategies.

Two functions rather than one with `taxonomy=None`, because the strategies
return different shapes: zero-shot emits free-text labels with no aggregation,
taxonomy-guided emits code-bound results that need the per-step join. A
`taxonomy=None` flag would hide a changing return type behind a default argument.

`few_shot` IS a plain parameter, because it does not change the return shape.
"""
from __future__ import annotations

import asyncio
import logging

from ...core.llm import (
    ModelOutputError,
    complete,
    json_schema_format,
    parse_model_json,
)
from ...domain.taxonomy import Taxonomy

from .prompts import FEW_SHOT, TAXONOMY_GUIDED, ZERO_SHOT
from .schema import (
    CATEGORY_JSON_SCHEMA,
    ZERO_SHOT_JSON_SCHEMA,
    CategoryResult,
    ZeroShotResult,
)

logger = logging.getLogger(__name__)


async def _call(
    client, model, template, variables, params, on_prompt=None,
    response_format=None, extra_body=None,
):
    rendered = template.render(**variables)
    if on_prompt:
        on_prompt(rendered)
    text = await complete(
        client,
        model,
        rendered["system"],
        rendered["user"],
        response_format=response_format,
        extra_body=extra_body,
        **params,
    )
    return text


async def zero_shot(
    client, model, trajectory_text, *, params=None, artifacts=None, extra_body=None
) -> tuple[ZeroShotResult, str]:
    """Annotator 1: one call, whole trajectory, no taxonomy."""
    params = params or {}

    def save(rendered):
        if artifacts:
            artifacts.write_prompt("zero-shot", rendered["user"])

    raw = await _call(
        client,
        model,
        ZERO_SHOT,
        {"input_text": trajectory_text},
        params,
        save,
        response_format=json_schema_format("zero_shot_annotations", ZERO_SHOT_JSON_SCHEMA),
        extra_body=extra_body,
    )
    if artifacts:
        artifacts.write_raw("zero-shot", raw)
    try:
        return ZeroShotResult.model_validate(parse_model_json(raw)), raw
    except ModelOutputError as exc:
        # Zero-shot is a single call, so there is nothing to salvage — but the
        # response is already on disk, and a decoder traceback is a worse
        # signpost than the file that holds the evidence.
        where = f" The response is in {artifacts.cell}/raw/zero-shot.json." if artifacts else ""
        raise ModelOutputError(f"{exc}{where}", exc.raw) from None


async def taxonomy_guided(
    client,
    model,
    trajectory_text,
    taxonomy: Taxonomy,
    *,
    few_shot: str | None = None,
    params=None,
    artifacts=None,
    extra_body=None,
) -> list[CategoryResult]:
    """Annotator 2 (and 3 when `few_shot` is supplied): one call per category.

    The calls are independent, so they run concurrently. The previous sequential
    loop took 8m05s for 7 categories; this collapses toward the slowest call.
    """
    params = params or {}
    template = FEW_SHOT if few_shot else TAXONOMY_GUIDED

    async def one(category):
        variables = {
            "category_name": category.name,
            "taxonomy_slice": category.text,
            "input_text": trajectory_text,
        }
        if few_shot:
            variables["few_shot_examples"] = few_shot

        def save(rendered):
            if artifacts:
                artifacts.write_prompt(
                    f"{taxonomy.name}-cat-{category.number}", rendered["user"]
                )

        raw = await _call(
            client,
            model,
            template,
            variables,
            params,
            save,
            response_format=json_schema_format(
                "category_annotations", CATEGORY_JSON_SCHEMA
            ),
            extra_body=extra_body,
        )
        if artifacts:
            artifacts.write_raw(f"cat-{category.number}", raw)
        try:
            parsed = parse_model_json(raw)
            parsed.setdefault("category", category.name)
            return CategoryResult.model_validate(parsed)
        except ModelOutputError as exc:
            logger.error(
                "Category %s (%s) returned unparseable output; skipping. %s",
                category.number,
                category.name,
                exc,
            )
            return None
        except Exception:
            logger.exception(
                "Category %s (%s) failed validation; skipping",
                category.number,
                category.name,
            )
            return None

    results = await asyncio.gather(*(one(c) for c in taxonomy.categories))
    return [r for r in results if r is not None]


STRATEGIES = {
    "zero-shot": zero_shot,
    "taxonomy-guided": taxonomy_guided,
    # "few-shot": partial(taxonomy_guided, few_shot=...),  # INACTIVE — exemplar/gold split unresolved
}

DEFERRED = {"few-shot": "deferred: the exemplar/gold split is unresolved "
                        "(see documentation/ojw_evaluation/06-prompts-and-strategies.md)"}
