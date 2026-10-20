"""Pluggable statement taxonomy: which primary statement a heading names.

Why this is a separate component
--------------------------------
Recognising "Consolidated Financial Position" as the balance sheet is **jurisdiction and
language specific**. The default below is tuned to US-GAAP filings written in English,
because that is what SEC EDGAR publishes. A filing source from another country — IFRS,
another language, another regulator's rendering — needs a different vocabulary, and
should not require touching retrieval, routing or provenance to supply one.

Everything that consumes a taxonomy works in terms of `StatementKind`, never in terms of
a particular filer's wording. So a new source implements one class:

    class IfrsStatementTaxonomy(StatementTaxonomy):
        def classify(self, heading: str) -> StatementKind | None:
            ...

and passes it to the retriever. The routing map, the provenance check and the chunker are
unchanged.

What is deliberately NOT here: page/report structure (a property of the source format,
handled by the ingestion component), and the metric-to-statement map (an accounting fact
that holds across jurisdictions — revenue is on the income statement everywhere).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Protocol


class StatementKind(str, Enum):
    """The primary statements, named by role rather than by any filer's wording."""

    INCOME = "Consolidated Statements of Operations"
    BALANCE = "Consolidated Balance Sheets"
    CASHFLOW = "Consolidated Statements of Cash Flows"
    COMPREHENSIVE_INCOME = "Consolidated Statements of Comprehensive Income"
    EQUITY = "Consolidated Statements of Shareholders' Equity"


class StatementTaxonomy(Protocol):
    """Maps a statement in some reporting tradition onto a StatementKind.

    `content` is the page's text when available. A title-only implementation may ignore
    it; a content-based one needs it, because titles are precisely where filers differ
    ("Consolidated Statement of Income (Loss)", "Consolidated Results of Operations",
    "Consolidated Financial Position at December 31").
    """

    def classify(self, heading: str, content: str | None = None) -> StatementKind | None:
        ...


# Supporting schedules are not the statement itself and must never be mistaken for one.
_SUBORDINATE = re.compile(
    r"\((?:parenthetical|parentheticals|details|tables|policies)\)\s*$", re.I)


class UsGaapStatementTaxonomy:
    """Default taxonomy for English-language US-GAAP filings (SEC EDGAR)."""

    _PATTERNS: tuple[tuple[re.Pattern[str], StatementKind], ...] = (
        (re.compile(r"^(?:the\s+)?(?:consolidated|combined|condensed)?\s*"
                    r"(?:statements?\s+of\s+)?cash\s+flows?$", re.I),
         StatementKind.CASHFLOW),
        (re.compile(r"^(?:the\s+)?(?:consolidated|combined|condensed)?\s*"
                    r"(?:statements?\s+of\s+)?(?:financial\s+position|balance\s+sheets?)$", re.I),
         StatementKind.BALANCE),
        (re.compile(r"^(?:the\s+)?(?:consolidated|combined|condensed)?\s*"
                    r"statements?\s+of\s+comprehensive\s+(?:income|loss)$", re.I),
         StatementKind.COMPREHENSIVE_INCOME),
        (re.compile(r"^(?:the\s+)?(?:consolidated|combined|condensed)?\s*"
                    r"(?:statements?\s+of\s+)?(?:results\s+of\s+operations|operations|"
                    r"income|earnings|profit\s+and\s+loss)$", re.I),
         StatementKind.INCOME),
        (re.compile(r"^(?:the\s+)?(?:consolidated|combined|condensed)?\s*"
                    r"statements?\s+of\s+(?:changes\s+in\s+)?"
                    r"(?:shareholders|stockholders|shareowners)[’']?\s*equity$", re.I),
         StatementKind.EQUITY),
    )

    def normalize(self, heading: str) -> str:
        """Strip the qualifiers filers append to a statement title."""
        cleaned = " ".join(heading.replace("’", "'").split())
        # " - USD ($) $ in Millions | 12 Months Ended"
        cleaned = re.split(r"\s+[-–—|]\s+", cleaned)[0].strip()
        # " at December 31", " for the Years Ended ..."
        cleaned = re.sub(r"(?i)\s+(?:at|as\s+of|for(?:\s+the)?)\s+.*$", "", cleaned).strip()
        return re.sub(r"[.:]$", "", cleaned).strip()

    def classify(self, heading: str, content: str | None = None) -> StatementKind | None:
        del content                      # this taxonomy reads titles only
        cleaned = " ".join(heading.replace("’", "'").split())
        if not cleaned or _SUBORDINATE.search(cleaned):
            return None
        cleaned = self.normalize(heading)
        for pattern, kind in self._PATTERNS:
            if pattern.match(cleaned):
                return kind
        return None


DEFAULT_TAXONOMY: StatementTaxonomy = UsGaapStatementTaxonomy()


