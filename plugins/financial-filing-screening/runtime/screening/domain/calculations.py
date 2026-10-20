"""Pure Decimal-based financial calculations."""

from __future__ import annotations

from decimal import Decimal, localcontext

from screening.domain.models import (
    BalanceSheetReconciliation,
    CompanyFinancials,
    FinancialMetric,
    MetricName,
    MetricUnit,
)


class FinancialCalculationError(ValueError):
    """Raised when supplied financial values cannot produce a valid calculation."""


def _require_metric(metric: FinancialMetric, expected_name: MetricName) -> None:
    if metric.name is not expected_name:
        raise FinancialCalculationError(
            f"expected metric {expected_name.value!r}, received {metric.name.value!r}"
        )


def _require_same_unit(left: FinancialMetric, right: FinancialMetric) -> None:
    if left.unit is not right.unit:
        raise FinancialCalculationError(
            f"metric units must match: {left.unit.value!r} != {right.unit.value!r}"
        )


_UNIT_FACTOR = {
    MetricUnit.USD: Decimal(1),
    MetricUnit.USD_THOUSANDS: Decimal(1_000),
    MetricUnit.USD_MILLIONS: Decimal(1_000_000),
}


def to_common_unit(metrics: dict[str, FinancialMetric]) -> dict[str, FinancialMetric]:
    """Restate every metric in the finest unit any of them uses.

    One filing can present its statements at different scales: Hershey's FY2025 cash-flow
    statement is rendered in dollars while its other statements are in thousands. Each
    figure is correct as printed, so the filing should be screened, not rejected. Scaling
    down to the finest unit is exact (a multiplication by a power of ten), so no figure is
    rounded. Metrics already in a single unit are returned unchanged.
    """
    units = {metric.unit for metric in metrics.values()}
    if len(units) <= 1:
        return dict(metrics)
    target = min(units, key=_UNIT_FACTOR.__getitem__)
    return {
        alias: metric if metric.unit is target else metric.model_copy(update={
            "value": metric.value * _UNIT_FACTOR[metric.unit] / _UNIT_FACTOR[target],
            "unit": target,
        })
        for alias, metric in metrics.items()
    }


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 28
        return numerator / denominator


def revenue_growth(current_revenue: FinancialMetric, prior_revenue: FinancialMetric) -> Decimal:
    """Return period-over-period revenue growth as a ratio, not a percentage."""

    _require_metric(current_revenue, MetricName.REVENUE)
    _require_metric(prior_revenue, MetricName.REVENUE)
    _require_same_unit(current_revenue, prior_revenue)
    if prior_revenue.value <= 0:
        raise FinancialCalculationError("prior revenue must be greater than zero")
    return _divide(current_revenue.value - prior_revenue.value, prior_revenue.value)


def operating_margin(operating_income: FinancialMetric, revenue: FinancialMetric) -> Decimal:
    """Return operating income divided by revenue as a ratio."""

    _require_metric(operating_income, MetricName.OPERATING_INCOME)
    _require_metric(revenue, MetricName.REVENUE)
    _require_same_unit(operating_income, revenue)
    if revenue.value <= 0:
        raise FinancialCalculationError("revenue must be greater than zero")
    return _divide(operating_income.value, revenue.value)


def positive_operating_cash_flow(operating_cash_flow: FinancialMetric) -> bool:
    """Return true only when operating cash flow is strictly positive."""

    _require_metric(operating_cash_flow, MetricName.OPERATING_CASH_FLOW)
    return operating_cash_flow.value > 0


def liabilities_to_assets(liabilities: FinancialMetric, assets: FinancialMetric) -> Decimal:
    """Return total liabilities divided by total assets as a ratio."""

    _require_metric(liabilities, MetricName.LIABILITIES)
    _require_metric(assets, MetricName.ASSETS)
    _require_same_unit(liabilities, assets)
    if assets.value <= 0:
        raise FinancialCalculationError("assets must be greater than zero")
    return _divide(liabilities.value, assets.value)


def reconcile_balance_sheet(
    financials: CompanyFinancials,
    *,
    tolerance: Decimal = Decimal("0"),
) -> BalanceSheetReconciliation:
    """Reconcile assets against every supplied right-hand balance-sheet component."""

    if isinstance(tolerance, (float, bool)):
        raise FinancialCalculationError(
            "use Decimal, int, or a decimal string for reconciliation tolerance"
        )
    tolerance = Decimal(tolerance)
    if tolerance < 0:
        raise FinancialCalculationError("reconciliation tolerance cannot be negative")

    # Filers use two presentations for noncontrolling interests, and the choice is not
    # recorded anywhere machine-readable:
    #   * NCI reported OUTSIDE total equity — then assets = liabilities + equity + NCI
    #   * NCI reported INSIDE total equity (ASC 810, the more common presentation)
    #     — then assets = liabilities + equity, and adding NCI again double-counts it.
    #
    # Assuming the first produced a spurious FAIL on a real Caterpillar 10-K: every figure
    # was extracted correctly (assets 78,453, liabilities 63,824, equity 14,629, NCI 41)
    # and the check still reported a 41 imbalance, because 78,453 - 63,824 is exactly
    # 14,629 — the equity figure already contained the NCI.
    #
    # So both readings are evaluated and the reconciling one is reported. A genuine
    # imbalance still fails, because it fails under both. The cost is narrow and worth
    # stating: an error whose size happens to equal the optional components exactly would
    # be masked.
    full = financials.supplied_balance_sheet_components()
    core = (financials.liabilities, financials.shareholders_equity)

    candidates = []
    for components in (full, core):
        total = sum((component.value for component in components), start=Decimal("0"))
        candidates.append((components, total, financials.assets.value - total))

    # Prefer a reconciling reading; otherwise report the closest, so the difference shown
    # is the smallest genuine discrepancy rather than an artefact of the assumption.
    reconciling = [c for c in candidates if abs(c[2]) <= tolerance]
    components, component_total, difference = (
        reconciling[0] if reconciling else min(candidates, key=lambda c: abs(c[2])))

    return BalanceSheetReconciliation(
        assets=financials.assets.value,
        component_total=component_total,
        difference=difference,
        tolerance=tolerance,
        included_components=tuple(component.name for component in components),
        is_reconciled=abs(difference) <= tolerance,
    )
