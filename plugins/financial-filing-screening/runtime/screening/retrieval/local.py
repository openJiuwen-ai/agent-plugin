"""Dependency-free lexical retrieval over locally parsed Form 10-K pages."""

from __future__ import annotations

import math
import re
import textwrap
import unicodedata
from collections import Counter
from collections.abc import Iterable
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from screening.domain import Evidence
from screening.ingestion import ParsedPdf, PdfPageText


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
#: upper bound on how many leading lines may form a page's period/unit header
_MAX_PAGE_HEADER_LINES = 4
#: a chunk already carrying period context does not need the page header copied into it
_HAS_PERIOD_CONTEXT = re.compile(
    r"(?i)\b(?:12|3|6|9)\s+months\s+ended\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"[a-z]*\.?\s+\d{1,2},\s*(?:19|20)\d{2}")

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_ITEM_HEADING_PATTERN = re.compile(
    r"^item\s+(?P<number>\d+[a-z]?)\.\s*(?P<title>.+)$",
    re.IGNORECASE,
)
_NOTE_HEADING_PATTERN = re.compile(
    r"^note\s+(?P<number>\d+)\s*[-\u2013\u2014]\s*(?P<title>.+)$",
    re.IGNORECASE,
)
# Statement-name recognition lives in `screening.retrieval.taxonomy`, because it is
# jurisdiction- and language-specific: a filing source from another country supplies its
# own StatementTaxonomy and nothing else in retrieval, routing or provenance changes.
from screening.retrieval.taxonomy import (  # noqa: E402
    DEFAULT_TAXONOMY,
    StatementKind,
    StatementTaxonomy,
)


def classify_statement_heading(heading: str,
                               taxonomy: StatementTaxonomy | None = None) -> str | None:
    """Canonical primary-statement name for a heading, or None."""
    kind = (taxonomy or DEFAULT_TAXONOMY).classify(heading)
    return kind.value if kind is not None else None


_STATEMENT_NAMES = {
    "consolidated balance sheets": "Consolidated Balance Sheets",
    "consolidated statements of cash flows": "Consolidated Statements of Cash Flows",
    "consolidated statements of comprehensive income": (
        "Consolidated Statements of Comprehensive Income"
    ),
    "consolidated statements of operations": "Consolidated Statements of Operations",
    "consolidated statements of shareholders' equity": (
        "Consolidated Statements of Shareholders' Equity"
    ),
    "consolidated statements of stockholders' equity": (
        "Consolidated Statements of Stockholders' Equity"
    ),
    "notes to consolidated financial statements": (
        "Notes to Consolidated Financial Statements"
    ),
}
_KNOWN_SECTION_NAMES = {
    "business risks": "Business Risks",
    "company stock performance": "Company Stock Performance",
    "general risks": "General Risks",
    "gross margin": "Gross Margin",
    "legal and other contingencies": "Legal and Other Contingencies",
    "liquidity and capital resources": "Liquidity and Capital Resources",
    "macroeconomic and industry risks": "Macroeconomic and Industry Risks",
    "operating expenses": "Operating Expenses",
    "products and services performance": "Products and Services Performance",
    "provision for income taxes": "Provision for Income Taxes",
    "report of independent registered public accounting firm": (
        "Report of Independent Registered Public Accounting Firm"
    ),
    "supply concentrations": "Supply Concentrations",
}
_SYNONYM_GROUPS = (
    frozenset({"revenue", "revenues", "sale", "sales"}),
    frozenset({"shareholder", "shareholders", "stockholder", "stockholders"}),
    frozenset({"supplier", "suppliers"}),
    frozenset({"regulatory", "regulation", "regulations", "legal", "litigation", "lawsuit"}),
    frozenset({"manufacture", "manufacturer", "manufacturers", "manufacturing", "production"}),
)


class _RetrievalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DocumentChunk(_RetrievalModel):
    """A page-bounded text chunk with complete retrieval provenance."""

    chunk_index: int = Field(ge=0)
    source_filename: NonEmptyString
    pdf_page_index: int = Field(ge=0)
    printed_document_page: NonEmptyString | None = None
    section: NonEmptyString
    text: NonEmptyString
    #: page line number (1-based, see screening.citations) of each line of `text`
    line_numbers: tuple[int, ...]

    @model_validator(mode="after")
    def one_line_number_per_line(self) -> Self:
        if len(self.line_numbers) != len(self.text.splitlines()):
            raise ValueError("DocumentChunk needs exactly one line number per text line")
        return self

    def as_evidence(self) -> Evidence:
        return Evidence(
            source_filename=self.source_filename,
            pdf_page_index=self.pdf_page_index,
            printed_document_page=self.printed_document_page,
            section_or_statement_name=self.section,
            exact_row_label=None,
            chunk_text=self.text,
            line_numbers=self.line_numbers,
        )


#: a page line and its 1-based line number on that page
_NumberedLine = tuple[int, str]


def _strip_blank_edges(lines: list[_NumberedLine]) -> list[_NumberedLine]:
    start, end = 0, len(lines)
    while start < end and not lines[start][1].strip():
        start += 1
    while end > start and not lines[end - 1][1].strip():
        end -= 1
    return lines[start:end]


def _joined(lines: list[_NumberedLine]) -> str:
    return "\n".join(text for _, text in lines)


class RetrievalHit(_RetrievalModel):
    """A diagnostic BM25 score paired with domain evidence."""

    rank: int = Field(ge=1)
    score: float = Field(gt=0)
    evidence: Evidence


def _normalized_heading(line: str) -> str:
    return " ".join(line.strip().replace("’", "'").split())


def _detect_section(line: str, taxonomy: StatementTaxonomy | None = None) -> str | None:
    heading = _normalized_heading(line)
    if not heading or len(heading) > 180:
        return None

    item_match = _ITEM_HEADING_PATTERN.fullmatch(heading)
    if item_match:
        if re.search(r"\s\d{1,3}$", item_match.group("title")):
            return None
        return f"Item {item_match.group('number').upper()}. {item_match.group('title')}"

    note_match = _NOTE_HEADING_PATTERN.fullmatch(heading)
    if note_match:
        return f"Note {note_match.group('number')} - {note_match.group('title')}"

    lookup_key = heading.casefold()
    if lookup_key in _STATEMENT_NAMES:
        return _STATEMENT_NAMES[lookup_key]
    generalised = classify_statement_heading(heading, taxonomy)
    if generalised is not None:
        return generalised
    if lookup_key in _KNOWN_SECTION_NAMES:
        return _KNOWN_SECTION_NAMES[lookup_key]
    if re.fullmatch(r"part\s+[ivxlcdm]+", heading, re.IGNORECASE):
        return heading.upper()
    return None


def _page_sections(
    page: PdfPageText,
    starting_section: str,
    taxonomy: StatementTaxonomy | None = None,
) -> tuple[list[tuple[str, list[_NumberedLine]]], str]:
    current_section = starting_section
    current_lines: list[_NumberedLine] = []
    sections: list[tuple[str, list[_NumberedLine]]] = []
    found_section_on_page = False

    for number, line in enumerate(page.raw_text.splitlines(), start=1):
        detected_section = _detect_section(line, taxonomy)
        if detected_section is not None and current_lines:
            text = _joined(current_lines).strip()
            if text and (found_section_on_page or len(text) > 120):
                sections.append((current_section, _strip_blank_edges(current_lines)))
                current_lines = []
        if detected_section is not None:
            current_section = detected_section
            found_section_on_page = True
        current_lines.append((number, line.rstrip()))

    if _joined(current_lines).strip():
        sections.append((current_section, _strip_blank_edges(current_lines)))
    return sections, current_section


def _split_long_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    return textwrap.wrap(
        line,
        width=max_chars,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
    )


def _trailing_overlap(lines: list[_NumberedLine], overlap_chars: int) -> list[_NumberedLine]:
    if overlap_chars == 0:
        return []
    selected: list[_NumberedLine] = []
    size = 0
    for line in reversed(lines):
        addition = len(line[1]) + (1 if selected else 0)
        if selected and size + addition > overlap_chars:
            break
        selected.append(line)
        size += addition
        if size >= overlap_chars:
            break
    return list(reversed(selected))


def _split_section_text(
    lines: list[_NumberedLine],
    *,
    max_chars: int,
    overlap_chars: int,
) -> Iterable[list[_NumberedLine]]:
    # A line too long for one chunk is wrapped; every part keeps the page line's number,
    # so a citation of any part resolves to the whole line on the page.
    logical_lines = [
        (number, part)
        for number, line in lines
        for part in _split_long_line(line.rstrip(), max_chars)
    ]
    current: list[_NumberedLine] = []

    for line in logical_lines:
        proposed_size = len(_joined([*current, line]))
        if current and proposed_size > max_chars:
            chunk = _strip_blank_edges(current)
            if chunk:
                yield chunk
            current = _trailing_overlap(current, overlap_chars)
            if current and len(_joined([*current, line])) > max_chars:
                current = []
        current.append(line)

    chunk = _strip_blank_edges(current)
    if chunk:
        yield chunk


def chunk_document(
    document: ParsedPdf,
    *,
    max_chars: int = 1_600,
    overlap_chars: int = 200,
    section_per_page: bool = False,
    taxonomy: StatementTaxonomy | None = None,
) -> tuple[DocumentChunk, ...]:
    """Create ordered, page-bounded chunks with deterministic section labels.

    `section_per_page` controls whether a section may span pages. In a continuous PDF it
    must: a statement begun on one page carries on to the next, so the section is carried
    forward until a new heading appears. That is the default.

    It is wrong for a filing whose pages are *independent units* — SEC's rendered reports,
    where each page is a whole statement, note or schedule. There, carrying forward makes
    one statement heading claim every page after it: on a real 10-K this labelled 536 of
    566 chunks "Consolidated Balance Sheets", which would defeat any section-based routing.
    With this flag each page starts its own section, falling back to the page's first line
    when the heading is not a recognised one.
    """

    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0:
        raise ValueError("overlap_chars cannot be negative")
    if overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be smaller than max_chars")

    chunks: list[DocumentChunk] = []
    current_section = "Document"
    for page in document.pages:
        page_header: list[_NumberedLine] = []
        if section_per_page:
            lines = [(n, l) for n, l in enumerate(page.raw_text.splitlines(), start=1)
                     if l.strip()]
            first = lines[0][1] if lines else ""
            detected = _detect_section(first, taxonomy)
            heading = _normalized_heading(first)
            current_section = detected or (heading[:180] if heading else "Document")
            # A statement's period columns are declared once, at the top of the page
            # ("... | 12 Months Ended | Dec. 31, 2019 | Dec. 31, 2018"). Splitting the page
            # into chunks leaves every chunk but the first without them, so a row reading
            # "6,912 | 6,558 | 5,706" cannot be bound to a fiscal year at all — and the
            # model correctly refuses to extract it. The header travels with every chunk.
            # Take leading lines up to AND INCLUDING the one that names the periods.
            # A fixed count is wrong: the report title, a title-plus-units line and the
            # date row appear in varying order, so counting two lines captured both
            # titles and left the dates behind — which is the whole point of the header.
            header_lines: list[_NumberedLine] = []
            for line in lines[:_MAX_PAGE_HEADER_LINES]:
                header_lines.append(line)
                if _HAS_PERIOD_CONTEXT.search(line[1]):
                    break
            page_header = (header_lines
                           if any(_HAS_PERIOD_CONTEXT.search(l) for _, l in header_lines)
                           else [])
        page_sections, carried = _page_sections(page, current_section, taxonomy)
        if not section_per_page:
            current_section = carried
        for section, section_lines in page_sections:
            for chunk_lines in _split_section_text(
                section_lines,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            ):
                # Prepend the page's period/unit header unless this chunk already
                # begins with it. Verbatim citation is unaffected: the data rows are
                # copied through unchanged, and the header lines are themselves
                # verbatim lines from the same page.
                # Only copy the header in when this chunk cannot date its own columns.
                # Copying it unconditionally inflated prompts past the model's context
                # window and the requests were rejected outright (400, input_tokens).
                # Header lines keep their own page line numbers, so they are citable too.
                chunk_text = _joined(chunk_lines)
                if (page_header
                        and chunk_lines[:len(page_header)] != page_header
                        and not _HAS_PERIOD_CONTEXT.search(chunk_text)):
                    chunk_lines = [*page_header, *chunk_lines]
                text = _joined(chunk_lines).strip()
                chunks.append(
                    DocumentChunk(
                        chunk_index=len(chunks),
                        source_filename=document.source_filename,
                        pdf_page_index=page.pdf_page_index,
                        printed_document_page=page.printed_document_page,
                        section=section,
                        text=text,
                        line_numbers=tuple(number for number, _ in chunk_lines),
                    )
                )
    return tuple(chunks)


def _tokenize(text: str) -> list[str]:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return _TOKEN_PATTERN.findall(ascii_text.casefold())


def _expand_query_tokens(tokens: list[str]) -> list[str]:
    expanded = set(tokens)
    for group in _SYNONYM_GROUPS:
        if expanded.intersection(group):
            expanded.update(group)
    return sorted(expanded)


def _phrase_boost(chunk: DocumentChunk, query_tokens: list[str]) -> float:
    """Apply small lexical boosts for exact phrases and cash-flow terminology."""

    if not query_tokens:
        return 0.0
    text_tokens = _tokenize(chunk.text)
    section_tokens = _tokenize(chunk.section)
    text = " ".join(text_tokens)
    section = " ".join(section_tokens)
    query_phrase = " ".join(query_tokens)
    boost = 0.0

    if len(query_tokens) > 1 and query_phrase in text:
        boost += 8.0
    if len(query_tokens) > 1 and query_phrase in section:
        boost += 4.0
    for left, right in zip(query_tokens, query_tokens[1:]):
        phrase = f"{left} {right}"
        if phrase in text:
            boost += 2.0

    query_set = set(query_tokens)
    if {"operating", "cash", "flow"}.issubset(query_set):
        if chunk.section == "Consolidated Statements of Cash Flows":
            boost += 10.0
        if "cash generated by operating activities" in text:
            boost += 8.0
    return boost


class LocalBM25Retriever:
    """Small in-memory BM25 index for one or more local document chunks."""

    def __init__(
        self,
        chunks: Iterable[DocumentChunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if k1 <= 0:
            raise ValueError("k1 must be greater than zero")
        if not 0 <= b <= 1:
            raise ValueError("b must be between zero and one")

        self.chunks = tuple(chunks)
        self.k1 = k1
        self.b = b
        self._document_tokens = tuple(
            _tokenize(f"{chunk.section}\n{chunk.section}\n{chunk.text}")
            for chunk in self.chunks
        )
        self._term_frequencies = tuple(Counter(tokens) for tokens in self._document_tokens)
        self._document_frequencies = Counter(
            token
            for tokens in self._document_tokens
            for token in set(tokens)
        )
        self._average_document_length = (
            sum(len(tokens) for tokens in self._document_tokens) / len(self._document_tokens)
            if self._document_tokens
            else 0.0
        )

    @classmethod
    def from_document(
        cls,
        document: ParsedPdf,
        *,
        max_chars: int = 1_600,
        overlap_chars: int = 200,
        section_per_page: bool = False,
    ) -> Self:
        return cls(
            chunk_document(
                document,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
                section_per_page=section_per_page,
            )
        )

    def _score(self, document_index: int, query_tokens: list[str]) -> float:
        if not self.chunks or self._average_document_length == 0:
            return 0.0

        frequencies = self._term_frequencies[document_index]
        document_length = len(self._document_tokens[document_index])
        score = 0.0
        for token in query_tokens:
            term_frequency = frequencies[token]
            if term_frequency == 0:
                continue
            document_frequency = self._document_frequencies[token]
            inverse_document_frequency = math.log(
                1 + (len(self.chunks) - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            denominator = term_frequency + self.k1 * (
                1 - self.b
                + self.b * document_length / self._average_document_length
            )
            score += inverse_document_frequency * (
                term_frequency * (self.k1 + 1) / denominator
            )
        return score

    def search(self, query: str, *, top_k: int = 5) -> tuple[RetrievalHit, ...]:
        """Return scored local matches for diagnostics."""

        if not query.strip():
            raise ValueError("query cannot be blank")
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero")

        original_query_tokens = _tokenize(query)
        query_tokens = _expand_query_tokens(original_query_tokens)
        scored = [
            (
                self._score(index, query_tokens)
                + _phrase_boost(chunk, original_query_tokens),
                chunk,
            )
            for index, chunk in enumerate(self.chunks)
        ]
        ranked = sorted(
            (item for item in scored if item[0] > 0),
            key=lambda item: (-item[0], item[1].pdf_page_index, item[1].chunk_index),
        )[:top_k]
        return tuple(
            RetrievalHit(rank=rank, score=score, evidence=chunk.as_evidence())
            for rank, (score, chunk) in enumerate(ranked, start=1)
        )

    def retrieve(self, query: str, *, top_k: int = 5) -> tuple[Evidence, ...]:
        """Return top-k domain Evidence objects in descending relevance order."""

        return tuple(hit.evidence for hit in self.search(query, top_k=top_k))


class StatementRoutedRetriever:
    """BM25 restricted to the statement a metric can legitimately come from.

    Plain BM25 over a whole filing does not discriminate: a real 10-K yields thousands of
    chunks and dozens of note tables repeat terms like "total liabilities" more densely
    than the balance sheet does, so scores cluster within a few percent and the right
    chunk lands at rank 2-5 or not at all.

    Widening the section vocabulary alone did not fix that (labelled chunks rose from 9.5%
    to 60.1% with no change in recall). The term-frequency signal simply cannot separate a
    primary statement from a note that talks about the same things.

    Which statement holds a given metric is not a retrieval question at all — it is known.
    Total liabilities is on the balance sheet. So the candidate set is restricted first, by
    section, and BM25 runs only inside it. Chunks whose section is unknown are kept as a
    fallback so an unrecognised heading degrades rather than eliminates the evidence.
    """

    #: canonical section names, taken from the taxonomy's vocabulary-independent kinds
    INCOME = StatementKind.INCOME.value
    BALANCE = StatementKind.BALANCE.value
    CASHFLOW = StatementKind.CASHFLOW.value
    FALLBACK = "Document"

    def __init__(self, chunks, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._all = tuple(chunks)
        self._k1, self._b = k1, b
        self._cache: dict[tuple[str, ...], LocalBM25Retriever] = {}

    @classmethod
    def from_document(cls, document, *, max_chars: int = 1_600,
                      overlap_chars: int = 200, section_per_page: bool = True,
                      taxonomy=None):
        # Defaults to True: this retriever exists for report-structured filings.
        return cls(chunk_document(document, max_chars=max_chars,
                                  overlap_chars=overlap_chars,
                                  section_per_page=section_per_page,
                                  taxonomy=taxonomy))

    def _index_for(self, sections: tuple[str, ...]) -> LocalBM25Retriever:
        key = tuple(sorted(sections))
        if key not in self._cache:
            wanted = set(sections)
            subset = [c for c in self._all if c.section in wanted]
            if not subset:
                subset = list(self._all)
            self._cache[key] = LocalBM25Retriever(subset, k1=self._k1, b=self._b)
        return self._cache[key]

    @property
    def chunks(self):
        """All indexed chunks — mirrors LocalBM25Retriever so callers are interchangeable."""
        return self._all

    def section_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self._all:
            counts[c.section] = counts.get(c.section, 0) + 1
        return counts

    def retrieve(self, query: str, *, sections: tuple[str, ...] | None = None,
                 top_k: int = 5):
        """Evidence for `query`: the routed statement first, then the rest of the filing.

        Routing is a PRIOR, not a filter. As a hard filter it cost recall wherever a
        metric is not presented on the statement it "should" be on — operating income
        fell from 100% to 77.8%, because some filers show operating profit only in a
        segment note, or head their income statement in wording no pattern recognises.

        So the routed section supplies the leading results and the unrestricted index
        fills any remaining slots. Recall can then only improve relative to plain BM25,
        while the routed chunks still get first claim on the budget.
        """
        if not sections:
            return LocalBM25Retriever(self._all, k1=self._k1,
                                      b=self._b).retrieve(query, top_k=top_k)

        wanted = tuple(sections) + (self.FALLBACK,)
        primary = self._index_for(wanted).retrieve(query, top_k=top_k)
        # Fall back to the whole filing only when the routed statement yielded NOTHING.
        # Topping up whenever it returned "too few" was worse than useless: a statement
        # page often produces just two chunks, which let six chunks of a segment-detail
        # note swamp the evidence 6-to-2. Mixed-page evidence then invites a citation
        # naming one page while quoting a row from another, which provenance rejects.
        if primary:
            return primary

        seen = {(e.pdf_page_index, e.chunk_text) for e in primary}
        globally = LocalBM25Retriever(self._all, k1=self._k1, b=self._b).retrieve(
            query, top_k=top_k)
        topped = list(primary)
        for evidence in globally:
            if len(topped) >= top_k:
                break
            key = (evidence.pdf_page_index, evidence.chunk_text)
            if key not in seen:
                seen.add(key)
                topped.append(evidence)
        return tuple(topped)
