"""Tests for Harvest, Grasshopper, Capital One, Gusto, and OFX source adapters."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ledgerdemain.adapters.base import normalize_description
from ledgerdemain.adapters.grasshopper import (
    GrasshopperApiAdapter,
    GrasshopperParseError,
)
from ledgerdemain.adapters.gusto import GustoParseError, GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter, HarvestParseError
from ledgerdemain.adapters.ofx import OfxParseError, OfxTransactionParser
from ledgerdemain.categorize import Categorizer
from ledgerdemain.cli import main
from ledgerdemain.config import GrasshopperConfig, HarvestConfig, load_config
from ledgerdemain.db import LedgerDB, make_bank_fingerprint
from ledgerdemain.http_client import HttpClient, HttpError, HttpResponse
from ledgerdemain.models import BankTransaction, HarvestInvoiceLineItem
from ledgerdemain.money import ZERO, Money
from ledgerdemain.planner import Planner
from ledgerdemain.reconcile import Reconciler
from tests.fixtures import (
    SAMPLE_CAPITAL_ONE_OFX_XML,
    SAMPLE_GUSTO_CONTRACTOR_CSV,
    SAMPLE_GUSTO_PAYROLL_CSV,
    SAMPLE_OFX_SGML,
    RoutingMockTransport,
    make_test_config,
    write_test_workspace_config,
)


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

    transport = RoutingMockTransport(handler)
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


def test_grasshopper_api_and_ofx(tmp_path: Path) -> None:
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

    transport = RoutingMockTransport(handler)
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

    ofx_path = tmp_path / "gh.ofx"
    ofx_path.write_text(SAMPLE_OFX_SGML, encoding="utf-8")
    ofx_txs = OfxTransactionParser().parse_file(ofx_path, "grasshopper", "grasshopper_checking")
    assert len(ofx_txs) == 2
    assert ofx_txs[0].external_id == "OFX-GH-1"
    assert ofx_txs[0].direction == "WITHDRAWAL"
    assert ofx_txs[1].direction == "DEPOSIT"


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

    # Re-parsing the same OFX file is idempotent on FITID
    with LedgerDB(tmp_path / "ofx.db") as db:
        db.init_schema()
        _, ins1 = db.upsert_bank_transactions(
            OfxTransactionParser().parse_file(
                ofx_path, source="capital_one", account_key="capital_one_card"
            )
        )
        _, ins2 = db.upsert_bank_transactions(
            OfxTransactionParser().parse_file(
                ofx_path, source="capital_one", account_key="capital_one_card"
            )
        )
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


@pytest.mark.parametrize(
    "bad_input",
    [
        "",
        "   \n\t  ",
        "not an ofx document at all",
        "Date,Description,Amount\n2026-01-01,Test,-10.00\n",
        "<!DOCTYPE html><html><body>Please sign in</body></html>",
        "<html><body><OFX><BANKTRANLIST></BANKTRANLIST></OFX></body></html>",
        "<OFX><SIGNONMSGSRSV1><SONRS></SONRS></SIGNONMSGSRSV1></OFX>",
        "<?xml version='1.0'?><OFX><SIGNONMSGSRSV1></SIGNONMSGSRSV1></OFX>",
        "<OFX>\x00<BANKTRANLIST></BANKTRANLIST></OFX>",
    ],
)
def test_ofx_rejects_non_ofx_empty_html_and_missing_banktranlist(bad_input: str) -> None:
    parser = OfxTransactionParser()
    with pytest.raises(OfxParseError) as exc_info:
        parser.parse_statement_text(bad_input, source="capital_one", account_key="capital_one_card")
    assert isinstance(exc_info.value, ValueError)


def test_ofx_zero_activity_statements_succeed() -> None:
    parser = OfxTransactionParser()
    sgml_empty = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>00001111</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101
<DTEND>20260131
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    sgml_stmt = parser.parse_statement_text(
        sgml_empty,
        source="grasshopper",
        account_key="grasshopper_savings",
        expected_currency="USD",
        expected_account_last4="1111",
    )
    assert sgml_stmt.transactions == []
    assert sgml_stmt.account_id == "00001111"
    assert sgml_stmt.start_date == "2026-01-01"

    xml_self_closing = """<?xml version="1.0" encoding="utf-8"?>
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
<CURDEF>USD</CURDEF>
<CCACCTFROM><ACCTID>9999</ACCTID></CCACCTFROM>
<BANKTRANLIST/>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"""
    xml_stmt = parser.parse_statement_text(
        xml_self_closing,
        source="capital_one",
        account_key="capital_one_card",
        expected_currency="USD",
        expected_account_last4="9999",
    )
    assert xml_stmt.transactions == []
    assert xml_stmt.account_id == "9999"


def test_ofx_lowercase_and_mixed_case_xml_parsed() -> None:
    lower_xml = """<?xml version="1.0" encoding="utf-8"?>
<ofx><creditcardmsgsrsv1><ccstmttrnrs><ccstmtrs>
<curdef>usd</curdef>
<ccacctfrom><acctid>9999</acctid></ccacctfrom>
<banktranlist>
  <dtstart>20260101</dtstart>
  <dtend>20260131</dtend>
  <stmttrn>
    <trntype>DEBIT</trntype>
    <dtposted>20260115</dtposted>
    <trnamt>-19.99</trnamt>
    <fitid>LOW-1</fitid>
    <name>LOWERCASE XML TEST</name>
  </stmttrn>
