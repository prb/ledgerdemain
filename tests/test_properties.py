"""Property-based tests using Hypothesis for declarative accounting and parsing invariants."""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgerdemain.adapters.capital_one import CapitalOnePdfAdapter
from ledgerdemain.adapters.ofx import OfxTransactionParser
from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import CategorizationRule
from ledgerdemain.db import make_bank_fingerprint
from ledgerdemain.models import (
    ZERO,
    BankTransaction,
    GustoPayrollRun,
    StagedLineItem,
    fmt_decimal,
    to_decimal,
)
from ledgerdemain.planner import BalanceError, validate_double_entry_balance

_MONEY_POS = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("50000.00"), places=2)
_MONEY_NONNEG = st.decimals(min_value=Decimal("0.00"), max_value=Decimal("20000.00"), places=2)
_MONEY_ANY = st.decimals(min_value=Decimal("-999999.99"), max_value=Decimal("999999.99"), places=2)


@given(d=_MONEY_ANY)
def test_decimal_roundtrip_and_currency_formats(d: Decimal) -> None:
    assert to_decimal(fmt_decimal(d)) == d

    abs_str = f"{abs(d):,.2f}"
    assert to_decimal(f"${abs_str}") == abs(d)
    if d != ZERO:
        assert to_decimal(f"-${abs_str}") == -abs(d)
        assert to_decimal(f"(${abs_str})") == -abs(d)


@given(
    anchor_account=st.sampled_from(["grasshopper_checking", "capital_one_card"]),
    specs=st.lists(
        st.tuples(
            st.sampled_from(["DEBIT", "CREDIT"]),
            st.sampled_from(["INCREASE", "DECREASE"]),
            _MONEY_POS,
        ),
        min_size=1,
        max_size=5,
    ),
)
def test_double_entry_balance_and_imbalance_rejection(
    anchor_account: str,
    specs: list[tuple[str, str, Decimal]],
) -> None:
    account_normals: dict[str, str] = {anchor_account: "DEBIT"}
    line_items: list[StagedLineItem] = []
    net_debit = ZERO

    for idx, (normal, balance, amt) in enumerate(specs):
        acct_key = f"acct_{idx}"
        account_normals[acct_key] = normal
        line_items.append(
            StagedLineItem(
                account_key=acct_key,
                wave_account_id=None,
                amount=amt,
                balance=balance,
            )
        )
        if normal == "DEBIT":
            net_debit += amt if balance == "INCREASE" else -amt
        else:
            net_debit += -amt if balance == "INCREASE" else amt

    if net_debit == ZERO:
        # Nudge first item so total net debit is strictly non-zero
        line_items[0].amount += Decimal("0.01")
        normal0, balance0, _ = specs[0]
        if normal0 == "DEBIT":
            net_debit += Decimal("0.01") if balance0 == "INCREASE" else Decimal("-0.01")
        else:
            net_debit += Decimal("-0.01") if balance0 == "INCREASE" else Decimal("0.01")

    direction = "WITHDRAWAL" if net_debit > ZERO else "DEPOSIT"
    anchor_amount = abs(net_debit)

    def lookup(key: str) -> str:
        return account_normals[key]

    validate_double_entry_balance(
        anchor_account_key=anchor_account,
        anchor_direction=direction,
        anchor_amount=anchor_amount,
        line_items=line_items,
        normal_balance_lookup=lookup,
    )

    # Perturbing any line item by 1 cent must fail balance validation
    perturbed = [
        StagedLineItem(
            account_key=li.account_key,
            wave_account_id=li.wave_account_id,
            amount=li.amount + Decimal("0.01") if i == 0 else li.amount,
            balance=li.balance,
        )
        for i, li in enumerate(line_items)
    ]
    with pytest.raises(BalanceError):
        validate_double_entry_balance(
            anchor_account_key=anchor_account,
            anchor_direction=direction,
            anchor_amount=anchor_amount,
            line_items=perturbed,
            normal_balance_lookup=lookup,
        )

    # Putting anchor account inside line items must always fail
    with_anchor = [
        StagedLineItem(
            account_key=anchor_account if i == 0 else li.account_key,
            wave_account_id=li.wave_account_id,
            amount=li.amount,
            balance=li.balance,
        )
        for i, li in enumerate(line_items)
    ]
    with pytest.raises(BalanceError):
        validate_double_entry_balance(
            anchor_account_key=anchor_account,
            anchor_direction=direction,
            anchor_amount=anchor_amount,
            line_items=with_anchor,
            normal_balance_lookup=lookup,
        )


