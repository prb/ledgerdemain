"""Tests for Harvest, Grasshopper, Capital One, Gusto, and OFX source adapters."""

from __future__ import annotations

import contextlib
import io
import json
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ledgerdemain.adapters.base import normalize_description
from ledgerdemain.adapters.capital_one import CapitalOneCsvAdapter, CapitalOnePdfAdapter
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter, GrasshopperCsvAdapter
from ledgerdemain.adapters.gusto import GustoParseError, GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter, HarvestCsvAdapter
from ledgerdemain.adapters.ofx import OfxTransactionParser
from ledgerdemain.categorize import Categorizer
from ledgerdemain.cli import main
from ledgerdemain.config import GrasshopperConfig, HarvestConfig
from ledgerdemain.db import LedgerDB, make_bank_fingerprint
from ledgerdemain.http_client import HttpClient, HttpResponse
from ledgerdemain.models import BankTransaction, HarvestInvoiceLineItem
from ledgerdemain.money import ZERO, Money
from ledgerdemain.planner import Planner
from ledgerdemain.reconcile import Reconciler
from tests.fixtures import (
    SAMPLE_CAPITAL_ONE_CSV,
    SAMPLE_CAPITAL_ONE_OFX_XML,
    SAMPLE_CAPITAL_ONE_PDF_LINES,
    SAMPLE_GRASSHOPPER_CSV,
    SAMPLE_GUSTO_CONTRACTOR_CSV,
    SAMPLE_GUSTO_PAYROLL_CSV,
    SAMPLE_HARVEST_CSV,
    SAMPLE_OFX_SGML,
    create_synthetic_pdf,
    make_test_config,
    write_test_workspace_config,
)

HandlerFn = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


class _RoutingMockTransport:
    def __init__(self, handler: HandlerFn) -> None:
        self.handler = handler
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        del timeout
        hdrs = dict(headers or {})
        self.requests.append((method, url, hdrs))
        return self.handler(method, url, hdrs, body)


