"""Failure-detector prompts, composed from shared fragments.

The three original prompt files were ~90% identical and had already drifted:
the DEDUPLICATION rule existed only in the zero-shot prompt. Composing from
fragments makes an edit to a shared rule impossible to miss.

On DEDUPLICATION specifically: it is deliberately NOT propagated to the taxonomy
prompts. The zero-shot prompt contains both "repeated or cascading failures must
each be annotated" and "report the same failure event on the same step only
once", which contradict each other unless "event" is precisely defined — and it
is not. Measured output shows repeated (step_id, code) entries carry *distinct*
evidence, i.e. they are separate occurrences. The taxonomy prompt's omission is
the correct behaviour; the zero-shot prompt is the one that needs reconciling.
"""
from __future__ import annotations

from ...core.prompts import PromptTemplate

# --------------------------------------------------------------------------- #
# Shared fragments                                                             #
# --------------------------------------------------------------------------- #

_TRACE_BLOCK = '''
  --------

  ## TRACE TO ANALYZE

  Trace Input:
  """
  {input_text}
  """

  --------
'''

_COMMON_RULES = """  - Include every step_id present in the trace exactly once, preserving the
    original trace/step order. Keep step_id values unchanged from the input.
  - "evidence": a verbatim excerpt from the trace that demonstrates the
    failure. Do not paraphrase; quote the trace directly.
  - "rationale": a brief explanation (1-3 sentences) of why this evidence
    constitutes a failure of the given type.
  - Repeated or cascading failures must each be annotated on the step where
    they occur, even if they share a root cause.
  - Output ONLY the JSON object. No preamble, no closing remarks, no markdown
    code fences."""

_AMBIGUITY_RULE = """  - If a failure is ambiguous or you are uncertain, still report it and
    prefix its failure_type with "[AMBIGUOUS] "."""

_SYSTEM_CORE = """  - EVIDENCE-GROUNDED: Every reported failure must be backed by a verbatim
    excerpt from the trace. Never invent failures without supporting evidence.
  - ORDER PRESERVATION: Keep steps in original trace order, each step_id
    appearing exactly once and unchanged from the input.
  - STRICT OUTPUT CONTRACT: Return only the specified JSON object, directly
    parseable with json.loads. No prose, no markdown fences."""

_CATEGORY_BLOCK = '''
  ## YOUR CATEGORY

  You are the specialist for the following taxonomy category:

  Category: {category_name}

  Taxonomy slice (the ONLY failure definitions you may use):
  """
  {taxonomy_slice}
  """

  --------
'''

_EXEMPLAR_BLOCK = '''
  ## ANNOTATED EXAMPLES

  Below are labelled examples from previously annotated traces by a human
  annotator. Each example shows a trace excerpt followed by the correct
  annotation output. Use them to calibrate what counts as a failure in your
  category, how much evidence to quote, and how to phrase rationales. Do NOT
  copy annotations from the examples into your output; they refer to different
  traces.

  """
  {few_shot_examples}
  """

  --------
'''

# --------------------------------------------------------------------------- #
# Templates                                                                    #
# --------------------------------------------------------------------------- #

ZERO_SHOT = PromptTemplate(
    system="""
  You are a failure-annotation agent for multi-agent system execution
  traces. Your only job is to read a structured JSON trace, detect every
  failure it contains, and produce one consolidated per-step annotation of
  those failures.

  Core rules:
"""
    + _SYSTEM_CORE
    + """
  - EXHAUSTIVE: Do not skip ambiguous, repeated, or cascading failures. When
    uncertain, annotate and mark with an "[AMBIGUOUS]" prefix rather than
    omitting.
""",
    user="""
  ## TASK
  Analyze the multi-agent execution trace below and identify failures
  in the trace, in relation to the agentic behavior, actions and decisions.

  Examine each step in the trace carefully and do not skip ambiguous, repeated, or
  cascading failures. You are the only annotator: detection, classification,
  and reporting are all your responsibility.
"""
    + _TRACE_BLOCK
    + """
  ## OUTPUT INSTRUCTIONS

  Return ONLY a single valid JSON object with the following structure:

  {{
    "annotations": [
      {{
        "step_id": "",
        "failures": [
          {{
            "failure_type": "",
            "evidence": "",
            "rationale": ""
          }}
        ]
      }}
    ]
  }}

  Annotation rules:
"""
    + _COMMON_RULES
    + """
  - For each step, list ALL failures you can identify in that step. A single
    step may contain zero, one, or multiple failures.
  - "failure_type": a short label naming the kind of failure, in your own
    words.
"""
    + _AMBIGUITY_RULE
    + """
  - Steps with no identifiable failure must still appear, with an empty
    "failures" list: [].
""",
    variables=["input_text"],
)


