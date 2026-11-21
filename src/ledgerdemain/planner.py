"""Double-entry balance validator and Wave transaction/invoice staging planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import AppConfig
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    ZERO,
    BankTransaction,
    GustoPayrollRun,
    HarvestInvoice,
    ReconciliationMatch,
    StagedInvoice,
    StagedLineItem,
    StagedTransaction,
    fmt_decimal,
)


class BalanceError(ValueError):
    """Raised when a staged transaction violates double-entry accounting rules."""


DEFAULT_NORMAL_BALANCE: dict[str, str] = {
    # Cash/Bank Assets
    "grasshopper_checking": "DEBIT",
    "grasshopper_savings": "DEBIT",
    "grasshopper_money_market": "DEBIT",
    # Liabilities
    "capital_one_card": "CREDIT",
    "payroll_taxes_payable": "CREDIT",
    "payroll_benefits_payable": "CREDIT",
    # Income
    "revenue_default": "CREDIT",
    "interest_income": "CREDIT",
    # Asset / Clearing & Expense accounts (DEBIT-normal)
    "invoice_payments_clearing": "DEBIT",
    "cc_transfer_clearing": "DEBIT",
    "bank_transfer_clearing": "DEBIT",
    "merchant_fees": "DEBIT",
    "payroll_gross_wages": "DEBIT",
    "payroll_reimbursements": "DEBIT",
    "payroll_employer_taxes": "DEBIT",
    "payroll_employer_benefits": "DEBIT",
    "payroll_processing_fees": "DEBIT",
    "contractors_expense": "DEBIT",
    "software_subscriptions": "DEBIT",
    "cloud_hosting": "DEBIT",
    "office_supplies": "DEBIT",
    "travel": "DEBIT",
    "meals": "DEBIT",
    "bank_fees": "DEBIT",
    "uncategorized_expense": "DEBIT",
}


def validate_double_entry_balance(
    anchor_account_key: str,
    anchor_direction: str,
    anchor_amount: Decimal,
    line_items: list[StagedLineItem],
    normal_balance_lookup: Callable[[str], str],
    anchor_wave_account_id: str | None = None,
) -> None:
    """Validate that line items strictly balance the anchor leg and follow Wave constraints."""
    if anchor_amount <= ZERO:
        raise BalanceError(f"Anchor amount must be positive, got {anchor_amount}")
    if anchor_direction not in {"DEPOSIT", "WITHDRAWAL"}:
        raise BalanceError(f"Invalid anchor direction: {anchor_direction!r}")
    if not line_items:
        raise BalanceError("Staged transaction must contain at least one line item")

    net_debit = ZERO
    for idx, item in enumerate(line_items):
        if item.amount <= ZERO:
            raise BalanceError(f"Line item {idx} ({item.account_key}) amount must be positive")
        if item.balance not in {"INCREASE", "DECREASE"}:
            raise BalanceError(f"Line item {idx} invalid balance: {item.balance!r}")
        if item.account_key == anchor_account_key:
            raise BalanceError(f"Anchor account {anchor_account_key!r} cannot appear in line items")
        if anchor_wave_account_id and item.wave_account_id == anchor_wave_account_id:
            raise BalanceError(
                f"Anchor Wave account {anchor_wave_account_id!r} cannot appear in line items"
            )

        normal = normal_balance_lookup(item.account_key).upper()
        if normal not in {"DEBIT", "CREDIT"}:
            raise BalanceError(f"Unknown normal balance type {normal!r} for {item.account_key!r}")

        if normal == "DEBIT":
            delta = item.amount if item.balance == "INCREASE" else -item.amount
        else:
            delta = -item.amount if item.balance == "INCREASE" else item.amount
        net_debit += delta

    expected_net_debit = anchor_amount if anchor_direction == "WITHDRAWAL" else -anchor_amount
    imbalance = net_debit - expected_net_debit
    if imbalance != ZERO:
        raise BalanceError(
            f"Double-entry imbalance of {fmt_decimal(imbalance)}: "
            f"expected line items net debit {fmt_decimal(expected_net_debit)} "
            f"for {anchor_direction} {fmt_decimal(anchor_amount)}, got {fmt_decimal(net_debit)}"
        )


@dataclass(slots=True)
class PlanSummary:
    """Counts produced by building staged transactions and invoices."""

    staged_transactions: int = 0
    needs_review: int = 0
    manual_transfers: int = 0
    manual_invoice_payments: int = 0
    staged_invoices: int = 0


class Planner:
    """Translates ingested data and confirmed matches into staged Wave payloads."""

    def __init__(self, db: LedgerDB, config: AppConfig, categorizer: Categorizer) -> None:
        self.db = db
        self.config = config
        self.categorizer = categorizer

    def normal_balance_for(self, account_key: str) -> str:
        """Resolve an account's normal balance from cached Wave accounts or defaults."""
        cached = self.db.resolve_wave_account(account_key, self.config.accounts)
        if cached is not None:
            return cached.normal_balance_type.upper()
        if account_key in DEFAULT_NORMAL_BALANCE:
            return DEFAULT_NORMAL_BALANCE[account_key]
        if account_key.endswith("_payable") or account_key.startswith("revenue"):
            return "CREDIT"
        return "DEBIT"

    def _wave_account_id(self, account_key: str) -> str | None:
        cached = self.db.resolve_wave_account(account_key, self.config.accounts)
        return cached.wave_id if cached else None

    def _balance_for_effect(self, account_key: str, desired_effect: str) -> str:
        """Return 'INCREASE' or 'DECREASE' to produce a DEBIT or CREDIT effect on account_key."""
        normal = self.normal_balance_for(account_key)
        if desired_effect == "DEBIT":
            return "INCREASE" if normal == "DEBIT" else "DECREASE"
        return "INCREASE" if normal == "CREDIT" else "DECREASE"

    def build_plan(self) -> PlanSummary:
        """Stage Harvest invoices and all bank/card transactions idempotently."""
        summary = PlanSummary()

        if self.config.accounting.mirror_harvest_invoices:
            for inv in self.db.list_harvest_invoices():
                if inv.id is None or inv.state == "draft":
                    continue
                staged_inv = self._build_staged_invoice(inv)
                self.db.upsert_staged_invoice(staged_inv)
                summary.staged_invoices += 1
        else:
            self.db.delete_unpushed_staged_invoices()

        confirmed = self.db.list_matches(status="confirmed")
        tx_matches: dict[int, list[ReconciliationMatch]] = {}
        for m in confirmed:
            tx_matches.setdefault(m.bank_tx_id, []).append(m)
            if m.counterparty_bank_tx_id is not None:
                tx_matches.setdefault(m.counterparty_bank_tx_id, []).append(m)

        for tx in self.db.list_bank_transactions():
            assert tx.id is not None
            matches = tx_matches.get(tx.id, [])
            staged = self._build_staged_transaction(tx, matches)
            self.db.upsert_staged_transaction(staged)
            if staged.status == "staged":
                summary.staged_transactions += 1
            elif staged.status == "needs_review":
                summary.needs_review += 1
            elif staged.status == "manual_transfer_required":
                summary.manual_transfers += 1
            elif staged.status == "manual_invoice_payment_required":
                summary.manual_invoice_payments += 1

        return summary

    def _build_staged_invoice(self, inv: HarvestInvoice) -> StagedInvoice:
        assert inv.id is not None
        cust = self.db.find_wave_customer_by_name(inv.client_name)
        items: list[dict[str, Any]] = []
        if inv.line_items:
            for li in inv.line_items:
                items.append(
                    {
                        "description": li.description
                        or li.kind
                        or f"Invoice #{inv.invoice_number}",
                        "quantity": str(li.quantity),
                        "unitPrice": fmt_decimal(li.unit_price),
                    }
                )
        else:
            items.append(
                {
                    "description": inv.subject or f"Harvest Invoice #{inv.invoice_number}",
                    "quantity": "1",
                    "unitPrice": fmt_decimal(inv.subtotal),
                }
            )

        return StagedInvoice(
            external_id=f"ld:inv:harvest:{inv.harvest_id}",
            harvest_invoice_id=inv.id,
            invoice_number=inv.invoice_number,
            customer_name=inv.client_name,
            wave_customer_id=cust.wave_id if cust else None,
            invoice_date=inv.issue_date,
            due_date=inv.due_date,
            wave_status=self.config.accounting.wave_invoice_create_status,
            items=items,
            total_amount=inv.total_amount,
            status="staged",
        )

    def _build_staged_transaction(
        self, tx: BankTransaction, matches: list[ReconciliationMatch]
    ) -> StagedTransaction:
        assert tx.id is not None
        ext_id = f"ld:tx:{tx.fingerprint[:24]}"
        anchor_wave_id = self._wave_account_id(tx.account_key)

        transfer_matches = [m for m in matches if m.match_type in {"cc_transfer", "bank_transfer"}]
        if transfer_matches:
            return self._plan_transfer(tx, transfer_matches[0], ext_id, anchor_wave_id)

        gusto_matches = [m for m in matches if m.match_type.startswith("gusto_")]
        if gusto_matches:
            return self._plan_gusto(tx, gusto_matches[0], ext_id, anchor_wave_id)

        harvest_matches = [m for m in matches if m.match_type == "harvest_deposit"]
        if harvest_matches:
            return self._plan_harvest_deposit(tx, harvest_matches, ext_id, anchor_wave_id)

        if self._looks_like_unpaired_cc_payment(tx):
            return StagedTransaction(
                external_id=ext_id,
                bank_tx_id=tx.id,
                tx_date=tx.posted_date,
                description=tx.description,
                notes="Unpaired credit card payment leg",
                anchor_account_key=tx.account_key,
                anchor_wave_account_id=anchor_wave_id,
                anchor_amount=tx.amount,
                anchor_direction=tx.direction,
                line_items=[],
                category_rule="unpaired_cc_payment",
                status="needs_review",
                review_reason="Possible credit card transfer — awaiting matching statement leg",
            )

        cat = self.categorizer.categorize(tx)
        if cat is None:
            return StagedTransaction(
                external_id=ext_id,
                bank_tx_id=tx.id,
                tx_date=tx.posted_date,
                description=tx.description,
                notes="",
                anchor_account_key=tx.account_key,
                anchor_wave_account_id=anchor_wave_id,
                anchor_amount=tx.amount,
                anchor_direction=tx.direction,
                line_items=[],
                category_rule="",
                status="needs_review",
                review_reason="No categorization rule matched",
            )

        desired_effect = "DEBIT" if tx.direction == "WITHDRAWAL" else "CREDIT"
        line_items = [
            StagedLineItem(
                account_key=cat.account_key,
                wave_account_id=self._wave_account_id(cat.account_key),
                amount=tx.amount,
                balance=self._balance_for_effect(cat.account_key, desired_effect),
                description=tx.description,
            )
        ]
        return self._finalize_staged(
            tx=tx,
            ext_id=ext_id,
            anchor_wave_id=anchor_wave_id,
            description=tx.description,
            notes=cat.notes,
            line_items=line_items,
            rule_name=cat.rule_name,
        )

    def _plan_transfer(
        self,
        tx: BankTransaction,
        match: ReconciliationMatch,
        ext_id: str,
        anchor_wave_id: str | None,
    ) -> StagedTransaction:
        assert tx.id is not None
        is_bank = match.match_type == "bank_transfer"
        other_id = match.counterparty_bank_tx_id if match.bank_tx_id == tx.id else match.bank_tx_id
        other_tx = self.db.get_bank_transaction(other_id) if other_id is not None else None

        if tx.direction == "WITHDRAWAL":
            from_acct = tx.account_key
            to_acct = (
                other_tx.account_key
                if other_tx
                else ("bank_account" if is_bank else "capital_one_card")
            )
        else:
            from_acct = other_tx.account_key if other_tx else "grasshopper_checking"
            to_acct = tx.account_key

        kind_label = "bank" if is_bank else "CC"
        strategy = self.config.accounting.transfer_strategy
        if strategy == "manual_transfer":
            return StagedTransaction(
                external_id=ext_id,
                bank_tx_id=tx.id,
                tx_date=tx.posted_date,
                description=tx.description,
                notes=f"Paired {kind_label} transfer with bank_tx #{other_id}",
                anchor_account_key=tx.account_key,
                anchor_wave_account_id=anchor_wave_id,
                anchor_amount=tx.amount,
                anchor_direction=tx.direction,
                line_items=[],
                category_rule="bank_transfer_manual" if is_bank else "cc_transfer_manual",
                status="manual_transfer_required",
                review_reason=(
                    f"Record as Transfer in Wave UI ({from_acct} -> {to_acct}, "
                    f"${fmt_decimal(tx.amount)}, paired with bank_tx #{other_id})"
                ),
            )

        desired_effect = "DEBIT" if tx.direction == "WITHDRAWAL" else "CREDIT"
        clearing_key = "bank_transfer_clearing" if is_bank else "cc_transfer_clearing"
        desc_prefix = "Bank Transfer" if is_bank else "Credit Card Payment Transfer"
        line_items = [
            StagedLineItem(
                account_key=clearing_key,
                wave_account_id=self._wave_account_id(clearing_key),
                amount=tx.amount,
                balance=self._balance_for_effect(clearing_key, desired_effect),
                description=f"{desc_prefix} ({from_acct} -> {to_acct}, paired bank_tx #{other_id})",
            )
        ]
        return self._finalize_staged(
            tx=tx,
            ext_id=ext_id,
            anchor_wave_id=anchor_wave_id,
            description=tx.description,
            notes=(
                f"{kind_label} transfer via clearing account "
                f"({from_acct} -> {to_acct}, paired bank_tx #{other_id})"
            ),
            line_items=line_items,
            rule_name="bank_transfer_clearing" if is_bank else "cc_transfer_clearing",
        )

    def _plan_gusto(
        self,
        tx: BankTransaction,
        match: ReconciliationMatch,
        ext_id: str,
        anchor_wave_id: str | None,
    ) -> StagedTransaction:
        assert tx.id is not None
        run = self.db.get_gusto_payroll_run(match.gusto_run_id or -1)
        if run is None:
            return StagedTransaction(
                external_id=ext_id,
                bank_tx_id=tx.id,
                tx_date=tx.posted_date,
                description=tx.description,
                notes="",
                anchor_account_key=tx.account_key,
                anchor_wave_account_id=anchor_wave_id,
                anchor_amount=tx.amount,
                anchor_direction=tx.direction,
                line_items=[],
                category_rule=match.match_type,
                status="needs_review",
                review_reason="Matched Gusto payroll run missing from database",
            )

        line_items: list[StagedLineItem] = []
        if match.match_type == "gusto_contractor":
            line_items.append(
                self._make_line(
                    "contractors_expense",
                    run.net_disbursement,
                    "INCREASE",
                    "Gusto contractor payouts",
                )
            )
        elif match.match_type == "gusto_net_pay":
            if run.gross_wages > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_gross_wages", run.gross_wages, "INCREASE", "Gross wages"
                    )
                )
            if run.reimbursements > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_reimbursements",
                        run.reimbursements,
                        "INCREASE",
                        "Expense reimbursements",
                    )
                )
            if run.employee_taxes > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_taxes_payable",
                        run.employee_taxes,
                        "INCREASE",
                        "Employee tax withholdings",
                    )
                )
            if run.employee_benefits > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_benefits_payable",
                        run.employee_benefits,
                        "INCREASE",
                        "Employee benefit deductions",
                    )
                )
        elif match.match_type == "gusto_taxes":
            if run.employee_taxes > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_taxes_payable",
                        run.employee_taxes,
                        "DECREASE",
                        "Remit employee tax withholdings",
                    )
                )
            if run.employer_taxes > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_employer_taxes",
                        run.employer_taxes,
                        "INCREASE",
                        "Employer payroll taxes",
                    )
                )
        elif match.match_type == "gusto_benefits":
            if run.employee_benefits > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_benefits_payable",
                        run.employee_benefits,
                        "DECREASE",
                        "Remit employee benefit deductions",
                    )
                )
            if run.employer_benefits > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_employer_benefits",
                        run.employer_benefits,
                        "INCREASE",
                        "Employer benefit contributions",
                    )
                )
        elif match.match_type == "gusto_combined":
            if run.gross_wages > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_gross_wages", run.gross_wages, "INCREASE", "Gross wages"
                    )
                )
            if run.reimbursements > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_reimbursements",
                        run.reimbursements,
                        "INCREASE",
                        "Expense reimbursements",
                    )
                )
            if run.employer_taxes > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_employer_taxes",
                        run.employer_taxes,
                        "INCREASE",
                        "Employer payroll taxes",
                    )
                )
            if tx.amount == run.total_all_in_debit and run.employer_benefits > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_employer_benefits",
                        run.employer_benefits,
                        "INCREASE",
                        "Employer benefit contributions",
                    )
                )
            elif run.employee_benefits > ZERO:
                line_items.append(
                    self._make_line(
                        "payroll_benefits_payable",
                        run.employee_benefits,
                        "INCREASE",
                        "Employee benefit deductions",
                    )
                )

        notes = self._format_gusto_notes(run, match.match_type)
        label = match.match_type.replace("gusto_", "").replace("_", " ").title()
        return self._finalize_staged(
            tx=tx,
            ext_id=ext_id,
            anchor_wave_id=anchor_wave_id,
            description=f"Gusto {label} ({run.check_date})",
            notes=notes,
            line_items=line_items,
            rule_name=match.match_type,
        )

    def _plan_harvest_deposit(
        self,
        tx: BankTransaction,
        matches: list[ReconciliationMatch],
        ext_id: str,
        anchor_wave_id: str | None,
    ) -> StagedTransaction:
        assert tx.id is not None
        matched_invoices: list[HarvestInvoice] = []
        for m in matches:
            if m.harvest_invoice_id is not None:
                inv = self.db.get_harvest_invoice(m.harvest_invoice_id)
                if inv is not None:
                    matched_invoices.append(inv)

        inv_labels = ", ".join(
            f"#{inv.invoice_number} ({inv.client_name})" for inv in matched_invoices
        )
        if (
            self.config.accounting.mirror_harvest_invoices
            and self.config.accounting.mirrored_invoice_deposit_strategy == "manual_ui_payment"
        ):
            return StagedTransaction(
                external_id=ext_id,
                bank_tx_id=tx.id,
                tx_date=tx.posted_date,
                description=tx.description,
                notes=f"Matched Harvest invoice(s): {inv_labels}",
                anchor_account_key=tx.account_key,
                anchor_wave_account_id=anchor_wave_id,
                anchor_amount=tx.amount,
                anchor_direction=tx.direction,
                line_items=[],
                category_rule="harvest_manual_ui_payment",
                status="manual_invoice_payment_required",
                review_reason=(
                    f"Record payment in Wave UI against mirrored invoice(s): {inv_labels}"
                ),
            )

        target_key = (
            "invoice_payments_clearing"
            if self.config.accounting.mirror_harvest_invoices
            else "revenue_default"
        )
        credit_balance = self._balance_for_effect(target_key, "CREDIT")
        line_items: list[StagedLineItem] = []

        if matched_invoices:
            total_invoiced = sum((inv.total_amount for inv in matched_invoices), ZERO)
            for inv in matched_invoices:
                cust = self.db.find_wave_customer_by_name(inv.client_name)
                line_items.append(
                    StagedLineItem(
                        account_key=target_key,
                        wave_account_id=self._wave_account_id(target_key),
                        amount=inv.total_amount,
                        balance=credit_balance,
                        description=f"Payment for Harvest Invoice #{inv.invoice_number}",
                        customer_name=inv.client_name,
                        wave_customer_id=cust.wave_id if cust else None,
                    )
                )
            if total_invoiced > tx.amount:
                fee = total_invoiced - tx.amount
                line_items.append(
                    self._make_line(
                        "merchant_fees",
                        fee,
                        self._balance_for_effect("merchant_fees", "DEBIT"),
                        f"Payment processor fee ({inv_labels})",
                    )
                )
        else:
            line_items.append(
                StagedLineItem(
                    account_key=target_key,
                    wave_account_id=self._wave_account_id(target_key),
                    amount=tx.amount,
                    balance=credit_balance,
                    description=tx.description,
                )
            )

        desc = f"Harvest Invoice Payment: {inv_labels}" if inv_labels else tx.description
        return self._finalize_staged(
            tx=tx,
            ext_id=ext_id,
            anchor_wave_id=anchor_wave_id,
            description=desc,
            notes=f"Matched Harvest deposit ({target_key}): {inv_labels}",
            line_items=line_items,
            rule_name="harvest_deposit",
        )

    def _make_line(
        self, account_key: str, amount: Decimal, balance: str, description: str
    ) -> StagedLineItem:
        return StagedLineItem(
            account_key=account_key,
            wave_account_id=self._wave_account_id(account_key),
            amount=amount,
            balance=balance,
            description=description,
        )

    def _finalize_staged(
        self,
        tx: BankTransaction,
        ext_id: str,
        anchor_wave_id: str | None,
        description: str,
        notes: str,
        line_items: list[StagedLineItem],
        rule_name: str,
    ) -> StagedTransaction:
        assert tx.id is not None
        try:
            validate_double_entry_balance(
                anchor_account_key=tx.account_key,
                anchor_direction=tx.direction,
                anchor_amount=tx.amount,
                line_items=line_items,
                normal_balance_lookup=self.normal_balance_for,
                anchor_wave_account_id=anchor_wave_id,
            )
            status = "staged"
            review_reason = ""
        except BalanceError as err:
            status = "needs_review"
            review_reason = str(err)

        return StagedTransaction(
            external_id=ext_id,
            bank_tx_id=tx.id,
            tx_date=tx.posted_date,
            description=description,
            notes=notes,
            anchor_account_key=tx.account_key,
            anchor_wave_account_id=anchor_wave_id,
            anchor_amount=tx.amount,
            anchor_direction=tx.direction,
            line_items=line_items,
            category_rule=rule_name,
            status=status,
            review_reason=review_reason,
        )

    def _looks_like_unpaired_cc_payment(self, tx: BankTransaction) -> bool:
        if tx.source == "grasshopper" and tx.direction == "WITHDRAWAL":
            return any(
                p.search(tx.description) for p in self.config.patterns.grasshopper_cc_payment
            )
        if tx.source == "capital_one" and tx.direction == "DEPOSIT":
            return (
                any(p.search(tx.description) for p in self.config.patterns.capital_one_payment)
                or "payment" in tx.source_category.lower()
            )
        return False

    @staticmethod
    def _format_gusto_notes(run: GustoPayrollRun, match_type: str) -> str:
        parts = [
            f"Gusto {match_type} check_date={run.check_date}",
            f"period={run.pay_period_start}..{run.pay_period_end}",
            f"employees={run.employee_count}",
            f"gross={fmt_decimal(run.gross_wages)}",
            f"reimb={fmt_decimal(run.reimbursements)}",
            f"ee_tax={fmt_decimal(run.employee_taxes)}",
            f"er_tax={fmt_decimal(run.employer_taxes)}",
        ]
        if run.tax_breakdown:
            tax_items = ", ".join(f"{k}={v}" for k, v in sorted(run.tax_breakdown.items()))
            parts.append(f"tax_detail=[{tax_items}]")
        return "; ".join(parts)