@given(
    net_pay=_MONEY_POS,
    reimbursements=_MONEY_NONNEG,
    ee_taxes=_MONEY_NONNEG,
    er_taxes=_MONEY_NONNEG,
    ee_benefits=_MONEY_NONNEG,
    er_benefits=_MONEY_NONNEG,
)
def test_gusto_payroll_identity_and_clearing_balances(
    net_pay: Decimal,
    reimbursements: Decimal,
    ee_taxes: Decimal,
    er_taxes: Decimal,
    ee_benefits: Decimal,
    er_benefits: Decimal,
) -> None:
    gross_wages = net_pay + ee_taxes + ee_benefits
    run = GustoPayrollRun(
        fingerprint="prop-run",
        check_date="2026-01-15",
        pay_period_start="2026-01-01",
        pay_period_end="2026-01-15",
        run_type="regular",
        gross_wages=gross_wages,
        reimbursements=reimbursements,
        employee_taxes=ee_taxes,
        employer_taxes=er_taxes,
        employee_benefits=ee_benefits,
        employer_benefits=er_benefits,
        net_pay=net_pay,
    )

    assert (
        run.gross_wages + run.reimbursements + run.employer_taxes + run.employer_benefits
        == run.net_disbursement + run.total_tax_debit + run.total_benefits_debit
    )

    normals = {
        "payroll_gross_wages": "DEBIT",
        "payroll_reimbursements": "DEBIT",
        "payroll_employer_taxes": "DEBIT",
        "payroll_employer_benefits": "DEBIT",
        "payroll_taxes_payable": "CREDIT",
        "payroll_benefits_payable": "CREDIT",
    }

    all_lines: list[StagedLineItem] = []

    net_lines = [StagedLineItem("payroll_gross_wages", None, gross_wages, "INCREASE")]
    if reimbursements > ZERO:
        net_lines.append(StagedLineItem("payroll_reimbursements", None, reimbursements, "INCREASE"))
    if ee_taxes > ZERO:
        net_lines.append(StagedLineItem("payroll_taxes_payable", None, ee_taxes, "INCREASE"))
    if ee_benefits > ZERO:
        net_lines.append(StagedLineItem("payroll_benefits_payable", None, ee_benefits, "INCREASE"))
    validate_double_entry_balance(
        "grasshopper_checking", "WITHDRAWAL", run.net_disbursement, net_lines, normals.__getitem__
    )
    all_lines.extend(net_lines)

    if run.total_tax_debit > ZERO:
        tax_lines: list[StagedLineItem] = []
        if ee_taxes > ZERO:
            tax_lines.append(StagedLineItem("payroll_taxes_payable", None, ee_taxes, "DECREASE"))
        if er_taxes > ZERO:
            tax_lines.append(StagedLineItem("payroll_employer_taxes", None, er_taxes, "INCREASE"))
        validate_double_entry_balance(
            "grasshopper_checking",
            "WITHDRAWAL",
            run.total_tax_debit,
            tax_lines,
            normals.__getitem__,
        )
        all_lines.extend(tax_lines)

    if run.total_benefits_debit > ZERO:
        ben_lines: list[StagedLineItem] = []
        if ee_benefits > ZERO:
            ben_lines.append(
                StagedLineItem("payroll_benefits_payable", None, ee_benefits, "DECREASE")
            )
        if er_benefits > ZERO:
            ben_lines.append(
                StagedLineItem("payroll_employer_benefits", None, er_benefits, "INCREASE")
            )
        validate_double_entry_balance(
            "grasshopper_checking",
            "WITHDRAWAL",
            run.total_benefits_debit,
            ben_lines,
            normals.__getitem__,
        )
        all_lines.extend(ben_lines)

    tax_payable_net = sum(
        (
            (li.amount if li.balance == "INCREASE" else -li.amount)
            for li in all_lines
            if li.account_key == "payroll_taxes_payable"
        ),
        ZERO,
    )
    ben_payable_net = sum(
        (
            (li.amount if li.balance == "INCREASE" else -li.amount)
            for li in all_lines
            if li.account_key == "payroll_benefits_payable"
        ),
        ZERO,
    )
    assert tax_payable_net == ZERO
    assert ben_payable_net == ZERO


