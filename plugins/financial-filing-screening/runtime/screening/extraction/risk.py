"""Structured, evidence-bound qualitative risk assessment."""

from __future__ import annotations

from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from screening.domain import Evidence, RiskAssessment, RiskCategory, RiskStatus
from screening.extraction.openjiuwen import OpenJiuwenExtractionConfig


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

RISK_QUERIES = {
    RiskCategory.REGULATORY: "regulatory legal litigation government investigation risk",
    RiskCategory.SUPPLY_CHAIN: (
        "suppliers manufacturing production supply-chain concentration dependency risk"
    ),
}


class _RiskExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskExtractionRequest(_RiskExtractionModel):
    category: RiskCategory


class RiskExtractionResult(_RiskExtractionModel):
    """A model risk classification that retains its exact supporting citation."""

    status: RiskStatus
    category: RiskCategory
    rationale: NonEmptyString | None
    source_filename: NonEmptyString | None
    pdf_page_index: int | None = Field(default=None, ge=0)
    printed_document_page: NonEmptyString | None
    section_or_statement_name: NonEmptyString | None
    evidence_text: NonEmptyString | None
    error_message: NonEmptyString | None

    @model_validator(mode="after")
    def validate_status_contract(self) -> Self:
        if self.status in {RiskStatus.CLEAR, RiskStatus.FLAG}:
            required = (
                "rationale",
                "source_filename",
                "pdf_page_index",
                "section_or_statement_name",
                "evidence_text",
            )
            missing = [name for name in required if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    f"{self.status.value} risk result is missing support: "
                    + ", ".join(missing)
                )
            if self.error_message is not None:
                raise ValueError(
                    f"{self.status.value} risk result cannot contain error_message"
                )
        elif self.error_message is None:
            # An UNVERIFIED result must say WHY it could not be verified, but the model
            # naturally writes that explanation into `rationale` — the field whose whole
            # purpose is to explain a risk judgement — rather than into a separate
            # `error_message`. On real filings it returned, for example:
            #     status: UNVERIFIED, category: regulatory,
            #     rationale: "The evidence provided does not explicitly ..."
            #     error_message: null
            # and the run was rejected outright despite carrying a perfectly good reason.
            # Demanding the same sentence in a second field is a contract wart, not a
            # safeguard, so a non-empty rationale satisfies the requirement and is
            # promoted into error_message so downstream consumers see one shape.
            if self.rationale and self.rationale.strip():
                object.__setattr__(self, "error_message", self.rationale.strip())
            else:
                raise ValueError(
                    "UNVERIFIED risk result requires error_message or rationale")
        return self

    def to_risk_assessment(self) -> RiskAssessment:
        evidence: tuple[Evidence, ...] = ()
        if self.status in {RiskStatus.CLEAR, RiskStatus.FLAG}:
            assert self.source_filename is not None
            assert self.pdf_page_index is not None
            assert self.section_or_statement_name is not None
            assert self.evidence_text is not None
            evidence = (
                Evidence(
                    source_filename=self.source_filename,
                    pdf_page_index=self.pdf_page_index,
                    printed_document_page=self.printed_document_page,
                    section_or_statement_name=self.section_or_statement_name,
                    chunk_text=self.evidence_text,
                ),
            )
        return RiskAssessment(
            category=self.category,
            status=self.status,
            rationale=self.rationale or self.error_message,
            evidence=evidence,
        )


def risk_response_schema() -> dict[str, Any]:
    nullable_string = {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [status.value for status in RiskStatus],
            },
            "category": {
                "type": "string",
                "enum": [category.value for category in RiskCategory],
            },
            "rationale": nullable_string,
            "source_filename": nullable_string,
            "pdf_page_index": {
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]
            },
            "printed_document_page": nullable_string,
            "section_or_statement_name": nullable_string,
            "evidence_text": nullable_string,
            "error_message": nullable_string,
        },
        "required": [
            "status",
            "category",
            "rationale",
            "source_filename",
            "pdf_page_index",
            "printed_document_page",
            "section_or_statement_name",
            "evidence_text",
            "error_message",
        ],
        "additionalProperties": False,
    }


