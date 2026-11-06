"""Bundled assets must resolve without optional local trajectory samples."""
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[4] / "plugins/observability-and-evaluation"

from ojw_evaluation.core import config
from ojw_evaluation.core.providers import sampling_params, supports_sampling_params


@pytest.mark.parametrize("name,path", sorted({
    "TAXONOMY_DIR": config.TAXONOMY_DIR,
    **{f"taxonomy:{name}": path for name, path in config.TAXONOMIES.items()},
}.items()))
def test_bundled_asset_path_exists(name, path):
    assert path.exists(), f"{name} -> {path}"


def test_assets_resolve_inside_destination_plugin():
    assert config.PACKAGE_ROOT == PACKAGE / "rails/evaluation"


def test_no_hardcoded_home_paths_in_source():
    offenders = [
        p
        for p in PACKAGE.rglob("*.py")
        if "/home/" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"hardcoded home paths in: {offenders}"


# --------------------------------------------------------------------------- #
# Sampling-parameter policy — the bug that broke the documented command         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5", "gpt-5-mini", "o3"])
def test_reasoning_models_reject_sampling_params(model):
    assert not supports_sampling_params(model)
    assert sampling_params(model, temperature=0.7, top_p=0.9) == {}


@pytest.mark.parametrize("model", ["gpt-4o", "qwen3-32b", "DeepSeek-V4-Flash-W8A8"])
def test_other_models_keep_sampling_params(model):
    assert supports_sampling_params(model)
    assert sampling_params(model, temperature=0.7, top_p=0.9) == {
        "temperature": 0.7,
        "top_p": 0.9,
    }


def test_unset_params_are_omitted():
    assert sampling_params("gpt-4o", None, None) == {}


def test_bundled_taxonomies_match_source_assets():
    source_assets = (
        PACKAGE
        / "rails" / "evaluation" / "assets" / "taxonomies"
    )
    for name in ("ours", "mast"):
        assert config.TAXONOMIES[name].read_bytes() == (source_assets / f"{name}.txt").read_bytes()