def test_harvest_api_pagination_and_updated_since() -> None:
    def handler(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        del method, body
        assert headers["Harvest-Account-Id"] == "harv-1"
        assert "Ledgerdemain" in headers["User-Agent"]
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        page = qs.get("page", ["1"])[0]
        if parsed.path.endswith("/clients"):
            clients_payload: dict[str, object] = {
                "clients": [{"id": 10, "name": "Acme Corp", "currency": "USD", "is_active": True}],
                "next_page": None,
            }
            return HttpResponse(200, {}, json.dumps(clients_payload).encode("utf-8"))
        if parsed.path.endswith("/invoices"):
            if page == "1":
                inv_page1: dict[str, object] = {
                    "invoices": [
                        {
                            "id": 101,
                            "number": "1001",
                            "client": {"id": 10, "name": "Acme Corp"},
                            "issue_date": "2026-01-01",
                            "due_date": "2026-01-31",
                            "paid_at": "2026-01-05T12:00:00Z",
                            "state": "paid",
                            "currency": "USD",
                            "amount": "3000.00",
                            "tax_amount": "0.00",
                            "due_amount": "0.00",
                            "subject": "Jan Retainer",
                            "line_items": [
                                {
                                    "kind": "Service",
                                    "description": "Engineering",
                                    "quantity": 20,
                                    "unit_price": 150.0,
                                    "amount": 3000.0,
                                }
                            ],
                        }
                    ],
                    "next_page": 2,
                }
                return HttpResponse(200, {}, json.dumps(inv_page1).encode("utf-8"))
            inv_page2: dict[str, object] = {
                "invoices": [
                    {
                        "id": 102,
                        "number": "1002",
                        "client": {"id": 10, "name": "Acme Corp"},
                        "issue_date": "2026-01-15",
                        "due_date": "2026-02-14",
                        "state": "open",
                        "currency": "USD",
                        "amount": "1500.00",
                        "due_amount": "1500.00",
                        "line_items": [],
                    }
                ],
                "next_page": None,
            }
            return HttpResponse(200, {}, json.dumps(inv_page2).encode("utf-8"))
        if parsed.path.endswith("/invoices/101/payments"):
            pay_payload: dict[str, object] = {
                "invoice_payments": [
                    {
                        "id": 501,
                        "amount": "3000.00",
                        "paid_date": "2026-01-05",
                        "payment_gateway": {"name": "ACH"},
                        "notes": "Wire ref 99",
                    }
                ],
                "next_page": None,
            }
            return HttpResponse(200, {}, json.dumps(pay_payload).encode("utf-8"))
        raise AssertionError(f"Unexpected URL: {url}")

    transport = _RoutingMockTransport(handler)
    adapter = HarvestApiAdapter(
        HarvestConfig(account_id="harv-1", access_token="tok-1"),
        http_client=HttpClient(transport=transport, sleep_fn=lambda _: None),
    )
    clients = adapter.fetch_clients()
    invoices = adapter.fetch_invoices(updated_since="2026-01-01")
    payments = adapter.fetch_payments_for_invoice("101")

    assert len(clients) == 1
    assert len(invoices) == 2
    assert invoices[0].paid_date == "2026-01-05"
    assert invoices[0].subtotal == Money.parse("3000.00")
    assert payments[0].payment_gateway == "ACH"
    assert any("updated_since=2026-01-01T00%3A00%3A00Z" in u for _, u, _ in transport.requests)


def test_harvest_csv_adapter(tmp_path: Path) -> None:
    p = tmp_path / "harvest.csv"
    p.write_text(SAMPLE_HARVEST_CSV, encoding="utf-8")
    invs = HarvestCsvAdapter().parse_file(p)

    assert len(invs) == 3
    assert invs[0].invoice_number == "1001"
    assert invs[0].total_amount == Money.parse("3000.00")
    assert invs[2].due_amount == Money.parse("1500.00")


def test_grasshopper_api_and_csv_and_ofx(tmp_path: Path) -> None:
    def handler(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        del method, body
        assert headers["X-API-Secret"] == "sec-123"
        if "cursor=c2" in url:
            page2: dict[str, object] = {
                "transactions": [
                    {
                        "id": "gh-2",
                        "posted_date": "2026-01-06",
                        "description": "GUSTO PAY 260106",
                        "amount_cents": 200050,
                        "direction": "DEBIT",
                    }
                ]
            }
            return HttpResponse(200, {}, json.dumps(page2).encode("utf-8"))
        page1: dict[str, object] = {
            "transactions": [
                {
                    "id": "gh-1",
                    "date": "2026-01-05",
                    "description": "ACME CORP ACH",
                    "amount": "3000.00",
                }
            ],
            "next_cursor": "c2",
        }
        return HttpResponse(200, {}, json.dumps(page1).encode("utf-8"))

    transport = _RoutingMockTransport(handler)
    api = GrasshopperApiAdapter(
        GrasshopperConfig(
            account_id="acct-99",
            access_token="gh-tok",
            header_secret="sec-123",
        ),
        http_client=HttpClient(transport=transport, sleep_fn=lambda _: None),
    )
    txs = api.fetch_transactions(from_date="2026-01-01")
    assert len(txs) == 2
    assert txs[0].direction == "DEPOSIT"
    assert txs[1].direction == "WITHDRAWAL"
    assert txs[1].amount == Money.parse("2000.50")

    csv_path = tmp_path / "gh.csv"
    csv_path.write_text(SAMPLE_GRASSHOPPER_CSV, encoding="utf-8")
    csv_txs = GrasshopperCsvAdapter().parse_file(csv_path)
    assert len(csv_txs) == 6
    assert csv_txs[0].direction == "DEPOSIT"
    assert csv_txs[1].direction == "WITHDRAWAL"

    ofx_path = tmp_path / "gh.ofx"
    ofx_path.write_text(SAMPLE_OFX_SGML, encoding="utf-8")
    ofx_txs = OfxTransactionParser().parse_file(ofx_path, "grasshopper", "grasshopper_checking")
    assert len(ofx_txs) == 2
    assert ofx_txs[0].external_id == "OFX-GH-1"
    assert ofx_txs[0].direction == "WITHDRAWAL"
    assert ofx_txs[1].direction == "DEPOSIT"


def test_capital_one_csv_duplicates_and_idempotency(tmp_path: Path) -> None:
    csv_path = tmp_path / "co.csv"
    csv_path.write_text(SAMPLE_CAPITAL_ONE_CSV, encoding="utf-8")
    txs = CapitalOneCsvAdapter().parse_file(csv_path)
    assert len(txs) == 6
    # Same-day identical coffees must get distinct fingerprints
    assert txs[1].fingerprint != txs[2].fingerprint
    assert txs[3].direction == "DEPOSIT"  # Figma refund Credit
    assert txs[5].direction == "DEPOSIT"  # Autopay

    with LedgerDB(tmp_path / "test.db") as db:
        db.init_schema()
        _, ins1 = db.upsert_bank_transactions(txs)
        _, ins2 = db.upsert_bank_transactions(CapitalOneCsvAdapter().parse_file(csv_path))
        assert ins1 == 6
        assert ins2 == 0


def test_capital_one_pdf_year_rollover_and_sections(tmp_path: Path) -> None:
    pdf_path = tmp_path / "capone_jan2026.pdf"
    create_synthetic_pdf(SAMPLE_CAPITAL_ONE_PDF_LINES, pdf_path)
    txs = CapitalOnePdfAdapter().parse_file(pdf_path)

    assert len(txs) == 4
    assert txs[0].posted_date == "2025-12-21"
    assert txs[0].direction == "DEPOSIT"
    assert txs[0].amount == Money.parse("250.00")
    assert txs[0].card_last4 == "4321"

    assert txs[1].posted_date == "2026-01-02"
    assert txs[1].direction == "DEPOSIT"
    assert txs[2].posted_date == "2025-12-29"
    assert txs[2].direction == "WITHDRAWAL"
    assert txs[3].posted_date == "2026-01-05"
    assert txs[3].direction == "WITHDRAWAL"


def test_gusto_payroll_and_contractor_csv(tmp_path: Path) -> None:
    pay_path = tmp_path / "gusto_payroll.csv"
    pay_path.write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")
    runs = GustoPayrollCsvAdapter().parse_file(pay_path)
    assert len(runs) == 1
    run = runs[0]
    # Must NOT double-count Totals row
    assert run.employee_count == 2
    assert run.gross_wages == Money.parse("4500.00")
    assert run.reimbursements == Money.parse("150.00")
    assert run.employee_taxes == Money.parse("944.25")
    assert run.employer_taxes == Money.parse("371.25")
    assert run.net_pay == Money.parse("3555.75")
    assert run.net_disbursement == Money.parse("3705.75")
    assert run.total_tax_debit == Money.parse("1315.50")
    assert run.tax_breakdown["futa"] == "27.00"

    # Accounting identity check
    assert (
        run.gross_wages + run.reimbursements + run.employer_taxes + run.employer_benefits
        == run.net_disbursement + run.total_tax_debit + run.total_benefits_debit
    )

    contr_path = tmp_path / "gusto_contractors.csv"
    contr_path.write_text(SAMPLE_GUSTO_CONTRACTOR_CSV, encoding="utf-8")
    c_runs = GustoPayrollCsvAdapter().parse_file(contr_path, run_type="contractor")
    assert len(c_runs) == 1
    assert c_runs[0].net_disbursement == Money.parse("1200.00")
    assert c_runs[0].run_type == "contractor"


def test_capital_one_ofx_xml_statement_and_dtuser_and_card_last4(tmp_path: Path) -> None:
    ofx_path = tmp_path / "capone.ofx"
    ofx_path.write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")

    stmt = OfxTransactionParser().parse_statement_file(
        ofx_path, source="capital_one", account_key="capital_one_card"
    )
    assert stmt.account_id == "9999"
    assert stmt.currency == "USD"
    assert stmt.start_date == "2026-01-01"
    assert stmt.end_date == "2026-01-31"
    assert stmt.ledger_balance == Money.parse("-1234.56")
    assert stmt.ledger_balance_as_of == "2026-02-01"
    assert stmt.available_balance == Money.parse("8765.44")
    assert len(stmt.transactions) == 2

    charge, payment = stmt.transactions
    assert charge.tx_date == "2026-01-08"
    assert charge.posted_date == "2026-01-10"
    assert charge.direction == "WITHDRAWAL"
    assert charge.amount == Money.parse("42.50")
    assert charge.external_id == "FIT-C1-001"
    assert charge.card_last4 == "8888"
    assert "CCACCTTO" not in charge.raw

    # Payment has no <CCACCTTO>, so it falls back to statement-level <CCACCTFROM>
    assert payment.tx_date == "2026-01-15"
    assert payment.posted_date == "2026-01-15"
    assert payment.direction == "DEPOSIT"
    assert payment.amount == Money.parse("500.00")
    assert payment.card_last4 == "9999"

    # Re-ingesting via CapitalOneCsvAdapter (which delegates .ofx) is idempotent on FITID
    with LedgerDB(tmp_path / "ofx.db") as db:
        db.init_schema()
        _, ins1 = db.upsert_bank_transactions(CapitalOneCsvAdapter().parse_file(ofx_path))
        _, ins2 = db.upsert_bank_transactions(CapitalOneCsvAdapter().parse_file(ofx_path))
        assert ins1 == 2
        assert ins2 == 0


def test_ofx_unescaped_ampersand_fallback() -> None:
    raw_ofx = """<?xml version="1.0" encoding="utf-8" ?>
<?OFX OFXHEADER="200" VERSION="202"?>
<OFX>
<CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
<CURDEF>USD</CURDEF>
<CCACCTFROM><ACCTID>9999</ACCTID></CCACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101000000.000</DTSTART>
<DTEND>20260131000000.000</DTEND>
<STMTTRN>
  <TRNTYPE>DEBIT</TRNTYPE>
  <DTPOSTED>20260112000000.000</DTPOSTED>
  <DTUSER>20260111000000.000</DTUSER>
  <TRNAMT>-85.00</TRNAMT>
  <FITID>FIT-AMP-1</FITID>
  <NAME>AT&T WIRELESS</NAME>
  <CCACCTTO><ACCTID>9999</ACCTID></CCACCTTO>
  <MEMO>AT&amp;T WIRELESS</MEMO>
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>-85.00</BALAMT><DTASOF>20260131000000.000</DTASOF></LEDGERBAL>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>"""
    stmt = OfxTransactionParser().parse_statement_text(
        raw_ofx, source="capital_one", account_key="capital_one_card"
    )
    assert stmt.account_id == "9999"
    assert stmt.ledger_balance == Money.parse("-85.00")
    assert len(stmt.transactions) == 1
    tx = stmt.transactions[0]
    assert tx.tx_date == "2026-01-11"
    assert tx.posted_date == "2026-01-12"
    assert tx.card_last4 == "9999"
    assert tx.description == "AT&T WIRELESS"
    assert tx.external_id == "FIT-AMP-1"


def test_gusto_payroll_journal_multi_section_and_scorp_imputed_benefits(tmp_path: Path) -> None:
    cols = [
        "Last Name",
        "First Name",
        "Gross Earnings",
        "Synthetic HSA (Employee Deduction)",
        "Synthetic HSA (Company Contribution)",
        "Synthetic Medical (Employee Deduction)",
        "Synthetic Medical (Company Contribution)",
        "Employee Taxes",
        "Federal Income Tax (Employee)",
        "Social Security (Employee)",
        "Medicare (Employee)",
        "Additional Medicare (Employee)",
        "Employer Taxes",
        "Social Security (Employer)",
        "Medicare (Employer)",
        "FUTA (Employer)",
        "One Time Reimbursement",
        "Net Pay",
        "Reimbursements",
        "Donations",
        "Check Amount",
        "Employer Cost",
    ]
    header = ",".join(f'"{c}"' for c in cols)
    summary_row = (
        '"Doe","Jane","13200.00","0.00","1000.00","0.00","2200.00","765.00",'
        '"0.00","620.00","145.00","0.00","790.00","620.00","145.00","25.00",'
        '"0.0","9235.00","0.00","0.00","9235.00","13990.00"'
    )
    ytd_totals = summary_row.replace('"Doe","Jane"', '"Totals",""')
    ben_row = (
        '"Doe","Jane","3200.00","","1000.00","0.00","2200.00","0.00",'
        '"0.00","","","","0.00","","","","0.0","0.00","0.00","0.00","0.00","3200.00"'
    )
    ben_totals = ben_row.replace('"Doe","Jane"', '"Payroll Totals",""')
    cash_row = (
        '"Doe","Jane","10000.00","","","","","765.00",'
        '"0.00","620.00","145.00","0.00","790.00","620.00","145.00","25.00",'
        '"0.0","9235.00","0.00","0.00","9235.00","10790.00"'
    )
    cash_totals = cash_row.replace('"Doe","Jane"', '"Payroll Totals",""')
    journal_lines = [
        '"Payroll journal"',
        "",
        '"Synthetic Labs LLC"',
        '"100 Example Way"',
        '"Portland","OR","97201"',
        "",
        '"Payroll Journal Summary by Employee"',
        '"Per Employee Summary for pay dates that fall between 01/01/2026 and 12/31/2026"',
        header,
        summary_row,
        ytd_totals,
        "",
        '"Employee Earnings"',
        '"Off-cycle payroll for Jane Doe (August 2026)"',
        '"Pay period"," 08/01/2026 - 08/31/2026"',
        '"Pay day"," 11/02/2026"',
        header,
        ben_row,
        ben_totals,
        "",
        '""',
        '"Off-cycle payroll for Jane Doe (Wages to 10/31)"',
        '"Pay period"," 08/01/2026 - 10/31/2026"',
        '"Pay day"," 11/04/2026"',
        header,
        cash_row,
        cash_totals,
    ]
    journal_csv = "\n".join(journal_lines) + "\n"
    csv_path = tmp_path / "gusto_journal.csv"
    csv_path.write_text(journal_csv, encoding="utf-8")

    runs = GustoPayrollCsvAdapter().parse_file(csv_path)
    assert len(runs) == 2

    imputed_run, cash_run = runs
    assert imputed_run.run_type == "off_cycle"
    assert imputed_run.check_date == "2026-11-02"
    assert imputed_run.pay_period_start == "2026-08-01"
    assert imputed_run.pay_period_end == "2026-08-31"
    assert imputed_run.gross_wages == Money.parse("3200.00")
    assert imputed_run.employer_benefits == Money.parse("3200.00")
    assert imputed_run.employee_benefits == ZERO
    assert imputed_run.net_pay == ZERO
    assert imputed_run.raw["non_cash_imputed_benefits"] is True

    assert cash_run.run_type == "off_cycle"
    assert cash_run.check_date == "2026-11-04"
    assert cash_run.pay_period_start == "2026-08-01"
    assert cash_run.pay_period_end == "2026-10-31"
    assert cash_run.gross_wages == Money.parse("10000.00")
    assert cash_run.employee_taxes == Money.parse("765.00")
    assert cash_run.employer_taxes == Money.parse("790.00")
    assert cash_run.net_pay == Money.parse("9235.00")
    assert cash_run.total_tax_debit == Money.parse("1555.00")
    assert cash_run.tax_breakdown == {
        "ee_medicare": "145.00",
        "ee_social_security": "620.00",
        "er_medicare": "145.00",
        "er_social_security": "620.00",
        "futa": "25.00",
    }
    assert cash_run.raw["non_cash_imputed_benefits"] is False

    # Reconciler skips the imputed-benefit run and matches GUSTO NET + TAX to the cash run
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        db.upsert_gusto_payroll_runs(runs)
        txs = [
            BankTransaction(
                source="grasshopper",
                account_key="grasshopper_checking",
                external_id=None,
                fingerprint=make_bank_fingerprint(
                    source="grasshopper",
                    account_key="grasshopper_checking",
                    external_id=None,
                    posted_date="2026-11-03",
                    direction="WITHDRAWAL",
                    amount=Money.parse("9235.00"),
                    normalized_description=normalize_description("GUSTO NET 999999 SYNTH"),
                ),
                tx_date="2026-11-03",
                posted_date="2026-11-03",
                description="GUSTO NET 999999 SYNTH",
                normalized_description=normalize_description("GUSTO NET 999999 SYNTH"),
                amount=Money.parse("9235.00"),
                direction="WITHDRAWAL",
            ),
            BankTransaction(
                source="grasshopper",
                account_key="grasshopper_checking",
                external_id=None,
                fingerprint=make_bank_fingerprint(
                    source="grasshopper",
                    account_key="grasshopper_checking",
                    external_id=None,
                    posted_date="2026-11-03",
                    direction="WITHDRAWAL",
                    amount=Money.parse("1555.00"),
                    normalized_description=normalize_description("GUSTO TAX 888888 SYNTH"),
                ),
                tx_date="2026-11-03",
                posted_date="2026-11-03",
                description="GUSTO TAX 888888 SYNTH",
                normalized_description=normalize_description("GUSTO TAX 888888 SYNTH"),
                amount=Money.parse("1555.00"),
                direction="WITHDRAWAL",
            ),
        ]
        db.upsert_bank_transactions(txs)

        rec = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
        assert rec.gusto_matches == 2
        stored_runs = db.list_gusto_payroll_runs()
        cash_run_id = next(
            r.id for r in stored_runs if r.raw.get("non_cash_imputed_benefits") is False
        )
        matches = db.list_matches(status="confirmed")
        assert len(matches) == 2
        assert {m.match_type for m in matches} == {"gusto_net_pay", "gusto_taxes"}
        assert {m.gusto_run_id for m in matches} == {cash_run_id}

        plan_summary = Planner(db, cfg, Categorizer([])).build_plan()
        assert plan_summary.staged_transactions == 2
        tax_net = ZERO
        for stx in db.list_staged_transactions():
            for li in stx.line_items:
                if li.account_key == "payroll_taxes_payable":
                    tax_net += li.amount if li.balance == "INCREASE" else -li.amount
        assert tax_net == ZERO


def test_grasshopper_amount_cents_integral_float_and_non_integral_rejection() -> None:
    def fetch_payload(tx_rows: list[dict[str, object]]) -> list[BankTransaction]:
        def handler(
            method: str, url: str, headers: dict[str, str], body: bytes | None
        ) -> HttpResponse:
            del method, url, headers, body
            return HttpResponse(200, {}, json.dumps({"transactions": tx_rows}).encode("utf-8"))

        api = GrasshopperApiAdapter(
            GrasshopperConfig(account_id="acct-99", access_token="gh-tok", header_secret="sec-123"),
            http_client=HttpClient(
                transport=_RoutingMockTransport(handler),
                sleep_fn=lambda _: None,
            ),
        )
        return api.fetch_transactions()

    txs = fetch_payload(
        [
            {
                "id": "gh-float-cents",
                "posted_date": "2026-01-07",
                "description": "FLOAT CENTS DEBIT",
                "amount_cents": 1234.0,
                "direction": "DEBIT",
            },
            {
                "id": "gh-str-cents",
                "posted_date": "2026-01-08",
                "description": "STRING CENTS CREDIT",
                "amount_cents": "5678.00",
                "direction": "CREDIT",
            },
        ]
    )
    assert len(txs) == 2
    assert txs[0].amount == Money(1234)
    assert txs[1].amount == Money(5678)

    with pytest.raises(ValueError, match="Non-integral amount_cents"):
        fetch_payload(
            [
                {
                    "id": "gh-frac-cents",
                    "posted_date": "2026-01-09",
                    "description": "FRAC CENTS",
                    "amount_cents": 1234.5,
                    "direction": "DEBIT",
                }
            ]
        )

    with pytest.raises(TypeError, match="does not accept bool"):
        fetch_payload(
            [
                {
                    "id": "gh-bool-cents",
                    "posted_date": "2026-01-10",
                    "description": "BOOL CENTS",
                    "amount_cents": True,
                    "direction": "DEBIT",
                }
            ]
        )


def test_harvest_line_item_quantity_validation() -> None:
    def parse_invoice_qty(raw_qty: object, *, omit_qty: bool = False) -> str:
        line: dict[str, object] = {
            "kind": "Service",
            "description": "Consulting",
            "unit_price": "100.00",
            "amount": "100.00",
        }
        if not omit_qty:
            line["quantity"] = raw_qty
        inv = HarvestApiAdapter._parse_api_invoice(
            {
                "id": 999,
                "number": "999",
                "client": {"id": 1, "name": "Acme"},
                "issue_date": "2026-01-01",
                "due_date": "2026-01-31",
                "state": "open",
                "currency": "USD",
                "amount": "100.00",
                "due_amount": "100.00",
                "line_items": [line],
            }
        )
        return inv.line_items[0].quantity

    for valid in ("1", "20", "2.5", "0.25", " 3.5 "):
        direct = HarvestInvoiceLineItem(
            kind="Service",
            description="Consulting",
            quantity=valid,
            unit_price=Money(10000),
            amount=Money(10000),
        )
        assert direct.quantity == valid.strip()
        assert parse_invoice_qty(valid) == valid.strip()

    assert parse_invoice_qty(None) == "1"
    assert parse_invoice_qty(None, omit_qty=True) == "1"

    for invalid in ("abc", "", "   ", "NaN", "inf"):
        with pytest.raises(ValueError):
            HarvestInvoiceLineItem(
                kind="Service",
                description="Consulting",
                quantity=invalid,
                unit_price=Money(10000),
                amount=Money(10000),
            )
        with pytest.raises(ValueError):
            parse_invoice_qty(invalid)

    bad_bool: Any = True
    with pytest.raises(TypeError):
        HarvestInvoiceLineItem(
            kind="Service",
            description="Consulting",
            quantity=bad_bool,
            unit_price=Money(10000),
            amount=Money(10000),
        )
    with pytest.raises(ValueError):
        parse_invoice_qty(True)


def test_gusto_rejects_empty_and_unrecognized_csvs(tmp_path: Path) -> None:
    adapter = GustoPayrollCsvAdapter()

    for name, content in (
        ("empty.csv", ""),
        ("whitespace.csv", "  , \n\n  \n"),
        ("garbage.csv", "Foo,Bar,Baz\n1,2,3\n"),
        ("missing_net.csv", "Employee,Check Date,Gross Earnings\nAlice,2026-01-10,1000.00\n"),
        ("header_only.csv", "Employee,Check Date,Gross Earnings,Net Pay\n"),
        (
            "totals_only.csv",
            "Employee,Check Date,Gross Earnings,Net Pay\nTotals,2026-01-10,1000.00,1000.00\n",
        ),
    ):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        with pytest.raises(GustoParseError):
            adapter.parse_file(p)

    for name, content in (
        ("c_empty.csv", ""),
        ("c_whitespace.csv", " \n  ,  \n"),
        ("c_garbage.csv", "Vendor,Category,Notes\nAcme,Design,Misc\n"),
        ("c_missing_amt.csv", "Contractor,Payment Date,Method\nDana,2026-01-18,ACH\n"),
        ("c_header_only.csv", "Contractor,Payment Date,Payment Amount\n"),
        (
            "c_totals_only.csv",
            "Contractor,Payment Date,Payment Amount\nTotals,2026-01-18,1200.00\n",
        ),
    ):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        with pytest.raises(GustoParseError):
            adapter.parse_contractor_csv(p)
        with pytest.raises(GustoParseError):
            adapter.parse_file(p, run_type="contractor")


def test_gusto_journal_section_validation_failures(tmp_path: Path) -> None:
    adapter = GustoPayrollCsvAdapter()

    # Metadata section missing Pay day
    missing_pay_day = "\n".join(
        [
            '"Payroll journal"',
            '"Employee Earnings"',
            '"Off-cycle payroll for Jane Doe (August 2026)"',
            '"Pay period"," 08/01/2026 - 08/31/2026"',
            '"Last Name","First Name","Gross Earnings","Employee Taxes","Net Pay"',
            '"Doe","Jane","1000.00","200.00","800.00"',
        ]
    )
    p1 = tmp_path / "missing_pay_day.csv"
    p1.write_text(missing_pay_day, encoding="utf-8")
    with pytest.raises(GustoParseError, match="Pay day"):
        adapter.parse_file(p1)

    # Empty Pay day value
    empty_pay_day = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Jane Doe (August 2026)"',
            '"Pay period"," 08/01/2026 - 08/31/2026"',
            '"Pay day",""',
            '"Last Name","First Name","Gross Earnings","Net Pay"',
            '"Doe","Jane","1000.00","1000.00"',
        ]
    )
    p2 = tmp_path / "empty_pay_day.csv"
    p2.write_text(empty_pay_day, encoding="utf-8")
    with pytest.raises(GustoParseError, match="pay day"):
        adapter.parse_file(p2)

    # Section with metadata & header but zero employee rows (only Payroll Totals)
    zero_employees = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Jane Doe (August 2026)"',
            '"Pay period"," 08/01/2026 - 08/31/2026"',
            '"Pay day"," 11/02/2026"',
            '"Last Name","First Name","Gross Earnings","Employee Taxes","Net Pay"',
            '"Payroll Totals","","1000.00","200.00","800.00"',
        ]
    )
    p3 = tmp_path / "zero_employees.csv"
    p3.write_text(zero_employees, encoding="utf-8")
    with pytest.raises(GustoParseError, match="zero employee rows"):
        adapter.parse_file(p3)


