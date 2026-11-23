"""Every template must render — including inactive ones.

few-shot is not registered in STRATEGIES, so nothing else exercises it. Without
this test an unfillable placeholder or a broken format string would sit
undetected until the strategy is switched back on.
"""
import pytest

from ojw_evaluation.modules.failure_detector.prompts import ALL_TEMPLATES

DUMMY = {
    "input_text": "TRAJECTORY",
    "category_name": "CATEGORY",
    "taxonomy_slice": "SLICE",
    "few_shot_examples": "EXAMPLES",
}


@pytest.mark.parametrize("name", sorted(ALL_TEMPLATES))
def test_template_renders(name):
    template = ALL_TEMPLATES[name]
    rendered = template.render(**{k: DUMMY[k] for k in template.variables})
    assert rendered["system"].strip()
    assert rendered["user"].strip()


@pytest.mark.parametrize("name", sorted(ALL_TEMPLATES))
def test_no_unsubstituted_placeholders(name):
    """No `{variable}` survives rendering.

    Note the rendered text legitimately contains single braces: the JSON
    skeleton in OUTPUT INSTRUCTIONS is written `{{...}}` and renders to `{...}`.
    So check for the variable names specifically, not for braces.
    """
    template = ALL_TEMPLATES[name]
    rendered = template.render(**{k: DUMMY[k] for k in template.variables})
    for part in rendered.values():
        for variable in DUMMY:
            assert f"{{{variable}}}" not in part


@pytest.mark.parametrize("name", sorted(ALL_TEMPLATES))
def test_trajectory_is_injected(name):
    template = ALL_TEMPLATES[name]
    rendered = template.render(**{k: DUMMY[k] for k in template.variables})
    assert "TRAJECTORY" in rendered["user"]


def test_missing_variable_is_rejected():
    with pytest.raises(ValueError, match="Missing values"):
        ALL_TEMPLATES["taxonomy-guided"].render(input_text="x")


def test_few_shot_template_includes_exemplars():
    rendered = ALL_TEMPLATES["few-shot"].render(**DUMMY)
    assert "EXAMPLES" in rendered["user"]


def test_taxonomy_prompt_does_not_carry_the_deduplication_rule():
    """Deliberate: repeated (step_id, code) entries carry distinct evidence, so
    they are separate occurrences. Propagating the zero-shot DEDUPLICATION rule
    would suppress genuine repeated failures."""
    rendered = ALL_TEMPLATES["taxonomy-guided"].render(
        **{k: DUMMY[k] for k in ALL_TEMPLATES["taxonomy-guided"].variables}
    )
    assert "DEDUPLICATION" not in rendered["system"]
