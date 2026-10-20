"""Line-number citations: the model names lines, the code reads them.

Why this exists
---------------
The extraction contract used to ask the model to *type* the evidence: copy the table row
it read, verbatim. On real filings that was the least reliable part of the workflow. The
model copied the prompt's own example row, dropped the current-year column, and wrote the
requested figure into the first column of a row that does not exist — each time with the
correct value. Every prompt adjustment moved the failure somewhere else.

Now each page line has a stable ID ("L41" is the 41st line of the page's text) and the
evidence shown to the model is prefixed with those IDs. The model cites 1–3 IDs; this
module checks them and copies the lines verbatim from the page. The grounding guarantee is
unchanged — every figure is tied to real lines on the cited page — but there is no longer
any text for the model to reproduce.

This is the ONE implementation of the check. Extraction (`validate_extraction_support`)
and final provenance (`validate_result_provenance`) both call it; they used to disagree,
and a relaxation added to one was rejected by the other.

Line IDs are plain text lines, not table cells, so HTML and PDF pages are cited the same
way. Blank lines are counted, so an ID is simply an index into `raw_text.splitlines()`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: at most this many lines may be cited for one figure
MAX_CITED_LINES = 3
#: cited lines must lie within this many lines of each other (last - first)
MAX_LINE_SPAN = 3

_LINE_ID = re.compile(r"^L([1-9][0-9]*)$")

_NUMBER_PATTERN = re.compile(
    r"(?<![\w+\-−])(?P<open>\()?[^\S\r\n]*"
    r"(?P<sign_before>[+\-−])?[^\S\r\n]*\$?[^\S\r\n]*"
    r"(?P<sign_after>[+\-−])?[^\S\r\n]*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"[^\S\r\n]*(?P<close>\))?(?![\w])"
)


class CitationError(ValueError):
    """A line citation that does not support the figure it is attached to."""


@dataclass(frozen=True)
class ResolvedCitation:
    line_numbers: tuple[int, ...]
    text: str


def page_lines(raw_text: str) -> list[str]:
    """The page's lines, indexed by line ID - 1."""
    return raw_text.splitlines()


def format_line_id(number: int) -> str:
    return f"L{number}"


def number_lines(text: str, line_numbers: Sequence[int]) -> str:
    """Prefix each line of `text` with its page line ID, for the model to cite."""
    lines = text.splitlines()
    if len(lines) != len(line_numbers):
        raise ValueError("one line number is required per line of text")
    return "\n".join(f"{format_line_id(n)}  {line}" for n, line in zip(line_numbers, lines))


def parse_line_ids(ids: Iterable[str]) -> tuple[int, ...]:
    numbers = []
    for raw in ids:
        match = _LINE_ID.fullmatch(str(raw).strip())
        if match is None:
            raise CitationError(f"{raw!r} is not a line ID (expected L<number>).")
        numbers.append(int(match.group(1)))
    return tuple(numbers)


def numbers_in_text(text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for match in _NUMBER_PATTERN.finditer(text):
        if bool(match.group("open")) != bool(match.group("close")):
            continue
        signs = [s for s in (match.group("sign_before"), match.group("sign_after")) if s]
        # A sign belongs to the number. Never reinterpret its unsigned suffix
        # as positive, or combine explicit signs with accounting parentheses.
        if len(signs) > 1 or (match.group("open") and signs):
            continue
        try:
            value = Decimal(match.group("number").replace(",", ""))
        except InvalidOperation:
            continue
        if match.group("open") or signs in (["-"], ["−"]):
            value = -value
        values.add(value)
    return values


# Typographic variants a model does not reliably reproduce: filers print "shareholders’"
# (U+2019) where the model types "shareholders'". Comparison only; stored text is unchanged.
_TYPOGRAPHIC = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                              "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00a0": " "})


def _normalize(text: str) -> str:
    return " ".join(text.translate(_TYPOGRAPHIC).split())


def resolve_line_citation(
    raw_page_text: str,
    line_numbers: Sequence[int],
    *,
    row_label: str | None,
    value: Decimal | None = None,
) -> ResolvedCitation:
    """Check a line citation against the page and return the cited lines verbatim.

    Raises CitationError when a cited line does not exist on the page or is blank, when
    more than MAX_CITED_LINES are cited or they span more than MAX_LINE_SPAN lines, when
    the row label is not in the cited lines, or when `value` is given and is not one of
    the numbers printed in them. `value` is the figure as printed, in the unit the
    statement states; the unit itself is checked by the caller against the page header.
    """
    numbers = tuple(sorted(set(line_numbers)))
    if not numbers:
        raise CitationError("No lines were cited.")
    if len(numbers) > MAX_CITED_LINES:
        raise CitationError(f"At most {MAX_CITED_LINES} lines may be cited.")
    if numbers[-1] - numbers[0] > MAX_LINE_SPAN:
        raise CitationError(
            f"Cited lines are more than {MAX_LINE_SPAN} lines apart "
            f"({format_line_id(numbers[0])}..{format_line_id(numbers[-1])}).")
    lines = page_lines(raw_page_text)
    missing = [format_line_id(n) for n in numbers if n > len(lines) or not lines[n - 1].strip()]
    if missing:
        raise CitationError("Cited line does not exist on the cited page: "
                            + ", ".join(missing) + ".")
    # Stripped only at the ends, as every evidence model stores its text; the lines
    # themselves are copied unchanged.
    text = "\n".join(lines[n - 1] for n in numbers).strip()
    if row_label is not None and _normalize(row_label) not in _normalize(text):
        raise CitationError("The exact row label is not in the cited lines.")
    if value is not None and value not in numbers_in_text(text):
        raise CitationError("The extracted value is not printed in the cited lines.")
    return ResolvedCitation(line_numbers=numbers, text=text)