def test_gusto_is_summary_row_checks_first_name_without_dropping_employees(
    tmp_path: Path,
) -> None:
    csv_text = "\n".join(
        [
            '"Employee Earnings"',
            '"Regular payroll"',
            '"Pay period: 01/01/2026 - 01/15/2026"',
            '"Pay day: 01/20/2026"',
            (
                '"Last Name","First Name","Gross Earnings",'
                '"Employee Taxes","Employer Taxes","Net Pay","Employer Cost"'
            ),
            '"Totale","Jane","2000.00","400.00","153.00","1600.00","2153.00"',
            '"","Totals","2000.00","400.00","153.00","1600.00","2153.00"',
            '"","Payroll Totals","2000.00","400.00","153.00","1600.00","2153.00"',
            '"","Company Totals","2000.00","400.00","153.00","1600.00","2153.00"',
        ]
    )
    p = tmp_path / "first_name_totals.csv"
    p.write_text(csv_text, encoding="utf-8")
    runs = GustoPayrollCsvAdapter().parse_file(p)
    assert len(runs) == 1
    run = runs[0]
    assert run.employee_count == 1
    assert run.gross_wages == Money.parse("2000.00")
    assert run.employee_taxes == Money.parse("400.00")
    assert run.employer_taxes == Money.parse("153.00")
    assert run.net_pay == Money.parse("1600.00")


