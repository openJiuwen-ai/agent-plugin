"""Single-agent openJiuwen workflow for end-to-end filing screening."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from openjiuwen.core.workflow import WorkflowComponent

from screening.domain import (
    CompanyFinancials,
    CriterionResult,
    Evidence,
    FinancialMetric,
    MetricName,
    PolicyThresholds,
    RiskAssessment,
    RiskCategory,
    RiskStatus,
    ScreeningResult,
    decision_reasons,
    evaluate_financial_criteria,
    final_policy_decision,
)
from screening.citations import CitationError, resolve_line_citation
from screening.extraction.openjiuwen import _error_result
from screening.domain.calculations import to_common_unit
from screening.extraction import (
    RISK_QUERIES,
    ExtractionStatus,
    MetricExtractionRequest,
    OpenJiuwenExtractionConfig,
    RiskExtractionRequest,
    create_metric_extraction_component,
    create_risk_assessment_component,
    extraction_response_schema,
    metric_result_from_payload,
    retrieve_metric_evidence,
    risk_response_schema,
    risk_result_from_payload,
    serialize_evidence,
    validate_extraction_support,
    validate_risk_support,
)
from screening.ingestion import ParsedPdf, parse_pdf
from screening.reporting import generate_screening_report
from screening.retrieval import LocalBM25Retriever


class ScreeningWorkflowError(RuntimeError):
    """Raised when a workflow stage cannot produce a trustworthy result."""


class ScreeningWorkflowInput(BaseModel):
    """Public structured input for the complete screening workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filing_path: Path
    policy: PolicyThresholds

    @model_validator(mode="after")
    def require_supported_filing_path(self) -> Self:
        """A filing is either a single PDF or a directory of rendered HTML reports.

        The check was originally "must end in .pdf". That is the right guard for PDF
        ingestion and the wrong one in general: it encoded the source format into the
        workflow's public input, so adding a second ingestion component would have meant
        changing the contract every caller depends on. It now accepts either shape and
        leaves the format decision to the ingestion component, which is where it belongs.
        """
        if self.filing_path.suffix.casefold() == ".pdf":
            return self
        if self.filing_path.is_dir():
            return self
        raise ValueError(
            "filing_path must point to a PDF file or to a directory of HTML reports")


@dataclass(frozen=True, slots=True)
class _MetricTask:
    alias: str
    metric_name: MetricName
    year_offset: int = 0
    required: bool = True


FINANCIAL_TASKS = (
    _MetricTask("revenue_current", MetricName.REVENUE),
    _MetricTask("revenue_prior", MetricName.REVENUE, year_offset=-1),
    _MetricTask("operating_income", MetricName.OPERATING_INCOME),
    _MetricTask("operating_cash_flow", MetricName.OPERATING_CASH_FLOW),
    _MetricTask("assets", MetricName.ASSETS),
    _MetricTask("liabilities", MetricName.LIABILITIES),
    _MetricTask("shareholders_equity", MetricName.SHAREHOLDERS_EQUITY),
    _MetricTask(
        "redeemable_noncontrolling_interests",
        MetricName.REDEEMABLE_NONCONTROLLING_INTERESTS,
        required=False,
    ),
    _MetricTask(
        "noncontrolling_interests",
        MetricName.NONCONTROLLING_INTERESTS,
        required=False,
    ),
    _MetricTask(
        "other_temporary_mezzanine_equity",
        MetricName.OTHER_TEMPORARY_MEZZANINE_EQUITY,
        required=False,
    ),
)

RISK_TASKS = (RiskCategory.REGULATORY, RiskCategory.SUPPLY_CHAIN)

_EXACT_REGISTRANT_PATTERN = re.compile(
    r"(?im)^[ \t]*(?P<name>[^\n]{2,120}?)[ \t]*\n"
    r"(?:[ \t]*\n)*[ \t]*\(Exact name of Registrant as specified in its charter\)"
)
_LEGAL_NAME_PATTERN = re.compile(
    r"(?i)^[A-Z][^\n]{1,120}?(?:Inc\.|Incorporated|Corporation|Corp\.|Ltd\.|LLC|plc)$"
)
_FISCAL_YEAR_PATTERNS = (
    re.compile(r"(?i)for the fiscal year ended[^\n]*\b(?P<year>20\d{2})\b"),
    re.compile(r"(?i)\bFY\s*(?P<year>20\d{2})\b"),
    re.compile(r"(?i)\b(?P<year>20\d{2})\s+Form\s+10-K\b"),
)