@given(
    source=st.sampled_from(["grasshopper", "capital_one"]),
    tx_date=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31)),
    direction=st.sampled_from(["DEPOSIT", "WITHDRAWAL"]),
    amount=_MONEY_POS,
    desc=st.text(min_size=1, max_size=40),
    occ=st.integers(min_value=0, max_value=10),
)
def test_deterministic_fingerprinting(
    source: str,
    tx_date: date,
    direction: str,
    amount: Decimal,
    desc: str,
    occ: int,
) -> None:
    iso = tx_date.isoformat()
    norm = " ".join(desc.split()).upper()
    fp1 = make_bank_fingerprint(source, "acct", None, iso, direction, amount, norm, occ)
    fp2 = make_bank_fingerprint(source, "acct", None, iso, direction, amount, norm, occ)
    assert fp1 == fp2

    fp_next_occ = make_bank_fingerprint(source, "acct", None, iso, direction, amount, norm, occ + 1)
    fp_other_dir = make_bank_fingerprint(
        source,
        "acct",
        None,
        iso,
        "WITHDRAWAL" if direction == "DEPOSIT" else "DEPOSIT",
        amount,
        norm,
        occ,
    )
    fp_other_amt = make_bank_fingerprint(
        source, "acct", None, iso, direction, amount + Decimal("0.01"), norm, occ
    )
    assert fp1 != fp_next_occ
    assert fp1 != fp_other_dir
    assert fp1 != fp_other_amt


@given(
    year=st.integers(min_value=2000, max_value=2098),
    dec_day=st.integers(min_value=15, max_value=31),
    jan_day=st.integers(min_value=1, max_value=14),
)
def test_capital_one_pdf_billing_cycle_year_rollover(year: int, dec_day: int, jan_day: int) -> None:
    start_dt = date(year, 12, 15)
    end_dt = date(year + 1, 1, 14)

    resolved_dec = CapitalOnePdfAdapter._resolve_statement_date(
        f"Dec {dec_day:02d}", start_dt, end_dt
    )
    resolved_jan = CapitalOnePdfAdapter._resolve_statement_date(
        f"Jan {jan_day:02d}", start_dt, end_dt
    )
    assert resolved_dec == date(year, 12, dec_day).isoformat()
    assert resolved_jan == date(year + 1, 1, jan_day).isoformat()


@given(
    priorities=st.lists(
        st.integers(min_value=1, max_value=1000), min_size=2, max_size=6, unique=True
    ),
    data=st.data(),
)
def test_categorizer_priority_monotonicity(priorities: list[int], data: st.DataObject) -> None:
    rules = [
        CategorizationRule(
            name=f"Rule-{p}",
            priority=p,
            account_key=f"acct_{p}",
            description_pattern=re.compile(r"(?i)vendor"),
        )
        for p in priorities
    ]
    permuted = data.draw(st.permutations(rules))
    tx = BankTransaction(
        source="capital_one",
        account_key="capital_one_card",
        external_id=None,
        fingerprint="fp",
        tx_date="2026-01-05",
        posted_date="2026-01-05",
        description="VENDOR CHARGE",
        normalized_description="VENDOR CHARGE",
        amount=Decimal("10.00"),
        direction="WITHDRAWAL",
    )
    result = Categorizer(list(permuted)).categorize(tx)
    assert result is not None
    min_p = min(priorities)
    assert result.rule_name == f"Rule-{min_p}"
    assert result.account_key == f"acct_{min_p}"