def test_gusto_accounting_sanity_checks_catch_renamed_tax_columns_and_corrupt_imputed(
    tmp_path: Path,
) -> None:
    adapter = GustoPayrollCsvAdapter()

    # Renamed Employee Taxes column breaks net_pay = gross - ee_tax - ee_ben
    renamed_ee_tax = "\n".join(
        [
            (
                "Employee,Check Date,Gross Earnings,EE Tax Withholdings Renamed,"
                "Employer Taxes,Net Pay,Employer Cost"
            ),
            "Alice Smith,2026-01-10,5000.00,1000.00,400.00,4000.00,5400.00",
        ]
    )
    p_ee = tmp_path / "renamed_ee_tax.csv"
    p_ee.write_text(renamed_ee_tax, encoding="utf-8")
    with pytest.raises(GustoParseError, match="net pay mismatch"):
        adapter.parse_file(p_ee)

    # Renamed Employer Taxes column breaks Employer Cost = gross + er_tax + er_ben
    renamed_er_tax = "\n".join(
        [
            (
                "Employee,Check Date,Gross Earnings,Employee Taxes,"
                "Company Tax Renamed,Net Pay,Employer Cost"
            ),
            "Alice Smith,2026-01-10,5000.00,1000.00,400.00,4000.00,5400.00",
        ]
    )
    p_er = tmp_path / "renamed_er_tax.csv"
    p_er.write_text(renamed_er_tax, encoding="utf-8")
    with pytest.raises(GustoParseError, match="employer cost mismatch"):
        adapter.parse_file(p_er)

    # Corrupt imputed run: Employer Cost double-counts Gross + Employer Benefits
    corrupt_imputed_cost = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Paul Brown (November)"',
            '"Pay period: 11/01/2026 to 11/30/2026"',
            '"Pay day: 12/01/2026"',
            (
                '"Last Name","First Name","Gross Earnings",'
                '"Medical (Imputed)","Medical (Company Contribution)",'
                '"Employee Taxes","Employer Taxes","Net Pay","Employer Cost"'
            ),
            '"Brown","Paul","2522.29","2522.29","2522.29","0.00","0.00","0.00","5044.58"',
        ]
    )
    p_imp_cost = tmp_path / "corrupt_imputed_cost.csv"
    p_imp_cost.write_text(corrupt_imputed_cost, encoding="utf-8")
    with pytest.raises(GustoParseError, match="employer cost mismatch"):
        adapter.parse_file(p_imp_cost)

    # Corrupt imputed run: non-zero net pay on (Imputed) column run
    corrupt_imputed_net = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Paul Brown (November)"',
            '"Pay period: 11/01/2026 to 11/30/2026"',
            '"Pay day: 12/01/2026"',
            (
                '"Last Name","First Name","Gross Earnings",'
                '"Medical (Imputed)","Medical (Company Contribution)",'
                '"Employee Taxes","Employer Taxes","Net Pay","Employer Cost"'
            ),
            '"Brown","Paul","2522.29","2522.29","2522.29","0.00","0.00","2522.29","2522.29"',
        ]
    )
    p_imp_net = tmp_path / "corrupt_imputed_net.csv"
    p_imp_net.write_text(corrupt_imputed_net, encoding="utf-8")
    with pytest.raises(GustoParseError, match="imputed run"):
        adapter.parse_file(p_imp_net)

    # Valid imputed run with employer taxes: Employer Cost = Employer Benefits + Employer Taxes
    valid_imputed_with_tax = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Jane Doe (Group Life Imputed)"',
            '"Pay period: 11/01/2026 to 11/30/2026"',
            '"Pay day: 12/01/2026"',
            (
                '"Last Name","First Name","Gross Earnings",'
                '"Group Term Life (Imputed)","Group Term Life (Company Contribution)",'
                '"Employee Taxes","Employer Taxes","Net Pay","Employer Cost"'
            ),
            '"Doe","Jane","500.00","500.00","500.00","0.00","38.25","0.00","538.25"',
        ]
    )
    p_imp_tax = tmp_path / "valid_imputed_with_tax.csv"
    p_imp_tax.write_text(valid_imputed_with_tax, encoding="utf-8")
    imp_runs = adapter.parse_file(p_imp_tax)
    assert len(imp_runs) == 1
    assert imp_runs[0].raw["non_cash_imputed_benefits"] is False
    assert imp_runs[0].employer_taxes == Money.parse("38.25")
    assert imp_runs[0].employer_benefits == Money.parse("500.00")
    assert imp_runs[0].total_tax_debit == Money.parse("38.25")