</banktranlist>
</ccstmtrs></ccstmttrnrs></creditcardmsgsrsv1></ofx>"""
    stmt = OfxTransactionParser().parse_statement_text(
        lower_xml,
        source="capital_one",
        account_key="capital_one_card",
        expected_currency="USD",
        expected_account_last4="9999",
    )
    assert stmt.currency == "USD"
    assert len(stmt.transactions) == 1
    assert stmt.transactions[0].amount == Money.parse("19.99")
    assert stmt.transactions[0].external_id == "LOW-1"

    mixed_xml = lower_xml.replace("<ofx>", "<Ofx>").replace("</ofx>", "</Ofx>")
    mixed_no_decl = mixed_xml.split("\n", 1)[1]
    stmt2 = OfxTransactionParser().parse_statement_text(
        mixed_no_decl, source="capital_one", account_key="capital_one_card"
    )
    assert len(stmt2.transactions) == 1


def test_ofx_truncated_or_malformed_xml_raises_instead_of_sgml_fallback() -> None:
    parser = OfxTransactionParser()

    # Truncated XML missing </OFX> even though one <STMTTRN> is complete
    truncated_xml = """<?xml version="1.0" encoding="utf-8"?>
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
<CURDEF>USD</CURDEF>
<CCACCTFROM><ACCTID>9999</ACCTID></CCACCTFROM>
<BANKTRANLIST>
<STMTTRN>
  <TRNTYPE>DEBIT</TRNTYPE>
  <DTPOSTED>20260110</DTPOSTED>
  <TRNAMT>-42.50</TRNAMT>
  <FITID>FIT-1</FITID>
  <NAME>PARTIAL TX</NAME>
</STMTTRN>
</BANKTRANLIST>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>"""
    with pytest.raises(OfxParseError, match="</OFX>"):
        parser.parse_statement_text(
            truncated_xml, source="capital_one", account_key="capital_one_card"
        )

    # Malformed XML with closing </OFX> present must fail loudly, not fall back to SGML regexes
    malformed_xml_with_decl = """<?xml version="1.0" encoding="utf-8"?>
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS>
<CURDEF>USD</CURDEF>
<CCACCTFROM><ACCTID>9999</ACCTID></CCACCTFROM>
<BANKTRANLIST>
<STMTTRN>
  <TRNTYPE>DEBIT</TRNTYPE>
  <DTPOSTED>20260110</DTPOSTED>
  <TRNAMT>-42.50</MISMATCH>
  <FITID>FIT-1</FITID>
  <NAME>BROKEN XML</NAME>