def _taxonomy_user(exemplars: bool) -> str:
    return (
        _CATEGORY_BLOCK
        + """
  ## TASK
  Analyze the multi-agent execution trace below and identify every failure
  that belongs to YOUR category. Examine each step carefully and do not skip
  ambiguous, repeated, or cascading failures.

  --------
"""
        + (_EXEMPLAR_BLOCK if exemplars else "")
        + _TRACE_BLOCK
        + """
  ## OUTPUT INSTRUCTIONS

  Return ONLY a single valid JSON object with the following structure:

  {{
    "category": "{category_name}",
    "annotations": [
      {{
        "step_id": "",
        "failures": [
          {{
            "code": "",
            "category": "",
            "sub_category": "",
            "failure_type": "",
            "evidence": "",
            "rationale": ""
          }}
        ]
      }}
    ]
  }}

  Annotation rules:
"""
        + _COMMON_RULES
        + """
  - Only report failures defined in your taxonomy slice above. If a failure
    you notice belongs to a different category, DO NOT report it; another
    specialist owns it.
  - "code": the exact failure code from your taxonomy slice (e.g. "1.1.1").
  - "category" and "sub_category": copied verbatim from your taxonomy slice.
  - "failure_type": the exact failure-type name from your taxonomy slice.
"""
        + _AMBIGUITY_RULE
        + """
  - Steps with no failure from your category must still appear, with an
    empty "failures" list: [].
"""
    )


_TAXONOMY_SYSTEM = (
    """
  You are one category specialist inside a multi-agent failure-annotation
  system. You annotate failures belonging to exactly one taxonomy category;
  other specialists handle the remaining categories.

  Core rules:
  - CATEGORY DISCIPLINE: Only annotate failures defined in the taxonomy
    slice you are given. Never report failures outside your category.
  - TAXONOMY FIDELITY: Use codes, category names, sub-categories, and
    failure-type names exactly as written in your taxonomy slice. Never
    invent new codes or labels.
"""
    + _SYSTEM_CORE
    + """
  - EXHAUSTIVE WITHIN CATEGORY: Do not skip ambiguous, repeated, or
    cascading failures in your category. When uncertain, annotate and mark
    with an "[AMBIGUOUS]" prefix rather than omitting.
"""
)

TAXONOMY_GUIDED = PromptTemplate(
    system=_TAXONOMY_SYSTEM,
    user=_taxonomy_user(exemplars=False),
    variables=["category_name", "taxonomy_slice", "input_text"],
)

# Kept but INACTIVE — few-shot is not registered in detect.STRATEGIES.
# Built from the same fragments as TAXONOMY_GUIDED so it cannot drift, and
# exercised by tests/test_prompts.py, which renders every template here.
FEW_SHOT = PromptTemplate(
    system=_TAXONOMY_SYSTEM
    + """
  - EXAMPLE CALIBRATION: Annotated examples, when provided, define the
    expected level of granularity, evidence style, and rationale phrasing.
    Follow their style but never reuse their content for the new trace.
""",
    user=_taxonomy_user(exemplars=True),
    variables=["category_name", "taxonomy_slice", "few_shot_examples", "input_text"],
)

ALL_TEMPLATES = {
    "zero-shot": ZERO_SHOT,
    "taxonomy-guided": TAXONOMY_GUIDED,
    "few-shot": FEW_SHOT,
}
