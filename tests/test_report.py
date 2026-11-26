"""Tests for reporting, officer health/HSA double-entry, and S-Corp benefits tie-out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ledgerdemain.config import AppConfig, load_config
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    StagedLineItem,
    StagedTransaction,
)
from ledgerdemain.money import ZERO, Money, format_money
from ledgerdemain.planner import DEFAULT_NORMAL_BALANCE, validate_double_entry_balance
from ledgerdemain.report import build_report, format_report_text
from tests.fixtures import write_test_workspace_config


def _setup_workspace(tmp_path: Path) -> tuple[AppConfig, LedgerDB]:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    cfg = load_config(cfg_path)
    db = LedgerDB(cfg.general.db_path)
    db.init_schema()
    return cfg, db


def _add_staged_bank_tx(
    db: LedgerDB,
    *,
    idx: int,
    tx_date: str,
    account_key: str,
    direction: str,
    amount: Money,
    description: str,
    target_account: str,
    category_rule: str,
    status: str = "pushed",
) -> None:
    tx = BankTransaction(
        source="grasshopper",
        account_key="grasshopper_checking",
        external_id=None,
        fingerprint=f"fp-bank-{idx}",
        tx_date=tx_date,
        posted_date=tx_date,
        description=description,
        normalized_description=description,
        amount=amount,
        direction=direction,
    )
    db.upsert_bank_transactions([tx])
    stored_tx = next(t for t in db.list_bank_transactions() if t.fingerprint == tx.fingerprint)
    assert stored_tx.id is not None

    balance = "INCREASE" if direction == "WITHDRAWAL" else "DECREASE"
    stx = StagedTransaction(
        external_id=f"ext-{idx}",
        bank_tx_id=stored_tx.id,
        tx_date=tx_date,
        description=description,
        notes="",
        anchor_account_key="grasshopper_checking",
        anchor_wave_account_id=None,
        anchor_amount=amount,
        anchor_direction=direction,
        line_items=[
            StagedLineItem(
                account_key=target_account,
                wave_account_id=None,
                amount=amount,
                balance=balance,
            )
        ],
        category_rule=category_rule,
        status=status,
    )
    db.upsert_staged_transaction(stx)


def _add_imputed_gusto_run(
    db: LedgerDB,
    *,
    idx: int,
    check_date: str,
    cobra_amount: Money,
    hsa_amount: Money = ZERO,
) -> None:
    total = cobra_amount + hsa_amount
    row: dict[str, str] = {
        "first name": "Paul",
        "last name": "Brown",
        "gross earnings": format_money(total),
        "salesforce cobra | s-corporation owner's medical insurance (company contribution)": (
            format_money(cobra_amount) if cobra_amount > ZERO else ""
        ),
        "fidelity hsa | s-corporation owner's health savings account (company contribution)": (
            format_money(hsa_amount) if hsa_amount > ZERO else ""
        ),
    }
    db.upsert_gusto_payroll_runs(
        [
            GustoPayrollRun(
                fingerprint=f"fp-gusto-{idx}",
                check_date=check_date,
                pay_period_start=check_date,
                pay_period_end=check_date,
                run_type="off_cycle",
                gross_wages=total,
                reimbursements=ZERO,
                employee_taxes=ZERO,
                employer_taxes=ZERO,
                employee_benefits=ZERO,
                employer_benefits=total,
                net_pay=ZERO,
                employee_count=1,
                raw={"rows": [row], "non_cash_imputed_benefits": True},
            )
        ]
    )


def test_officer_benefits_accounts_double_entry_balance() -> None:
    assert DEFAULT_NORMAL_BALANCE["officer_health_insurance"] == "DEBIT"
    assert DEFAULT_NORMAL_BALANCE["officer_hsa"] == "DEBIT"

    def lookup(key: str) -> str:
        return DEFAULT_NORMAL_BALANCE[key]

    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money(252229),
        [StagedLineItem("officer_health_insurance", None, Money(252229), "INCREASE")],
        lookup,
    )
    validate_double_entry_balance(
        "grasshopper_money_market",
        "WITHDRAWAL",
        Money(974959),
        [StagedLineItem("officer_hsa", None, Money(974959), "INCREASE")],
        lookup,
    )
    validate_double_entry_balance(
        "grasshopper_money_market",
        "DEPOSIT",
        Money(975000),
        [StagedLineItem("officer_hsa", None, Money(975000), "DECREASE")],
        lookup,
    )


def test_scorp_benefits_tieout_balanced_when_split(tmp_path: Path) -> None:
    cfg, db = _setup_workspace(tmp_path)
    try:
        cobra = Money(252229)
        hsa = Money(975000)
        _add_imputed_gusto_run(db, idx=1, check_date="2026-11-02", cobra_amount=cobra)
        _add_imputed_gusto_run(
            db, idx=2, check_date="2026-11-02", cobra_amount=cobra, hsa_amount=hsa
        )

        _add_staged_bank_tx(
            db,
            idx=1,
            tx_date="2026-06-11",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=cobra + cobra,
            description="INSPIRAFINANCIAL T6892560-E",
            target_account="officer_health_insurance",
            category_rule="Officer / Shareholder Health Insurance (COBRA)",
        )
        _add_staged_bank_tx(
            db,
            idx=2,
            tx_date="2026-09-28",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=hsa,
            description="PAULB 2026 HSA CON TRIBUTION",
            target_account="officer_hsa",
            category_rule="Officer / Shareholder HSA Contributions",
        )
        _add_staged_bank_tx(
            db,
            idx=3,
            tx_date="2026-09-30",
            account_key="grasshopper_checking",
            direction="DEPOSIT",
            amount=hsa,
            description="ACH ORIG CR RETURN ACH RTN R23",
            target_account="officer_hsa",
            category_rule="Returned HSA Contribution Offset (R23)",
        )
        _add_staged_bank_tx(
            db,
            idx=4,
            tx_date="2026-09-30",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=hsa,
            description="PAULB2026 HSA CONT RIBUTION (PAYROLL)",
            target_account="officer_hsa",
            category_rule="Officer / Shareholder HSA Contributions",
        )

        rep: dict[str, Any] = build_report(db, cfg)
        tie = rep["scorp_benefits_tieout"]
        assert tie == {
            "imputed_run_count": 2,
            "gusto_health_insurance": "5044.58",
            "gusto_hsa": "9750.00",
            "gusto_other": "0.00",
            "gusto_total": "14794.58",
            "bank_health_insurance": "5044.58",
            "bank_hsa": "9750.00",
            "bank_unsplit_employer_benefits": "0.00",
            "bank_total": "14794.58",
            "health_insurance_variance": "0.00",
            "hsa_variance": "0.00",
            "total_variance": "0.00",
            "in_balance": True,
        }
        text = format_report_text(rep)
        assert "S-Corp Imputed Benefits Tie-Out (2 run(s)) [BALANCED]:" in text
    finally:
        db.close()


def test_scorp_benefits_tieout_flags_unjournaled_month_and_hsa_gap(tmp_path: Path) -> None:
    cfg, db = _setup_workspace(tmp_path)
    try:
        for i in range(1, 5):
            _add_imputed_gusto_run(db, idx=i, check_date="2026-11-02", cobra_amount=Money(252229))
        _add_imputed_gusto_run(
            db, idx=5, check_date="2026-11-02", cobra_amount=Money(252229), hsa_amount=Money(975000)
        )

        _add_staged_bank_tx(
            db,
            idx=1,
            tx_date="2026-10-01",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=Money(1513374),
            description="INSPIRAFINANCIAL 6 MONTHS",
            target_account="officer_health_insurance",
            category_rule="Officer / Shareholder Health Insurance (COBRA)",
        )
        _add_staged_bank_tx(
            db,
            idx=2,
            tx_date="2026-09-30",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=Money(974959),
            description="PAULB2026 HSA CONT RIBUTION (PAYROLL)",
            target_account="officer_hsa",
            category_rule="Officer / Shareholder HSA Contributions",
        )

        rep = build_report(db, cfg)
        tie = rep["scorp_benefits_tieout"]
        assert tie["imputed_run_count"] == 5
        assert tie["gusto_health_insurance"] == "12611.45"
        assert tie["gusto_hsa"] == "9750.00"
        assert tie["gusto_total"] == "22361.45"
        assert tie["bank_health_insurance"] == "15133.74"
        assert tie["bank_hsa"] == "9749.59"
        assert tie["bank_total"] == "24883.33"
        assert tie["health_insurance_variance"] == "2522.29"
        assert tie["hsa_variance"] == "-0.41"
        assert tie["total_variance"] == "2521.88"
        assert tie["in_balance"] is False

        text = format_report_text(rep)
        assert "S-Corp Imputed Benefits Tie-Out (5 run(s)) [VARIANCE DETECTED]:" in text
        assert "Variance=$2522.29" in text
        assert "Variance=$-0.41" in text
        assert "Variance=$2521.88" in text
        assert "cross-month timing reconciles" not in text

        oct_text = format_report_text(build_report(db, cfg, period="2026-10"))
        assert "cross-month timing reconciles" in oct_text
    finally:
        db.close()


def test_scorp_benefits_tieout_excludes_gusto_cash_payroll_and_tracks_unsplit(
    tmp_path: Path,
) -> None:
    cfg, db = _setup_workspace(tmp_path)
    try:
        # Cash payroll debit matched to Gusto should NOT pollute S-Corp unsplit bucket
        _add_staged_bank_tx(
            db,
            idx=1,
            tx_date="2026-11-04",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=Money(50000),
            description="GUSTO PAYROLL BENEFITS DEBIT",
            target_account="payroll_employer_benefits",
            category_rule="gusto_benefits",
        )
        # Direct rule-categorized legacy S-Corp entry on payroll_employer_benefits IS tracked
        _add_staged_bank_tx(
            db,
            idx=2,
            tx_date="2026-09-09",
            account_key="grasshopper_checking",
            direction="WITHDRAWAL",
            amount=Money(252229),
            description="INSPIRAFINANCIAL LEGACY UNSPLIT",
            target_account="payroll_employer_benefits",
            category_rule="S-Corp Employer Benefits (COBRA & HSA)",
        )
        _add_imputed_gusto_run(db, idx=1, check_date="2026-11-02", cobra_amount=Money(252229))

        rep = build_report(db, cfg)
        tie = rep["scorp_benefits_tieout"]
        assert tie["bank_health_insurance"] == "0.00"
        assert tie["bank_hsa"] == "0.00"
        assert tie["bank_unsplit_employer_benefits"] == "2522.29"
        assert tie["bank_total"] == "2522.29"
        assert tie["total_variance"] == "0.00"
        assert tie["in_balance"] is True

        text = format_report_text(rep)
        assert "Unsplit Employer Benefits (Bank): $2522.29" in text
    finally:
        db.close()