def test_gusto_colon_and_to_metadata_formats_preserve_fingerprint(tmp_path: Path) -> None:
    headers = (
        '"Last Name","First Name","Gross Earnings",'
        '"Medical (Company Contribution)","Employee Taxes","Employer Taxes",'
        '"Net Pay","Reimbursements","Employer Cost"'
    )
    row = '"Doe","Jane","3200.00","3200.00","0.00","0.00","0.00","0.00","3200.00"'

    two_cell = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Jane Doe (August 2026)"',
            '"Pay period"," 08/01/2026 - 08/31/2026"',
            '"Pay day"," 11/02/2026"',
            headers,
            row,
            row.replace('"Doe","Jane"', '"Payroll Totals",""'),
        ]
    )
    colon_and_to = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Jane Doe (August 2026)"',
            '"Pay period: 08/01/2026 to 08/31/2026"',
            '"Pay day: 11/02/2026"',
            headers,
            row,
            row.replace('"Doe","Jane"', '"Payroll Totals",""'),
        ]
    )

    p1 = tmp_path / "two_cell.csv"
    p2 = tmp_path / "colon_to.csv"
    p1.write_text(two_cell, encoding="utf-8")
    p2.write_text(colon_and_to, encoding="utf-8")

    r1 = GustoPayrollCsvAdapter().parse_file(p1)
    r2 = GustoPayrollCsvAdapter().parse_file(p2)
    assert len(r1) == 1
    assert len(r2) == 1
    assert r2[0].check_date == "2026-11-02"
    assert r2[0].pay_period_start == "2026-08-01"
    assert r2[0].pay_period_end == "2026-08-31"
    assert r2[0].gross_wages == r1[0].gross_wages
    assert r2[0].employer_benefits == r1[0].employer_benefits
    assert r2[0].net_pay == r1[0].net_pay
    assert r2[0].fingerprint == r1[0].fingerprint


