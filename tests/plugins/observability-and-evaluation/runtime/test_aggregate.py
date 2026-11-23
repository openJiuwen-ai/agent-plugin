"""The aggregator's contract, including the one behaviour that is easy to
regress silently: duplicates must be PRESERVED.

Measured on a real run, all 7 repeated (step_id, code) pairs carried *different*
evidence. Collapsing them discards real citations and erases frequency.
"""
from ojw_evaluation.modules.failure_detector.aggregate import aggregate, dedup
from ojw_evaluation.modules.failure_detector.schema import CategoryResult


def cat(name, annotations):
    return CategoryResult.model_validate({"category": name, "annotations": annotations})


def test_joins_categories_on_step_id():
    results = [
        cat("c1", [{"step_id": "4", "failures": [{"code": "1.1.1", "failure_type": "a"}]}]),
        cat("c2", [{"step_id": "4", "failures": [{"code": "2.1.3", "failure_type": "b"}]}]),
    ]
    steps = aggregate(results)
    assert len(steps) == 1
    assert steps[0].failure_codes == ["1.1.1", "2.1.3"]
    assert len(steps[0].failures) == 2


def test_duplicates_with_distinct_evidence_are_preserved():
    """The regression that would silently destroy citations."""
    results = [
        cat(
            "c1",
            [
                {
                    "step_id": "4",
                    "failures": [
                        {"code": "1.1.1", "failure_type": "a", "evidence": "quote one"},
                        {"code": "1.1.1", "failure_type": "a", "evidence": "quote two"},
                    ],
                }
            ],
        )
    ]
    steps = aggregate(results)
    assert len(steps[0].failures) == 2
    assert {f.evidence for f in steps[0].failures} == {"quote one", "quote two"}
    # failure_codes is a projection: distinct, while failures stays complete.
    assert steps[0].failure_codes == ["1.1.1"]


def test_failure_codes_is_distinct_while_failures_is_complete():
    results = [
        cat(
            "c1",
            [
                {
                    "step_id": "4",
                    "failures": [
                        {"code": "1.1.1", "failure_type": "a", "evidence": "x"},
                        {"code": "1.1.1", "failure_type": "a", "evidence": "y"},
                        {"code": "1.1.2", "failure_type": "b", "evidence": "z"},
                    ],
                }
            ],
        )
    ]
    step = aggregate(results)[0]
    assert len(step.failure_codes) == 2
    assert len(step.failures) == 3


def test_source_category_is_recorded():
    results = [
        cat("Cognitive", [{"step_id": "1", "failures": [{"code": "2.1.1", "failure_type": "a"}]}])
    ]
    assert aggregate(results)[0].failures[0].source_category == "Cognitive"


def test_steps_with_no_failures_survive():
    results = [
        cat("c1", [{"step_id": "1", "failures": []}, {"step_id": "2", "failures": []}])
    ]
    steps = aggregate(results)
    assert [s.step_id for s in steps] == ["1", "2"]
    assert all(s.failures == [] and s.failure_codes == [] for s in steps)


def test_step_order_follows_trajectory():
    results = [cat("c1", [{"step_id": "3", "failures": []}])]
    steps = aggregate(results, step_order=["1", "2", "3"])
    assert [s.step_id for s in steps] == ["1", "2", "3"]


def test_unexpected_step_id_is_appended_not_dropped():
    results = [cat("c1", [{"step_id": "99", "failures": []}])]
    steps = aggregate(results, step_order=["1"])
    assert [s.step_id for s in steps] == ["1", "99"]


# --------------------------------------------------------------------------- #
# Opt-in dedup                                                                 #
# --------------------------------------------------------------------------- #

DUPES = [
    {
        "step_id": "4",
        "failures": [
            {"code": "1.1.1", "failure_type": "a", "evidence": "one"},
            {"code": "1.1.1", "failure_type": "a", "evidence": "two"},
            {"code": "1.1.1", "failure_type": "a", "evidence": "one"},
        ],
    }
]


def test_dedup_none_is_the_default_and_changes_nothing():
    steps = aggregate([cat("c1", DUPES)])
    assert len(dedup(steps, "none")[0].failures) == 3


def test_dedup_exact_only_collapses_identical_evidence():
    steps = dedup(aggregate([cat("c1", DUPES)]), "exact")
    assert len(steps[0].failures) == 2
    assert {f.evidence for f in steps[0].failures} == {"one", "two"}


def test_dedup_by_code_is_lossy():
    steps = dedup(aggregate([cat("c1", DUPES)]), "by_code")
    assert len(steps[0].failures) == 1