def create_risk_assessment_component(config: OpenJiuwenExtractionConfig) -> Any:
    """Create an LLMComponent that classifies one risk from retrieved evidence."""

    from openjiuwen.core.foundation.llm import (
        ModelClientConfig,
        ModelRequestConfig,
        SystemMessage,
        UserMessage,
    )
    from openjiuwen.core.workflow import LLMCompConfig, LLMComponent

    response_schema = risk_response_schema()
    return LLMComponent(
        LLMCompConfig(
            model_client_config=ModelClientConfig(
                client_provider=config.provider,
                api_key=config.api_key,
                api_base=config.api_base,
                verify_ssl=config.verify_ssl,
                max_retries=config.max_retries,
            ),
            model_config=ModelRequestConfig(
                model=config.model_name,
                max_tokens=config.max_tokens,
                **config.request_options(),
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "qualitative_risk_assessment",
                        "strict": True,
                        "schema": response_schema,
                    },
                },
            ),
            system_prompt_template=SystemMessage(
                content=(
                    "Assess exactly one qualitative risk category using only the supplied "
                    "EVIDENCE_CHUNKS. Return FLAG when the filing explicitly describes a "
                    "material exposure, dependency, concentration, proceeding, or uncertainty "
                    "that requires review. Return CLEAR only when the supplied text explicitly "
                    "supports the absence or mitigation of that risk; silence is not CLEAR. "
                    "Return UNVERIFIED when the evidence is missing, ambiguous, or insufficient. "
                    "For CLEAR or FLAG, copy the shortest relevant verbatim excerpt and its exact "
                    "filename, zero-based PDF page index, independently supplied printed page, "
                    "and section. Never use outside knowledge. Do not calculate financial ratios, "
                    "apply policy thresholds, or choose a screening rating. Never guess."
                )
            ),
            user_prompt_template=UserMessage(
                content=(
                    "REQUEST:\n{{request_json}}\n\n"
                    "EVIDENCE_CHUNKS (the only allowed source):\n{{evidence_json}}"
                )
            ),
            response_format={"type": "json"},
            output_config=response_schema,
            enable_history=False,
        )
    )


def risk_result_from_payload(payload: Any) -> RiskExtractionResult:
    if isinstance(payload, dict):
        payload = dict(payload)
        for field in (
            "rationale",
            "source_filename",
            "pdf_page_index",
            "printed_document_page",
            "section_or_statement_name",
            "evidence_text",
            "error_message",
        ):
            payload.setdefault(field, None)
    try:
        return RiskExtractionResult.model_validate(payload)
    except ValidationError as exc:
        raise RuntimeError(
            "openJiuwen returned invalid risk JSON: "
            f"{exc.errors(include_url=False)}"
        ) from exc


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _unverified_result(
    request: RiskExtractionRequest,
    message: str,
) -> RiskExtractionResult:
    return RiskExtractionResult(
        status=RiskStatus.UNVERIFIED,
        category=request.category,
        rationale=None,
        source_filename=None,
        pdf_page_index=None,
        printed_document_page=None,
        section_or_statement_name=None,
        evidence_text=None,
        error_message=message,
    )


def validate_risk_support(
    candidate: RiskExtractionResult,
    request: RiskExtractionRequest,
    evidence: tuple[Evidence, ...],
) -> RiskExtractionResult:
    """Downgrade any risk classification not supported by a retrieved chunk."""

    if candidate.category is not request.category:
        return _unverified_result(request, "Model returned a different risk category.")
    if candidate.status is RiskStatus.UNVERIFIED:
        return candidate

    matching = tuple(
        item
        for item in evidence
        if item.source_filename == candidate.source_filename
        and item.pdf_page_index == candidate.pdf_page_index
        and item.printed_document_page == candidate.printed_document_page
        and item.section_or_statement_name == candidate.section_or_statement_name
        and item.chunk_text is not None
    )
    if not matching:
        return _unverified_result(
            request,
            "Returned provenance does not match a retrieved risk-evidence chunk.",
        )

    assert candidate.evidence_text is not None
    chunks = tuple(_normalize_text(item.chunk_text or "") for item in matching)
    excerpt = _normalize_text(candidate.evidence_text)
    lines = tuple(
        _normalize_text(line)
        for line in candidate.evidence_text.splitlines()
        if line.strip()
    )
    supported = any(excerpt in chunk for chunk in chunks) or (
        bool(lines) and all(any(line in chunk for chunk in chunks) for line in lines)
    )
    if not supported:
        return _unverified_result(
            request,
            "Risk evidence text contains a line not found in the cited retrieved chunk.",
        )
    return candidate
