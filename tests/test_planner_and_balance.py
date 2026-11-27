"""Tests for double-entry validation, accounting modes, payroll clearing, and CC transfers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import CategorizationRule, load_rules
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    HarvestInvoice,
    StagedLineItem,
    WaveAccount,
)
from ledgerdemain.money import ZERO, Money
from ledgerdemain.planner import (
    DEFAULT_NORMAL_BALANCE,
    BalanceError,
    Planner,
    UnresolvedAccountError,
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


def test_harvest_deposit_is_direct_income_and_payroll_clearing_nets_to_zero(
    tmp_path: Path,
) -> None:
    cfg = make_test_config(tmp_path)
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

        assert summary.staged_transactions == 4
        staged = db.list_staged_transactions()
        dep_tx = next(t for t in staged if t.anchor_direction == "DEPOSIT")
        assert dep_tx.status == "staged"
        assert dep_tx.line_items[0].account_key == "revenue_default"
        assert "(revenue_default)" in (dep_tx.notes or "")

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


def test_cc_transfer_is_manual_and_unpaired_needs_review(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
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
        assert all(t.status == "manual_transfer_required" for t in paired)
        assert all(t.line_items == [] for t in paired)

        unpaired = next(t for t in staged if t.anchor_amount == Money.parse("250.00"))
        assert unpaired.status == "needs_review"


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


_EQUITY_AND_LOAN_ACCOUNTS = [
    WaveAccount("w-cs", "Common Shares", "EQUITY", "COMMON_SHARES", "CREDIT"),
    WaveAccount("w-sl", "Shareholder Loan", "LIABILITY", "OTHER_LIABILITY", "CREDIT"),
]


def _equity_and_loan_planner(tmp_path: Path) -> tuple[Planner, LedgerDB]:
    """Bank rows for a share deposit, a personal charge and a loan repayment, plus one default."""
    cfg = make_test_config(tmp_path)
    cfg.accounts["common_shares"] = "Common Shares"
    cfg.accounts["shareholder_loan"] = "Shareholder Loan"
    rules = [
        CategorizationRule("Shares", 10, "common_shares", re.compile("OPENING DEPOSIT")),
        CategorizationRule("Charge", 10, "shareholder_loan", re.compile("PERSONAL CHARGE")),
        CategorizationRule("Repayment", 10, "shareholder_loan", re.compile("LOAN REPAYMENT")),
        CategorizationRule("Figma", 10, "software_subscriptions", re.compile("FIGMA")),
    ]
    db = LedgerDB(cfg.general.db_path)
    db.init_schema()
    db.upsert_bank_transactions(
        [
            _tx("grasshopper", "2026-11-05", "OPENING DEPOSIT", "100.00", "DEPOSIT", "fp-open"),
            _tx("grasshopper", "2026-11-06", "PERSONAL CHARGE", "1104.40", "WITHDRAWAL", "fp-chg"),
            _tx("grasshopper", "2026-11-07", "LOAN REPAYMENT", "1000.00", "DEPOSIT", "fp-rep"),
            _tx("grasshopper", "2026-11-08", "FIGMA.COM", "15.00", "WITHDRAWAL", "fp-figma"),
        ]
    )
    return Planner(db, cfg, Categorizer(rules)), db


def test_plan_names_unresolved_accounts_and_stages_nothing_on_an_empty_cache(
    tmp_path: Path,
) -> None:
    planner, db = _equity_and_loan_planner(tmp_path)
    with db:
        with pytest.raises(UnresolvedAccountError) as exc_info:
            planner.build_plan()

        assert exc_info.value.account_keys == ["common_shares", "shareholder_loan"]
        assert "sync-meta" in str(exc_info.value)
        # The transaction on a built-in account is not staged either: the plan is all or nothing.
        assert db.list_staged_transactions() == []


def test_plan_names_only_the_account_missing_from_a_partial_cache(tmp_path: Path) -> None:
    planner, db = _equity_and_loan_planner(tmp_path)
    with db:
        db.upsert_wave_accounts(_EQUITY_AND_LOAN_ACCOUNTS[:1])

        with pytest.raises(UnresolvedAccountError) as exc_info:
            planner.build_plan()

        assert exc_info.value.account_keys == ["shareholder_loan"]
        assert db.list_staged_transactions() == []


def test_plan_labels_credit_normal_accounts_from_the_synced_cache(tmp_path: Path) -> None:
    planner, db = _equity_and_loan_planner(tmp_path)
    with db:
        db.upsert_wave_accounts(_EQUITY_AND_LOAN_ACCOUNTS)

        summary = planner.build_plan()

        assert summary.staged_transactions == 4
        by_desc = {tx.description: tx for tx in db.list_staged_transactions()}
        # A credit effect on a credit-normal account is an INCREASE, and a debit effect a DECREASE.
        assert by_desc["OPENING DEPOSIT"].line_items[0].balance == "INCREASE"
        assert by_desc["PERSONAL CHARGE"].line_items[0].balance == "DECREASE"
        assert by_desc["LOAN REPAYMENT"].line_items[0].balance == "INCREASE"
        assert by_desc["FIGMA.COM"].line_items[0].balance == "INCREASE"
        assert {tx.status for tx in by_desc.values()} == {"staged"}


def test_normal_balance_has_no_name_based_guess(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        planner = Planner(db, cfg, Categorizer([]))

        assert planner.normal_balance_for("software_subscriptions") == "DEBIT"
        assert planner.normal_balance_for("revenue_default") == "CREDIT"
        for key in ("not_an_account", "extra_payable", "revenue_other"):
            with pytest.raises(UnresolvedAccountError, match=key):
                planner.normal_balance_for(key)
