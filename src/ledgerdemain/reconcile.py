"""Cross-source reconciliation across Harvest, Gusto, Grasshopper, and Capital One."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from ledgerdemain.config import AppConfig
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    BankTransaction,
    HarvestInvoice,
    HarvestPayment,
    ReconciliationMatch,
)
from ledgerdemain.money import ZERO, Money, format_money

_PROCESSOR_TOKENS = ("STRIPE", "PAYPAL", "SQUARE", "HARVEST", "ACH", "WIRE")
_BANK_TRANSFER_KEYWORDS = ("TRANSFER", "XFER", "SWEEP", "SAVINGS", "MONEY MARKET", "INTERNAL")
MatchKey = tuple[str, int, int | None, int | None, int | None, int | None]


@dataclass(slots=True)
class ReconcileSummary:
    """Summary counts produced by running reconciliation."""

    harvest_matches: int = 0
    gusto_matches: int = 0
    cc_transfer_matches: int = 0
    bank_transfer_matches: int = 0
    auto_confirmed: int = 0
    total_proposed: int = 0


def _days_between(iso_a: str, iso_b: str) -> int:
    return abs((date.fromisoformat(iso_a) - date.fromisoformat(iso_b)).days)


def _matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _client_tokens(name: str) -> list[str]:
    return [tok for tok in re.split(r"[^A-Za-z0-9]+", name.upper()) if len(tok) >= 3]


def _has_client_or_invoice_hint(tx: BankTransaction, inv: HarvestInvoice) -> bool:
    haystack = tx.normalized_description
    if inv.invoice_number and inv.invoice_number.upper() in haystack:
        return True
    return any(tok in haystack for tok in _client_tokens(inv.client_name))


class Reconciler:
    """Finds and persists cross-source matches across ingested datasets."""

    def __init__(self, db: LedgerDB, config: AppConfig) -> None:
        self.db = db
        self.config = config

    def reconcile_all(self, confirm_high_confidence: bool = False) -> ReconcileSummary:
        """Run transfer, Gusto payroll, and Harvest deposit reconciliation idempotently."""
        summary = ReconcileSummary()
        existing = self.db.list_matches()

        claimed_tx_ids: set[int] = set()
        claimed_payment_ids: set[int] = set()
        claimed_invoice_ids: set[int] = set()
        claimed_gusto_keys: set[tuple[int, str]] = set()
        rejected_keys: set[MatchKey] = set()

        for m in existing:
            key = (
                m.match_type,
                m.bank_tx_id,
                m.counterparty_bank_tx_id,
                m.harvest_invoice_id,
                m.harvest_payment_id,
                m.gusto_run_id,
            )
            if m.status == "rejected":
                rejected_keys.add(key)
                continue
            if m.status == "confirmed":
                claimed_tx_ids.add(m.bank_tx_id)
                if m.counterparty_bank_tx_id is not None:
                    claimed_tx_ids.add(m.counterparty_bank_tx_id)
                if m.harvest_payment_id is not None:
                    claimed_payment_ids.add(m.harvest_payment_id)
                if m.harvest_invoice_id is not None:
                    claimed_invoice_ids.add(m.harvest_invoice_id)
                if m.gusto_run_id is not None:
                    claimed_gusto_keys.add((m.gusto_run_id, m.match_type))

        summary.cc_transfer_matches = self._reconcile_cc_transfers(
            claimed_tx_ids, rejected_keys, confirm_high_confidence
        )
        summary.bank_transfer_matches = self._reconcile_bank_transfers(
            claimed_tx_ids, rejected_keys, confirm_high_confidence
        )
        summary.gusto_matches = self._reconcile_gusto_payroll(
            claimed_tx_ids, claimed_gusto_keys, rejected_keys, confirm_high_confidence
        )
        summary.harvest_matches = self._reconcile_harvest_deposits(
            claimed_tx_ids,
            claimed_payment_ids,
            claimed_invoice_ids,
            rejected_keys,
            confirm_high_confidence,
        )

        all_after = self.db.list_matches()
        summary.auto_confirmed = sum(1 for m in all_after if m.status == "confirmed")
        summary.total_proposed = sum(1 for m in all_after if m.status == "proposed")
        return summary

    def _save_match(self, match: ReconciliationMatch, confirm_high_confidence: bool) -> int:
        if (
            confirm_high_confidence
            and match.confidence >= self.config.accounting.auto_confirm_threshold
        ):
            match.status = "confirmed"
        else:
            match.status = "proposed"
        return self.db.upsert_match(match)

    def _reconcile_cc_transfers(
        self,
        claimed_tx_ids: set[int],
        rejected_keys: set[MatchKey],
        confirm_high_confidence: bool,
    ) -> int:
        gh_withdrawals = [
            tx
            for tx in self.db.list_bank_transactions(source="grasshopper", direction="WITHDRAWAL")
            if tx.id is not None
            and tx.id not in claimed_tx_ids
            and _matches_any(self.config.patterns.grasshopper_cc_payment, tx.description)
        ]
        co_deposits = [
            tx
            for tx in self.db.list_bank_transactions(source="capital_one", direction="DEPOSIT")
            if tx.id is not None
            and tx.id not in claimed_tx_ids
            and (
                _matches_any(self.config.patterns.capital_one_payment, tx.description)
                or "payment" in tx.source_category.lower()
            )
        ]

        window = self.config.accounting.transfer_match_window_days
        matched = 0
        for gh_tx in gh_withdrawals:
            assert gh_tx.id is not None
            candidates: list[tuple[int, BankTransaction]] = []
            for co_tx in co_deposits:
                assert co_tx.id is not None
                if co_tx.id in claimed_tx_ids:
                    continue
                if gh_tx.amount != co_tx.amount:
                    continue
                diff = _days_between(gh_tx.posted_date, co_tx.posted_date)
                if diff <= window:
                    key = ("cc_transfer", gh_tx.id, co_tx.id, None, None, None)
                    if key not in rejected_keys:
                        candidates.append((diff, co_tx))

            if not candidates:
                continue
            candidates.sort(key=lambda pair: (pair[0], pair[1].id or 0))
            diff, best_co = candidates[0]
            assert best_co.id is not None

            self._save_match(
                ReconciliationMatch(
                    match_type="cc_transfer",
                    bank_tx_id=gh_tx.id,
                    counterparty_bank_tx_id=best_co.id,
                    matched_amount=gh_tx.amount,
                    confidence=0.98,
                    reason=(
                        f"Exact CC payment transfer match ${format_money(gh_tx.amount)} "
                        f"({diff}d apart: GH {gh_tx.posted_date} <-> CO {best_co.posted_date})"
                    ),
                ),
                confirm_high_confidence,
            )
            claimed_tx_ids.add(gh_tx.id)
            claimed_tx_ids.add(best_co.id)
            matched += 1
        return matched

    def _reconcile_bank_transfers(
        self,
        claimed_tx_ids: set[int],
        rejected_keys: set[MatchKey],
        confirm_high_confidence: bool,
    ) -> int:
        gh_withdrawals = [
            tx
            for tx in self.db.list_bank_transactions(source="grasshopper", direction="WITHDRAWAL")
            if tx.id is not None and tx.id not in claimed_tx_ids
        ]
        gh_deposits = [
            tx
            for tx in self.db.list_bank_transactions(source="grasshopper", direction="DEPOSIT")
            if tx.id is not None and tx.id not in claimed_tx_ids
        ]
        window = self.config.accounting.transfer_match_window_days
        matched = 0

        for w_tx in gh_withdrawals:
            assert w_tx.id is not None
            candidates: list[tuple[int, int, BankTransaction, float]] = []
            for d_tx in gh_deposits:
                assert d_tx.id is not None
                if d_tx.id in claimed_tx_ids:
                    continue
                if d_tx.account_key == w_tx.account_key:
                    continue
                if d_tx.amount != w_tx.amount:
                    continue
                diff = _days_between(w_tx.posted_date, d_tx.posted_date)
                if diff > window:
                    continue
                key: MatchKey = ("bank_transfer", w_tx.id, d_tx.id, None, None, None)
                if key in rejected_keys:
                    continue
                has_kw = any(
                    kw in w_tx.normalized_description or kw in d_tx.normalized_description
                    for kw in _BANK_TRANSFER_KEYWORDS
                )
                conf = 0.98 if has_kw else 0.94
                candidates.append((0 if has_kw else 1, diff, d_tx, conf))

            if not candidates:
                continue
            candidates.sort(key=lambda item: (item[0], item[1], item[2].id or 0))
            _, diff, best_dep, conf = candidates[0]
            assert best_dep.id is not None

            self._save_match(
                ReconciliationMatch(
                    match_type="bank_transfer",
                    bank_tx_id=w_tx.id,
                    counterparty_bank_tx_id=best_dep.id,
                    matched_amount=w_tx.amount,
                    confidence=conf,
                    reason=(
                        f"Internal bank transfer {w_tx.account_key} -> {best_dep.account_key} "
                        f"${format_money(w_tx.amount)} ({diff}d apart)"
                    ),
                ),
                confirm_high_confidence,
            )
            claimed_tx_ids.add(w_tx.id)
            claimed_tx_ids.add(best_dep.id)
            matched += 1
        return matched

    def _reconcile_gusto_payroll(
        self,
        claimed_tx_ids: set[int],
        claimed_gusto_keys: set[tuple[int, str]],
        rejected_keys: set[MatchKey],
        confirm_high_confidence: bool,
    ) -> int:
        gusto_withdrawals = [
            tx
            for tx in self.db.list_bank_transactions(source="grasshopper", direction="WITHDRAWAL")
            if tx.id is not None
            and tx.id not in claimed_tx_ids
            and _matches_any(self.config.patterns.gusto_payroll, tx.description)
            and not _matches_any(self.config.patterns.gusto_fee, tx.description)
        ]
        runs = self.db.list_gusto_payroll_runs()
        window = self.config.accounting.payroll_match_window_days
        matched = 0

        for run in runs:
            assert run.id is not None
            if bool(run.raw.get("non_cash_imputed_benefits")):
                continue
            nearby = [
                tx
                for tx in gusto_withdrawals
                if tx.id is not None
                and tx.id not in claimed_tx_ids
                and _days_between(tx.posted_date, run.check_date) <= window
            ]

            if run.run_type == "contractor":
                if (run.id, "gusto_contractor") in claimed_gusto_keys:
                    continue
                tx_match = self._pick_gusto_candidate(
                    nearby,
                    run.net_disbursement,
                    run.check_date,
                    ("CONTRACTOR", "PAY"),
                    "gusto_contractor",
                    run.id,
                    rejected_keys,
                )
                if tx_match is not None:
                    tx, conf = tx_match
                    assert tx.id is not None
                    self._save_match(
                        ReconciliationMatch(
                            match_type="gusto_contractor",
                            bank_tx_id=tx.id,
                            gusto_run_id=run.id,
                            matched_amount=tx.amount,
                            confidence=conf,
                            reason=f"Gusto contractor payout check_date={run.check_date}",
                        ),
                        confirm_high_confidence,
                    )
                    claimed_tx_ids.add(tx.id)
                    claimed_gusto_keys.add((run.id, "gusto_contractor"))
                    matched += 1
                continue

            matched_split = False

            if run.net_disbursement > ZERO and (run.id, "gusto_net_pay") not in claimed_gusto_keys:
                net_match = self._pick_gusto_candidate(
                    nearby,
                    run.net_disbursement,
                    run.check_date,
                    ("NET", "PAY"),
                    "gusto_net_pay",
                    run.id,
                    rejected_keys,
                )
                if net_match is not None:
                    tx, conf = net_match
                    assert tx.id is not None
                    self._save_match(
                        ReconciliationMatch(
                            match_type="gusto_net_pay",
                            bank_tx_id=tx.id,
                            gusto_run_id=run.id,
                            matched_amount=tx.amount,
                            confidence=conf,
                            reason=(
                                f"Gusto net pay + reimbursements check_date={run.check_date} "
                                f"({run.pay_period_start}..{run.pay_period_end})"
                            ),
                        ),
                        confirm_high_confidence,
                    )
                    claimed_tx_ids.add(tx.id)
                    claimed_gusto_keys.add((run.id, "gusto_net_pay"))
                    nearby = [item for item in nearby if item.id != tx.id]
                    matched += 1
                    matched_split = True

            if run.total_tax_debit > ZERO and (run.id, "gusto_taxes") not in claimed_gusto_keys:
                tax_match = self._pick_gusto_candidate(
                    nearby,
                    run.total_tax_debit,
                    run.check_date,
                    ("TAX",),
                    "gusto_taxes",
                    run.id,
                    rejected_keys,
                )
                if tax_match is not None:
                    tx, conf = tax_match
                    assert tx.id is not None
                    self._save_match(
                        ReconciliationMatch(
                            match_type="gusto_taxes",
                            bank_tx_id=tx.id,
                            gusto_run_id=run.id,
                            matched_amount=tx.amount,
                            confidence=conf,
                            reason=(
                                f"Gusto payroll tax debit check_date={run.check_date} "
                                f"(EE={format_money(run.employee_taxes)}, "
                                f"ER={format_money(run.employer_taxes)})"
                            ),
                        ),
                        confirm_high_confidence,
                    )
                    claimed_tx_ids.add(tx.id)
                    claimed_gusto_keys.add((run.id, "gusto_taxes"))
                    nearby = [item for item in nearby if item.id != tx.id]
                    matched += 1
                    matched_split = True

            if (
                run.total_benefits_debit > ZERO
                and (run.id, "gusto_benefits") not in claimed_gusto_keys
            ):
                ben_match = self._pick_gusto_candidate(
                    nearby,
                    run.total_benefits_debit,
                    run.check_date,
                    ("BEN", "401", "INS", "HEALTH"),
                    "gusto_benefits",
                    run.id,
                    rejected_keys,
                )
                if ben_match is not None:
                    tx, conf = ben_match
                    assert tx.id is not None
                    self._save_match(
                        ReconciliationMatch(
                            match_type="gusto_benefits",
                            bank_tx_id=tx.id,
                            gusto_run_id=run.id,
                            matched_amount=tx.amount,
                            confidence=conf,
                            reason=f"Gusto benefits debit check_date={run.check_date}",
                        ),
                        confirm_high_confidence,
                    )
                    claimed_tx_ids.add(tx.id)
                    claimed_gusto_keys.add((run.id, "gusto_benefits"))
                    nearby = [item for item in nearby if item.id != tx.id]
                    matched += 1

            if (
                not matched_split
                and (run.id, "gusto_combined") not in claimed_gusto_keys
                and (run.id, "gusto_net_pay") not in claimed_gusto_keys
                and (run.id, "gusto_taxes") not in claimed_gusto_keys
            ):
                for target_amt, label in (
                    (run.combined_net_and_tax_debit, "net+taxes"),
                    (run.total_all_in_debit, "net+taxes+benefits"),
                ):
                    if target_amt <= ZERO:
                        continue
                    comb_match = self._pick_gusto_candidate(
                        nearby,
                        target_amt,
                        run.check_date,
                        ("PAY", "TAX", "GUSTO"),
                        "gusto_combined",
                        run.id,
                        rejected_keys,
                    )
                    if comb_match is not None:
                        tx, _ = comb_match
                        assert tx.id is not None
                        self._save_match(
                            ReconciliationMatch(
                                match_type="gusto_combined",
                                bank_tx_id=tx.id,
                                gusto_run_id=run.id,
                                matched_amount=tx.amount,
                                confidence=0.95,
                                reason=f"Gusto combined ({label}) check_date={run.check_date}",
                            ),
                            confirm_high_confidence,
                        )
                        claimed_tx_ids.add(tx.id)
                        claimed_gusto_keys.add((run.id, "gusto_combined"))
                        matched += 1
                        break

        return matched

    @staticmethod
    def _pick_gusto_candidate(
        candidates: list[BankTransaction],
        target_amount: Money,
        check_date: str,
        keywords: tuple[str, ...],
        match_type: str,
        run_id: int,
        rejected_keys: set[MatchKey],
    ) -> tuple[BankTransaction, float] | None:
        scored: list[tuple[int, int, BankTransaction, float]] = []
        for tx in candidates:
            assert tx.id is not None
            if tx.amount != target_amount:
                continue
            key = (match_type, tx.id, None, None, None, run_id)
            if key in rejected_keys:
                continue
            has_kw = any(kw in tx.normalized_description for kw in keywords)
            diff = _days_between(tx.posted_date, check_date)
            conf = (
                0.98
                if has_kw
                else (0.95 if match_type in {"gusto_contractor", "gusto_benefits"} else 0.92)
            )
            scored.append((0 if has_kw else 1, diff, tx, conf))

        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2].id or 0))
        return scored[0][2], scored[0][3]

    def _reconcile_harvest_deposits(
        self,
        claimed_tx_ids: set[int],
        claimed_payment_ids: set[int],
        claimed_invoice_ids: set[int],
        rejected_keys: set[MatchKey],
        confirm_high_confidence: bool,
    ) -> int:
        deposits = [
            tx
            for tx in self.db.list_bank_transactions(source="grasshopper", direction="DEPOSIT")
            if tx.id is not None
            and tx.id not in claimed_tx_ids
            and not _matches_any(self.config.patterns.bank_interest, tx.description)
        ]
        invoices = [
            inv
            for inv in self.db.list_harvest_invoices()
            if inv.id is not None and inv.state != "draft"
        ]
        inv_by_harvest_id = {inv.harvest_id: inv for inv in invoices}
        payments = self.db.list_harvest_payments()
        window = self.config.accounting.deposit_match_window_days
        matched = 0

        # Tier 1: Exact HarvestPayment match
        for tx in deposits:
            assert tx.id is not None
            if tx.id in claimed_tx_ids:
                continue
            pay_candidates: list[tuple[int, int, HarvestPayment, HarvestInvoice | None]] = []
            for pay in payments:
                assert pay.id is not None
                if pay.id in claimed_payment_ids:
                    continue
                if pay.amount != tx.amount:
                    continue
                diff = _days_between(tx.posted_date, pay.paid_date)
                if diff > window:
                    continue
                inv = inv_by_harvest_id.get(pay.invoice_harvest_id)
                if inv and inv.id in claimed_invoice_ids:
                    continue
                key: MatchKey = (
                    "harvest_deposit",
                    tx.id,
                    None,
                    inv.id if inv else None,
                    pay.id,
                    None,
                )
                if key in rejected_keys:
                    continue
                hint = 0 if (inv and _has_client_or_invoice_hint(tx, inv)) else 1
                pay_candidates.append((hint, diff, pay, inv))

            if pay_candidates:
                pay_candidates.sort(key=lambda item: (item[0], item[1], item[2].id or 0))
                hint, diff, best_pay, best_inv = pay_candidates[0]
                assert best_pay.id is not None
                inv_label = (
                    f"Inv #{best_inv.invoice_number} ({best_inv.client_name})"
                    if best_inv
                    else best_pay.invoice_harvest_id
                )
                self._save_match(
                    ReconciliationMatch(
                        match_type="harvest_deposit",
                        bank_tx_id=tx.id,
                        harvest_invoice_id=best_inv.id if best_inv else None,
                        harvest_payment_id=best_pay.id,
                        matched_amount=tx.amount,
                        confidence=0.98,
                        reason=f"Exact Harvest payment match to {inv_label} ({diff}d apart)",
                    ),
                    confirm_high_confidence,
                )
                claimed_tx_ids.add(tx.id)
                claimed_payment_ids.add(best_pay.id)
                if best_inv and best_inv.id is not None:
                    claimed_invoice_ids.add(best_inv.id)
                matched += 1

        # Tier 2: Exact single HarvestInvoice match
        for tx in deposits:
            assert tx.id is not None
            if tx.id in claimed_tx_ids:
                continue
            inv_candidates: list[tuple[int, int, HarvestInvoice, float]] = []
            for inv in invoices:
                assert inv.id is not None
                if inv.id in claimed_invoice_ids:
                    continue
                if inv.total_amount != tx.amount:
                    continue
                if not self._invoice_within_deposit_window(inv, tx.posted_date, window):
                    continue
                inv_key: MatchKey = ("harvest_deposit", tx.id, None, inv.id, None, None)
                if inv_key in rejected_keys:
                    continue
                has_hint = _has_client_or_invoice_hint(tx, inv)
                ref_date = inv.paid_date or inv.issue_date
                diff = _days_between(tx.posted_date, ref_date)
                conf = 0.96 if has_hint else 0.92
                inv_candidates.append((0 if has_hint else 1, diff, inv, conf))

            if inv_candidates:
                inv_candidates.sort(key=lambda item: (item[0], item[1], item[2].id or 0))
                _, diff, best_inv, conf = inv_candidates[0]
                assert best_inv.id is not None
                self._save_match(
                    ReconciliationMatch(
                        match_type="harvest_deposit",
                        bank_tx_id=tx.id,
                        harvest_invoice_id=best_inv.id,
                        matched_amount=tx.amount,
                        confidence=conf,
                        reason=(
                            f"Exact match to Harvest Inv #{best_inv.invoice_number} "
                            f"({best_inv.client_name}, {diff}d from reference date)"
                        ),
                    ),
                    confirm_high_confidence,
                )
                claimed_tx_ids.add(tx.id)
                claimed_invoice_ids.add(best_inv.id)
                matched += 1

        # Tier 3: Multi-invoice batch payment (2-4 invoices for same client summing to deposit)
        for tx in deposits:
            assert tx.id is not None
            if tx.id in claimed_tx_ids:
                continue
            client_groups: dict[str, list[HarvestInvoice]] = {}
            for inv in invoices:
                assert inv.id is not None
                if inv.id in claimed_invoice_ids:
                    continue
                if inv.total_amount >= tx.amount or inv.total_amount <= ZERO:
                    continue
                if not self._invoice_within_deposit_window(inv, tx.posted_date, window):
                    continue
                inv_key = ("harvest_deposit", tx.id, None, inv.id, None, None)
                if inv_key in rejected_keys:
                    continue
                client_groups.setdefault(inv.client_name.lower(), []).append(inv)

            subset = self._find_batch_subset(tx.amount, client_groups)
            if subset:
                inv_nums = ", ".join(f"#{inv.invoice_number}" for inv in subset)
                for inv in subset:
                    assert inv.id is not None
                    self._save_match(
                        ReconciliationMatch(
                            match_type="harvest_deposit",
                            bank_tx_id=tx.id,
                            harvest_invoice_id=inv.id,
                            matched_amount=inv.total_amount,
                            confidence=0.86,
                            reason=(
                                f"Multi-invoice batch deposit ({inv_nums}) for {inv.client_name}"
                            ),
                        ),
                        confirm_high_confidence,
                    )
                    claimed_invoice_ids.add(inv.id)
                claimed_tx_ids.add(tx.id)
                matched += len(subset)

        # Tier 4: Net-of-fee payment processor deposit (0.1% - 4.0% + $0.35 below invoice total)
        for tx in deposits:
            assert tx.id is not None
            if tx.id in claimed_tx_ids:
                continue
            is_processor = any(tok in tx.normalized_description for tok in _PROCESSOR_TOKENS)
            fee_candidates: list[tuple[int, Money, HarvestInvoice]] = []
            for inv in invoices:
                assert inv.id is not None
                if inv.id in claimed_invoice_ids:
                    continue
                if inv.total_amount <= tx.amount:
                    continue
                fee = inv.total_amount - tx.amount
                min_fee = inv.total_amount.mul_ratio(1, 1000)
                max_fee = inv.total_amount.mul_ratio(4, 100) + Money(35)
                if not (min_fee <= fee <= max_fee):
                    continue
                if not self._invoice_within_deposit_window(inv, tx.posted_date, window):
                    continue
                has_hint = _has_client_or_invoice_hint(tx, inv)
                if not (is_processor or has_hint):
                    continue
                inv_key = ("harvest_deposit", tx.id, None, inv.id, None, None)
                if inv_key in rejected_keys:
                    continue
                fee_candidates.append((0 if has_hint else 1, fee, inv))

            if fee_candidates:
                fee_candidates.sort(key=lambda item: (item[0], item[1], item[2].id or 0))
                _, fee, best_inv = fee_candidates[0]
                assert best_inv.id is not None
                self._save_match(
                    ReconciliationMatch(
                        match_type="harvest_deposit",
                        bank_tx_id=tx.id,
                        harvest_invoice_id=best_inv.id,
                        matched_amount=tx.amount,
                        confidence=0.78,
                        reason=(
                            "Possible net-of-fee payment for Harvest Inv "
                            f"#{best_inv.invoice_number} "
                            f"({best_inv.client_name}; fee=${format_money(fee)})"
                        ),
                    ),
                    confirm_high_confidence,
                )
                claimed_tx_ids.add(tx.id)
                claimed_invoice_ids.add(best_inv.id)
                matched += 1

        return matched

    @staticmethod
    def _invoice_within_deposit_window(
        inv: HarvestInvoice, deposit_date: str, window_days: int
    ) -> bool:
        if inv.paid_date:
            return _days_between(deposit_date, inv.paid_date) <= window_days
        dep = date.fromisoformat(deposit_date)
        iss = date.fromisoformat(inv.issue_date)
        delta = (dep - iss).days
        return -window_days <= delta <= 120

    @staticmethod
    def _find_batch_subset(
        target: Money, client_groups: dict[str, list[HarvestInvoice]]
    ) -> list[HarvestInvoice] | None:
        from itertools import combinations

        for invs in client_groups.values():
            if len(invs) < 2:
                continue
            ordered = sorted(invs, key=lambda i: (i.issue_date, i.id or 0))[:16]
            for size in range(2, min(5, len(ordered) + 1)):
                for combo in combinations(ordered, size):
                    if sum((item.total_amount for item in combo), ZERO) == target:
                        return list(combo)
        return None