def infer_filing_identity(document: ParsedPdf) -> tuple[str, int]:
    """Infer cover-page company name and current fiscal year without an LLM."""

    cover_text = "\n".join(page.raw_text for page in document.pages[:4])
    company_match = _EXACT_REGISTRANT_PATTERN.search(cover_text)
    if company_match is not None:
        company_name = " ".join(company_match.group("name").split())
    else:
        company_name = ""
        for raw_line in cover_text.splitlines()[:80]:
            line = " ".join(raw_line.split())
            if _LEGAL_NAME_PATTERN.fullmatch(line):
                company_name = line
                break
    if not company_name:
        raise ScreeningWorkflowError(
            "Could not determine the registrant name from the filing cover page."
        )

    fiscal_year: int | None = None
    for pattern in _FISCAL_YEAR_PATTERNS:
        year_match = pattern.search(cover_text)
        if year_match is not None:
            fiscal_year = int(year_match.group("year"))
            break
    if fiscal_year is None:
        raise ScreeningWorkflowError(
            "Could not determine the fiscal year from the filing cover page."
        )
    return company_name, fiscal_year


class IngestFilingComponent(WorkflowComponent):
    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        try:
            workflow_input = ScreeningWorkflowInput.model_validate(inputs)
            document = parse_pdf(workflow_input.filing_path)
            company_name, fiscal_year = infer_filing_identity(document)
        except ScreeningWorkflowError:
            raise
        except Exception as exc:
            raise ScreeningWorkflowError(f"Filing ingestion failed: {exc}") from exc
        return {
            "document": document,
            "company_name": company_name,
            "fiscal_year": fiscal_year,
            "policy": workflow_input.policy,
        }


class IngestHtmlFilingComponent(WorkflowComponent):
    """Ingest a filing supplied as a directory of rendered HTML reports.

    Interchangeable with IngestFilingComponent: same inputs, same outputs, same ParsedPdf
    contract. Only the source format differs, so nothing downstream changes.
    """

    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        from screening.ingestion.html import (parse_html_filing,
                                              infer_html_filing_identity)
        try:
            workflow_input = ScreeningWorkflowInput.model_validate(inputs)
            document = parse_html_filing(workflow_input.filing_path)
            # Prefer the cover page's labelled fields; fall back to the prose heuristics
            # so an unusual rendering still has a chance.
            try:
                company_name, fiscal_year = infer_html_filing_identity(document)
            except Exception:
                company_name, fiscal_year = infer_filing_identity(document)
        except ScreeningWorkflowError:
            raise
        except Exception as exc:
            raise ScreeningWorkflowError(f"Filing ingestion failed: {exc}") from exc
        return {
            "document": document,
            "company_name": company_name,
            "fiscal_year": fiscal_year,
            "policy": workflow_input.policy,
        }


def _statement_taxonomy_for(document, config):
    """Identify the filing's primary statements once, with the model.

    Pattern-matching statement titles scored 47/51 across 17 filers and could not handle
    "Consolidated Statement of Income (Loss)" or Microsoft's wording. Handing the model
    the filing's ~135 report titles and asking which three are the primary statements
    scored 51/51 — one call per filing, against the nine extraction calls that follow.

    Falls back to the pattern taxonomy if the call fails, so a model outage degrades the
    run rather than ending it.
    """
    from screening.retrieval.taxonomy import LlmStatementTaxonomy

    if config is None:
        return None
    try:
        taxonomy = LlmStatementTaxonomy(_taxonomy_call(config))
        titles = [(page.printed_document_page or str(page.pdf_page_index),
                   page.raw_text.split("\n")[0][:110])
                  for page in document.pages[:_TAXONOMY_MAX_PAGES]]
        taxonomy.identify(titles)
        # A call that succeeds but names nothing must not be returned: passing an empty
        # taxonomy bypasses the pattern fallback entirely and leaves every page
        # unclassified, which is strictly worse than not asking the model at all.
        return taxonomy if taxonomy.identified else None
    except Exception:
        return None                      # pattern taxonomy remains the default


