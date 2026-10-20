"""Local, page-aware PDF text extraction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Self

import pymupdf
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_FOOTER_HEIGHT_FRACTION = 0.15
_TRAILING_TEXT_LINE_LIMIT = 8
_TEXT_BLOCK_TYPE = 0
#: how far ahead to look for a layout line matching the next text line
_LINE_MATCH_LOOKAHEAD = 4
_PAGE_LABEL_PATTERN = r"(?:[A-Za-z]{1,3}-)?\d{1,4}"
_PAGE_COUNTER_PATTERN = re.compile(r"\bpage\s+\d+\s+of\s+\d+\b", re.IGNORECASE)
_FORM_10K_PATTERN = re.compile(
    rf"\bform\s+10-k\b.*?(?:\||:|[-\u2013\u2014])\s*"
    rf"(?:page\s+)?(?P<label>{_PAGE_LABEL_PATTERN})\s*$",
    re.IGNORECASE,
)
_STANDALONE_PAGE_PATTERN = re.compile(
    rf"^(?:page\s+)?(?P<label>{_PAGE_LABEL_PATTERN})$",
    re.IGNORECASE,
)


class PdfIngestionError(RuntimeError):
    """Raised when a local PDF cannot be safely parsed."""


class _IngestionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PdfPageText(_IngestionModel):
    """Raw text and separate physical/printed identifiers for one PDF page."""

    pdf_page_index: int = Field(ge=0)
    printed_document_page: NonEmptyString | None = None
    raw_text: str
    #: Position (x0, y0, x1, y1, in PDF points) of each line of `raw_text`, indexed like
    #: `raw_text.splitlines()`, so a line citation ("L41") can be highlighted later. None
    #: for a line whose position could not be matched, and for sources without layout
    #: (HTML).
    line_boxes: tuple[tuple[float, float, float, float] | None, ...] | None = None


class ParsedPdf(_IngestionModel):
    """A locally parsed PDF whose pages remain ordered and page-aware."""

    source_path: Path
    source_filename: NonEmptyString
    pages: tuple[PdfPageText, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @model_validator(mode="after")
    def validate_physical_page_order(self) -> Self:
        actual = tuple(page.pdf_page_index for page in self.pages)
        expected = tuple(range(len(self.pages)))
        if actual != expected:
            raise ValueError("PDF page indexes must be contiguous, ordered, and zero-based")
        return self


def _normalize_page_label(label: str) -> str | None:
    prefix, separator, number = label.partition("-")
    numeric_part = int(number if separator else prefix)
    if not 1 <= numeric_part <= 999:
        return None
    if separator:
        return f"{prefix.upper()}-{numeric_part}"
    return str(numeric_part)


def _candidate_labels(text: str, *, allow_standalone: bool) -> set[str]:
    candidates: set[str] = set()
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line or _PAGE_COUNTER_PATTERN.search(line):
            continue

        match = _FORM_10K_PATTERN.search(line)
        if match is None and allow_standalone:
            match = _STANDALONE_PAGE_PATTERN.fullmatch(line)
        if match is not None:
            label = _normalize_page_label(match.group("label"))
            if label is not None:
                candidates.add(label)
    return candidates


def detect_printed_page_number(
    footer_text: str,
    *,
    raw_page_text: str | None = None,
) -> str | None:
    """Return one reliable printed page label, or ``None``.

    Accepted evidence is deliberately narrow: a standalone number/page label or
    a trailing label on a line containing ``Form 10-K``. Browser/renderer page
    counters and conflicting candidates are rejected. If positional footer text
    has no candidate, the last few non-empty raw-text lines are checked, but that
    fallback accepts only an explicit ``Form 10-K`` footer line.
    """

    footer_candidates = _candidate_labels(footer_text, allow_standalone=True)
    if len(footer_candidates) == 1:
        return next(iter(footer_candidates))
    if footer_candidates or raw_page_text is None:
        return None

    trailing_lines = [line for line in raw_page_text.splitlines() if line.strip()][
        -_TRAILING_TEXT_LINE_LIMIT:
    ]
    trailing_candidates = _candidate_labels(
        "\n".join(trailing_lines),
        allow_standalone=False,
    )
    if len(trailing_candidates) != 1:
        return None
    return next(iter(trailing_candidates))


def _extract_footer_text(page: pymupdf.Page) -> str:
    footer_top = page.rect.height * (1 - _FOOTER_HEIGHT_FRACTION)
    footer_blocks: list[str] = []
    for block in page.get_text("blocks", sort=True):
        _x0, y0, _x1, _y1, text, _block_number, block_type = block
        if block_type == _TEXT_BLOCK_TYPE and y0 >= footer_top:
            footer_blocks.append(text)
    return "\n".join(footer_blocks)


def _line_boxes(page: pymupdf.Page, raw_text: str
                ) -> tuple[tuple[float, float, float, float] | None, ...]:
    """Match each line of `raw_text` to a laid-out line's bounding box.

    The text itself still comes from get_text("text"), unchanged, so citations and every
    other consumer see exactly what they saw before; positions are looked up alongside.
    Layout lines are consumed in reading order and matched on whitespace-normalised text,
    looking a few lines ahead to step over lines that the two extraction modes split
    differently. A line that cannot be matched gets None rather than a guessed box.
    """
    laid_out: list[tuple[str, tuple[float, float, float, float]]] = []
    for block in page.get_text("dict", sort=True).get("blocks", []):
        if block.get("type") != _TEXT_BLOCK_TYPE:
            continue
        for line in block.get("lines", []):
            text = " ".join("".join(span.get("text", "") for span in line.get("spans", [])).split())
            if text:
                laid_out.append((text, tuple(float(v) for v in line["bbox"])))
    boxes: list[tuple[float, float, float, float] | None] = []
    cursor = 0
    for raw_line in raw_text.splitlines():
        wanted = " ".join(raw_line.split())
        box = None
        if wanted:
            for offset in range(_LINE_MATCH_LOOKAHEAD):
                if cursor + offset < len(laid_out) and laid_out[cursor + offset][0] == wanted:
                    box = laid_out[cursor + offset][1]
                    cursor += offset + 1
                    break
        boxes.append(box)
    return tuple(boxes)


def _extract_page(page: pymupdf.Page) -> PdfPageText:
    raw_text = page.get_text("text", sort=True)
    return PdfPageText(
        pdf_page_index=page.number,
        printed_document_page=detect_printed_page_number(
            _extract_footer_text(page),
            raw_page_text=raw_text,
        ),
        raw_text=raw_text,
        line_boxes=_line_boxes(page, raw_text),
    )


def parse_pdf(path: str | Path) -> ParsedPdf:
    """Parse a local PDF into ordered pages while preserving each raw text value."""

    source_path = Path(path).expanduser().resolve()
    if not source_path.exists():
        raise PdfIngestionError(f"PDF does not exist: {source_path}")
    if not source_path.is_file():
        raise PdfIngestionError(f"PDF path is not a file: {source_path}")

    try:
        document = pymupdf.open(source_path)
    except Exception as exc:
        raise PdfIngestionError(f"Could not open PDF {source_path}: {exc}") from exc

    try:
        if not document.is_pdf:
            raise PdfIngestionError(f"File is not a PDF: {source_path}")
        if document.needs_pass:
            raise PdfIngestionError(f"PDF is password-protected: {source_path}")

        pages = tuple(_extract_page(page) for page in document)
    except PdfIngestionError:
        raise
    except Exception as exc:
        raise PdfIngestionError(f"Could not extract text from {source_path}: {exc}") from exc
    finally:
        document.close()

    return ParsedPdf(
        source_path=source_path,
        source_filename=source_path.name,
        pages=pages,
    )
