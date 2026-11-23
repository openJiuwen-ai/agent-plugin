import pytest

from ojw_evaluation.core.llm import ModelOutputError, json_schema_format, parse_model_json
from ojw_evaluation.modules.failure_detector.schema import (
    CATEGORY_JSON_SCHEMA,
    ZERO_SHOT_JSON_SCHEMA,
    CategoryResult,
    TaxonomyStep,
    ZeroShotResult,
)


def test_step_id_stays_a_string():
    """Real ids include '3.1' and '8.2'; an int would mangle nested steps."""
    step = TaxonomyStep.model_validate({"step_id": "3.1", "failures": []})
    assert step.step_id == "3.1"


def test_numeric_step_id_is_coerced_to_string():
    assert TaxonomyStep.model_validate({"step_id": 8, "failures": []}).step_id == "8"


def test_ambiguous_prefix_is_lifted_into_a_flag():
    """13 of 58 measured failures carried the prefix; in-band it splits one
    taxonomy name into two apparent types."""
    step = TaxonomyStep.model_validate(
        {
            "step_id": "9",
            "failures": [
                {
                    "code": "1.1.1",
                    "failure_type": "[AMBIGUOUS] Task Specification Disobedience",
                    "evidence": "x",
                }
            ],
        }
    )
    failure = step.failures[0]
    assert failure.ambiguous is True
    assert failure.failure_type == "Task Specification Disobedience"


def test_non_ambiguous_failure_keeps_flag_false():
    step = TaxonomyStep.model_validate(
        {"step_id": "9", "failures": [{"code": "1.1.1", "failure_type": "Foo"}]}
    )
    assert step.failures[0].ambiguous is False


def test_empty_failures_survive():
    step = TaxonomyStep.model_validate({"step_id": "5", "failures": []})
    assert step.failures == []
    assert step.failure_codes == []


def test_zero_shot_contract_has_no_codes():
    result = ZeroShotResult.model_validate(
        {"annotations": [{"step_id": "1", "failures": [{"failure_type": "drift"}]}]}
    )
    assert not hasattr(result.annotations[0], "failure_codes")


# --------------------------------------------------------------------------- #
# Tolerant parsing                                                             #
# --------------------------------------------------------------------------- #

PAYLOAD = '{"category": "c", "annotations": [{"step_id": "1", "failures": []}]}'


def test_parses_bare_json():
    assert parse_model_json(PAYLOAD)["category"] == "c"


def test_parses_fenced_json():
    assert parse_model_json(f"```json\n{PAYLOAD}\n```")["category"] == "c"


def test_parses_json_after_think_block():
    text = f"<think>reasoning here</think>\n{PAYLOAD}"
    assert parse_model_json(text)["category"] == "c"


def test_parses_json_with_prose_around_it():
    text = f"Here is the result:\n{PAYLOAD}\nHope that helps."
    assert parse_model_json(text)["category"] == "c"


def test_garbage_raises_cleanly():
    with pytest.raises(ValueError):
        parse_model_json("no json at all")


def test_empty_raises_cleanly():
    with pytest.raises(ValueError):
        parse_model_json("   ")


def test_category_result_round_trips():
    result = CategoryResult.model_validate(parse_model_json(PAYLOAD))
    assert result.annotations[0].step_id == "1"


# --------------------------------------------------------------------------- #
# The measured qwen3-32b breakdown                                             #
# --------------------------------------------------------------------------- #

# Excerpted from a real response: the model held the frame for ~10 steps, then
# began emitting object members with a value and no key.
DEGENERATE = """{
  "annotations": [
    {"step_id": "8.2", "failures": []},
    {
      "step_id": "4",
      {
        "failures": []
      }
    }
  ]
}"""


def test_structurally_broken_output_raises_model_output_error():
    with pytest.raises(ModelOutputError) as caught:
        parse_model_json(DEGENERATE)
    # The message must name the breakage, not hand back a decoder traceback.
    assert "not valid JSON" in str(caught.value)
    assert "line" in str(caught.value)


def test_model_output_error_carries_the_raw_response():
    """So the caller can point at the artefact instead of re-printing it."""
    with pytest.raises(ModelOutputError) as caught:
        parse_model_json(DEGENERATE)
    assert caught.value.raw == DEGENERATE


def test_broken_output_is_not_repaired_into_annotations():
    """Guessing an omitted key would fabricate an annotation. It must fail loudly."""
    with pytest.raises(ModelOutputError):
        ZeroShotResult.model_validate(parse_model_json(DEGENERATE))


# --------------------------------------------------------------------------- #
# Wire schemas                                                                 #
# --------------------------------------------------------------------------- #


def _walk_objects(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _walk_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_objects(item)


@pytest.mark.parametrize("schema", [ZERO_SHOT_JSON_SCHEMA, CATEGORY_JSON_SCHEMA])
def test_wire_schemas_satisfy_strict_mode(schema):
    """Strict mode rejects a schema that omits either of these; the API 400s."""
    for obj in _walk_objects(schema):
        assert obj.get("additionalProperties") is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


@pytest.mark.parametrize("schema", [ZERO_SHOT_JSON_SCHEMA, CATEGORY_JSON_SCHEMA])
def test_wire_schemas_carry_no_defaults(schema):
    """`default` is not in the strict-mode subset — the reason these are hand-written."""
    assert "default" not in repr(schema)


def test_wire_schemas_match_the_pydantic_contracts():
    """The model is asked for exactly the fields the prompts show, no more."""
    zero = ZERO_SHOT_JSON_SCHEMA["properties"]["annotations"]["items"]
    assert sorted(zero["properties"]) == ["failures", "step_id"]
    assert sorted(zero["properties"]["failures"]["items"]["properties"]) == [
        "evidence",
        "failure_type",
        "rationale",
    ]

    cat = CATEGORY_JSON_SCHEMA["properties"]["annotations"]["items"]
    assert sorted(cat["properties"]["failures"]["items"]["properties"]) == [
        "category",
        "code",
        "evidence",
        "failure_type",
        "rationale",
        "sub_category",
    ]


def test_json_schema_format_wraps_for_the_api():
    wrapped = json_schema_format("zero_shot_annotations", ZERO_SHOT_JSON_SCHEMA)
    assert wrapped["type"] == "json_schema"
    assert wrapped["json_schema"]["strict"] is True
    assert wrapped["json_schema"]["schema"] is ZERO_SHOT_JSON_SCHEMA