#: how many leading reports to show the model; primary statements are always near the front
_TAXONOMY_MAX_PAGES = 16


def _taxonomy_call(config):
    """One structured request against the configured model endpoint."""
    import json as _json
    import urllib.request

    def call(system: str, user: str, schema: dict):
        payload = {
            "model": config.model_name,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": 300, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "statements", "strict": True,
                                                "schema": schema}},
        }
        request = urllib.request.Request(
            f"{str(config.api_base).rstrip('/')}/chat/completions",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {config.api_key}"},
            method="POST")
        with urllib.request.urlopen(request, timeout=120) as response:
            body = _json.loads(response.read().decode())
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        try:
            return _json.loads(text[text.find("{"): text.rfind("}") + 1])
        except Exception:
            return {}

    return call


class RetrieveFinancialEvidenceComponent(WorkflowComponent):
    def __init__(self, *, top_k: int, routed: bool = False, config=None) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero")
        self.top_k = top_k
        self.routed = routed
        self.config = config

    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        document = ParsedPdf.model_validate(inputs["document"])
        fiscal_year = int(inputs["fiscal_year"])
        if self.routed:
            from screening.retrieval import StatementRoutedRetriever
            taxonomy = _statement_taxonomy_for(document, self.config)
            retriever = StatementRoutedRetriever.from_document(document,
                                                               taxonomy=taxonomy)
        else:
            retriever = LocalBM25Retriever.from_document(document)
        requests: dict[str, MetricExtractionRequest] = {}
        evidence_by_task: dict[str, tuple[Evidence, ...]] = {}
        request_json_by_task: dict[str, str] = {}
        evidence_json_by_task: dict[str, str] = {}

        for task in FINANCIAL_TASKS:
            request = MetricExtractionRequest(
                metric_name=task.metric_name,
                fiscal_year=fiscal_year + task.year_offset,
            )
            evidence = retrieve_metric_evidence(
                retriever,
                task.metric_name,
                top_k=self.top_k,
            )
            requests[task.alias] = request
            evidence_by_task[task.alias] = evidence
            request_json_by_task[task.alias] = request.model_dump_json()
            evidence_json_by_task[task.alias] = serialize_evidence(evidence, numbered=True)

        return {
            "document": document,
            "company_name": inputs["company_name"],
            "fiscal_year": fiscal_year,
            "policy": PolicyThresholds.model_validate(inputs["policy"]),
            "requests": requests,
            "evidence_by_task": evidence_by_task,
            "request_json_by_task": request_json_by_task,
            "evidence_json_by_task": evidence_json_by_task,
        }


#: metrics that can be computed from other reported figures when not printed directly
_DERIVABLE_METRICS = frozenset({"liabilities"})


def _derive_metric(alias: str, metrics: dict[str, FinancialMetric]) -> FinancialMetric | None:
    """Compute a balance-sheet figure the filing does not state outright.

    Only `liabilities` is derivable today: assets minus equity. Equity may or may not
    already include noncontrolling interests depending on the filer's presentation, so
    the components are added only when they were separately reported AND doing so does
    not overshoot assets — the same ambiguity handled in `reconcile_balance_sheet`.

    Returns None when the inputs are not available, so the caller still reports an honest
    failure rather than inventing a number.
    """
    if alias != "liabilities":
        return None
    assets = metrics.get("assets")
    equity = metrics.get("shareholders_equity")
    if assets is None or equity is None:
        return None

    equity_total = equity.value
    for optional in ("noncontrolling_interests", "redeemable_noncontrolling_interests",
                     "other_temporary_mezzanine_equity"):
        component = metrics.get(optional)
        if component is not None and equity_total + component.value <= assets.value:
            equity_total += component.value

    derived_value = assets.value - equity_total
    if derived_value < 0:
        return None
    return FinancialMetric(
        name=MetricName.LIABILITIES,
        value=derived_value,
        unit=assets.unit,
        # A derived balance-sheet figure belongs to the same instant as the figures it
        # was computed from; assets carries that period.
        fiscal_period=assets.fiscal_period,
        evidence=tuple(assets.evidence) + tuple(equity.evidence),
    )