def test_gusto_cash_reimbursements_scorp_zero_gross_and_overrides(tmp_path: Path) -> None:
    adapter = GustoPayrollCsvAdapter()

    # Cash run with reimbursements & Employer Cost passes both with/without-reimb conventions
    reimb_csv = "\n".join(
        [
            (
                "Check Date,Employee,Gross Earnings,Reimbursements,"
                "Employee Taxes,Employer Taxes,Net Pay,Employer Cost"
            ),
            "2026-01-10,Alice Smith,3000.00,150.00,579.50,247.50,2570.50,3397.50",
        ]
    )
    p_reimb = tmp_path / "reimb_er_cost.csv"
    p_reimb.write_text(reimb_csv, encoding="utf-8")
    runs = adapter.parse_file(p_reimb)
    assert len(runs) == 1
    assert runs[0].net_pay == Money.parse("2420.50")
    assert runs[0].reimbursements == Money.parse("150.00")

    # Corrupt S-Corp imputed run with zero Gross Earnings fails loudly
    zero_gross_scorp = "\n".join(
        [
            '"Employee Earnings"',
            '"Off-cycle payroll for Paul Brown (September 2026)"',
            '"Pay period: 09/01/2026 - 09/30/2026"',
            '"Pay day: 11/02/2026"',
            (
                '"Last Name","First Name","Gross Earnings",'
                "\"Salesforce COBRA | S-Corporation Owner's Medical Insurance "
                '(Company Contribution)","Employee Taxes","Employer Taxes","Net Pay"'
            ),
            '"Brown","Paul","0.00","2522.29","0.00","0.00","0.00"',
        ]
    )
    p_scorp = tmp_path / "zero_gross_scorp.csv"
    p_scorp.write_text(zero_gross_scorp, encoding="utf-8")
    with pytest.raises(GustoParseError, match="imputed run"):
        adapter.parse_file(p_scorp)

    # Column overrides continue to work cleanly on flat payroll CSVs
    override_csv = "\n".join(
        [
            "Pay Date,Worker,Total Gross Earnings,Employee Taxes,Take Home Pay",
            "2026-02-01,Jane Doe,2000.00,400.00,1600.00",
        ]
    )
    p_ovr = tmp_path / "override.csv"
    p_ovr.write_text(override_csv, encoding="utf-8")
    ovr_adapter = GustoPayrollCsvAdapter({"gross_wages": "Total Gross Earnings"})
    ovr_runs = ovr_adapter.parse_file(p_ovr)
    assert len(ovr_runs) == 1
    assert ovr_runs[0].gross_wages == Money.parse("2000.00")
    assert ovr_runs[0].net_pay == Money.parse("1600.00")


