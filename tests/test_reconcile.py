"""Tests for cross-source reconciliation across Harvest, Gusto, Grasshopper, and Capital One."""

from __future__ import annotations

from pathlib import Path

from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    HarvestInvoice,
    HarvestPayment,
)
from ledgerdemain.money import ZERO, Money
from ledgerdemain.reconcile import Reconciler
from tests.fixtures import make_test_config


def _bank_tx(
    source: str,
    posted_date: str,
    desc: str,
    amount: str,
    direction: str,
    fp: str,
) -> BankTransaction:
    return BankTransaction(
        source=source,
        account_key="grasshopper_checking" if source == "grasshopper" else "capital_one_card",
        external_id=None,
        fingerprint=fp,
        tx_date=posted_date,
        posted_date=posted_date,
        description=desc,
        normalized_description=desc.upper(),
        amount=Money.parse(amount),
        direction=direction,
    )


def test_harvest_single_batch_and_net_fee_matches(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_harvest_invoices(
            [
                HarvestInvoice(
                    "1",
                    "1001",
                    "c1",
                    "Acme Corp",
                    "2026-01-01",
                    "2026-01-31",
                    "2026-01-05",
                    "paid",
                    "USD",
                    Money.parse("1000.00"),
                    ZERO,
                    Money.parse("1000.00"),
                    ZERO,
                ),
                HarvestInvoice(
                    "2",
                    "1002",
                    "c2",
                    "Batch Co",
                    "2026-01-02",
                    "2026-02-01",
                    None,
                    "open",
                    "USD",
                    Money.parse("400.00"),
                    ZERO,
                    Money.parse("400.00"),
                    Money.parse("400.00"),
                ),
                HarvestInvoice(
                    "3",
                    "1003",
                    "c2",
                    "Batch Co",
                    "2026-01-03",
                    "2026-02-02",
                    None,
                    "open",
                    "USD",
                    Money.parse("600.00"),
                    ZERO,
                    Money.parse("600.00"),
                    Money.parse("600.00"),
                ),
                HarvestInvoice(
                    "4",
                    "1004",
                    "c3",
                    "Widget Labs",
                    "2026-01-04",
                    "2026-02-03",
                    "2026-01-12",
                    "paid",
                    "USD",
                    Money.parse("2000.00"),
                    ZERO,
                    Money.parse("2000.00"),
                    ZERO,
                ),
            ]
        )
        db.upsert_harvest_payments(
            [HarvestPayment("p1", "1", "2026-01-05", Money.parse("1000.00"), "ACH")]
        )
        db.upsert_bank_transactions(
            [
                _bank_tx(
                    "grasshopper", "2026-01-06", "ACH ACME CORP", "1000.00", "DEPOSIT", "gh-1"
                ),
                _bank_tx(
                    "grasshopper", "2026-01-10", "WIRE BATCH CO", "1000.00", "DEPOSIT", "gh-2"
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-01-12",
                    "STRIPE TRANSFER WIDGET LABS",
                    "1941.70",
                    "DEPOSIT",
                    "gh-3",
                ),
            ]
        )

        summary = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        assert summary.harvest_matches == 4  # 1 exact payment + 2 batch + 1 net-of-fee
        matches = db.list_matches(match_type="harvest_deposit")
        assert len(matches) == 4
        statuses = {m.reason.split()[0]: m.status for m in matches}
        assert statuses["Exact"] == "confirmed"
        assert statuses["Multi-invoice"] == "proposed"  # 0.86 < 0.90
        assert statuses["Possible"] == "proposed"  # 0.78 < 0.90


def test_gusto_split_combined_contractor_and_fee_exclusion(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_gusto_payroll_runs(
            [
                GustoPayrollRun(
                    fingerprint="run-split",
                    check_date="2026-01-10",
                    pay_period_start="2026-01-01",
                    pay_period_end="2026-01-15",
                    run_type="regular",
                    gross_wages=Money.parse("4500.00"),
                    reimbursements=Money.parse("150.00"),
                    employee_taxes=Money.parse("900.00"),
                    employer_taxes=Money.parse("350.00"),
                    employee_benefits=Money.parse("100.00"),
                    employer_benefits=Money.parse("200.00"),
                    net_pay=Money.parse("3500.00"),
                ),
                GustoPayrollRun(
                    fingerprint="run-comb",
                    check_date="2026-01-25",
                    pay_period_start="2026-01-16",
                    pay_period_end="2026-01-31",
                    run_type="regular",
                    gross_wages=Money.parse("2000.00"),
                    reimbursements=ZERO,
                    employee_taxes=Money.parse("400.00"),
                    employer_taxes=Money.parse("160.00"),
                    employee_benefits=ZERO,
                    employer_benefits=ZERO,
                    net_pay=Money.parse("1600.00"),
                ),
                GustoPayrollRun(
                    fingerprint="run-contr",
                    check_date="2026-01-18",
                    pay_period_start="2026-01-18",
                    pay_period_end="2026-01-18",
                    run_type="contractor",
                    gross_wages=Money.parse("1200.00"),
                    reimbursements=ZERO,
                    employee_taxes=ZERO,
                    employer_taxes=ZERO,
                    employee_benefits=ZERO,
                    employer_benefits=ZERO,
                    net_pay=Money.parse("1200.00"),
                ),
            ]
        )
        db.upsert_bank_transactions(
            [
                _bank_tx(
                    "grasshopper",
                    "2026-01-09",
                    "GUSTO FEE MONTHLY",
                    "45.00",
                    "WITHDRAWAL",
                    "gh-fee",
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-01-10",
                    "GUSTO PAY 260110",
                    "3650.00",
                    "WITHDRAWAL",
                    "gh-net",
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-01-10",
                    "GUSTO TAX 260110",
                    "1250.00",
                    "WITHDRAWAL",
                    "gh-tax",
                ),
                _bank_tx(
                    "grasshopper", "2026-01-11", "GUSTO BEN 401K", "300.00", "WITHDRAWAL", "gh-ben"
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-01-18",
                    "GUSTO CONTRACTOR PAY",
                    "1200.00",
                    "WITHDRAWAL",
                    "gh-ctr",
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-01-25",
                    "GUSTO PAYROLL DEBIT",
                    "2160.00",
                    "WITHDRAWAL",
                    "gh-comb",
                ),
            ]
        )

        summary = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        assert summary.gusto_matches == 5
        matches = db.list_matches(status="confirmed")
        types = sorted(m.match_type for m in matches)
        assert types == [
            "gusto_benefits",
            "gusto_combined",
            "gusto_contractor",
            "gusto_net_pay",
            "gusto_taxes",
        ]


def test_cc_transfer_matching_and_unpaired_held(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_bank_transactions(
            [
                _bank_tx(
                    "grasshopper",
                    "2026-01-12",
                    "CAPITAL ONE AUTOPAY ACH",
                    "500.00",
                    "WITHDRAWAL",
                    "gh-cc1",
                ),
                _bank_tx(
                    "capital_one",
                    "2026-01-13",
                    "CAPITAL ONE AUTOPAY PYMT",
                    "500.00",
                    "DEPOSIT",
                    "co-cc1",
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-01-29",
                    "CAPITAL ONE ONLINE PMT",
                    "750.00",
                    "WITHDRAWAL",
                    "gh-cc2",
                ),
            ]
        )
        summary = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        assert summary.cc_transfer_matches == 1
        assert summary.auto_confirmed == 1


def test_cc_transfer_requires_a_payment_description(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_bank_transactions(
            [
                _bank_tx(
                    "grasshopper",
                    "2026-01-12",
                    "CAPITAL ONE AUTOPAY ACH",
                    "500.00",
                    "WITHDRAWAL",
                    "gh-cc1",
                ),
                _bank_tx(
                    "capital_one",
                    "2026-01-13",
                    "ACME REFUND",
                    "500.00",
                    "DEPOSIT",
                    "co-refund",
                ),
            ]
        )
        summary = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        assert summary.cc_transfer_matches == 0


def test_cc_transfer_matches_online_and_mobile_payment_descriptions(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_bank_transactions(
            [
                _bank_tx(
                    "grasshopper",
                    "2026-01-12",
                    "CAPITAL ONE AUTOPAY ACH",
                    "500.00",
                    "WITHDRAWAL",
                    "gh-1",
                ),
                _bank_tx(
                    "capital_one",
                    "2026-01-12",
                    "CAPITAL ONE ONLINE PYMT",
                    "500.00",
                    "DEPOSIT",
                    "co-1",
                ),
                _bank_tx(
                    "grasshopper",
                    "2026-02-12",
                    "CAPITAL ONE AUTOPAY ACH",
                    "300.00",
                    "WITHDRAWAL",
                    "gh-2",
                ),
                _bank_tx(
                    "capital_one",
                    "2026-02-12",
                    "CAPITAL ONE MOBILE PYMT",
                    "300.00",
                    "DEPOSIT",
                    "co-2",
                ),
            ]
        )
        summary = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        assert summary.cc_transfer_matches == 2