def _candidate_or_error(payload: Any, request: MetricExtractionRequest):
    """One metric's model answer, or an ERROR result for that metric alone.

    An answer that breaks the result contract (e.g. VERIFIED with every field null, seen
    for an optional metric on 3M FY2018) used to raise and end the whole filing. It is now
    recorded as that metric's failure; whether the filing survives is decided below by
    whether the metric is required, like any other unverified metric.
    """
    try:
        return metric_result_from_payload(payload)
    except RuntimeError as exc:
        return _error_result(request, str(exc))


class DeterministicFinancialCalculationsComponent(WorkflowComponent):
    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        requests = {
            alias: MetricExtractionRequest.model_validate(value)
            for alias, value in inputs["requests"].items()
        }
        evidence_by_task = {
            alias: tuple(Evidence.model_validate(item) for item in values)
            for alias, values in inputs["evidence_by_task"].items()
        }
        document = ParsedPdf.model_validate(inputs["document"])
        metrics: dict[str, FinancialMetric] = {}
        failures: list[str] = []
        deferred: list[tuple[Any, Any]] = []

        for task in FINANCIAL_TASKS:
            request = requests[task.alias]
            candidate = _candidate_or_error(inputs["candidates"][task.alias], request)
            validated = validate_extraction_support(
                candidate,
                request,
                evidence_by_task[task.alias],
                document,
            )
            if validated.status is ExtractionStatus.VERIFIED:
                metrics[task.alias] = validated.to_financial_metric()
            elif task.required and task.alias in _DERIVABLE_METRICS:
                # Deferred to a second pass: FINANCIAL_TASKS processes `liabilities`
                # BEFORE `shareholders_equity`, so deriving it here would always look for
                # an equity figure that has not been extracted yet.
                deferred.append((task, validated))
                # Some filers never print the line at all. A balance sheet may show only
                # "Total liabilities and stockholders' equity", in which case total
                # liabilities is not a figure to be found — it is one to be computed from
                # figures that ARE printed. Asking a model to extract an absent number is
                # unfair, and on real filings it correctly refuses:
                #   "The total liabilities and stockholders' equity is reported, but the
                #    specific value of liabilities is not explicitly stated."
                # The arithmetic belongs here, with the other deterministic calculations,
                # not in the model.
            elif task.required:
                failures.append(
                    f"{task.alias}: {validated.error_message or validated.status.value}"
                )

        # Statements in one filing may be printed at different scales; restate them in
        # one unit before any arithmetic, including the derivation below.
        metrics = to_common_unit(metrics)

        # Second pass: metrics the filing never states outright, computed from figures it
        # does state. Runs after every extraction has been attempted so the inputs exist.
        for task, validated in deferred:
            derived = _derive_metric(task.alias, metrics)
            if derived is not None:
                metrics[task.alias] = derived
            else:
                failures.append(
                    f"{task.alias}: {validated.error_message or validated.status.value}"
                    " (and could not be derived from the other balance-sheet figures)")

        if failures:
            raise ScreeningWorkflowError(
                "Required financial extraction was not verified: " + "; ".join(failures)
            )

        financials = CompanyFinancials(
            company_name=inputs["company_name"],
            fiscal_year=inputs["fiscal_year"],
            revenue_current=metrics["revenue_current"],
            revenue_prior=metrics["revenue_prior"],
            operating_income=metrics["operating_income"],
            operating_cash_flow=metrics["operating_cash_flow"],
            assets=metrics["assets"],
            liabilities=metrics["liabilities"],
            shareholders_equity=metrics["shareholders_equity"],
            redeemable_noncontrolling_interests=metrics.get(
                "redeemable_noncontrolling_interests"
            ),
            noncontrolling_interests=metrics.get("noncontrolling_interests"),
            other_temporary_mezzanine_equity=metrics.get(
                "other_temporary_mezzanine_equity"
            ),
        )
        policy = PolicyThresholds.model_validate(inputs["policy"])
        criteria = evaluate_financial_criteria(financials, policy)
        return {
            "document": document,
            "financials": financials,
            "criteria": criteria,
            "policy": policy,
        }