</STMTTRN>
</BANKTRANLIST>
</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>"""
    with pytest.raises(OfxParseError, match="Malformed XML"):
        parser.parse_statement_text(
            malformed_xml_with_decl, source="capital_one", account_key="capital_one_card"
        )

    # Malformed XML without <?xml?> declaration (has XML leaf closing tags)
    malformed_xml_no_decl = malformed_xml_with_decl.split("\n", 1)[1]
    with pytest.raises(OfxParseError, match="Malformed XML"):
        parser.parse_statement_text(
            malformed_xml_no_decl, source="capital_one", account_key="capital_one_card"
        )


def test_ofx_sgml_unclosed_stmttrn_terminated_before_balances() -> None:
    sgml_ledger_before_close = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>00001234</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260120120000
<TRNAMT>-75.00
<FITID>SGML-UNCLOSED-1
<NAME>ACME CLOUD
<LEDGERBAL>
<BALAMT>9876.54
<DTASOF>20260121120000
</LEDGERBAL>
<AVAILBAL>
<BALAMT>9000.00
<DTASOF>20260121120000
</AVAILBAL>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    stmt = OfxTransactionParser().parse_statement_text(
        sgml_ledger_before_close, source="grasshopper", account_key="grasshopper_checking"
    )
    assert len(stmt.transactions) == 1
    tx = stmt.transactions[0]
    assert tx.amount == Money.parse("75.00")
    assert tx.direction == "WITHDRAWAL"
    assert tx.description == "ACME CLOUD"
    assert "BALAMT" not in tx.raw
    assert "DTASOF" not in tx.raw
    assert stmt.ledger_balance == Money.parse("9876.54")
    assert stmt.available_balance == Money.parse("9000.00")

    # Verify direct SGML extractor termination across <AVAILBAL>, </STMTRS>, </CCSTMTRS>, </OFX>, $
    for terminator in ("<AVAILBAL><BALAMT>500.00", "</STMTRS>", "</CCSTMTRS>", "</OFX>", ""):
        snippet = (
            "<BANKTRANLIST><STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260122"
            "<TRNAMT>125.00<FITID>T-1<NAME>DIRECT SNIPPET\n"
            f"{terminator}"
        )
        _, entries = OfxTransactionParser._extract_sgml_entries(snippet)
        assert len(entries) == 1
        assert "BALAMT" not in entries[0]
        assert entries[0]["TRNAMT"] == "125.00"


def test_ofx_truncated_sgml_raises() -> None:
    parser = OfxTransactionParser()

    missing_ofx_close = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260120<TRNAMT>-10.00<FITID>1<NAME>ITEM
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>"""
    with pytest.raises(OfxParseError, match="</OFX>"):
        parser.parse_statement_text(
            missing_ofx_close, source="grasshopper", account_key="grasshopper_checking"
        )

    missing_banktranlist_close = """OFXHEADER:100
DATA:OFXSGML
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260120<TRNAMT>-10.00<FITID>1<NAME>ITEM
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    with pytest.raises(OfxParseError, match="</BANKTRANLIST>"):
        parser.parse_statement_text(
            missing_banktranlist_close, source="grasshopper", account_key="grasshopper_checking"
        )

    trailing_junk = f"{SAMPLE_OFX_SGML}\nTRUNCATED_TRAILING_DATA"
    with pytest.raises(OfxParseError, match="trailing"):
        parser.parse_statement_text(
            trailing_junk, source="grasshopper", account_key="grasshopper_checking"
        )


@pytest.mark.parametrize(
    ("body", "pattern"),
    [
        (
            "<STMTTRN><TRNTYPE>DEBIT<TRNAMT>-10.00<FITID>1<NAME>NO DATE</STMTTRN>",
            "DTPOSTED",
        ),
        (
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>not-a-date<TRNAMT>-10.00<FITID>1</STMTTRN>",
            "Invalid OFX date",
        ),
        (
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261399<TRNAMT>-10.00<FITID>1</STMTTRN>",
            "Invalid OFX date",
        ),
        (
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260120<FITID>1<NAME>NO AMT</STMTTRN>",
            "TRNAMT",
        ),
        (
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260120<TRNAMT>N/A<FITID>1</STMTTRN>",
            "Invalid OFX monetary amount",
        ),
        (
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260120<TRNAMT>NaN<FITID>1</STMTTRN>",
            "Invalid OFX monetary amount",
        ),
        (
            "<STMTTRN></STMTTRN>",
            "DTPOSTED",
        ),
    ],
)
def test_ofx_missing_or_invalid_stmttrn_fields_raise(body: str, pattern: str) -> None:
    doc = (
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
        f"<BANKTRANLIST>{body}</BANKTRANLIST>"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )
    with pytest.raises(OfxParseError, match=pattern):
        OfxTransactionParser().parse_statement_text(
            doc, source="grasshopper", account_key="grasshopper_checking"
        )


def test_ofx_stmttrn_outside_banktranlist_raises_parity_error() -> None:
    bad_xml = """<?xml version="1.0" encoding="utf-8"?>
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<BANKTRANLIST/>
<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260120</DTPOSTED><TRNAMT>-10.00</TRNAMT></STMTTRN>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"""
    with pytest.raises(OfxParseError, match="count mismatch"):
        OfxTransactionParser().parse_statement_text(
            bad_xml, source="grasshopper", account_key="grasshopper_checking"
        )


def test_ofx_expected_currency_and_account_last4_validation() -> None:
    parser = OfxTransactionParser()

    eur_xml = SAMPLE_CAPITAL_ONE_OFX_XML.replace("<CURDEF>USD</CURDEF>", "<CURDEF>EUR</CURDEF>")
    with pytest.raises(OfxParseError, match="currency mismatch"):
        parser.parse_statement_text(
            eur_xml,
            source="capital_one",
            account_key="capital_one_card",
            expected_currency="USD",
        )

    # Case-insensitive match passes
    stmt = parser.parse_statement_text(
        SAMPLE_CAPITAL_ONE_OFX_XML,
        source="capital_one",
        account_key="capital_one_card",
        expected_currency="usd",
        expected_account_last4="9999",
    )
    assert stmt.currency == "USD"

    # Legacy SGML fixture without <CURDEF> defaults to USD and rejects non-USD expectations
    stmt_sgml = parser.parse_statement_text(
        SAMPLE_OFX_SGML,
        source="grasshopper",
        account_key="grasshopper_checking",
        expected_currency="USD",
    )
    assert stmt_sgml.currency == "USD"
    with pytest.raises(OfxParseError, match="currency mismatch"):
        parser.parse_statement_text(
            SAMPLE_OFX_SGML,
            source="grasshopper",
            account_key="grasshopper_checking",
            expected_currency="EUR",
        )

    # Account last-4 mismatches and normalization
    with pytest.raises(OfxParseError, match="account mismatch"):
        parser.parse_statement_text(
            SAMPLE_CAPITAL_ONE_OFX_XML,
            source="capital_one",
            account_key="capital_one_card",
            expected_account_last4="1234",
        )
    with pytest.raises(OfxParseError, match="account mismatch"):
        parser.parse_statement_text(
            SAMPLE_OFX_SGML,
            source="grasshopper",
            account_key="grasshopper_checking",
            expected_account_last4="9999",
        )
    masked_xml = SAMPLE_CAPITAL_ONE_OFX_XML.replace(
        "<ACCTID>9999</ACCTID>", "<ACCTID>0000-9999</ACCTID>"
    )
    stmt_masked = parser.parse_statement_text(
        masked_xml,
        source="capital_one",
        account_key="capital_one_card",
        expected_account_last4="****9999",
    )
    assert stmt_masked.account_id == "0000-9999"


def test_cli_ofx_validation_account_override_and_currency_mismatch(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    cfg_path = cfg.config_dir / "ledgerdemain.toml"
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write(
            """
