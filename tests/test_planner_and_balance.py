"""Tests for double-entry validation, accounting modes, payroll clearing, and CC transfers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import ConfigError, load_rules
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    HarvestInvoice,
    StagedLineItem,
)
from ledgerdemain.money import ZERO, Money
from ledgerdemain.planner import (
    DEFAULT_NORMAL_BALANCE,
    BalanceError,
    Planner,
    validate_double_entry_balance,
)
from ledgerdemain.reconcile import Reconciler
from tests.fixtures import make_test_config


def _lookup(key: str) -> str:
    return DEFAULT_NORMAL_BALANCE.get(key, "DEBIT")


def _tx(
    source: str,
    posted_date: str,
    desc: str,
    amt: str,
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
        amount=Money.parse(amt),
        direction=direction,
    )


def test_validate_double_entry_all_nine_patterns_and_rejections() -> None:
    # 1. Bank deposit (sales)
    validate_double_entry_balance(
        "grasshopper_checking",
        "DEPOSIT",
        Money.parse("3000.00"),
        [StagedLineItem("revenue_default", None, Money.parse("3000.00"), "INCREASE")],
        _lookup,
    )
    # 2. Bank expense
    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money.parse("45.00"),
        [StagedLineItem("payroll_processing_fees", None, Money.parse("45.00"), "INCREASE")],
        _lookup,
    )
    # 3. Credit card purchase
    validate_double_entry_balance(
        "capital_one_card",
        "WITHDRAWAL",
        Money.parse("142.50"),
        [StagedLineItem("cloud_hosting", None, Money.parse("142.50"), "INCREASE")],
        _lookup,
    )
    # 4. Credit card refund
    validate_double_entry_balance(
        "capital_one_card",
        "DEPOSIT",
        Money.parse("15.00"),
        [StagedLineItem("software_subscriptions", None, Money.parse("15.00"), "DECREASE")],
        _lookup,
    )
    # 5. Gusto Net split
    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money.parse("3650.00"),
        [
            StagedLineItem("payroll_gross_wages", None, Money.parse("4500.00"), "INCREASE"),
            StagedLineItem("payroll_reimbursements", None, Money.parse("150.00"), "INCREASE"),
            StagedLineItem("payroll_taxes_payable", None, Money.parse("900.00"), "INCREASE"),
            StagedLineItem("payroll_benefits_payable", None, Money.parse("100.00"), "INCREASE"),
        ],
        _lookup,
    )
    # 6. Gusto Tax clearing
    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money.parse("1250.00"),
        [
            StagedLineItem("payroll_taxes_payable", None, Money.parse("900.00"), "DECREASE"),
            StagedLineItem("payroll_employer_taxes", None, Money.parse("350.00"), "INCREASE"),
        ],
        _lookup,
    )
    # 7. Gusto Benefits clearing
    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money.parse("300.00"),
        [
            StagedLineItem("payroll_benefits_payable", None, Money.parse("100.00"), "DECREASE"),
            StagedLineItem("payroll_employer_benefits", None, Money.parse("200.00"), "INCREASE"),
        ],
        _lookup,
    )
    # 8. Gusto combined
    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money.parse("2160.00"),
        [
            StagedLineItem("payroll_gross_wages", None, Money.parse("2000.00"), "INCREASE"),
            StagedLineItem("payroll_employer_taxes", None, Money.parse("160.00"), "INCREASE"),
        ],
        _lookup,
    )
    # 9. Harvest net-of-merchant-fee
    validate_double_entry_balance(
        "grasshopper_checking",
        "DEPOSIT",
        Money.parse("1940.00"),
        [
            StagedLineItem("revenue_default", None, Money.parse("2000.00"), "INCREASE"),
            StagedLineItem("merchant_fees", None, Money.parse("60.00"), "INCREASE"),
        ],
        _lookup,
    )

    # Reject 1-cent imbalance
    with pytest.raises(BalanceError):
        validate_double_entry_balance(
            "capital_one_card",
            "WITHDRAWAL",
            Money.parse("100.00"),
            [StagedLineItem("cloud_hosting", None, Money.parse("99.99"), "INCREASE")],
            _lookup,
        )

    # Reject anchor in line items
    with pytest.raises(BalanceError):
        validate_double_entry_balance(
            "grasshopper_checking",
            "WITHDRAWAL",
            Money.parse("100.00"),
            [StagedLineItem("grasshopper_checking", None, Money.parse("100.00"), "DECREASE")],
            _lookup,
        )


def test_invalid_mirror_plus_direct_income_config_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        make_test_config(tmp_path, mirror_harvest_invoices=True, deposit_strategy="direct_income")


@pytest.mark.parametrize(
    ("mirror", "strat", "expected_status", "expected_acc"),
    [
        (True, "ar_clearing", "staged", "invoice_payments_clearing"),
        (True, "manual_ui_payment", "manual_invoice_payment_required", None),
        (False, "direct_income", "staged", "revenue_default"),
    ],
)
def test_harvest_accounting_strategies_and_payroll_clearing(
    tmp_path: Path,
    mirror: bool,
    strat: str,
    expected_status: str,
    expected_acc: str | None,
) -> None:
    sub_dir = tmp_path / strat
    sub_dir.mkdir()
    cfg = make_test_config(sub_dir, mirror_harvest_invoices=mirror, deposit_strategy=strat)
    rules = load_rules(cfg.general.rules_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_harvest_invoices(
            [
                HarvestInvoice(
                    "101",
                    "1001",
                    "10",
                    "Acme Corp",
                    "2026-01-01",
                    "2026-01-31",
                    "2026-01-05",
                    "paid",
                    "USD",
                    Money.parse("3000.00"),
                    ZERO,
                    Money.parse("3000.00"),
                    ZERO,
                )
            ]
        )
        db.upsert_gusto_payroll_runs(
            [
                GustoPayrollRun(
                    fingerprint="g1",
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
                )
            ]
        )
        db.upsert_bank_transactions(
            [
                _tx("grasshopper", "2026-01-05", "ACH ACME CORP", "3000.00", "DEPOSIT", "gh-dep"),
                _tx(
                    "grasshopper",
                    "2026-01-10",
                    "GUSTO PAY 260110",
                    "3650.00",
                    "WITHDRAWAL",
                    "gh-net",
                ),
                _tx(
                    "grasshopper",
                    "2026-01-10",
                    "GUSTO TAX 260110",
                    "1250.00",
                    "WITHDRAWAL",
                    "gh-tax",
                ),
                _tx(
                    "grasshopper",
                    "2026-01-10",
                    "GUSTO BEN 260110",
                    "300.00",
                    "WITHDRAWAL",
                    "gh-ben",
                ),
            ]
        )
        Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        summary = Planner(db, cfg, Categorizer(rules)).build_plan()

        assert summary.staged_invoices == (1 if mirror else 0)
        staged = db.list_staged_transactions()
        dep_tx = next(t for t in staged if t.anchor_direction == "DEPOSIT")
        assert dep_tx.status == expected_status
        if expected_acc:
            assert dep_tx.line_items[0].account_key == expected_acc

        # Verify Payroll Taxes Payable & Benefits Payable net to $0.00
        tax_net = ZERO
        ben_net = ZERO
        for stx in staged:
            for li in stx.line_items:
                delta = li.amount if li.balance == "INCREASE" else -li.amount
                if li.account_key == "payroll_taxes_payable":
                    tax_net += delta
                elif li.account_key == "payroll_benefits_payable":
                    ben_net += delta
        assert tax_net == ZERO
        assert ben_net == ZERO


@pytest.mark.parametrize(
    ("strat", "expected_status"),
    [
        ("manual_transfer", "manual_transfer_required"),
        ("clearing_account", "staged"),
    ],
)
def test_cc_transfer_manual_vs_clearing_and_unpaired(
    tmp_path: Path, strat: str, expected_status: str
) -> None:
    sub_dir = tmp_path / strat
    sub_dir.mkdir()
    cfg = make_test_config(sub_dir, transfer_strategy=strat)
    rules = load_rules(cfg.general.rules_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_bank_transactions(
            [
                _tx(
                    "grasshopper",
                    "2026-01-12",
                    "CAPITAL ONE AUTOPAY ACH",
                    "500.00",
                    "WITHDRAWAL",
                    "gh-p",
                ),
                _tx(
                    "capital_one",
                    "2026-01-12",
                    "CAPITAL ONE AUTOPAY PYMT",
                    "500.00",
                    "DEPOSIT",
                    "co-p",
                ),
                _tx(
                    "grasshopper",
                    "2026-01-25",
                    "CAPITAL ONE ONLINE PMT",
                    "250.00",
                    "WITHDRAWAL",
                    "gh-unpaired",
                ),
            ]
        )
        Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        Planner(db, cfg, Categorizer(rules)).build_plan()

        staged = db.list_staged_transactions()
        paired = [t for t in staged if t.anchor_amount == Money.parse("500.00")]
        assert len(paired) == 2
        assert all(t.status == expected_status for t in paired)
        if strat == "clearing_account":
            assert paired[0].line_items[0].account_key == "cc_transfer_clearing"
            assert paired[0].line_items[0].balance == "INCREASE"
            assert paired[1].line_items[0].account_key == "cc_transfer_clearing"
            assert paired[1].line_items[0].balance == "DECREASE"

        unpaired = next(t for t in staged if t.anchor_amount == Money.parse("250.00"))
        assert unpaired.status == "needs_review"


def test_switching_off_invoice_mirroring_clears_unpushed_staged_invoices(
    tmp_path: Path,
) -> None:
    mirror_cfg = make_test_config(
        tmp_path, mirror_harvest_invoices=True, deposit_strategy="ar_clearing"
    )
    rules = load_rules(mirror_cfg.general.rules_path)
    with LedgerDB(mirror_cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_harvest_invoices(
            [
                HarvestInvoice(
                    "201",
                    "2001",
                    "20",
                    "Synthetic Client",
                    "2026-03-01",
                    "2026-03-31",
                    "2026-03-10",
                    "paid",
                    "USD",
                    Money.parse("1500.00"),
                    ZERO,
                    Money.parse("1500.00"),
                    ZERO,
                )
            ]
        )
        db.upsert_bank_transactions(
            [
                _tx(
                    "grasshopper",
                    "2026-03-10",
                    "SYNTHETIC CLIENT ACH",
                    "1500.00",
                    "DEPOSIT",
                    "gh-inv-201",
                )
            ]
        )
        Reconciler(db, mirror_cfg).reconcile_all(confirm_high_confidence=True)
        first_plan = Planner(db, mirror_cfg, Categorizer(rules)).build_plan()
        assert first_plan.staged_invoices == 1
        assert len(db.list_staged_invoices()) == 1
        assert (
            db.list_staged_transactions()[0].line_items[0].account_key
            == "invoice_payments_clearing"
        )

        cash_cfg = make_test_config(
            tmp_path, mirror_harvest_invoices=False, deposit_strategy="direct_income"
        )
        second_plan = Planner(db, cash_cfg, Categorizer(rules)).build_plan()
        assert second_plan.staged_invoices == 0
        assert db.list_staged_invoices() == []
        staged_txs = db.list_staged_transactions()
        assert len(staged_txs) == 1
        assert staged_txs[0].line_items[0].account_key == "revenue_default"


@pytest.mark.parametrize(
    ("desc", "expected_rule"),
    [
        ("CAPITAL ONE ONLINE PYMT", "unpaired_cc_payment"),
        ("CAPITAL ONE MOBILE PYMT", "unpaired_cc_payment"),
        ("ACME REFUND", None),
    ],
)
def test_unpaired_capital_one_credit_uses_description_pattern_only(
    tmp_path: Path, desc: str, expected_rule: str | None
) -> None:
    cfg = make_test_config(tmp_path)
    rules = load_rules(cfg.general.rules_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_bank_transactions(
            [_tx("capital_one", "2026-01-12", desc, "75.00", "DEPOSIT", "co-x")]
        )
        Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        Planner(db, cfg, Categorizer(rules)).build_plan()
        (staged,) = db.list_staged_transactions()
        if expected_rule is None:
            assert staged.category_rule != "unpaired_cc_payment"
        else:
            assert staged.category_rule == expected_rule
            assert staged.status == "needs_review"