def test_gusto_cash_runs_with_imputed_benefits_tiny_net_and_corrupt_imputed_only(
    tmp_path: Path,
) -> None:
    adapter = GustoPayrollCsvAdapter()

    # 1. Cash run with (Imputed) column where Gross Earnings excludes imputed benefits
    kai_repro = "\n".join(
        [
            (
                "Check Date,Employee,Gross Earnings,Employee Taxes,"
                "Employer Taxes,Health (Imputed),Net Pay"
            ),
            "2026-03-01,Owner,5000.00,1000.00,300.00,200.00,4000.00",
        ]
    )
    p_kai = tmp_path / "kai_repro.csv"
    p_kai.write_text(kai_repro, encoding="utf-8")
    kai_runs = adapter.parse_file(p_kai)
    assert len(kai_runs) == 1
    assert kai_runs[0].raw["non_cash_imputed_benefits"] is False
    assert kai_runs[0].gross_wages == Money.parse("5000.00")
    assert kai_runs[0].net_pay == Money.parse("4000.00")

    # 2. Cash run where Gross Earnings includes imputed benefits (and with Company Contrib + Cost)
    gross_incl_imputed = "\n".join(
        [
            (
                "Check Date,Employee,Gross Earnings,Health (Imputed),"
                "Employee Taxes,Employer Taxes,Net Pay"
            ),
            "2026-03-15,Owner,5200.00,200.00,1000.00,300.00,4000.00",
        ]
    )
    p_incl = tmp_path / "gross_incl_imputed.csv"
    p_incl.write_text(gross_incl_imputed, encoding="utf-8")
    incl_runs = adapter.parse_file(p_incl)
    assert len(incl_runs) == 1
    assert incl_runs[0].raw["non_cash_imputed_benefits"] is False
    assert incl_runs[0].gross_wages == Money.parse("5200.00")
    assert incl_runs[0].net_pay == Money.parse("4000.00")

    gross_incl_with_cost = "\n".join(
        [
            (
                "Check Date,Employee,Gross Earnings,Health (Imputed),"
                "Health (Company Contribution),Employee Taxes,Employer Taxes,"
                "Reimbursements,Net Pay,Employer Cost"
            ),
            "2026-03-20,Owner,5200.00,200.00,200.00,1000.00,300.00,150.00,4150.00,5650.00",
        ]
    )
    p_incl_cost = tmp_path / "gross_incl_imputed_cost.csv"
    p_incl_cost.write_text(gross_incl_with_cost, encoding="utf-8")
    incl_cost_runs = adapter.parse_file(p_incl_cost)
    assert len(incl_cost_runs) == 1
    assert incl_cost_runs[0].raw["non_cash_imputed_benefits"] is False
    assert incl_cost_runs[0].net_pay == Money.parse("4000.00")

    # 3. Tiny non-zero net pay ($0.01 and $0.02) must NOT be reclassified as non-cash imputed
    for cents_str in ("0.01", "0.02"):
        tiny_csv = "\n".join(
            [
                (
                    "Check Date,Employee,Gross Earnings,Health (Company Contribution),"
                    "Employee Taxes,Employer Taxes,Net Pay"
                ),
                f"2026-04-01,Owner,{cents_str},{cents_str},0.00,0.00,{cents_str}",
            ]
        )
        p_tiny = tmp_path / f"tiny_{cents_str}.csv"
        p_tiny.write_text(tiny_csv, encoding="utf-8")
        tiny_runs = adapter.parse_file(p_tiny)
        assert len(tiny_runs) == 1
        assert tiny_runs[0].raw["non_cash_imputed_benefits"] is False
        assert tiny_runs[0].net_pay == Money.parse(cents_str)

    # 4. Corrupt imputed-only run missing company contribution fails loudly
    corrupt_missing_er_ben = "\n".join(
        [
            "Check Date,Employee,Gross Earnings,Medical (Imputed),Net Pay",
            "2026-05-01,Owner,2522.29,2522.29,0.00",
        ]
    )
    p_corrupt = tmp_path / "corrupt_missing_er_ben.csv"
    p_corrupt.write_text(corrupt_missing_er_ben, encoding="utf-8")
    with pytest.raises(GustoParseError, match="imputed run"):
        adapter.parse_file(p_corrupt)


