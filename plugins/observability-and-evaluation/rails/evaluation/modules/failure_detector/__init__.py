"""Step-level failure detection over agentic structured trajectories.

The module contract, for now, is simply what this package exports. A formal
Protocol and a registry arrive when a second evaluation module exists and the
shape can be inferred from two examples rather than one.
"""
from .aggregate import aggregate, dedup
from .detect import DEFERRED, STRATEGIES, taxonomy_guided, zero_shot
from .schema import (
    CategoryResult,
    TaxonomyFailure,
    TaxonomyStep,
    ZeroShotFailure,
    ZeroShotResult,
    ZeroShotStep,
)

NAME = "failure-detector"

__all__ = [
    "NAME",
    "STRATEGIES",
    "DEFERRED",
    "zero_shot",
    "taxonomy_guided",
    "aggregate",
    "dedup",
    "CategoryResult",
    "TaxonomyStep",
    "TaxonomyFailure",
    "ZeroShotResult",
    "ZeroShotStep",
    "ZeroShotFailure",
]