class RetrieveRiskEvidenceComponent(WorkflowComponent):
    def __init__(self, *, top_k: int, section_per_page: bool = False) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be greater than zero")
        self.top_k = top_k
        self.section_per_page = section_per_page

    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        document = ParsedPdf.model_validate(inputs["document"])
        retriever = LocalBM25Retriever.from_document(
            document, section_per_page=self.section_per_page,
        )
        requests: dict[str, RiskExtractionRequest] = {}
        evidence_by_risk: dict[str, tuple[Evidence, ...]] = {}
        request_json_by_risk: dict[str, str] = {}
        evidence_json_by_risk: dict[str, str] = {}

        for category in RISK_TASKS:
            request = RiskExtractionRequest(category=category)
            evidence = retriever.retrieve(RISK_QUERIES[category], top_k=self.top_k)
            key = category.value
            requests[key] = request
            evidence_by_risk[key] = evidence
            request_json_by_risk[key] = request.model_dump_json()
            evidence_json_by_risk[key] = serialize_evidence(evidence)

        return {
            "document": document,
            "financials": CompanyFinancials.model_validate(inputs["financials"]),
            "criteria": tuple(
                CriterionResult.model_validate(item) for item in inputs["criteria"]
            ),
            "policy": PolicyThresholds.model_validate(inputs["policy"]),
            "requests": requests,
            "evidence_by_risk": evidence_by_risk,
            "request_json_by_risk": request_json_by_risk,
            "evidence_json_by_risk": evidence_json_by_risk,
        }


class DeterministicPolicyEvaluationComponent(WorkflowComponent):
    def __init__(self, *, partial_risk_coverage: bool = False) -> None:
        self.partial_risk_coverage = partial_risk_coverage

    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        risks: list[RiskAssessment] = []
        for category in RISK_TASKS:
            key = category.value
            request = RiskExtractionRequest.model_validate(inputs["requests"][key])
            candidate = risk_result_from_payload(inputs["candidates"][key])
            evidence = tuple(
                Evidence.model_validate(item) for item in inputs["evidence_by_risk"][key]
            )
            validated = validate_risk_support(candidate, request, evidence)
            assessment = validated.to_risk_assessment()
            if self.partial_risk_coverage and assessment.status is RiskStatus.CLEAR:
                assessment = assessment.model_copy(update={
                    "status": RiskStatus.UNVERIFIED,
                    "rationale": (
                        "Overall risk coverage is unverified because the SEC rendered-report "
                        "bundle provides only selected disclosures. The model's passage-level "
                        "assessment was CLEAR: " + (assessment.rationale or "No rationale supplied.")
                    ),
                })
            risks.append(assessment)

        financials = CompanyFinancials.model_validate(inputs["financials"])
        criteria = tuple(
            CriterionResult.model_validate(item) for item in inputs["criteria"]
        )
        risks_tuple = tuple(risks)
        decision = final_policy_decision(criteria, risks_tuple)
        result = ScreeningResult(
            company_name=financials.company_name,
            fiscal_year=financials.fiscal_year,
            criteria=criteria,
            risks=risks_tuple,
            decision=decision,
            decision_reasons=decision_reasons(decision, criteria, risks_tuple),
        )
        return {
            "document": ParsedPdf.model_validate(inputs["document"]),
            "screening_result": result,
        }


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def validate_evidence_provenance(evidence: Evidence, document: ParsedPdf) -> None:
    """Verify one final citation against its physical source page."""

    if evidence.source_filename != document.source_filename:
        raise ScreeningWorkflowError(
            f"Provenance source mismatch: {evidence.source_filename!r}."
        )
    if evidence.pdf_page_index >= document.page_count:
        raise ScreeningWorkflowError(
            f"Provenance PDF page is out of range: {evidence.pdf_page_index}."
        )
    page = document.pages[evidence.pdf_page_index]
    if page.printed_document_page != evidence.printed_document_page:
        raise ScreeningWorkflowError(
            "Provenance printed page does not match the parsed physical PDF page."
        )
    if evidence.chunk_text is None:
        raise ScreeningWorkflowError("Final evidence is missing its source excerpt.")

    if evidence.line_numbers is not None:
        # A line citation: re-resolve it with the SAME check extraction used, and
        # require the stored excerpt to be exactly what those page lines say.
        try:
            citation = resolve_line_citation(page.raw_text, evidence.line_numbers,
                                             row_label=evidence.exact_row_label)
        except CitationError as exc:
            raise ScreeningWorkflowError(f"Final citation is invalid: {exc}") from exc
        if citation.text != evidence.chunk_text:
            raise ScreeningWorkflowError(
                "Final evidence text differs from the cited lines on its page.")
        return

    # Quoted evidence (the risk path still quotes text rather than citing lines).
    normalized_page = _normalize_text(page.raw_text)
    normalized_lines = tuple(
        _normalize_text(line)
        for line in evidence.chunk_text.splitlines()
        if line.strip()
    )
    if not normalized_lines or not all(line in normalized_page for line in normalized_lines):
        raise ScreeningWorkflowError(
            "Final evidence text was not found on its cited physical PDF page."
        )
    if evidence.exact_row_label is not None and _normalize_text(
        evidence.exact_row_label
    ) not in _normalize_text(evidence.chunk_text):
        raise ScreeningWorkflowError(
            "Final financial evidence does not contain its exact row label."
        )


