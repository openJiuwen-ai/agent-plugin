"""Validated results produced by the non-deterministic extraction boundary."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from screening.domain import Evidence, FinancialMetric, MetricName, MetricUnit


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractionStatus(StrEnum):
    """Whether a model response survived deterministic evidence validation."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    ERROR = "ERROR"


class MetricExtractionRequest(_ExtractionModel):
    """One reported financial fact to locate for one fiscal year."""

    metric_name: MetricName
    fiscal_year: int = Field(ge=1900, le=9999)


class MetricExtractionResult(_ExtractionModel):
    """A structured model answer, including an explicit failure state.

    Every field except ``evidence_text`` is in the LLM JSON contract. Fields that
    cannot be supported may be ``None`` only when the status is not ``VERIFIED``. The
    printed document page remains optional even for verified values because it
    must never be inferred from the physical PDF page index.

    The model cites evidence by line ID (``lines``: "L41", ...). It never types the
    evidence: ``evidence_text`` is filled by `validate_extraction_support`, which copies
    the cited lines verbatim from the page (see ``screening.citations``).
    """

    status: ExtractionStatus
    metric_name: MetricName
    fiscal_year: int = Field(ge=1900, le=9999)
    value: Decimal | None
    unit: MetricUnit | None
    exact_row_label: NonEmptyString | None
    source_filename: NonEmptyString | None
    pdf_page_index: int | None = Field(default=None, ge=0)
    printed_document_page: NonEmptyString | None
    section_or_statement_name: NonEmptyString | None
    lines: tuple[NonEmptyString, ...] | None
    error_message: NonEmptyString | None
    #: filled by validation from the cited lines; never supplied by the model
    evidence_text: NonEmptyString | None = None

    @field_validator("value", mode="before")
    @classmethod
    def convert_json_number_to_decimal(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("value must be a decimal number, not a boolean")
        if isinstance(value, float):
            return str(value)
        return value

    @model_validator(mode="after")
    def validate_status_contract(self) -> Self:
        supported_fields = (
            "value",
            "unit",
            "exact_row_label",
            "source_filename",
            "pdf_page_index",
            "section_or_statement_name",
            "lines",
        )
        if self.status is ExtractionStatus.VERIFIED:
            missing = [name for name in supported_fields if getattr(self, name) is None]
            if missing:
                raise ValueError(
                    "VERIFIED extraction is missing required support: " + ", ".join(missing)
                )
            if self.error_message is not None:
                raise ValueError("VERIFIED extraction cannot contain error_message")
        else:
            if self.value is not None:
                raise ValueError("UNVERIFIED and ERROR extraction values must be null")
            if self.error_message is None:
                raise ValueError("UNVERIFIED and ERROR extractions require error_message")
        return self

    def to_financial_metric(self) -> FinancialMetric:
        """Convert only deterministically verified extraction output to the domain."""

        if self.status is not ExtractionStatus.VERIFIED:
            raise ValueError("only a VERIFIED extraction can become a FinancialMetric")

        assert self.value is not None
        assert self.unit is not None
        assert self.exact_row_label is not None
        assert self.source_filename is not None
        assert self.pdf_page_index is not None
        assert self.section_or_statement_name is not None
        assert self.evidence_text is not None, "citation was never resolved against the page"
        assert self.lines is not None
        from screening.citations import parse_line_ids

        return FinancialMetric(
            name=self.metric_name,
            value=self.value,
            unit=self.unit,
            fiscal_period=f"FY{self.fiscal_year}",
            evidence=(
                Evidence(
                    source_filename=self.source_filename,
                    pdf_page_index=self.pdf_page_index,
                    printed_document_page=self.printed_document_page,
                    section_or_statement_name=self.section_or_statement_name,
                    exact_row_label=self.exact_row_label,
                    chunk_text=self.evidence_text,
                    line_numbers=tuple(sorted(set(parse_line_ids(self.lines)))),
                ),
            ),
        )