def _run_cli_capture(cfg_path: Path, argv: list[str]) -> tuple[int, str, str]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = main(["--config", str(cfg_path), *argv])
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def test_cli_gusto_ingest_file_failure_and_inbox_partial_success(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    bad_csv = "\n".join(
        [
            "Check Date,Employee,Gross Earnings,Medical (Imputed),Net Pay",
            "2026-05-01,Owner,2522.29,2522.29,0.00",
        ]
    )

    # (a) Explicit --file <bad.csv> fails immediately with exit code 2 and stderr error
    explicit_bad = tmp_path / "explicit_bad.csv"
    explicit_bad.write_text(bad_csv, encoding="utf-8")
    code, _, err = _run_cli_capture(cfg_path, ["ingest", "gusto", "--file", str(explicit_bad)])
    assert code == 2
    assert "Error:" in err
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        assert db.list_gusto_payroll_runs() == []

    # (b) Inbox with corrupt + valid CSV persists valid run, reports bad file, returns 2
    gusto_inbox = tmp_path / "inbox/gusto"
    gusto_inbox.mkdir(parents=True, exist_ok=True)
    (gusto_inbox / "01_corrupt.csv").write_text(bad_csv, encoding="utf-8")
    (gusto_inbox / "02_valid.csv").write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")

    code, out, err = _run_cli_capture(cfg_path, ["ingest", "gusto"])
    assert code == 2
    assert "Gusto: 1 file(s), read 1, inserted 1 payroll run(s)." in out
    assert "Error ingesting Gusto (01_corrupt.csv):" in err
    assert "Ingest completed with 1 failed file(s): 01_corrupt.csv" in err
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        runs = db.list_gusto_payroll_runs()
        assert len(runs) == 1
        assert runs[0].gross_wages == Money.parse("4500.00")
        assert runs[0].net_pay == Money.parse("3555.75")

    # (c) `ingest all` finishes remaining sources, persists valid Gusto run, returns 2
    all_ws = tmp_path / "all_ws"
    all_ws.mkdir()
    all_cfg = write_test_workspace_config(all_ws, include_api_creds=False)
    (all_ws / "inbox/harvest").mkdir(parents=True, exist_ok=True)
    (all_ws / "inbox/harvest/invoices.csv").write_text(SAMPLE_HARVEST_CSV, encoding="utf-8")
    (all_ws / "inbox/gusto").mkdir(parents=True, exist_ok=True)
    (all_ws / "inbox/gusto/01_corrupt.csv").write_text(bad_csv, encoding="utf-8")
    (all_ws / "inbox/gusto/02_valid.csv").write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")

    code, out, err = _run_cli_capture(all_cfg, ["ingest", "all"])
    assert code == 2
    assert "Harvest CSV (invoices.csv): 3 invoices." in out
    assert "Gusto (02_valid.csv): read 1, inserted 1 run(s)." in out
    assert "Error ingesting Gusto (01_corrupt.csv):" in err
    assert "Ingest completed with 1 failed file(s): 01_corrupt.csv" in err
    with LedgerDB(all_ws / "data/ledgerdemain.db") as db:
        assert len(db.list_gusto_payroll_runs()) == 1
        assert len(db.list_harvest_invoices()) == 3