class LayaStatementTaxonomy:
    """Content-based taxonomy using Laya, an open-source System-1 decision model.

    Motivation: the pattern-based taxonomy reads TITLES, and titles are exactly what
    varies between filers and jurisdictions. It was tuned on three companies and missed
    3M's income statement on first contact, because 3M writes "Consolidated Statement of
    Income (Loss)" and the pattern was end-anchored. Each such miss invites another
    special case, which does not converge.

    Reading the statement's CONTENT instead sidesteps title wording entirely: a balance
    sheet lists assets and liabilities at a date whatever it is called, in whatever
    language. This is a four-option choice, which is where Laya is strongest — its
    accuracy falls off sharply above roughly twenty options.

    Classification is cached per page, because a filing is classified once and queried
    many times.
    """

    _QUESTION = {
        "kind": {
            "type": "choice",
            "instructions": (
                "Which primary financial statement is this table? Answer 'other' for "
                "notes, schedules, segment breakdowns, parentheticals and cover pages."),
            "criteria": {
                "income": "Income statement: revenue or net sales, cost of sales, "
                          "operating expenses, operating profit, net income, earnings "
                          "per share. Covers a PERIOD.",
                "balance": "Balance sheet / statement of financial position: assets, "
                           "liabilities and equity AT A DATE. Totals balance.",
                "cashflow": "Cash flow statement: operating, investing and financing "
                            "activities, net change in cash.",
                "equity": "Statement of changes in shareholders' equity: share capital, "
                          "retained earnings, movements in equity.",
                "other": "Anything else: notes, segment detail, parentheticals, cover "
                         "page, accounting policies, schedules.",
            },
        }
    }
    _MAP = {"income": StatementKind.INCOME, "balance": StatementKind.BALANCE,
            "cashflow": StatementKind.CASHFLOW, "equity": StatementKind.EQUITY}

    def __init__(self, agent=None, *, max_chars: int = 2500,
                 min_probability: float = 0.45) -> None:
        self._agent = agent
        self._max_chars = max_chars
        # Below this the choice is a coin toss; returning None lets the caller fall back
        # rather than commit to a guess.
        self._min_probability = min_probability
        self._cache: dict[str, StatementKind | None] = {}

    def _lazy_agent(self):
        if self._agent is None:
            import laya
            self._agent = laya.Agent()
        return self._agent

    def classify(self, heading: str, content: str | None = None) -> StatementKind | None:
        text = (content or heading or "").strip()
        if not text:
            return None
        # A subordinate schedule is identifiable from its title alone and is not worth
        # a model call.
        if _SUBORDINATE.search(" ".join(heading.split())):
            return None
        key = text[: self._max_chars]
        if key in self._cache:
            return self._cache[key]
        answer = self._lazy_agent().system_one(key, self._QUESTION)["answers"]["kind"]
        choice = answer["choice"]
        probability = answer["probabilities"].get(choice, 0.0)
        kind = self._MAP.get(choice) if probability >= self._min_probability else None
        self._cache[key] = kind
        return kind


class LlmStatementTaxonomy:
    """Identify the primary statements from the filing's list of report titles.

    One model call per FILING, not per page. A filing's table of contents is a few
    thousand tokens, and choosing which entries are the primary statements is a narrow,
    constrained task with a small answer space — the shape a small model handles well.

    This exists because the alternatives each fail in a characteristic way:

    * pattern matching reads titles but cannot generalise — "Consolidated Statement of
      Income (Loss)" defeats an end-anchored rule, and every filer brings a new variant;
    * a content classifier generalises but costs one call per page (~135 per filing) and
      still confused cash-flow statements in measurement.

    A model reading titles gets the generality without the per-page cost, and needs no
    jurisdiction-specific vocabulary: the same prompt works wherever the reader can name
    the statements.

    Usage differs from the other taxonomies: call `identify()` once with the whole title
    list, then `classify()` answers from the cached result.
    """

    _KINDS = {"income": StatementKind.INCOME, "balance": StatementKind.BALANCE,
              "cashflow": StatementKind.CASHFLOW, "equity": StatementKind.EQUITY}

    SYSTEM = (
        "You are given the list of report titles from one company's annual financial "
        "filing. Identify which entries are the PRIMARY consolidated financial "
        "statements.\n"
        "- income: the statement of income / operations / earnings (revenue through to "
        "net income), covering a period.\n"
        "- balance: the balance sheet / statement of financial position, at a date.\n"
        "- cashflow: the statement of cash flows.\n"
        "Companies word these differently; match on meaning, not wording. NEVER choose a "
        "title marked (Parenthetical), (Details), (Tables) or (Policies) — those are "
        "supporting schedules. Comprehensive income is NOT the income statement. Return "
        "the report id exactly as given, or null if the filing does not contain that "
        "statement."
    )

    @staticmethod
    def schema() -> dict:
        return {"type": "object",
                "properties": {k: {"anyOf": [{"type": "string"}, {"type": "null"}]}
                               for k in ("income", "balance", "cashflow")},
                "required": ["income", "balance", "cashflow"],
                "additionalProperties": False}

    def __init__(self, call) -> None:
        """`call(system, user, schema) -> dict` performs one model request."""
        self._call = call
        # Keyed by the heading TEXT, because that is what the chunker passes to
        # classify(). An earlier version keyed this by report id ("R3") while the
        # chunker passed the title, so classify() returned None for every page: no
        # statement was ever recognised, routing collapsed to whole-filing BM25, and
        # 19 of 20 filings failed end to end. The report id is kept as a second key.
        self._by_heading: dict[str, StatementKind] = {}
        self._by_report_id: dict[str, StatementKind] = {}

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(text.split()).casefold()[:110]

    def identify(self, titles: list[tuple[str, str]]) -> dict[str, str]:
        """Map kind -> report id, given [(report_id, title), ...]."""
        listing = "\n".join(f"{rid}\t{title}" for rid, title in titles)
        answer = self._call(self.SYSTEM, f"REPORTS:\n{listing}", self.schema()) or {}
        chosen: dict[str, str] = {}
        title_of = {rid: title for rid, title in titles}
        for kind, rid in answer.items():
            if isinstance(rid, str) and rid in title_of and kind in self._KINDS:
                chosen[kind] = rid
                self._by_report_id[rid] = self._KINDS[kind]
                self._by_heading[self._key(title_of[rid])] = self._KINDS[kind]
        return chosen

    @property
    def identified(self) -> bool:
        """True once the model has named at least one primary statement."""
        return bool(self._by_heading)

    def classify(self, heading: str, content: str | None = None) -> StatementKind | None:
        del content
        return (self._by_heading.get(self._key(heading))
                or self._by_report_id.get(heading.strip()))
