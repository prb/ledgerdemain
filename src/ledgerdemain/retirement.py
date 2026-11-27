"""Solo 401(k) employer profit-sharing allowance, consumed totals, and CLI report helpers."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ledgerdemain.config import AppConfig, RetirementConfig
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import GustoPayrollRun
from ledgerdemain.money import ZERO, Money, format_money, parse_money

_FOUR01K_COL_RE = re.compile(r"\b401\s*\(?k\)?\b")
_CATCHUP_COL_RE = re.compile(r"\bcatch[\s-]*up\b")
_YEAR_PREFIX_RE = re.compile(r"^(\d{4})-")


def floor_ratio(amount: Money, numerator: int, denominator: int) -> Money:
    """Scale Money by numerator / denominator using integer floor (never exceeding exact cap)."""
    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or not isinstance(numerator, int)
        or not isinstance(denominator, int)
    ):
        raise TypeError("floor_ratio numerator and denominator must be ints")
    if numerator < 0:
        raise ValueError("floor_ratio numerator cannot be negative")
    if denominator <= 0:
        raise ValueError("floor_ratio denominator must be positive")
    if amount <= ZERO or numerator == 0:
        return ZERO
    return Money((amount.cents * numerator) // denominator)


def _extract_employee_401k_deferrals(runs: list[GustoPayrollRun]) -> tuple[Money, Money]:
    """Return (regular_deferrals, catchup_deferrals) from itemized Gusto employee deductions."""
    regular = ZERO
    catchup = ZERO

    for run in runs:
        if run.run_type == "contractor":
            continue
        rows = run.raw.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for col, cell_val in row.items():
                if not isinstance(col, str) or not cell_val:
                    continue
                col_lower = col.lower()
                if not col_lower.endswith("(employee deduction)"):
                    continue
                if not _FOUR01K_COL_RE.search(col_lower):
                    continue
                amt = parse_money(cell_val, ZERO)
                if amt == ZERO:
                    continue
                if _CATCHUP_COL_RE.search(col_lower):
                    catchup += amt
                else:
                    regular += amt

    return regular, catchup


def compute_solo401k_limits(
    *,
    cash_w2_wages: Money,
    imputed_scorp_benefits: Money,
    employee_deferrals: Money,
    consumed: Money,
    retirement: RetirementConfig,
) -> dict[str, Money | str]:
    """Pure fixed-point calculation of Solo 401(k) employer profit-sharing allowance."""
    w2_box5_wages = cash_w2_wages
    w2_box1_wages = cash_w2_wages + imputed_scorp_benefits
    eligible_raw = w2_box5_wages if retirement.compensation_basis == "w2_box5" else w2_box1_wages
    eligible_capped = max(ZERO, min(eligible_raw, retirement.compensation_cap))

    rate_cap = floor_ratio(
        eligible_capped,
        retirement.employer_rate_numerator,
        retirement.employer_rate_denominator,
    )
    section_415c_headroom = max(ZERO, retirement.annual_additions_limit - employee_deferrals)
    max_employer_profit_sharing = min(rate_cap, section_415c_headroom)
    remaining = max(ZERO, max_employer_profit_sharing - consumed)
    excess = max(ZERO, consumed - max_employer_profit_sharing)

    if eligible_capped == ZERO:
        limiting_factor = "zero_compensation"
    elif section_415c_headroom < rate_cap:
        limiting_factor = "section_415c_limit"
    else:
        limiting_factor = "employer_rate_cap"

    return {
        "w2_box5_wages": w2_box5_wages,
        "w2_box1_wages": w2_box1_wages,
        "eligible_compensation": eligible_capped,
        "employer_rate_cap": rate_cap,
        "section_415c_headroom": section_415c_headroom,
        "max_employer_profit_sharing": max_employer_profit_sharing,
        "remaining": remaining,
        "excess": excess,
        "limiting_factor": limiting_factor,
    }


def _infer_tax_year(db: LedgerDB, explicit_year: int | None) -> int:
    if explicit_year is not None:
        if explicit_year < 1900 or explicit_year > 2999:
            raise ValueError(f"Invalid tax year: {explicit_year}")
        return explicit_year

    years: list[int] = []
    for run in db.list_gusto_payroll_runs():
        if m := _YEAR_PREFIX_RE.match(run.check_date):
            years.append(int(m.group(1)))
    if years:
        return max(years)

    for stx in db.list_staged_transactions():
        if m := _YEAR_PREFIX_RE.match(stx.tx_date):
            years.append(int(m.group(1)))
    if years:
        return max(years)

    return date.today().year


def build_solo401k_summary(
    db: LedgerDB,
    config: AppConfig,
    *,
    year: int | None = None,
    employee_deferrals_override: Money | None = None,
) -> dict[str, Any]:
    """Compute YTD Solo 401(k) profit-sharing limit, consumed amount, and remaining room."""
    target_year = _infer_tax_year(db, year)
    if employee_deferrals_override is not None and employee_deferrals_override < ZERO:
        formatted = format_money(employee_deferrals_override)
        raise ValueError(f"employee_deferrals cannot be negative, got {formatted}")

    period_prefix = f"{target_year}-"
    runs = db.list_gusto_payroll_runs(period_prefix=period_prefix)
    staged_txs = db.list_staged_transactions(period_prefix=period_prefix)

    cash_run_count = 0
    imputed_run_count = 0
    cash_w2_wages = ZERO
    imputed_scorp_benefits = ZERO

    for run in runs:
        if run.run_type == "contractor":
            continue
        if bool(run.raw.get("non_cash_imputed_benefits")):
            imputed_run_count += 1
            imputed_scorp_benefits += run.employer_benefits
        else:
            cash_run_count += 1
            cash_w2_wages += run.gross_wages

    detected_deferrals, detected_catchup = _extract_employee_401k_deferrals(runs)
    effective_deferrals = (
        employee_deferrals_override
        if employee_deferrals_override is not None
        else detected_deferrals
    )

    ret = config.retirement
    consumed = ZERO
    contributions: list[dict[str, Any]] = []
    for stx in staged_txs:
        if stx.status not in {"staged", "approved", "pushed"}:
            continue
        tx_net = ZERO
        for li in stx.line_items:
            if li.account_key == ret.profit_sharing_account_key:
                tx_net += li.amount if li.balance == "INCREASE" else -li.amount
        if tx_net != ZERO:
            consumed += tx_net
            contributions.append(
                {
                    "staged_tx_id": stx.id,
                    "tx_date": stx.tx_date,
                    "status": stx.status,
                    "amount": format_money(tx_net),
                    "description": stx.description,
                }
            )

    calc = compute_solo401k_limits(
        cash_w2_wages=cash_w2_wages,
        imputed_scorp_benefits=imputed_scorp_benefits,
        employee_deferrals=effective_deferrals,
        consumed=consumed,
        retirement=ret,
    )

    w2_box1_wages = calc["w2_box1_wages"]
    eligible_comp = calc["eligible_compensation"]
    rate_cap = calc["employer_rate_cap"]
    headroom = calc["section_415c_headroom"]
    max_ps = calc["max_employer_profit_sharing"]
    remaining = calc["remaining"]
    excess = calc["excess"]

    assert isinstance(w2_box1_wages, Money)
    assert isinstance(eligible_comp, Money)
    assert isinstance(rate_cap, Money)
    assert isinstance(headroom, Money)
    assert isinstance(max_ps, Money)
    assert isinstance(remaining, Money)
    assert isinstance(excess, Money)

    return {
        "tax_year": target_year,
        "compensation_basis": ret.compensation_basis,
        "profit_sharing_account_key": ret.profit_sharing_account_key,
        "employer_rate_numerator": ret.employer_rate_numerator,
        "employer_rate_denominator": ret.employer_rate_denominator,
        "cash_w2_run_count": cash_run_count,
        "imputed_benefit_run_count": imputed_run_count,
        "cash_w2_wages": format_money(cash_w2_wages),
        "imputed_scorp_benefits": format_money(imputed_scorp_benefits),
        "w2_box1_wages": format_money(w2_box1_wages),
        "eligible_compensation": format_money(eligible_comp),
        "compensation_cap": format_money(ret.compensation_cap),
        "employer_rate_cap": format_money(rate_cap),
        "annual_additions_limit": format_money(ret.annual_additions_limit),
        "employee_deferrals_detected": format_money(detected_deferrals),
        "employee_deferrals_used": format_money(effective_deferrals),
        "employee_catchup_detected": format_money(detected_catchup),
        "section_415c_headroom": format_money(headroom),
        "max_employer_profit_sharing": format_money(max_ps),
        "limiting_factor": str(calc["limiting_factor"]),
        "consumed": format_money(consumed),
        "remaining": format_money(remaining),
        "excess": format_money(excess),
        "over_limit": excess > ZERO,
        "contribution_count": len(contributions),
        "contributions": contributions,
    }


def format_solo401k_text(summary: dict[str, Any]) -> str:
    """Format a Solo 401(k) computation summary dict as concise readable text."""
    num = summary["employer_rate_numerator"]
    den = summary["employer_rate_denominator"]
    pct_display = f"{(num * 100) // den}%" if (num * 100) % den == 0 else f"{num}/{den}"
    basis_label = (
        "W-2 Box 5 / Medicare (cash W-2 wages, excl. >2% S-Corp imputed benefits)"
        if summary["compensation_basis"] == "w2_box5"
        else "W-2 Box 1 (cash W-2 wages + >2% S-Corp imputed benefits)"
    )

    lines = [
        f"=== Solo 401(k) Profit-Sharing Summary ({summary['tax_year']}) ===",
        f"   - Basis: {basis_label}",
        (
            f"   - Gusto Runs -> Cash W-2: {summary['cash_w2_run_count']} | "
            f">2% Imputed Benefits: {summary['imputed_benefit_run_count']}"
        ),
        (
            f"   - YTD W-2 Box 5 (Cash Wages): ${summary['cash_w2_wages']} | "
            f">2% Imputed Benefits: ${summary['imputed_scorp_benefits']} | "
            f"W-2 Box 1: ${summary['w2_box1_wages']}"
        ),
        (
            f"   - Eligible Compensation: ${summary['eligible_compensation']} "
            f"(§401(a)(17) cap: ${summary['compensation_cap']})"
        ),
        (
            f"   - {pct_display} Employer Profit-Sharing Cap (floored): "
            f"${summary['employer_rate_cap']}"
        ),
        (
            f"   - §415(c) Headroom: ${summary['section_415c_headroom']} "
            f"(limit ${summary['annual_additions_limit']} - employee deferrals "
            f"${summary['employee_deferrals_used']})"
        ),
        (
            f"   - Max Allowable Employer Profit Sharing: "
            f"${summary['max_employer_profit_sharing']} "
            f"[limited by: {summary['limiting_factor']}]"
        ),
        (
            f"   - Consumed ({summary['profit_sharing_account_key']}, "
            f"{summary['contribution_count']} entry/ies): ${summary['consumed']}"
        ),
        f"   - Remaining Profit-Sharing Room: ${summary['remaining']}",
    ]

    if summary["employee_catchup_detected"] != "0.00":
        lines.append(
            f"   - Employee Catch-Up Deferrals Detected (outside §415(c)): "
            f"${summary['employee_catchup_detected']}"
        )
    if summary["over_limit"]:
        lines.append(f"   ! [OVER LIMIT] Contributions exceed max by ${summary['excess']}")
    for item in summary["contributions"]:
        lines.append(
            f"     * [stx #{item['staged_tx_id']}] {item['tx_date']} [{item['status']}] "
            f"${item['amount']} — {item['description']}"
        )

    return "\n".join(lines)
