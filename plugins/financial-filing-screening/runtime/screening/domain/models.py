"""Typed, immutable models for deterministic financial screening."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _reject_binary_float(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("use Decimal, int, or a decimal string; binary floats are not accepted")
    return value


class _DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MetricName(StrEnum):
    REVENUE = "revenue"
    OPERATING_INCOME = "operating_income"
    OPERATING_CASH_FLOW = "operating_cash_flow"
    ASSETS = "assets"
    LIABILITIES = "liabilities"
    SHAREHOLDERS_EQUITY = "shareholders_equity"
    REDEEMABLE_NONCONTROLLING_INTERESTS = "redeemable_noncontrolling_interests"
    NONCONTROLLING_INTERESTS = "noncontrolling_interests"
    OTHER_TEMPORARY_MEZZANINE_EQUITY = "other_temporary_mezzanine_equity"


class MetricUnit(StrEnum):
    USD = "USD"
    USD_THOUSANDS = "USD_THOUSANDS"
    USD_MILLIONS = "USD_MILLIONS"


class RiskCategory(StrEnum):
    REGULATORY = "regulatory"
    SUPPLY_CHAIN = "supply_chain"


class RiskStatus(StrEnum):
    CLEAR = "CLEAR"
    FLAG = "FLAG"
    UNVERIFIED = "UNVERIFIED"


class CriterionName(StrEnum):
    REVENUE_GROWTH = "revenue_growth"
    OPERATING_MARGIN = "operating_margin"
    POSITIVE_OPERATING_CASH_FLOW = "positive_operating_cash_flow"
    LIABILITIES_TO_ASSETS = "liabilities_to_assets"
    BALANCE_SHEET_RECONCILIATION = "balance_sheet_reconciliation"


class CriterionStatus(StrEnum):
    PASS = "PASS"
    FLAG = "FLAG"
    FAIL = "FAIL"


class ScreeningDecision(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class Evidence(_DomainModel):
    """Provenance for a fact extracted from a document.

    ``pdf_page_index`` is the zero-based physical page position in the PDF.
    ``printed_document_page`` is the page identifier printed by the document and
    remains ``None`` when it is unknown. The two values are intentionally not
    inferred from one another. ``chunk_text`` is populated for retrieved evidence
    and may remain ``None`` for evidence attached directly to a known fact.
    """

    source_filename: NonEmptyString
    pdf_page_index: int = Field(ge=0)
    printed_document_page: NonEmptyString | None = None
    section_or_statement_name: NonEmptyString
    exact_row_label: NonEmptyString | None = None
    chunk_text: NonEmptyString | None = None
    #: 1-based line numbers on the page (see `screening.citations`). For retrieved
    #: evidence, one per line of `chunk_text`; for a cited figure, the lines cited.
    line_numbers: tuple[int, ...] | None = None


class FinancialMetric(_DomainModel):
    name: MetricName
    value: Decimal
    unit: MetricUnit
    fiscal_period: NonEmptyString
    evidence: tuple[Evidence, ...] = ()

    _validate_decimal_input = field_validator("value", mode="before")(_reject_binary_float)


class CompanyFinancials(_DomainModel):
    company_name: NonEmptyString
    fiscal_year: int = Field(ge=1900, le=9999)
    revenue_current: FinancialMetric
    revenue_prior: FinancialMetric
    operating_income: FinancialMetric
    operating_cash_flow: FinancialMetric
    assets: FinancialMetric
    liabilities: FinancialMetric
    shareholders_equity: FinancialMetric
    redeemable_noncontrolling_interests: FinancialMetric | None = None
    noncontrolling_interests: FinancialMetric | None = None
    other_temporary_mezzanine_equity: FinancialMetric | None = None

    @model_validator(mode="after")
    def validate_metric_roles_and_units(self) -> CompanyFinancials:
        expected_names = {
            "revenue_current": MetricName.REVENUE,
            "revenue_prior": MetricName.REVENUE,
            "operating_income": MetricName.OPERATING_INCOME,
            "operating_cash_flow": MetricName.OPERATING_CASH_FLOW,
            "assets": MetricName.ASSETS,
            "liabilities": MetricName.LIABILITIES,
            "shareholders_equity": MetricName.SHAREHOLDERS_EQUITY,
            "redeemable_noncontrolling_interests": MetricName.REDEEMABLE_NONCONTROLLING_INTERESTS,
            "noncontrolling_interests": MetricName.NONCONTROLLING_INTERESTS,
            "other_temporary_mezzanine_equity": MetricName.OTHER_TEMPORARY_MEZZANINE_EQUITY,
        }

        metrics: list[FinancialMetric] = []
        for field_name, expected_name in expected_names.items():
            metric = getattr(self, field_name)
            if metric is None:
                continue
            if metric.name is not expected_name:
                raise ValueError(
                    f"{field_name} must contain metric {expected_name.value!r}, "
                    f"not {metric.name.value!r}"
                )
            metrics.append(metric)

        units = {metric.unit for metric in metrics}
        if len(units) != 1:
            raise ValueError("all CompanyFinancials metrics must use the same unit")
        if self.revenue_current.value < 0:
            raise ValueError("current revenue cannot be negative")
        if self.revenue_prior.value <= 0:
            raise ValueError("prior revenue must be greater than zero")
        if self.assets.value <= 0:
            raise ValueError("assets must be greater than zero")
        if self.liabilities.value < 0:
            raise ValueError("liabilities cannot be negative")
        return self

    def supplied_balance_sheet_components(self) -> tuple[FinancialMetric, ...]:
        optional_components = (
            self.redeemable_noncontrolling_interests,
            self.noncontrolling_interests,
            self.other_temporary_mezzanine_equity,
        )
        return (
            self.liabilities,
            self.shareholders_equity,
            *(component for component in optional_components if component is not None),
        )


class RiskAssessment(_DomainModel):
    category: RiskCategory
    status: RiskStatus
    rationale: NonEmptyString | None = None
    evidence: tuple[Evidence, ...] = ()


class CriterionResult(_DomainModel):
    criterion: CriterionName
    status: CriterionStatus
    value: Decimal
    summary: NonEmptyString
    evidence: tuple[Evidence, ...] = ()

    _validate_decimal_input = field_validator("value", mode="before")(_reject_binary_float)


class BalanceSheetReconciliation(_DomainModel):
    assets: Decimal
    component_total: Decimal
    difference: Decimal
    tolerance: Decimal
    included_components: tuple[MetricName, ...] = Field(min_length=1)
    is_reconciled: bool

    _validate_decimal_inputs = field_validator(
        "assets", "component_total", "difference", "tolerance", mode="before"
    )(_reject_binary_float)

    @model_validator(mode="after")
    def validate_reconciliation(self) -> BalanceSheetReconciliation:
        if self.tolerance < 0:
            raise ValueError("reconciliation tolerance cannot be negative")
        expected_difference = self.assets - self.component_total
        if self.difference != expected_difference:
            raise ValueError("difference must equal assets minus component_total")
        if self.is_reconciled is not (abs(self.difference) <= self.tolerance):
            raise ValueError("is_reconciled does not match difference and tolerance")
        if len(set(self.included_components)) != len(self.included_components):
            raise ValueError("included balance-sheet components must be unique")
        return self


class PolicyThresholds(_DomainModel):
    min_revenue_growth_for_pass: Decimal = Decimal("0")
    min_operating_margin_for_pass: Decimal = Decimal("0.10")
    reject_operating_margin_at_or_below: Decimal = Decimal("0")
    max_liabilities_to_assets_for_pass: Decimal = Decimal("0.80")
    reject_liabilities_to_assets_above: Decimal = Decimal("1.00")
    reconciliation_tolerance: Decimal = Decimal("0")

    _validate_decimal_inputs = field_validator("*", mode="before")(_reject_binary_float)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> PolicyThresholds:
        if self.min_operating_margin_for_pass <= self.reject_operating_margin_at_or_below:
            raise ValueError("operating-margin pass threshold must exceed the reject threshold")
        if self.max_liabilities_to_assets_for_pass >= self.reject_liabilities_to_assets_above:
            raise ValueError("liabilities/assets pass threshold must be below the reject threshold")
        if self.reconciliation_tolerance < 0:
            raise ValueError("reconciliation tolerance cannot be negative")
        return self


class ScreeningResult(_DomainModel):
    company_name: NonEmptyString
    fiscal_year: int = Field(ge=1900, le=9999)
    criteria: tuple[CriterionResult, ...] = Field(min_length=1)
    risks: tuple[RiskAssessment, ...] = ()
    decision: ScreeningDecision
    decision_reasons: tuple[NonEmptyString, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_decision_and_uniqueness(self) -> ScreeningResult:
        criterion_names = [result.criterion for result in self.criteria]
        if len(set(criterion_names)) != len(criterion_names):
            raise ValueError("criteria must contain at most one result per criterion")

        risk_categories = [risk.category for risk in self.risks]
        if len(set(risk_categories)) != len(risk_categories):
            raise ValueError("risks must contain at most one assessment per category")

        if any(result.status is CriterionStatus.FAIL for result in self.criteria):
            expected_decision = ScreeningDecision.REJECT
        elif any(result.status is CriterionStatus.FLAG for result in self.criteria) or any(
            risk.status in {RiskStatus.FLAG, RiskStatus.UNVERIFIED} for risk in self.risks
        ):
            expected_decision = ScreeningDecision.REVIEW
        else:
            expected_decision = ScreeningDecision.PASS

        if self.decision is not expected_decision:
            raise ValueError(
                f"decision must be {expected_decision.value} for the supplied criteria and risks"
            )
        return self
