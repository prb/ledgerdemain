"""Tests for Harvest, Grasshopper, Capital One, Gusto, and OFX source adapters."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from ledgerdemain.adapters.base import normalize_description
from ledgerdemain.adapters.capital_one import CapitalOneCsvAdapter, CapitalOnePdfAdapter
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter, GrasshopperCsvAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter, HarvestCsvAdapter
from ledgerdemain.adapters.ofx import OfxTransactionParser
from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import GrasshopperConfig, HarvestConfig
from ledgerdemain.db import LedgerDB, make_bank_fingerprint
from ledgerdemain.http_client import HttpClient, HttpResponse
from ledgerdemain.models import ZERO, BankTransaction
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
    assert invoices[0].subtotal == Decimal("3000.00")
    assert payments[0].payment_gateway == "ACH"
    assert any("updated_since=2026-01-01T00%3A00%3A00Z" in u for _, u, _ in transport.requests)


def test_harvest_csv_adapter(tmp_path: Path) -> None:
    p = tmp_path / "harvest.csv"
    p.write_text(SAMPLE_HARVEST_CSV, encoding="utf-8")
    invs = HarvestCsvAdapter().parse_file(p)

    assert len(invs) == 3
    assert invs[0].invoice_number == "1001"
    assert invs[0].total_amount == Decimal("3000.00")
    assert invs[2].due_amount == Decimal("1500.00")


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
    assert txs[1].amount == Decimal("2000.50")

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
    assert txs[0].amount == Decimal("250.00")
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
    assert run.gross_wages == Decimal("4500.00")
    assert run.reimbursements == Decimal("150.00")
    assert run.employee_taxes == Decimal("944.25")
    assert run.employer_taxes == Decimal("371.25")
    assert run.net_pay == Decimal("3555.75")
    assert run.net_disbursement == Decimal("3705.75")
    assert run.total_tax_debit == Decimal("1315.50")
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
    assert c_runs[0].net_disbursement == Decimal("1200.00")
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
    assert stmt.ledger_balance == Decimal("-1234.56")
    assert stmt.ledger_balance_as_of == "2026-02-01"
    assert stmt.available_balance == Decimal("8765.44")
    assert len(stmt.transactions) == 2

    charge, payment = stmt.transactions
    assert charge.tx_date == "2026-01-08"
    assert charge.posted_date == "2026-01-10"
    assert charge.direction == "WITHDRAWAL"
    assert charge.amount == Decimal("42.50")
    assert charge.external_id == "FIT-C1-001"
    assert charge.card_last4 == "8888"
    assert "CCACCTTO" not in charge.raw

    # Payment has no <CCACCTTO>, so it falls back to statement-level <CCACCTFROM>
    assert payment.tx_date == "2026-01-15"
    assert payment.posted_date == "2026-01-15"
    assert payment.direction == "DEPOSIT"
    assert payment.amount == Decimal("500.00")
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
    assert stmt.ledger_balance == Decimal("-85.00")
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
    assert imputed_run.gross_wages == Decimal("3200.00")
    assert imputed_run.employer_benefits == Decimal("3200.00")
    assert imputed_run.employee_benefits == ZERO
    assert imputed_run.net_pay == ZERO
    assert imputed_run.raw["non_cash_imputed_benefits"] is True

    assert cash_run.run_type == "off_cycle"
    assert cash_run.check_date == "2026-11-04"
    assert cash_run.pay_period_start == "2026-08-01"
    assert cash_run.pay_period_end == "2026-10-31"
    assert cash_run.gross_wages == Decimal("10000.00")
    assert cash_run.employee_taxes == Decimal("765.00")
    assert cash_run.employer_taxes == Decimal("790.00")
    assert cash_run.net_pay == Decimal("9235.00")
    assert cash_run.total_tax_debit == Decimal("1555.00")
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
                    amount=Decimal("9235.00"),
                    normalized_description=normalize_description("GUSTO NET 999999 SYNTH"),
                ),
                tx_date="2026-11-03",
                posted_date="2026-11-03",
                description="GUSTO NET 999999 SYNTH",
                normalized_description=normalize_description("GUSTO NET 999999 SYNTH"),
                amount=Decimal("9235.00"),
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
                    amount=Decimal("1555.00"),
                    normalized_description=normalize_description("GUSTO TAX 888888 SYNTH"),
                ),
                tx_date="2026-11-03",
                posted_date="2026-11-03",
                description="GUSTO TAX 888888 SYNTH",
                normalized_description=normalize_description("GUSTO TAX 888888 SYNTH"),
                amount=Decimal("1555.00"),
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
        assert tax_net == Decimal("0.00")