def validate_result_provenance(
    result: ScreeningResult,
    document: ParsedPdf,
) -> None:
    """Verify every final citation against its physical source page."""

    all_evidence = tuple(
        item for criterion in result.criteria for item in criterion.evidence
    ) + tuple(item for risk in result.risks for item in risk.evidence)
    for evidence in all_evidence:
        validate_evidence_provenance(evidence, document)


class ProvenanceValidationComponent(WorkflowComponent):
    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        document = ParsedPdf.model_validate(inputs["document"])
        result = ScreeningResult.model_validate(inputs["screening_result"])
        validate_result_provenance(result, document)
        return {"screening_result": result}


class ReportGenerationComponent(WorkflowComponent):
    async def invoke(self, inputs: Any, session: Any, context: Any) -> dict[str, Any]:
        del session, context
        result = ScreeningResult.model_validate(inputs["screening_result"])
        return {"screening_result": generate_screening_report(result)}


def _llm_output_mapping(node_id: str, fields: list[str]) -> dict[str, str]:
    return {field: f"${{{node_id}.{field}}}" for field in fields}


def build_screening_workflow(
    config: OpenJiuwenExtractionConfig,
    *,
    financial_top_k: int = 6,
    risk_top_k: int = 6,
    source_format: str = "pdf",
) -> Any:
    """Build the fixed single-agent graph; only extraction/risk nodes use an LLM."""

    from openjiuwen.core.workflow import End, Start, Workflow, WorkflowCard

    workflow = Workflow(
        card=WorkflowCard(
            id="enterprise_financial_screening",
            name="Enterprise Financial Screening",
            version="1.0.0",
            description=(
                "Screens one local financial filing with evidence-bound extraction and "
                "deterministic policy evaluation."
            ),
            input_params=ScreeningWorkflowInput.model_json_schema(),
        )
    )
    workflow.set_start_comp(
        "start",
        Start(),
        inputs_schema={
            "filing_path": "${filing_path}",
            "policy": "${policy}",
        },
    )
    if source_format not in ("pdf", "html"):
        raise ValueError("source_format must be 'pdf' or 'html'")
    workflow.add_workflow_comp(
        "ingest_filing",
        IngestHtmlFilingComponent() if source_format == "html" else IngestFilingComponent(),
        inputs_schema={
            "filing_path": "${start.filing_path}",
            "policy": "${start.policy}",
        },
    )
    workflow.add_workflow_comp(
        "retrieve_financial_evidence",
        RetrieveFinancialEvidenceComponent(top_k=financial_top_k,
                                           routed=source_format == "html",
                                           config=config),
        inputs_schema={
            "document": "${ingest_filing.document}",
            "company_name": "${ingest_filing.company_name}",
            "fiscal_year": "${ingest_filing.fiscal_year}",
            "policy": "${ingest_filing.policy}",
        },
    )
    workflow.add_connection("start", "ingest_filing")
    workflow.add_connection("ingest_filing", "retrieve_financial_evidence")

    metric_fields = list(extraction_response_schema()["required"])
    metric_node_ids: list[str] = []
    for task in FINANCIAL_TASKS:
        node_id = f"extract_{task.alias}"
        metric_node_ids.append(node_id)
        workflow.add_workflow_comp(
            node_id,
            create_metric_extraction_component(config),
            inputs_schema={
                "request_json": (
                    f"${{retrieve_financial_evidence.request_json_by_task.{task.alias}}}"
                ),
                "evidence_json": (
                    f"${{retrieve_financial_evidence.evidence_json_by_task.{task.alias}}}"
                ),
            },
        )
        workflow.add_connection("retrieve_financial_evidence", node_id)

    workflow.add_workflow_comp(
        "deterministic_financial_calculations",
        DeterministicFinancialCalculationsComponent(),
        wait_for_all=True,
        inputs_schema={
            "document": "${retrieve_financial_evidence.document}",
            "company_name": "${retrieve_financial_evidence.company_name}",
            "fiscal_year": "${retrieve_financial_evidence.fiscal_year}",
            "policy": "${retrieve_financial_evidence.policy}",
            "requests": "${retrieve_financial_evidence.requests}",
            "evidence_by_task": "${retrieve_financial_evidence.evidence_by_task}",
            "candidates": {
                task.alias: _llm_output_mapping(
                    f"extract_{task.alias}", metric_fields
                )
                for task in FINANCIAL_TASKS
            },
        },
    )
    for node_id in metric_node_ids:
        workflow.add_connection(node_id, "deterministic_financial_calculations")

    workflow.add_workflow_comp(
        "retrieve_risk_evidence",
        RetrieveRiskEvidenceComponent(top_k=risk_top_k, section_per_page=source_format == "html"),
        inputs_schema={
            "document": "${deterministic_financial_calculations.document}",
            "financials": "${deterministic_financial_calculations.financials}",
            "criteria": "${deterministic_financial_calculations.criteria}",
            "policy": "${deterministic_financial_calculations.policy}",
        },
    )
    workflow.add_connection(
        "deterministic_financial_calculations",
        "retrieve_risk_evidence",
    )

    risk_fields = list(risk_response_schema()["required"])
    risk_node_ids: list[str] = []
    for category in RISK_TASKS:
        key = category.value
        node_id = f"assess_{key}_risk"
        risk_node_ids.append(node_id)
        workflow.add_workflow_comp(
            node_id,
            create_risk_assessment_component(config),
            inputs_schema={
                "request_json": f"${{retrieve_risk_evidence.request_json_by_risk.{key}}}",
                "evidence_json": f"${{retrieve_risk_evidence.evidence_json_by_risk.{key}}}",
            },
        )
        workflow.add_connection("retrieve_risk_evidence", node_id)

    workflow.add_workflow_comp(
        "deterministic_policy_evaluation",
        DeterministicPolicyEvaluationComponent(partial_risk_coverage=source_format == "html"),
        wait_for_all=True,
        inputs_schema={
            "document": "${retrieve_risk_evidence.document}",
            "financials": "${retrieve_risk_evidence.financials}",
            "criteria": "${retrieve_risk_evidence.criteria}",
            "policy": "${retrieve_risk_evidence.policy}",
            "requests": "${retrieve_risk_evidence.requests}",
            "evidence_by_risk": "${retrieve_risk_evidence.evidence_by_risk}",
            "candidates": {
                category.value: _llm_output_mapping(
                    f"assess_{category.value}_risk", risk_fields
                )
                for category in RISK_TASKS
            },
        },
    )
    for node_id in risk_node_ids:
        workflow.add_connection(node_id, "deterministic_policy_evaluation")

    workflow.add_workflow_comp(
        "validate_provenance",
        ProvenanceValidationComponent(),
        inputs_schema={
            "document": "${deterministic_policy_evaluation.document}",
            "screening_result": (
                "${deterministic_policy_evaluation.screening_result}"
            ),
        },
    )
    workflow.add_workflow_comp(
        "generate_report",
        ReportGenerationComponent(),
        inputs_schema={
            "screening_result": "${validate_provenance.screening_result}",
        },
    )
    result_fields = list(ScreeningResult.model_json_schema()["properties"])
    workflow.set_end_comp(
        "end",
        End(),
        inputs_schema={
            field: f"${{generate_report.screening_result.{field}}}"
            for field in result_fields
        },
    )
    workflow.add_connection(
        "deterministic_policy_evaluation",
        "validate_provenance",
    )
    workflow.add_connection("validate_provenance", "generate_report")
    workflow.add_connection("generate_report", "end")
    return workflow