[[grasshopper.accounts]]
account_key = "grasshopper_savings"
acctid_last4 = "1111"

[[grasshopper.accounts]]
account_key = "grasshopper_money_market"
acctid_last4 = "2222"
"""
        )

    sav_ofx = tmp_path / "sav.ofx"
    sav_ofx.write_text(
        """<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><ACCTID>00001111</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261105<TRNAMT>-100.00<FITID>S-1<NAME>TEST</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>""",
        encoding="utf-8",
    )

    # Forcing wrong --account-key fails loudly with exit code 2
    err_buf = io.StringIO()
    with contextlib.redirect_stderr(err_buf):
        code = main(
            [
                "--config",
                str(cfg_path),
                "ingest",
                "grasshopper",
                "--file",
                str(sav_ofx),
                "--account-key",
                "grasshopper_money_market",
            ]
        )
    assert code == 2
    assert "account mismatch" in err_buf.getvalue()

    # Matching --account-key succeeds
    out_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf):
        code = main(
            [
                "--config",
                str(cfg_path),
                "ingest",
                "grasshopper",
                "--file",
                str(sav_ofx),
                "--account-key",
                "grasshopper_savings",
            ]
        )
    assert code == 0

    # Capital One OFX currency mismatch against config currency fails loudly
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace('currency = "USD"', 'currency = "EUR"'),
        encoding="utf-8",
    )
    cap_ofx = tmp_path / "cap.ofx"
    cap_ofx.write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")
    err_buf2 = io.StringIO()
    with contextlib.redirect_stderr(err_buf2):
        code2 = main(["--config", str(cfg_path), "ingest", "capital-one", "--file", str(cap_ofx)])
    assert code2 == 2
    assert "currency mismatch" in err_buf2.getvalue()


def _run_cli_main(cfg_path: Path, argv: list[str]) -> tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        code = main(["--config", str(cfg_path), *argv])
    return code, out_buf.getvalue(), err_buf.getvalue()


def test_cli_capital_one_inbox_continues_after_corrupt_ofx(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    cfg_path = cfg.config_dir / "ledgerdemain.toml"
    co_inbox = cfg.general.inbox_dir / "capital_one"
    co_inbox.mkdir(parents=True, exist_ok=True)

    corrupt_ofx = co_inbox / "01_corrupt.ofx"
    corrupt_ofx.write_text(
        SAMPLE_CAPITAL_ONE_OFX_XML.split("</BANKTRANLIST>")[0],
        encoding="utf-8",
    )
    (co_inbox / "02_valid.ofx").write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")

    code, out, err = _run_cli_main(cfg_path, ["ingest", "capital-one"])
    assert code == 2
    assert "Error ingesting Capital One (01_corrupt.ofx):" in err
    assert "Ingest completed with 1 failed file(s): 01_corrupt.ofx" in err
    assert "Capital One: 1 file(s), read 2, inserted 2 new tx(s)." in out

    with LedgerDB(cfg.general.db_path) as db:
        txs = db.list_bank_transactions(source="capital_one")
    assert {tx.external_id for tx in txs} == {"FIT-C1-001", "FIT-C1-002"}

    code_single, _, err_single = _run_cli_main(
        cfg_path, ["ingest", "capital-one", "--file", str(corrupt_ofx)]
    )
    assert code_single == 2
    assert "Error:" in err_single
    assert "Ingest completed with" not in err_single


def test_cli_grasshopper_inbox_continues_after_corrupt_ofx(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    cfg_path = cfg.config_dir / "ledgerdemain.toml"
    gh_inbox = cfg.general.inbox_dir / "grasshopper"
    gh_inbox.mkdir(parents=True, exist_ok=True)

    corrupt_ofx = gh_inbox / "01_corrupt.ofx"
    corrupt_ofx.write_text(
        SAMPLE_OFX_SGML.split("</BANKTRANLIST>")[0],
        encoding="utf-8",
    )
    (gh_inbox / "02_valid.ofx").write_text(SAMPLE_OFX_SGML, encoding="utf-8")

    code, out, err = _run_cli_main(cfg_path, ["ingest", "grasshopper", "--inbox"])
    assert code == 2
    assert "Error ingesting Grasshopper (01_corrupt.ofx):" in err
    assert "Ingest completed with 1 failed file(s): 01_corrupt.ofx" in err
    assert "Grasshopper inbox: 1 file(s), read 2, inserted 2." in out

    with LedgerDB(cfg.general.db_path) as db:
        txs = db.list_bank_transactions(source="grasshopper")
    assert {tx.external_id for tx in txs} == {"OFX-GH-1", "OFX-GH-2"}

    code_single, _, err_single = _run_cli_main(
        cfg_path, ["ingest", "grasshopper", "--file", str(corrupt_ofx)]
    )
    assert code_single == 2
    assert "Error:" in err_single
    assert "Ingest completed with" not in err_single


def test_cli_ingest_all_continues_across_corrupt_inbox_ofx(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    cfg_path = cfg.config_dir / "ledgerdemain.toml"
    gh_inbox = cfg.general.inbox_dir / "grasshopper"
    co_inbox = cfg.general.inbox_dir / "capital_one"
    gh_inbox.mkdir(parents=True, exist_ok=True)
    co_inbox.mkdir(parents=True, exist_ok=True)

    (gh_inbox / "01_bad_gh.ofx").write_text(
        SAMPLE_OFX_SGML.split("</BANKTRANLIST>")[0],
        encoding="utf-8",
    )
    (gh_inbox / "02_good_gh.ofx").write_text(SAMPLE_OFX_SGML, encoding="utf-8")

    (co_inbox / "01_bad_co.ofx").write_text(
        SAMPLE_CAPITAL_ONE_OFX_XML.split("</BANKTRANLIST>")[0],
        encoding="utf-8",
    )
    (co_inbox / "02_good_co.ofx").write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")

    code, out, err = _run_cli_main(cfg_path, ["ingest", "all"])
    assert code == 2
    assert "Error ingesting Grasshopper (01_bad_gh.ofx):" in err
    assert "Error ingesting Capital One (01_bad_co.ofx):" in err
    assert "Ingest completed with 2 failed file(s): 01_bad_gh.ofx, 01_bad_co.ofx" in err
    assert "Grasshopper file (02_good_gh.ofx): read 2, inserted 2." in out
    assert "Capital One (02_good_co.ofx): read 2, inserted 2." in out

    with LedgerDB(cfg.general.db_path) as db:
        gh_txs = db.list_bank_transactions(source="grasshopper")
        co_txs = db.list_bank_transactions(source="capital_one")
    assert {tx.external_id for tx in gh_txs} == {"OFX-GH-1", "OFX-GH-2"}
    assert {tx.external_id for tx in co_txs} == {"FIT-C1-001", "FIT-C1-002"}


# An imputed-only run with no company contribution: fails Gusto's accounting cross-check.
_CORRUPT_GUSTO_CSV = "\n".join(
    [
        "Check Date,Employee,Gross Earnings,Medical (Imputed),Net Pay",
        "2026-05-01,Owner,2522.29,2522.29,0.00",
    ]
)


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
                transport=RoutingMockTransport(handler),
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

    with pytest.raises(GrasshopperParseError, match="does not accept bool"):
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
    corrupt_missing_er_ben = _CORRUPT_GUSTO_CSV
    p_corrupt = tmp_path / "corrupt_missing_er_ben.csv"
    p_corrupt.write_text(corrupt_missing_er_ben, encoding="utf-8")
    with pytest.raises(GustoParseError, match="imputed run"):
        adapter.parse_file(p_corrupt)


def test_cli_gusto_ingest_file_failure_and_inbox_partial_success(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    bad_csv = _CORRUPT_GUSTO_CSV

    # (a) Explicit --file <bad.csv> fails immediately with exit code 2 and stderr error
    explicit_bad = tmp_path / "explicit_bad.csv"
    explicit_bad.write_text(bad_csv, encoding="utf-8")
    code, _, err = _run_cli_main(cfg_path, ["ingest", "gusto", "--file", str(explicit_bad)])
    assert code == 2
    assert "Error:" in err
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        assert db.list_gusto_payroll_runs() == []

    # (b) Inbox with corrupt + valid CSV persists valid run, reports bad file, returns 2
    gusto_inbox = tmp_path / "inbox/gusto"
    gusto_inbox.mkdir(parents=True, exist_ok=True)
    (gusto_inbox / "01_corrupt.csv").write_text(bad_csv, encoding="utf-8")
    (gusto_inbox / "02_valid.csv").write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")

    code, out, err = _run_cli_main(cfg_path, ["ingest", "gusto"])
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
    (all_ws / "inbox/gusto").mkdir(parents=True, exist_ok=True)
    (all_ws / "inbox/gusto/01_corrupt.csv").write_text(bad_csv, encoding="utf-8")
    (all_ws / "inbox/gusto/02_valid.csv").write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")

    code, out, err = _run_cli_main(all_cfg, ["ingest", "all"])
    assert code == 2
    assert "Gusto (02_valid.csv): read 1, inserted 1 run(s)." in out
    assert "Error ingesting Gusto (01_corrupt.csv):" in err
    assert "Ingest completed with 1 failed file(s): 01_corrupt.csv" in err
    with LedgerDB(all_ws / "data/ledgerdemain.db") as db:
        assert len(db.list_gusto_payroll_runs()) == 1


def _write_gusto_inbox(root: Path, *, corrupt: bool = False) -> None:
    (root / "inbox/gusto").mkdir(parents=True, exist_ok=True)
    (root / "inbox/gusto/02_valid.csv").write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")
    if corrupt:
        (root / "inbox/gusto/01_corrupt.csv").write_text(_CORRUPT_GUSTO_CSV, encoding="utf-8")


def test_cli_ingest_all_continues_after_api_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=True)
    _write_gusto_inbox(tmp_path)

    def failing_harvest(*_args: object, **_kwargs: object) -> tuple[int, int, int]:
        raise HttpError(503, "harvest unavailable")

    def failing_grasshopper(*_args: object, **_kwargs: object) -> tuple[int, int]:
        raise ValueError("unexpected grasshopper payload")

    monkeypatch.setattr("ledgerdemain.cli._ingest_harvest_api", failing_harvest)
    monkeypatch.setattr("ledgerdemain.cli._ingest_grasshopper_api", failing_grasshopper)

    code, out, err = _run_cli_main(cfg_path, ["ingest", "all"])

    assert code == 2
    assert "Error ingesting Harvest API: HTTP 503: harvest unavailable" in err
    assert "Error ingesting Grasshopper API: unexpected grasshopper payload" in err
    assert "Ingest completed with 2 failed API step(s): Harvest API, Grasshopper API" in err
    assert "failed file(s)" not in err
    # Later sources still run after the failed API steps.
    assert "Gusto (02_valid.csv): read 1, inserted 1 run(s)." in out
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        assert len(db.list_gusto_payroll_runs()) == 1


def test_cli_ingest_all_reports_api_and_file_failures_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=True)
    _write_gusto_inbox(tmp_path, corrupt=True)

    def failing_harvest(*_args: object, **_kwargs: object) -> tuple[int, int, int]:
        raise HttpError(0, "Network request failed")

    def ok_grasshopper(*_args: object, **_kwargs: object) -> tuple[int, int]:
        return 0, 0

    monkeypatch.setattr("ledgerdemain.cli._ingest_harvest_api", failing_harvest)
    monkeypatch.setattr("ledgerdemain.cli._ingest_grasshopper_api", ok_grasshopper)

    code, out, err = _run_cli_main(cfg_path, ["ingest", "all"])

    assert code == 2
    assert "Ingest completed with 1 failed file(s): 01_corrupt.csv" in err
    assert "Ingest completed with 1 failed API step(s): Harvest API" in err
    assert "Grasshopper API: read 0, inserted 0." in out
    assert "Grasshopper API:" not in err
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        assert len(db.list_gusto_payroll_runs()) == 1


def test_cli_ingest_all_api_step_does_not_catch_database_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=True)

    def db_failure(*_args: object, **_kwargs: object) -> tuple[int, int, int]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("ledgerdemain.cli._ingest_harvest_api", db_failure)

    with pytest.raises(sqlite3.OperationalError):
        _run_cli_main(cfg_path, ["ingest", "all"])


def test_cli_gusto_inbox_continues_after_unreadable_csv(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    _write_gusto_inbox(tmp_path)
    unreadable = tmp_path / "inbox/gusto/01_unreadable.csv"
    unreadable.write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")
    unreadable.chmod(0)
    try:
        if os.access(unreadable, os.R_OK):
            pytest.skip("file permissions are not enforced for this user")
        code, out, err = _run_cli_main(cfg_path, ["ingest", "gusto"])
    finally:
        unreadable.chmod(0o600)

    assert code == 2
    assert "Gusto: 1 file(s), read 1, inserted 1 payroll run(s)." in out
    assert "Error ingesting Gusto (01_unreadable.csv):" in err
    assert "Ingest completed with 1 failed file(s): 01_unreadable.csv" in err


def _api_client(body: object) -> HttpClient:
    """HttpClient whose every response is the given JSON body."""

    def handler(
        method: str, url: str, headers: dict[str, str], payload: bytes | None
    ) -> HttpResponse:
        del method, url, headers, payload
        return HttpResponse(200, {}, json.dumps(body).encode("utf-8"))

    return HttpClient(transport=RoutingMockTransport(handler), sleep_fn=lambda _: None)


def _harvest_adapter(body: object) -> HarvestApiAdapter:
    return HarvestApiAdapter(
        HarvestConfig(account_id="harv-1", access_token="tok", user_agent="Ledgerdemain test"),
        http_client=_api_client(body),
    )


def _grasshopper_adapter(body: object) -> GrasshopperApiAdapter:
    return GrasshopperApiAdapter(
        GrasshopperConfig(account_id="acct-99", access_token="gh-tok", header_secret="sec-123"),
        http_client=_api_client(body),
    )


@pytest.mark.parametrize(
    ("fetch", "body", "message"),
    [
        (lambda a: a.fetch_clients(), {"clients": [{"name": "No Id"}]}, "clients entry #0"),
        (lambda a: a.fetch_clients(), ["not", "an", "object"], "must be a JSON object"),
        (lambda a: a.fetch_clients(), {"clients": "oops"}, "must be a list"),
        (lambda a: a.fetch_clients(), {"clients": ["scalar"]}, "clients entry #0"),
        (lambda a: a.fetch_invoices(), {"invoices": [{"number": "1"}]}, "invoices entry #0"),
        (
            lambda a: a.fetch_invoices(),
            {"invoices": [{"id": 1, "client": "Acme"}]},
            "invoices entry #0",
        ),
        (
            lambda a: a.fetch_payments_for_invoice("101"),
            {"invoice_payments": [{"amount": "5.00"}]},
            "invoice_payments entry #0",
        ),
        (lambda a: a.fetch_invoices(), 42, "must be a JSON object"),
        # Error bodies, renamed keys and empty bodies are not zero activity.
        (
            lambda a: a.fetch_clients(),
            {"error": "invalid_token", "error_description": "expired"},
            r"no 'clients' key \(got keys: error, error_description\)",
        ),
        (
            lambda a: a.fetch_invoices(),
            {"invoice_list": [], "next_page": None},
            "no 'invoices' key",
        ),
        (lambda a: a.fetch_payments_for_invoice("101"), {}, r"got keys: none"),
    ],
)
def test_harvest_api_rejects_malformed_payloads(
    fetch: Callable[[HarvestApiAdapter], object], body: object, message: str
) -> None:
    with pytest.raises(HarvestParseError, match=message):
        fetch(_harvest_adapter(body))


@pytest.mark.parametrize(
    ("fetch", "body", "message"),
    [
        (lambda a: a.fetch_transactions(), "oops", "must be a list or object"),
        (lambda a: a.fetch_transactions(), 7, "must be a list or object"),
        (lambda a: a.fetch_transactions(), {"transactions": "oops"}, "must be a list"),
        (
            lambda a: a.fetch_transactions(),
            {
                "transactions": [
                    {
                        "id": "x",
                        "posted_date": "2026-01-10",
                        "amount_cents": ["1"],
                        "direction": "DEBIT",
                    }
                ]
            },
            "Malformed Grasshopper transaction",
        ),
        (lambda a: a.list_accounts(), "oops", "must be a list or object"),
        (
            lambda a: a.fetch_transactions(),
            {"message": "Forbidden: IP not allowlisted"},
            r"none of transactions, data, items \(got keys: message\)",
        ),
        (lambda a: a.fetch_transactions(), {"txns": []}, "none of transactions"),
        (lambda a: a.list_accounts(), {}, r"none of accounts, data, items \(got keys: none\)"),
        (lambda a: a.fetch_transactions(), {"transactions": None}, "must be a list"),
        (lambda a: a.fetch_transactions(), {"transactions": ["x"]}, "item #0 must be an object"),
        (lambda a: a.list_accounts(), [{"id": "a1"}, 3], "item #1 must be an object"),
    ],
)
def test_grasshopper_api_rejects_malformed_payloads(
    fetch: Callable[[GrasshopperApiAdapter], object], body: object, message: str
) -> None:
    with pytest.raises(GrasshopperParseError, match=message):
        fetch(_grasshopper_adapter(body))


def test_cli_ingest_all_records_malformed_api_payloads_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=True)
    _write_gusto_inbox(tmp_path)

    def handler(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
        del method, headers, body
        if "harvest" in url:
            payload: dict[str, object] = {"clients": [{"name": "Missing id"}]}
        else:
            payload = {
                "transactions": [
                    {
                        "id": "gh-bool",
                        "posted_date": "2026-01-10",
                        "amount_cents": True,
                        "direction": "DEBIT",
                    }
                ]
            }
        return HttpResponse(200, {}, json.dumps(payload).encode("utf-8"))

    real_http_client = HttpClient

    def mock_http_client(*_args: object, **_kwargs: object) -> HttpClient:
        return real_http_client(transport=RoutingMockTransport(handler), sleep_fn=lambda _: None)

    monkeypatch.setattr("ledgerdemain.adapters.harvest.HttpClient", mock_http_client)
    monkeypatch.setattr("ledgerdemain.adapters.grasshopper.HttpClient", mock_http_client)

    code, out, err = _run_cli_main(cfg_path, ["ingest", "all"])

    assert code == 2
    assert "Error ingesting Harvest API: Malformed Harvest clients entry #0" in err
    assert "Error ingesting Grasshopper API: Malformed Grasshopper transaction" in err
    assert "Ingest completed with 2 failed API step(s): Harvest API, Grasshopper API" in err
    assert "Traceback" not in err
    assert "Gusto (02_valid.csv): read 1, inserted 1 run(s)." in out
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        assert len(db.list_gusto_payroll_runs()) == 1


def test_cli_capital_one_rejects_csv_and_pdf_files(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    for name in ("co.csv", "co.pdf"):
        path = tmp_path / name
        path.write_text("not supported", encoding="utf-8")
        code, _, err = _run_cli_main(cfg_path, ["ingest", "capital-one", "--file", str(path)])
        assert code != 0
        assert "Unsupported Capital One file type" in err

    cfg = load_config(cfg_path)
    inbox = cfg.general.inbox_dir / "capital_one"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "unsupported.csv").write_text("x", encoding="utf-8")
    (inbox / "unsupported.pdf").write_text("x", encoding="utf-8")
    code, out, err = _run_cli_main(cfg_path, ["ingest", "capital-one"])
    assert code == 2
    assert "Capital One: 0 file(s)" in out
    assert "Error ingesting Capital One (unsupported.csv): Unsupported Capital One file type" in err
    assert "Error ingesting Capital One (unsupported.pdf): Unsupported Capital One file type" in err
    assert "Ingest completed with 2 failed file(s): unsupported.csv, unsupported.pdf" in err


_INBOX_SAMPLES: dict[str, tuple[str, str, str, str]] = {
    # inbox folder -> (ingest argv target, valid file name, valid content, "Unsupported" label)
    "grasshopper": ("grasshopper", "valid.ofx", SAMPLE_OFX_SGML, "Grasshopper"),
    "capital_one": ("capital-one", "valid.ofx", SAMPLE_CAPITAL_ONE_OFX_XML, "Capital One"),
    "gusto": ("gusto", "valid.csv", SAMPLE_GUSTO_PAYROLL_CSV, "Gusto"),
}


@pytest.mark.parametrize("folder", list(_INBOX_SAMPLES))
@pytest.mark.parametrize("command", ["source", "all"])
def test_cli_inbox_reports_unsupported_file_and_ingests_supported(
    tmp_path: Path, folder: str, command: str
) -> None:
    target, valid_name, valid_text, label = _INBOX_SAMPLES[folder]
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    inbox = load_config(cfg_path).general.inbox_dir / folder
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / valid_name).write_text(valid_text, encoding="utf-8")
    (inbox / "statement.pdf").write_text("x", encoding="utf-8")

    argv = ["ingest", "all"] if command == "all" else ["ingest", target]
    code, _, err = _run_cli_main(cfg_path, argv)

    assert code == 2
    assert f"Error ingesting {label} (statement.pdf): Unsupported {label} file type '.pdf'" in err
    assert "Ingest completed with 1 failed file(s): statement.pdf" in err
    assert valid_name not in err
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        if folder == "gusto":
            assert len(db.list_gusto_payroll_runs()) == 1
        else:
            assert len(db.list_bank_transactions(source=folder)) == 2


@pytest.mark.parametrize("command", ["grasshopper", "capital-one", "gusto", "all"])
def test_cli_inbox_ignores_dotfiles_and_subdirectories(tmp_path: Path, command: str) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    root = load_config(cfg_path).general.inbox_dir
    for folder in ("grasshopper", "capital_one", "gusto", "harvest"):
        (root / folder / "archive").mkdir(parents=True, exist_ok=True)
        (root / folder / ".DS_Store").write_bytes(b"\x00")
        (root / folder / "archive" / "old.pdf").write_text("x", encoding="utf-8")

    code, _, err = _run_cli_main(cfg_path, ["ingest", command])

    assert code == 0
    assert err == ""


def test_cli_ingest_all_reports_leftover_harvest_inbox_files(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    inbox = load_config(cfg_path).general.inbox_dir / "harvest"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "invoices.csv").write_text("x", encoding="utf-8")
    (inbox / ".DS_Store").write_bytes(b"\x00")

    code, _, err = _run_cli_main(cfg_path, ["ingest", "all"])

    assert code == 2
    assert "Error ingesting Harvest (invoices.csv): Harvest is ingested through the API" in err
    assert "Ingest completed with 1 failed file(s): invoices.csv" in err


def test_cli_gusto_file_rejects_non_csv_with_clear_error(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    not_csv = tmp_path / "payroll.xlsx"
    not_csv.write_bytes(b"PK\x03\x04 not a csv")

    code, _, err = _run_cli_main(cfg_path, ["ingest", "gusto", "--file", str(not_csv)])

    assert code == 2
    assert "Unsupported Gusto file type '.xlsx' (payroll.xlsx); use a CSV export." in err
    assert "Ingest completed" not in err
    with LedgerDB(tmp_path / "data/ledgerdemain.db") as db:
        assert db.list_gusto_payroll_runs() == []


def _empty_body_client() -> HttpClient:
    """HttpClient whose every response is a 200 with no body."""

    def handler(
        method: str, url: str, headers: dict[str, str], payload: bytes | None
    ) -> HttpResponse:
        del method, url, headers, payload
        return HttpResponse(200, {}, b"")

    return HttpClient(transport=RoutingMockTransport(handler), sleep_fn=lambda _: None)


def test_api_adapters_reject_empty_response_body() -> None:
    harvest = HarvestApiAdapter(
        HarvestConfig(account_id="harv-1", access_token="tok", user_agent="Ledgerdemain test"),
        http_client=_empty_body_client(),
    )
    with pytest.raises(HarvestParseError, match="no 'invoices' key"):
        harvest.fetch_invoices()

    grasshopper = GrasshopperApiAdapter(
        GrasshopperConfig(account_id="acct-99", access_token="gh-tok", header_secret="sec-123"),
        http_client=_empty_body_client(),
    )
    with pytest.raises(GrasshopperParseError, match="none of transactions"):
        grasshopper.fetch_transactions()


@pytest.mark.parametrize(
    ("fetch", "body"),
    [
        (lambda a: a.fetch_clients(), {"clients": [], "next_page": None}),
        (lambda a: a.fetch_invoices(), {"invoices": [], "next_page": None}),
        (lambda a: a.fetch_payments_for_invoice("101"), {"invoice_payments": []}),
    ],
)
def test_harvest_api_accepts_zero_activity(
    fetch: Callable[[HarvestApiAdapter], list[object]], body: object
) -> None:
    assert fetch(_harvest_adapter(body)) == []


@pytest.mark.parametrize(
    ("fetch", "body"),
    [
        (lambda a: a.fetch_transactions(), {"transactions": []}),
        (lambda a: a.fetch_transactions(), {"data": [], "next_cursor": None}),
        (lambda a: a.fetch_transactions(), []),
        (lambda a: a.list_accounts(), {"accounts": []}),
    ],
)
def test_grasshopper_api_accepts_zero_activity(
    fetch: Callable[[GrasshopperApiAdapter], list[object]], body: object
) -> None:
    assert fetch(_grasshopper_adapter(body)) == []
