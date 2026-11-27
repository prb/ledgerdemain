"""Tests for Solo 401(k) [retirement] config, calculation invariants, and CLI output."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

import pytest

from ledgerdemain.cli import main
from ledgerdemain.config import (
    DEFAULT_ANNUAL_ADDITIONS_LIMIT,
    DEFAULT_COMPENSATION_CAP,
    ConfigError,
    load_config,
)
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    StagedLineItem,
    StagedTransaction,
)
from ledgerdemain.money import ZERO, Money
from ledgerdemain.planner import DEFAULT_NORMAL_BALANCE, validate_double_entry_balance
from ledgerdemain.retirement import build_solo401k_summary, floor_ratio, format_solo401k_text
from tests.fixtures import write_test_workspace_config


def _run_cli(cfg_path: Path, argv: list[str]) -> tuple[int, str, str]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    full_argv = ["--config", str(cfg_path), *argv]
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = main(full_argv)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _add_cash_run(
    db: LedgerDB,
    *,
    idx: int,
    check_date: str,
    gross_wages: Money,
    run_type: str = "off_cycle",
    regular_401k: Money = ZERO,
    catchup_401k: Money = ZERO,
) -> None:
    row: dict[str, str] = {"first name": "Paul", "last name": "Brown"}
    if regular_401k > ZERO:
        row["traditional 401(k) (employee deduction)"] = regular_401k.format()
    if catchup_401k > ZERO:
        row["401(k) catch-up (employee deduction)"] = catchup_401k.format()
    db.upsert_gusto_payroll_runs(
        [
            GustoPayrollRun(
                fingerprint=f"fp-cash-{idx}",
                check_date=check_date,
                pay_period_start=check_date,
                pay_period_end=check_date,
                run_type=run_type,
                gross_wages=gross_wages,
                reimbursements=ZERO,
                employee_taxes=ZERO,
                employer_taxes=ZERO,
                employee_benefits=regular_401k + catchup_401k,
                employer_benefits=ZERO,
                net_pay=gross_wages - regular_401k - catchup_401k,
                employee_count=1,
                raw={"rows": [row], "non_cash_imputed_benefits": False},
            )
        ]
    )


def _add_imputed_run(db: LedgerDB, *, idx: int, check_date: str, employer_benefits: Money) -> None:
    db.upsert_gusto_payroll_runs(
        [
            GustoPayrollRun(
                fingerprint=f"fp-imp-{idx}",
                check_date=check_date,
                pay_period_start=check_date,
                pay_period_end=check_date,
                run_type="off_cycle",
                gross_wages=employer_benefits,
                reimbursements=ZERO,
                employee_taxes=ZERO,
                employer_taxes=ZERO,
                employee_benefits=ZERO,
                employer_benefits=employer_benefits,
                net_pay=ZERO,
                employee_count=1,
                raw={"rows": [], "non_cash_imputed_benefits": True},
            )
        ]
    )


def _add_profit_sharing_tx(
    db: LedgerDB,
    *,
    idx: int,
    tx_date: str,
    amount: Money,
    direction: str = "WITHDRAWAL",
    status: str = "pushed",
    account_key: str = "solo_401k_profit_sharing",
    description: str = "FIDELITY SOLO 401K PROFIT SHARING",
) -> None:
    tx = BankTransaction(
        source="grasshopper",
        account_key="grasshopper_checking",
        external_id=None,
        fingerprint=f"fp-ps-bank-{idx}",
        tx_date=tx_date,
        posted_date=tx_date,
        description=description,
        normalized_description=description,
        amount=amount,
        direction=direction,
    )
    db.upsert_bank_transactions([tx])
    stored = next(t for t in db.list_bank_transactions() if t.fingerprint == tx.fingerprint)
    assert stored.id is not None

    balance = "INCREASE" if direction == "WITHDRAWAL" else "DECREASE"
    db.upsert_staged_transaction(
        StagedTransaction(
            external_id=f"ext-ps-{idx}",
            bank_tx_id=stored.id,
            tx_date=tx_date,
            description=description,
            notes="",
            anchor_account_key="grasshopper_checking",
            anchor_wave_account_id=None,
            anchor_amount=amount,
            anchor_direction=direction,
            line_items=[
                StagedLineItem(
                    account_key=account_key,
                    wave_account_id=None,
                    amount=amount,
                    balance=balance,
                )
            ],
            category_rule="Solo 401(k) Employer Profit Sharing",
            status=status,
        )
    )


def test_solo_401k_account_double_entry_balance() -> None:
    assert DEFAULT_NORMAL_BALANCE["solo_401k_profit_sharing"] == "DEBIT"

    def lookup(key: str) -> str:
        return DEFAULT_NORMAL_BALANCE[key]

    validate_double_entry_balance(
        "grasshopper_checking",
        "WITHDRAWAL",
        Money(420000),
        [StagedLineItem("solo_401k_profit_sharing", None, Money(420000), "INCREASE")],
        lookup,
    )
    validate_double_entry_balance(
        "grasshopper_checking",
        "DEPOSIT",
        Money(5000),
        [StagedLineItem("solo_401k_profit_sharing", None, Money(5000), "DECREASE")],
        lookup,
    )


def test_retirement_config_defaults_and_validation(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    cfg = load_config(cfg_path)
    assert cfg.retirement.compensation_basis == "w2_box5"
    assert cfg.retirement.annual_additions_limit == DEFAULT_ANNUAL_ADDITIONS_LIMIT
    assert cfg.retirement.compensation_cap == DEFAULT_COMPENSATION_CAP
    assert cfg.retirement.profit_sharing_account_key == "solo_401k_profit_sharing"
    assert cfg.retirement.employer_rate_numerator == 1
    assert cfg.retirement.employer_rate_denominator == 4

    base_toml = cfg_path.read_text(encoding="utf-8")
    cfg_path.write_text(
        base_toml
        + """