def build_screening_agent(
    config: OpenJiuwenExtractionConfig,
    *,
    financial_top_k: int = 6,
    risk_top_k: int = 6,
) -> Any:
    """Wrap the one fixed workflow in the current openJiuwen WorkflowAgent."""

    from openjiuwen.core.application.workflow_agent import WorkflowAgent, WorkflowAgentConfig

    workflow = build_screening_workflow(
        config,
        financial_top_k=financial_top_k,
        risk_top_k=risk_top_k,
    )
    agent = WorkflowAgent(
        WorkflowAgentConfig(
            id="enterprise_financial_screening_agent",
            version="1.0.0",
            description="Runs the deterministic enterprise screening workflow.",
        )
    )
    agent.add_workflows([workflow])
    return agent


def validate_screening_runner_output(output: Any) -> ScreeningResult:
    """Unwrap either Runner.run_workflow or WorkflowAgent output and validate it."""

    from openjiuwen.core.workflow import WorkflowExecutionState, WorkflowOutput

    workflow_output = output.get("output") if isinstance(output, dict) else output
    if not isinstance(workflow_output, WorkflowOutput):
        raise ScreeningWorkflowError(
            f"openJiuwen returned {type(workflow_output).__name__}; expected WorkflowOutput."
        )
    if workflow_output.state is not WorkflowExecutionState.COMPLETED:
        raise ScreeningWorkflowError(
            f"Screening workflow did not complete: {workflow_output.state.value}."
        )
    payload = workflow_output.result
    if isinstance(payload, dict) and set(payload) == {"output"}:
        payload = payload["output"]
    try:
        return ScreeningResult.model_validate(payload)
    except ValidationError as exc:
        raise ScreeningWorkflowError(
            "Screening workflow returned an invalid result: "
            f"{exc.errors(include_url=False)}"
        ) from exc


