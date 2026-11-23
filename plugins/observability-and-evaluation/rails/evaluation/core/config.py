"""Bundled taxonomy paths and standalone CLI defaults.

The Rail supplies its own session workspace output directory.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .providers import PROVIDERS

# Assets are read from the package; runtime paths come from explicit inputs.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY_DIR = PACKAGE_ROOT / "assets/taxonomies"

TAXONOMIES = {
    "ours": TAXONOMY_DIR / "ours.txt",
    "mast": TAXONOMY_DIR / "mast.txt",
}


# One source for CLI, HTTP request defaults and the setup form. None means the
# provider/model default, not an invented numeric sampling setting.
DEFAULT_PARAMETERS = {
    "client": "openai",
    "strategy": "taxonomy-guided",
    "taxonomy": "ours",
    "dedup": "none",
    "temperature": None,
    "top_p": None,
    "output_subdir": "runs",
}


def evaluation_defaults(client=None):
    values = dict(DEFAULT_PARAMETERS)
    values["client"] = client if client is not None else values["client"]
    profile = PROVIDERS[values["client"]]
    values.update(model=profile["default_model"], base_url=profile["base_url"])
    return values


@dataclass(frozen=True)
class RunConfig:
    """One invocation: what to run, on what, with which model."""

    module: str
    strategy: str
    model: str
    client: str
    trajectory: Path
    taxonomy: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    runs: int = 1
    output_dir: Path | None = None

    def __post_init__(self):
        output = self.output_dir if self.output_dir is not None else Path(self.trajectory).parent / "evaluation"
        object.__setattr__(self, "output_dir", Path(output).expanduser().resolve())

    def taxonomy_path(self) -> Path | None:
        return TAXONOMIES[self.taxonomy] if self.taxonomy else None


def declared_paths() -> dict[str, Path]:
    """Every path this module promises exists. Used by tests/ojw_evaluation/test_config.py."""
    return {
        "TAXONOMY_DIR": TAXONOMY_DIR,
        **{f"taxonomy:{k}": v for k, v in TAXONOMIES.items()},
    }
