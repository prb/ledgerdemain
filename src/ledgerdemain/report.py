"""Period financial summary, payroll tax liability check, and Wave manual action checklist."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from ledgerdemain.config import AppConfig
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import ZERO, fmt_decimal


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
            "deposits": fmt_decimal(info["deposits"]),
            "withdrawals": fmt_decimal(info["withdrawals"]),
            "net": fmt_decimal(info["deposits"] - info["withdrawals"]),
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
            "total_amount": fmt_decimal(inv.total_amount),
            "due_amount": fmt_decimal(inv.due_amount),
            "state": inv.state,
        }
        for inv in invoices
        if inv.state != "draft" and inv.id not in matched_inv_ids
    ]

    # Gusto payroll & clearing liability balances
    runs = db.list_gusto_payroll_runs(period_prefix=period)
    payroll_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
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
    for stx in staged_txs:
        if stx.status not in {"staged", "approved", "pushed"}:
            continue
        for li in stx.line_items:
            delta = li.amount if li.balance == "INCREASE" else -li.amount
            if li.account_key == "payroll_taxes_payable":
                tax_payable_net += delta
            elif li.account_key == "payroll_benefits_payable":
                benefits_payable_net += delta

    # Manual Wave UI checklist
    manual_transfers = [
        {
            "staged_tx_id": stx.id,
            "date": stx.tx_date,
            "source_account": stx.anchor_account_key,
            "direction": stx.anchor_direction,
            "amount": fmt_decimal(stx.anchor_amount),
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
            "amount": fmt_decimal(stx.anchor_amount),
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
            "amount": fmt_decimal(stx.anchor_amount),
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
            "total_invoiced": fmt_decimal(total_invoiced),
            "total_due": fmt_decimal(total_due),
            "matched_deposits": fmt_decimal(matched_deposit_total),
            "unmatched_invoices": unmatched_invoices,
        },
        "gusto_payroll": {
            "run_count": len(runs),
            "gross_wages": fmt_decimal(payroll_totals["gross_wages"]),
            "reimbursements": fmt_decimal(payroll_totals["reimbursements"]),
            "employee_taxes": fmt_decimal(payroll_totals["employee_taxes"]),
            "employer_taxes": fmt_decimal(payroll_totals["employer_taxes"]),
            "employee_benefits": fmt_decimal(payroll_totals["employee_benefits"]),
            "employer_benefits": fmt_decimal(payroll_totals["employer_benefits"]),
            "net_pay": fmt_decimal(payroll_totals["net_pay"]),
            "contractor_payouts": fmt_decimal(contractor_total),
            "payroll_taxes_payable_net": fmt_decimal(tax_payable_net),
            "payroll_benefits_payable_net": fmt_decimal(benefits_payable_net),
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