async def run_screening_workflow(
    workflow_input: ScreeningWorkflowInput,
    config: OpenJiuwenExtractionConfig,
    *,
    financial_top_k: int = 6,
    risk_top_k: int = 6,
    source_format: str = "pdf",
) -> ScreeningResult:
    """Run the ScreeningWorkflow through Runner.run_workflow."""

    from openjiuwen.core.runner import Runner

    workflow = build_screening_workflow(
        config,
        financial_top_k=financial_top_k,
        risk_top_k=risk_top_k,
        source_format=source_format,
    )
    runner_started = False
    try:
        runner_started = await Runner.start()
        if not runner_started:
            raise ScreeningWorkflowError("openJiuwen Runner.start() reported failure.")
        output = await Runner.run_workflow(
            workflow,
            workflow_input.model_dump(mode="json"),
        )
        return validate_screening_runner_output(output)
    finally:
        if runner_started:
            await Runner.stop()


async def run_screening_agent(
    workflow_input: ScreeningWorkflowInput,
    config: OpenJiuwenExtractionConfig,
    *,
    financial_top_k: int = 6,
    risk_top_k: int = 6,
) -> ScreeningResult:
    """Run the same fixed workflow through WorkflowAgent and Runner.run_agent."""

    from openjiuwen.core.runner import Runner

    agent = build_screening_agent(
        config,
        financial_top_k=financial_top_k,
        risk_top_k=risk_top_k,
    )
    runner_started = False
    try:
        runner_started = await Runner.start()
        if not runner_started:
            raise ScreeningWorkflowError("openJiuwen Runner.start() reported failure.")
        output = await Runner.run_agent(
            agent,
            {
                "query": str(workflow_input.filing_path),
                "policy": workflow_input.policy.model_dump(mode="json"),
                "conversation_id": f"screening-{uuid.uuid4()}",
            },
        )
        return validate_screening_runner_output(output)
    finally:
        if runner_started:
            await Runner.stop()