@given(
    dt_user=st.dates(min_value=date(2020, 1, 1), max_value=date(2029, 12, 20)),
    lag_days=st.integers(min_value=0, max_value=7),
    amount=_MONEY_POS,
    is_credit=st.booleans(),
    card_num=st.integers(min_value=1000, max_value=9999),
    fit_num=st.integers(min_value=1, max_value=999999),
)
def test_ofx_xml_roundtrip_preserves_dates_direction_fitid_and_card(
    dt_user: date,
    lag_days: int,
    amount: Decimal,
    is_credit: bool,
    card_num: int,
    fit_num: int,
) -> None:
    dt_posted = dt_user + timedelta(days=lag_days)
    signed = amount if is_credit else -amount
    trn_type = "CREDIT" if is_credit else "DEBIT"
    fit_id = f"FIT-{fit_num}"
    card_str = str(card_num)

    ofx_xml = (
        '<?xml version="1.0" encoding="utf-8" ?>\n'
        '<?OFX OFXHEADER="200" VERSION="202"?>\n'
        "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>\n"
        "<CURDEF>USD</CURDEF>\n"
        f"<CCACCTFROM><ACCTID>{card_str}</ACCTID></CCACCTFROM>\n"
        "<BANKTRANLIST>\n"
        "<STMTTRN>\n"
        f"  <TRNTYPE>{trn_type}</TRNTYPE>\n"
        f"  <DTPOSTED>{dt_posted.strftime('%Y%m%d')}000000.000</DTPOSTED>\n"
        f"  <DTUSER>{dt_user.strftime('%Y%m%d')}000000.000</DTUSER>\n"
        f"  <TRNAMT>{fmt_decimal(signed)}</TRNAMT>\n"
        f"  <FITID>{fit_id}</FITID>\n"
        "  <NAME>SYNTHETIC MERCHANT</NAME>\n"
        f"  <CCACCTTO><ACCTID>{card_str}</ACCTID></CCACCTTO>\n"
        "</STMTTRN>\n"
        "</BANKTRANLIST>\n"
        "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"
    )
    stmt = OfxTransactionParser().parse_statement_text(
        ofx_xml, source="capital_one", account_key="capital_one_card"
    )
    assert len(stmt.transactions) == 1
    tx = stmt.transactions[0]
    assert tx.tx_date == dt_user.isoformat()
    assert tx.posted_date == dt_posted.isoformat()
    assert tx.direction == ("DEPOSIT" if is_credit else "WITHDRAWAL")
    assert tx.amount == amount
    assert tx.card_last4 == card_str
    assert tx.external_id == fit_id


@given(
    amount=_MONEY_POS,
    clearing_normal=st.sampled_from(["DEBIT", "CREDIT"]),
)
def test_bank_transfer_clearing_nets_to_zero(amount: Decimal, clearing_normal: str) -> None:
    normals = {
        "grasshopper_savings": "DEBIT",
        "grasshopper_money_market": "DEBIT",
        "bank_transfer_clearing": clearing_normal,
    }
    out_balance = "INCREASE" if clearing_normal == "DEBIT" else "DECREASE"
    in_balance = "DECREASE" if clearing_normal == "DEBIT" else "INCREASE"

    out_line = StagedLineItem("bank_transfer_clearing", None, amount, out_balance)
    in_line = StagedLineItem("bank_transfer_clearing", None, amount, in_balance)

    validate_double_entry_balance(
        "grasshopper_savings", "WITHDRAWAL", amount, [out_line], normals.__getitem__
    )
    validate_double_entry_balance(
        "grasshopper_money_market", "DEPOSIT", amount, [in_line], normals.__getitem__
    )

    signed_sum = (amount if out_balance == "INCREASE" else -amount) + (
        amount if in_balance == "INCREASE" else -amount
    )
    assert signed_sum == ZERO
