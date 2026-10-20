"""Deterministic screening criteria and final policy decision."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from screening.domain.calculations import (
    liabilities_to_assets,
    operating_margin,
    positive_operating_cash_flow,
    reconcile_balance_sheet,
    revenue_growth,
)
from screening.domain.models import (
    CompanyFinancials,
    CriterionName,
    CriterionResult,
    CriterionStatus,
    Evidence,
    FinancialMetric,
    PolicyThresholds,
    RiskAssessment,
    RiskStatus,
    ScreeningDecision,
    ScreeningResult,
)


DEFAULT_POLICY = PolicyThresholds()


def _evidence_from(*metrics: FinancialMetric) -> tuple[Evidence, ...]:
    return tuple(evidence for metric in metrics for evidence in metric.evidence)


def evaluate_financial_criteria(
    financials: CompanyFinancials,
    policy: PolicyThresholds = DEFAULT_POLICY,
) -> tuple[CriterionResult, ...]:
    growth = revenue_growth(financials.revenue_current, financials.revenue_prior)
    if growth >= policy.min_revenue_growth_for_pass:
        growth_status = CriterionStatus.PASS
        growth_summary = "Revenue growth meets the minimum pass threshold."
    else:
        growth_status = CriterionStatus.FAIL
        growth_summary = "Revenue growth is below the minimum pass threshold."

    margin = operating_margin(financials.operating_income, financials.revenue_current)
    if margin >= policy.min_operating_margin_for_pass:
        margin_status = CriterionStatus.PASS
        margin_summary = "Operating margin meets the minimum pass threshold."
    elif margin > policy.reject_operating_margin_at_or_below:
        margin_status = CriterionStatus.FLAG
        margin_summary = "Operating margin is positive but below the pass threshold."
    else:
        margin_status = CriterionStatus.FAIL
        margin_summary = "Operating margin is at or below the reject threshold."

    cash_flow_is_positive = positive_operating_cash_flow(financials.operating_cash_flow)
    cash_flow_status = CriterionStatus.PASS if cash_flow_is_positive else CriterionStatus.FAIL
    cash_flow_summary = (
        "Operating cash flow is positive."
        if cash_flow_is_positive
        else "Operating cash flow is not positive."
    )

    leverage = liabilities_to_assets(financials.liabilities, financials.assets)
    if leverage <= policy.max_liabilities_to_assets_for_pass:
        leverage_status = CriterionStatus.PASS
        leverage_summary = "Liabilities/assets is within the pass threshold."
    elif leverage <= policy.reject_liabilities_to_assets_above:
        leverage_status = CriterionStatus.FLAG
        leverage_summary = "Liabilities/assets exceeds the pass threshold."
    else:
        leverage_status = CriterionStatus.FAIL
        leverage_summary = "Liabilities/assets exceeds the reject threshold."

    reconciliation = reconcile_balance_sheet(
        financials,
        tolerance=policy.reconciliation_tolerance,
    )
    reconciliation_status = (
        CriterionStatus.PASS if reconciliation.is_reconciled else CriterionStatus.FAIL
    )
    reconciliation_summary = (
        "Supplied balance-sheet components reconcile to assets."
        if reconciliation.is_reconciled
        else "Supplied balance-sheet components do not reconcile to assets."
    )

    balance_sheet_metrics = (financials.assets, *financials.supplied_balance_sheet_components())
    return (
        CriterionResult(
            criterion=CriterionName.REVENUE_GROWTH,
            status=growth_status,
            value=growth,
            summary=growth_summary,
            evidence=_evidence_from(financials.revenue_current, financials.revenue_prior),
        ),
        CriterionResult(
            criterion=CriterionName.OPERATING_MARGIN,
            status=margin_status,
            value=margin,
            summary=margin_summary,
            evidence=_evidence_from(financials.operating_income, financials.revenue_current),
        ),
        CriterionResult(
            criterion=CriterionName.POSITIVE_OPERATING_CASH_FLOW,
            status=cash_flow_status,
            value=financials.operating_cash_flow.value,
            summary=cash_flow_summary,
            evidence=_evidence_from(financials.operating_cash_flow),
        ),
        CriterionResult(
            criterion=CriterionName.LIABILITIES_TO_ASSETS,
            status=leverage_status,
            value=leverage,
            summary=leverage_summary,
            evidence=_evidence_from(financials.liabilities, financials.assets),
        ),
        CriterionResult(
            criterion=CriterionName.BALANCE_SHEET_RECONCILIATION,
            status=reconciliation_status,
            value=reconciliation.difference,
            summary=reconciliation_summary,
            evidence=_evidence_from(*balance_sheet_metrics),
        ),
    )


def final_policy_decision(
    criteria: Sequence[CriterionResult],
    risks: Sequence[RiskAssessment] = (),
) -> ScreeningDecision:
    """Apply FAIL > FLAG precedence to produce REJECT, REVIEW, or PASS."""

    if not criteria:
        raise ValueError("at least one criterion result is required")
    if any(result.status is CriterionStatus.FAIL for result in criteria):
        return ScreeningDecision.REJECT
    if any(result.status is CriterionStatus.FLAG for result in criteria) or any(
        risk.status in {RiskStatus.FLAG, RiskStatus.UNVERIFIED} for risk in risks
    ):
        return ScreeningDecision.REVIEW
    return ScreeningDecision.PASS


def decision_reasons(
    decision: ScreeningDecision,
    criteria: Sequence[CriterionResult],
    risks: Sequence[RiskAssessment],
) -> tuple[str, ...]:
    if decision is ScreeningDecision.REJECT:
        return tuple(
            f"{result.criterion.value}: {result.summary}"
            for result in criteria
            if result.status is CriterionStatus.FAIL
        )

    if decision is ScreeningDecision.REVIEW:
        criterion_reasons = tuple(
            f"{result.criterion.value}: {result.summary}"
            for result in criteria
            if result.status is CriterionStatus.FLAG
        )
        risk_reasons: list[str] = []
        for risk in risks:
            if risk.status not in {RiskStatus.FLAG, RiskStatus.UNVERIFIED}:
                continue
            fallback = (
                "Risk assessment is unverified."
                if risk.status is RiskStatus.UNVERIFIED
                else "Risk assessment is flagged."
            )
            risk_reasons.append(f"{risk.category.value}: {risk.rationale or fallback}")
        return criterion_reasons + tuple(risk_reasons)

    return ("All deterministic criteria passed and no risk assessments were flagged.",)


def screen_company(
    financials: CompanyFinancials,
    risks: Sequence[RiskAssessment] = (),
    policy: PolicyThresholds = DEFAULT_POLICY,
) -> ScreeningResult:
    """Evaluate one company without I/O, model calls, or workflow orchestration."""

    criteria = evaluate_financial_criteria(financials, policy)
    risks_tuple = tuple(risks)
    decision = final_policy_decision(criteria, risks_tuple)
    return ScreeningResult(
        company_name=financials.company_name,
        fiscal_year=financials.fiscal_year,
        criteria=criteria,
        risks=risks_tuple,
        decision=decision,
        decision_reasons=decision_reasons(decision, criteria, risks_tuple),
    )
