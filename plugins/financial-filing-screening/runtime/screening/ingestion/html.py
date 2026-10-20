"""HTML filing ingestion — the component-level alternative to `ingestion.pdf`.

The screening workflow's contract for its first component is: given a filing on disk,
return a `ParsedPdf` — ordered pages, each with a physical index, an optional printed
label, and raw text. Nothing downstream knows how that was produced, so supporting a new
source format is a component swap rather than a pipeline change.

A filing arrives here as a directory of SEC rendered reports (`R1.htm`, `R2.htm`, ...)
plus the `FilingSummary.xml` that names them. Each report is one coherent unit — a
statement, a note, the cover page — so each becomes one page:

    pdf_page_index        -> the report's position in the filing summary (0-based)
    printed_document_page -> the report's own identifier, e.g. "R5"
    raw_text              -> the report's title on its own line, then one table row per line

Rows are parsed structurally rather than by stripping tags, because the renderer puts a
newline inside every `<td>`; a naive substitution splits one table row across several
lines and destroys the column alignment needed to tell one fiscal year from another.
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path

from screening.ingestion.pdf import ParsedPdf, PdfPageText

_TAG = re.compile(r"<[^>]+>")
# The renderer appends an XBRL definition block after each table; it is UI chrome.
# Renderer chrome: the XBRL definition block after each table, and the element-metadata
# footer ("Namespace Prefix: | us-gaap_", "Data Type:", "Balance Type:", "Period Type:").
# Both are machine plumbing, not statement content, and they were consuming evidence slots
# that should hold table rows.
_CHROME = re.compile(
    r"(?i)^\s*\+?\s*references|Reference 1:|^\s*(namespace prefix|element name|data type|"
    r"balance type|period type|definition)\s*[:|]")


class HtmlIngestionError(RuntimeError):
    """Raised when a filing directory cannot be parsed into ordered pages."""


_TABLE_TAG = re.compile(r"(?i)<(/?)table\b[^>]*>")


def _drop_definition_popups(s: str) -> str:
    """Remove the renderer's hidden XBRL definition tables (`class="authRefData"`).

    Each statement row has one: a hidden pop-up holding the element's definition, its
    references and a nested details table. Text filtering (`_CHROME`) caught only some of
    its lines, and "X", "- Definition...", "- Details Name: | us-gaap_..." leaked into
    evidence chunks. Removed here by markup instead, matching nested <table> depth.
    """
    out: list[str] = []
    pos = 0
    for start in re.finditer(r'(?i)<table\b[^>]*class="authRefData"[^>]*>', s):
        if start.start() < pos:
            continue                     # nested inside a table already removed
        depth, end = 0, len(s)
        for tag in _TABLE_TAG.finditer(s, start.start()):
            depth += -1 if tag.group(1) else 1
            if depth == 0:
                end = tag.end()
                break
        out.append(s[pos:start.start()])
        pos = end
    out.append(s[pos:])
    return "".join(out)


def render_report(raw: str) -> str:
    """One rendered report as text, one table row per line."""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    s = _drop_definition_popups(s)
    lines: list[str] = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", s):
        cells = []
        for cell in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row):
            cell = re.sub(r"(?i)<br\s*/?>", " ", cell)
            cell = _html.unescape(_TAG.sub("", cell))
            cells.append(re.sub(r"\s+", " ", cell).strip())
        while cells and not cells[-1]:
            cells.pop()
        cells = [c for c in cells if c]
        if not cells:
            continue
        line = " | ".join(cells)
        if _CHROME.search(line):
            continue
        lines.append(line)
    return "\n".join(lines)


def report_titles(summary_xml: str) -> list[tuple[str, str]]:
    """(title, filename) for each rendered report, in filing order."""
    out: list[tuple[str, str]] = []
    for block in re.findall(r"(?is)<Report[^>]*>(.*?)</Report>", summary_xml):
        name = re.search(r"(?is)<ShortName>(.*?)</ShortName>", block)
        fn = re.search(r"(?is)<HtmlFileName>(.*?)</HtmlFileName>", block)
        if name and fn:
            out.append((_html.unescape(name.group(1)).strip(), fn.group(1).strip()))
    return out


def parse_html_filing(filing_path: str | Path) -> ParsedPdf:
    """Parse a directory of rendered HTML reports into a `ParsedPdf`."""
    directory = Path(filing_path)
    if not directory.is_dir():
        raise HtmlIngestionError(f"{directory} is not a filing directory")
    summary = directory / "FilingSummary.xml"
    if not summary.exists():
        raise HtmlIngestionError(f"no FilingSummary.xml in {directory}")

    titles = report_titles(summary.read_text(errors="replace"))
    if not titles:
        raise HtmlIngestionError(f"no rendered reports listed in {summary}")

    pages: list[PdfPageText] = []
    for title, filename in titles:
        report = directory / filename
        if not report.exists():
            continue                      # a report listed but not downloaded is skipped
        body = render_report(report.read_text(errors="replace"))
        text = f"{title}\n{body}".strip() or title
        pages.append(PdfPageText(
            pdf_page_index=len(pages),    # contiguous: ParsedPdf validates this
            printed_document_page=Path(filename).stem,
            raw_text=text,
        ))
    if not pages:
        raise HtmlIngestionError(f"no readable reports in {directory}")
    return ParsedPdf(source_path=directory, source_filename=f"{directory.name}.htm",
                     pages=tuple(pages))


# SEC's rendered cover page carries labelled identity fields, e.g.
#     Entity Registrant Name | CATERPILLAR INC
#     Document Fiscal Year Focus | 2019
# These are exact, so identity is read rather than guessed. The PDF path infers it from
# cover-page prose with a name pattern requiring a legal suffix like "Inc." — which does
# not match "CATERPILLAR INC" (no period) and is the wrong tool for a labelled table.
_COVER_FIELD = "{label} | "


def _cover_field(pages, label: str) -> str | None:
    for page in pages[:4]:
        for line in page.raw_text.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(label.lower() + " |"):
                value = stripped.split("|", 1)[1].strip()
                if value:
                    return value
    return None


def infer_html_filing_identity(document: ParsedPdf) -> tuple[str, int]:
    """Registrant name and fiscal year from the rendered cover page."""
    name = _cover_field(document.pages, "Entity Registrant Name")
    if not name:
        raise HtmlIngestionError(
            "Cover page has no 'Entity Registrant Name' field")

    year_text = (_cover_field(document.pages, "Document Fiscal Year Focus")
                 or _cover_field(document.pages, "Document Period End Date"))
    year = None
    if year_text:
        match = re.search(r"(19|20)\d{2}", year_text)
        if match:
            year = int(match.group(0))
    if year is None:
        raise HtmlIngestionError(
            "Cover page has no usable fiscal-year field")
    return " ".join(name.split()), year
