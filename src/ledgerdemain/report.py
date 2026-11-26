"""Period financial summary, payroll tax liability check, and Wave manual action checklist."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ledgerdemain.config import AppConfig
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import GustoPayrollRun
from ledgerdemain.money import ZERO, Money, format_money, parse_money

_HSA_COL_RE = re.compile(r"\bhsa\b")


def _classify_imputed_gusto_benefits(
    runs: list[GustoPayrollRun],
) -> tuple[int, Money, Money, Money]:
    """Return (imputed_run_count, health_insurance, hsa, other) from Gusto non-cash runs."""
    imputed_count = 0
    gusto_health = ZERO
    gusto_hsa = ZERO
    gusto_other = ZERO

    for run in runs:
        if run.run_type == "contractor" or not bool(run.raw.get("non_cash_imputed_benefits")):
            continue
        imputed_count += 1
        rows = run.raw.get("rows")
        itemized_found = False
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for col, cell_val in row.items():
                    if not isinstance(col, str) or not cell_val:
                        continue
                    col_lower = col.lower()
                    if not (
                        col_lower.endswith("(company contribution)")
                        or col_lower.endswith("(employer contribution)")
                    ):
                        continue
                    amt = parse_money(cell_val, ZERO)
                    if amt == ZERO:
                        continue
                    itemized_found = True
                    # Check HSA before generic "health" since "health savings account" contains it.
                    if "health savings" in col_lower or _HSA_COL_RE.search(col_lower):
                        gusto_hsa += amt
                    elif any(
                        kw in col_lower for kw in ("medical", "health", "cobra", "dental", "vision")
                    ):
                        gusto_health += amt
                    else:
                        gusto_other += amt
        if not itemized_found:
            gusto_other += run.employer_benefits

    return imputed_count, gusto_health, gusto_hsa, gusto_other


def build_report(
    db: LedgerDB,
    config: AppConfig,
    period: str | None = None,
) -> dict[str, Any]:
    """Build structured financial, reconciliation, payroll, and Wave checklist report."""
    txs = db.list_bank_transactions(period_prefix=period)
    by_source: dict[str, dict[str, Any]] = {}
    for tx in txs:
        bucket = by_source.setdefault(
            tx.source,
            {"count": 0, "deposits": ZERO, "withdrawals": ZERO},
        )
        bucket["count"] += 1
        if tx.direction == "DEPOSIT":
            bucket["deposits"] += tx.amount
        else:
            bucket["withdrawals"] += tx.amount

    overview = {
        src: {
            "count": info["count"],
            "deposits": format_money(info["deposits"]),
            "withdrawals": format_money(info["withdrawals"]),
            "net": format_money(info["deposits"] - info["withdrawals"]),
        }
        for src, info in sorted(by_source.items())
    }

    # Harvest AR summary
    invoices = db.list_harvest_invoices(period_prefix=period)
    confirmed_matches = db.list_matches(status="confirmed")
    matched_inv_ids = {
        m.harvest_invoice_id
        for m in confirmed_matches
        if m.match_type == "harvest_deposit" and m.harvest_invoice_id is not None
    }
    matched_deposit_total = sum(
        (m.matched_amount for m in confirmed_matches if m.match_type == "harvest_deposit"),
        ZERO,
    )
    total_invoiced = sum((inv.total_amount for inv in invoices if inv.state != "draft"), ZERO)
    total_due = sum((inv.due_amount for inv in invoices if inv.state != "draft"), ZERO)
    unmatched_invoices = [
        {
            "invoice_number": inv.invoice_number,
            "client_name": inv.client_name,
            "issue_date": inv.issue_date,
            "total_amount": format_money(inv.total_amount),
            "due_amount": format_money(inv.due_amount),
            "state": inv.state,
        }
        for inv in invoices
        if inv.state != "draft" and inv.id not in matched_inv_ids
    ]

    # Gusto payroll & clearing liability balances
    runs = db.list_gusto_payroll_runs(period_prefix=period)
    payroll_totals: dict[str, Money] = defaultdict(lambda: ZERO)
    contractor_total = ZERO
    for run in runs:
        if run.run_type == "contractor":
            contractor_total += run.net_disbursement
        else:
            payroll_totals["gross_wages"] += run.gross_wages
            payroll_totals["reimbursements"] += run.reimbursements
            payroll_totals["employee_taxes"] += run.employee_taxes
            payroll_totals["employer_taxes"] += run.employer_taxes
            payroll_totals["employee_benefits"] += run.employee_benefits
            payroll_totals["employer_benefits"] += run.employer_benefits
            payroll_totals["net_pay"] += run.net_pay

    staged_txs = db.list_staged_transactions(period_prefix=period)
    tax_payable_net = ZERO
    benefits_payable_net = ZERO
    bank_health = ZERO
    bank_hsa = ZERO
    bank_unsplit = ZERO
    for stx in staged_txs:
        if stx.status not in {"staged", "approved", "pushed"}:
            continue
        for li in stx.line_items:
            delta = li.amount if li.balance == "INCREASE" else -li.amount
            if li.account_key == "payroll_taxes_payable":
                tax_payable_net += delta
            elif li.account_key == "payroll_benefits_payable":
                benefits_payable_net += delta
            elif li.account_key == "officer_health_insurance":
                bank_health += delta
            elif li.account_key == "officer_hsa":
                bank_hsa += delta
            elif li.account_key == "payroll_employer_benefits" and not stx.category_rule.startswith(
                "gusto_"
            ):
                bank_unsplit += delta

    imputed_run_count, gusto_health, gusto_hsa, gusto_other = _classify_imputed_gusto_benefits(runs)
    gusto_total = gusto_health + gusto_hsa + gusto_other
    bank_total = bank_health + bank_hsa + bank_unsplit

    # Manual Wave UI checklist
    manual_transfers = [
        {
            "staged_tx_id": stx.id,
            "date": stx.tx_date,
            "source_account": stx.anchor_account_key,
            "direction": stx.anchor_direction,
            "amount": format_money(stx.anchor_amount),
            "description": stx.description,
            "reason": stx.review_reason,
        }
        for stx in staged_txs
        if stx.status == "manual_transfer_required"
    ]

    manual_invoice_payments = [
        {
            "staged_tx_id": stx.id,
            "date": stx.tx_date,
            "amount": format_money(stx.anchor_amount),
            "description": stx.description,
            "reason": stx.review_reason or stx.notes,
        }
        for stx in staged_txs
        if stx.status == "manual_invoice_payment_required"
        or (
            config.accounting.mirror_harvest_invoices
            and stx.category_rule == "harvest_deposit"
            and stx.status in {"staged", "approved", "pushed"}
        )
    ]

    attention_items = [
        {
            "staged_tx_id": stx.id,
            "date": stx.tx_date,
            "status": stx.status,
            "amount": format_money(stx.anchor_amount),
            "description": stx.description,
            "reason": stx.last_error or stx.review_reason,
        }
        for stx in staged_txs
        if stx.status in {"needs_review", "failed"}
    ]

    return {
        "period": period or "all",
        "overview": overview,
        "harvest_ar": {
            "invoice_count": len([i for i in invoices if i.state != "draft"]),
            "total_invoiced": format_money(total_invoiced),
            "total_due": format_money(total_due),
            "matched_deposits": format_money(matched_deposit_total),
            "unmatched_invoices": unmatched_invoices,
        },
        "gusto_payroll": {
            "run_count": len(runs),
            "gross_wages": format_money(payroll_totals["gross_wages"]),
            "reimbursements": format_money(payroll_totals["reimbursements"]),
            "employee_taxes": format_money(payroll_totals["employee_taxes"]),
            "employer_taxes": format_money(payroll_totals["employer_taxes"]),
            "employee_benefits": format_money(payroll_totals["employee_benefits"]),
            "employer_benefits": format_money(payroll_totals["employer_benefits"]),
            "net_pay": format_money(payroll_totals["net_pay"]),
            "contractor_payouts": format_money(contractor_total),
            "payroll_taxes_payable_net": format_money(tax_payable_net),
            "payroll_benefits_payable_net": format_money(benefits_payable_net),
        },
        "scorp_benefits_tieout": {
            "imputed_run_count": imputed_run_count,
            "gusto_health_insurance": format_money(gusto_health),
            "gusto_hsa": format_money(gusto_hsa),
            "gusto_other": format_money(gusto_other),
            "gusto_total": format_money(gusto_total),
            "bank_health_insurance": format_money(bank_health),
            "bank_hsa": format_money(bank_hsa),
            "bank_unsplit_employer_benefits": format_money(bank_unsplit),
            "bank_total": format_money(bank_total),
            "health_insurance_variance": format_money(bank_health - gusto_health),
            "hsa_variance": format_money(bank_hsa - gusto_hsa),
            "total_variance": format_money(bank_total - gusto_total),
            "in_balance": (
                bank_total == gusto_total
                and (
                    bank_unsplit != ZERO or (bank_health == gusto_health and bank_hsa == gusto_hsa)
                )
            ),
        },
        "wave_manual_checklist": {
            "transfers": manual_transfers,
            "invoice_payments": manual_invoice_payments,
            "attention_required": attention_items,
        },
    }


def format_report_text(report: dict[str, Any]) -> str:
    """Format a structured report dict into concise human-readable text."""
    lines: list[str] = [f"=== Ledgerdemain Report (period: {report['period']}) ===", ""]

    lines.append("1. Bank & Credit Card Overview")
    if not report["overview"]:
        lines.append("   (no transactions)")
    else:
        for src, data in report["overview"].items():
            lines.append(
                f"   - {src}: {data['count']} tx | "
                f"deposits=${data['deposits']} | withdrawals=${data['withdrawals']} | "
                f"net=${data['net']}"
            )

    ar = report["harvest_ar"]
    lines.extend(
        [
            "",
            "2. Harvest Accounts Receivable Summary",
            f"   - Invoices: {ar['invoice_count']} | Total Invoiced: ${ar['total_invoiced']} | "
            f"Matched Deposits: ${ar['matched_deposits']} | Open Due: ${ar['total_due']}",
        ]
    )
    if ar["unmatched_invoices"]:
        lines.append("   - Unmatched Invoices:")
        for inv in ar["unmatched_invoices"]:
            lines.append(
                f"     * #{inv['invoice_number']} ({inv['client_name']}) "
                f"{inv['issue_date']} ${inv['total_amount']} [{inv['state']}]"
            )

    pay = report["gusto_payroll"]
    lines.extend(
        [
            "",
            "3. Gusto Payroll & Tax Summary",
            f"   - Runs: {pay['run_count']} | Gross Wages: ${pay['gross_wages']} | "
            f"Reimbursements: ${pay['reimbursements']}",
            f"   - Employer Taxes: ${pay['employer_taxes']} | "
            f"Employee Taxes: ${pay['employee_taxes']}",
            f"   - Employer Benefits: ${pay['employer_benefits']} | "
            f"Employee Benefits: ${pay['employee_benefits']}",
            f"   - Contractor Payouts: ${pay['contractor_payouts']}",
            f"   - Clearing Net Balances -> Payroll Taxes Payable: "
            f"${pay['payroll_taxes_payable_net']} | "
            f"Payroll Benefits Payable: ${pay['payroll_benefits_payable_net']}",
        ]
    )

    tie = report["scorp_benefits_tieout"]
    if tie["imputed_run_count"] > 0 or tie["bank_total"] != "0.00":
        status_label = "[BALANCED]" if tie["in_balance"] else "[VARIANCE DETECTED]"
        lines.extend(
            [
                f"   - S-Corp Imputed Benefits Tie-Out ({tie['imputed_run_count']} run(s)) "
                f"{status_label}:",
                f"     * Health Insurance: Gusto=${tie['gusto_health_insurance']} | "
                f"Bank=${tie['bank_health_insurance']} | "
                f"Variance=${tie['health_insurance_variance']}",
                f"     * HSA: Gusto=${tie['gusto_hsa']} | Bank=${tie['bank_hsa']} | "
                f"Variance=${tie['hsa_variance']}",
            ]
        )
        if tie["bank_unsplit_employer_benefits"] != "0.00":
            lines.append(
                f"     * Unsplit Employer Benefits (Bank): ${tie['bank_unsplit_employer_benefits']}"
            )
        if tie["gusto_other"] != "0.00":
            lines.append(f"     * Other Imputed Benefits (Gusto): ${tie['gusto_other']}")
        lines.append(
            f"     * Total: Gusto=${tie['gusto_total']} | Bank=${tie['bank_total']} | "
            f"Variance=${tie['total_variance']}"
        )
        if report["period"] != "all":
            lines.append(
                "     * Note: Gusto imputed runs filter by check_date while bank entries "
                "filter by tx_date; cross-month timing reconciles across a full-year report."
            )

    chk = report["wave_manual_checklist"]
    lines.extend(["", "4. Wave Manual Action Checklist"])
    lines.append(f"   - Transfers to record in Wave UI ({len(chk['transfers'])}):")
    for tr in chk["transfers"]:
        lines.append(
            f"     * [tx #{tr['staged_tx_id']}] {tr['date']} {tr['source_account']} "
            f"{tr['direction']} ${tr['amount']} — {tr['reason']}"
        )
    lines.append(
        f"   - Mirrored Invoices / Payments to verify in Wave UI ({len(chk['invoice_payments'])}):"
    )
    for ip in chk["invoice_payments"]:
        lines.append(
            f"     * [tx #{ip['staged_tx_id']}] {ip['date']} ${ip['amount']} — {ip['reason']}"
        )
    lines.append(f"   - Items Needing Review or Failed ({len(chk['attention_required'])}):")
    for att in chk["attention_required"]:
        lines.append(
            f"     * [tx #{att['staged_tx_id']}] {att['date']} [{att['status']}] "
            f"${att['amount']} {att['description']} — {att['reason']}"
        )

    return "\n".join(lines)