[retirement]
compensation_basis = "w2_box1"
annual_additions_limit = "70000.00"
compensation_cap = "350000.00"
profit_sharing_account_key = "custom_401k"
""",
        encoding="utf-8",
    )
    custom_cfg = load_config(cfg_path)
    assert custom_cfg.retirement.compensation_basis == "w2_box1"
    assert custom_cfg.retirement.annual_additions_limit == Money(7_000_000)
    assert custom_cfg.retirement.compensation_cap == Money(35_000_000)
    assert custom_cfg.retirement.profit_sharing_account_key == "custom_401k"

    for bad_section in (
        '[retirement]\ncompensation_basis = "schedule_c"\n',
        '[retirement]\nannual_additions_limit = "0.00"\n',
        '[retirement]\ncompensation_cap = "abc"\n',
        "[retirement]\nemployer_rate_numerator = true\n",
        "[retirement]\nemployer_rate_numerator = 2\nemployer_rate_denominator = 1\n",
    ):
        cfg_path.write_text(base_toml + "\n" + bad_section, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(cfg_path)


def test_solo401k_w2_box5_excludes_imputed_benefits_and_tracks_consumed(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    cfg = load_config(cfg_path)
    db = LedgerDB(cfg.general.db_path)
    db.init_schema()
    try:
        # Cash W-2 run ($16,800.00) + 2 imputed runs ($24,883.74) + contractor run (excluded)
        _add_cash_run(
            db,
            idx=1,
            check_date="2026-11-04",
            gross_wages=Money(1_680_000),
            regular_401k=Money(100_000),
            catchup_401k=Money(50_000),
        )
        _add_imputed_run(db, idx=2, check_date="2026-11-02", employer_benefits=Money(1_513_374))
        _add_imputed_run(db, idx=3, check_date="2026-11-02", employer_benefits=Money(975_000))
        _add_cash_run(
            db,
            idx=4,
            check_date="2026-11-15",
            gross_wages=Money(500_000),
            run_type="contractor",
        )
        # Prior-year run should not pollute 2026 calculation
        _add_cash_run(db, idx=5, check_date="2025-12-31", gross_wages=Money(1_000_000))

        # Consumed: $2,500 pushed + $500 staged - $200 refund pushed = $2,800.00
        # needs_review / skipped should not count toward consumed
        _add_profit_sharing_tx(
            db, idx=1, tx_date="2026-11-10", amount=Money(250_000), status="pushed"
        )
        _add_profit_sharing_tx(
            db, idx=2, tx_date="2026-11-20", amount=Money(50_000), status="staged"
        )
        _add_profit_sharing_tx(
            db,
            idx=3,
            tx_date="2026-11-21",
            amount=Money(20_000),
            direction="DEPOSIT",
            status="pushed",
        )
        _add_profit_sharing_tx(
            db, idx=4, tx_date="2026-11-22", amount=Money(99_900), status="needs_review"
        )

        summary = build_solo401k_summary(db, cfg, year=2026)
        assert summary["tax_year"] == 2026
        assert summary["compensation_basis"] == "w2_box5"
        assert summary["cash_w2_run_count"] == 1
        assert summary["imputed_benefit_run_count"] == 2
        assert summary["cash_w2_wages"] == "16800.00"
        assert summary["imputed_scorp_benefits"] == "24883.74"
        assert summary["w2_box1_wages"] == "41683.74"
        assert summary["eligible_compensation"] == "16800.00"
        assert summary["employer_rate_cap"] == "4200.00"
        assert summary["employee_deferrals_detected"] == "1000.00"
        assert summary["employee_catchup_detected"] == "500.00"
        assert summary["section_415c_headroom"] == "71000.00"
        assert summary["max_employer_profit_sharing"] == "4200.00"
        assert summary["limiting_factor"] == "employer_rate_cap"
        assert summary["consumed"] == "2800.00"
        assert summary["remaining"] == "1400.00"
        assert summary["excess"] == "0.00"
        assert summary["over_limit"] is False
        assert summary["contribution_count"] == 3

        text = format_solo401k_text(summary)
        assert "Remaining Profit-Sharing Room: $1400.00" in text
        assert "Employee Catch-Up Deferrals Detected (outside §415(c)): $500.00" in text
    finally:
        db.close()


def test_solo401k_w2_box1_floor_rounding_415c_cap_and_over_limit(tmp_path: Path) -> None:
    # Exact floor check: $10,000.03 * 1/4 = $2,500.0075 -> floors to $2,500.00
    assert floor_ratio(Money(1_000_003), 1, 4) == Money(250_000)

    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        + '\n[retirement]\ncompensation_basis = "w2_box1"\ncompensation_cap = "360000.00"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    db = LedgerDB(cfg.general.db_path)
    db.init_schema()
    try:
        # $250,000.00 cash + $30,000.03 imputed = $280,000.03 Box 1 -> floor 25% = $70,000.00
        # Deferrals = $24,500.00 -> §415(c) headroom = $72,000.00 - $24,500.00 = $47,500.00
        _add_cash_run(
            db,
            idx=1,
            check_date="2026-10-15",
            gross_wages=Money(25_000_000),
            regular_401k=Money(2_450_000),
        )
        _add_imputed_run(db, idx=2, check_date="2026-10-15", employer_benefits=Money(3_000_003))
        _add_profit_sharing_tx(
            db, idx=1, tx_date="2026-10-20", amount=Money(5_000_000), status="pushed"
        )

        summary = build_solo401k_summary(db, cfg)
        assert summary["tax_year"] == 2026
        assert summary["employer_rate_cap"] == "70000.00"
        assert summary["section_415c_headroom"] == "47500.00"
        assert summary["max_employer_profit_sharing"] == "47500.00"
        assert summary["limiting_factor"] == "section_415c_limit"
        assert summary["consumed"] == "50000.00"
        assert summary["remaining"] == "0.00"
        assert summary["excess"] == "2500.00"
        assert summary["over_limit"] is True

        text = format_solo401k_text(summary)
        assert "[OVER LIMIT] Contributions exceed max by $2500.00" in text
    finally:
        db.close()


def test_cli_solo401k_text_json_and_override(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    cfg = load_config(cfg_path)
    db = LedgerDB(cfg.general.db_path)
    db.init_schema()
    try:
        _add_cash_run(db, idx=1, check_date="2026-06-01", gross_wages=Money(10_000_000))
    finally:
        db.close()

    code, out, err = _run_cli(cfg_path, ["solo401k", "--format", "json"])
    assert code == 0, err
    payload: dict[str, Any] = json.loads(out)
    assert payload["tax_year"] == 2026
    assert payload["max_employer_profit_sharing"] == "25000.00"
    assert payload["remaining"] == "25000.00"

    code_ov, out_ov, err_ov = _run_cli(
        cfg_path,
        ["solo401k", "--employee-deferrals", "65000.00", "--format", "text"],
    )
    assert code_ov == 0, err_ov
    assert (
        "Max Allowable Employer Profit Sharing: $7000.00 [limited by: section_415c_limit]" in out_ov
    )
    assert "Remaining Profit-Sharing Room: $7000.00" in out_ov
