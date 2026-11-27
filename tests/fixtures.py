"""Shared test fixtures, synthetic PDF/CSV/OFX data, and mock HTTP/Wave transports."""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ledgerdemain.config import AppConfig, load_config
from ledgerdemain.http_client import HttpResponse
from ledgerdemain.money import ZERO, parse_money


def create_synthetic_pdf(lines: list[str], path: Path) -> None:
    """Create a valid PDF 1.4 file with a FlateDecode content stream."""
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    stream_cmds = ["BT", "/F1 10 Tf", "50 750 Td", "14 TL"]
    for idx, line in enumerate(escaped):
        if idx == 0:
            stream_cmds.append(f"({line}) Tj")
        else:
            stream_cmds.append(f"({line}) '")
    stream_cmds.append("ET")
    raw_stream = "\n".join(stream_cmds).encode("latin-1")
    compressed = zlib.compress(raw_stream)

    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>\nendobj\n"
        ),
        b"4 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
        (
            f"5 0 obj\n<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode(
                "ascii"
            )
            + compressed
            + b"\nendstream\nendobj\n"
        ),
    ]

    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for obj in objects:
        offsets.append(len(buf))
        buf.extend(obj)

    xref_pos = len(buf)
    buf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    buf.extend(
        (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n").encode(
            "ascii"
        )
    )
    path.write_bytes(bytes(buf))


def write_test_workspace_config(
    tmp_dir: Path,
    *,
    mirror_harvest_invoices: bool = True,
    deposit_strategy: str = "ar_clearing",
    transfer_strategy: str = "manual_transfer",
    include_api_creds: bool = True,
) -> Path:
    """Write a complete test ledgerdemain.toml and rules.toml in tmp_dir."""
    cfg_path = tmp_dir / "ledgerdemain.toml"
    rules_path = tmp_dir / "rules.toml"

    wave_biz = "biz-test-1" if include_api_creds else ""
    wave_tok = "wave-secret-token" if include_api_creds else ""
    harv_acct = "harv-acct-1" if include_api_creds else ""
    harv_tok = "harv-secret-token" if include_api_creds else ""
    gh_acct = "gh-acct-1" if include_api_creds else ""
    gh_tok = "gh-secret-token" if include_api_creds else ""
    gh_sec = "gh-header-secret" if include_api_creds else ""

    cfg_path.write_text(
        f"""[general]
db_path = "data/ledgerdemain.db"
inbox_dir = "inbox"
rules_path = "rules.toml"
currency = "USD"

[accounting]
mirror_harvest_invoices = {"true" if mirror_harvest_invoices else "false"}
mirrored_invoice_deposit_strategy = "{deposit_strategy}"
wave_invoice_create_status = "SAVED"
transfer_strategy = "{transfer_strategy}"
deposit_match_window_days = 7
payroll_match_window_days = 5
transfer_match_window_days = 5
auto_confirm_threshold = 0.90

[wave]
endpoint = "https://gql.waveapps.com/graphql/public"
business_id = "{wave_biz}"
access_token = "{wave_tok}"
default_product_name = "Consulting Services"

[harvest]
base_url = "https://api.harvestapp.com/v2"
account_id = "{harv_acct}"
access_token = "{harv_tok}"
user_agent = "LedgerdemainTest (ops@example.com)"

[grasshopper]
base_url = "https://api.grasshopper.bank/v1"
account_id = "{gh_acct}"
access_token = "{gh_tok}"
header_secret = "{gh_sec}"
header_secret_name = "X-API-Secret"

[accounts]
grasshopper_checking = "Grasshopper Checking"
capital_one_card = "Capital One Credit Card"
revenue_default = "Sales"
invoice_payments_clearing = "Invoice Payments Clearing"
merchant_fees = "Merchant Processing Fees"
payroll_gross_wages = "Payroll - Gross Wages"
payroll_reimbursements = "Employee Reimbursements"
payroll_employer_taxes = "Payroll - Employer Taxes"
payroll_employer_benefits = "Payroll - Employer Benefits"
payroll_taxes_payable = "Payroll Taxes Payable"
payroll_benefits_payable = "Payroll Benefits Payable"
payroll_processing_fees = "Payroll Processing Fees"
contractors_expense = "Contractors"
cc_transfer_clearing = "Credit Card Payment Clearing"
software_subscriptions = "Software & Subscriptions"
cloud_hosting = "Cloud Hosting & Infrastructure"
office_supplies = "Office Supplies"
travel = "Travel"
meals = "Meals & Entertainment"
bank_fees = "Bank Service Charges"
uncategorized_expense = "Uncategorized Expense"

[patterns]
gusto_payroll = ["(?i)\\\\bgusto\\\\b"]
gusto_fee = ["(?i)gusto\\\\s*(fee|sub|invoice|monthly)"]
grasshopper_cc_payment = ["(?i)capital\\\\s*one", "(?i)cap\\\\s*one", "(?i)crcardpmt"]
capital_one_payment = [
  "(?i)capital\\\\s*one\\\\s*(autopay|online)\\\\s*pymt",
  "(?i)payment\\\\s*-?\\\\s*thank\\\\s*you",
  "(?i)^autopay\\\\b",
]
""",
        encoding="utf-8",
    )

    rules_path.write_text(
        """[[rules]]
name = "Gusto Monthly Fee"
priority = 10
source = "grasshopper"
direction = "WITHDRAWAL"
description_regex = "(?i)gusto\\\\s*(fee|sub|invoice|monthly)"
account_key = "payroll_processing_fees"

[[rules]]
name = "Cloud Hosting Vendors"
priority = 20
direction = "WITHDRAWAL"
description_regex = "(?i)(amazon\\\\s*web\\\\s*services|aws\\\\b|github|google\\\\s*cloud)"
account_key = "cloud_hosting"

[[rules]]
name = "Software Subscriptions"
priority = 30
direction = "WITHDRAWAL"
description_regex = "(?i)(figma|slack|notion|linear)"
account_key = "software_subscriptions"

[[rules]]
name = "Software Refunds"
priority = 35
source = "capital_one"
direction = "DEPOSIT"
description_regex = "(?i)(figma|slack|notion|linear|github|aws)"
account_key = "software_subscriptions"

[[rules]]
name = "Travel & Lodging"
priority = 50
direction = "WITHDRAWAL"
description_regex = "(?i)(united\\\\s*air|delta\\\\s*air|american\\\\s*air|southwest|marriott)"
account_key = "travel"
""",
        encoding="utf-8",
    )
    return cfg_path


def make_test_config(tmp_dir: Path, **kwargs: Any) -> AppConfig:
    """Create and load an AppConfig rooted at tmp_dir."""
    return load_config(write_test_workspace_config(tmp_dir, **kwargs))


SAMPLE_CAPITAL_ONE_CSV = """Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2026-01-03,2026-01-04,1234,AMAZON WEB SERVICES,Merchandise,142.50,
2026-01-05,2026-01-05,1234,BLUE BOTTLE COFFEE,Dining,6.25,
2026-01-05,2026-01-05,1234,BLUE BOTTLE COFFEE,Dining,6.25,
2026-01-06,2026-01-07,1234,FIGMA.COM,Merchandise,,15.00
2026-01-08,2026-01-08,1234,MYSTERY HARDWARE CO,Merchandise,88.00,
2026-01-12,2026-01-12,1234,CAPITAL ONE AUTOPAY PYMT,Payment/Credit,,500.00
"""

SAMPLE_CAPITAL_ONE_PDF_LINES = [
    "Capital One Spark Business Statement",
    "Billing Period: Dec 15, 2025 - Jan 14, 2026",
    "Card Ending in 4321",
    "Payments, Credits and Adjustments",
    "Dec 20 Dec 21 CAPITAL ONE ONLINE PYMT -$250.00",
    "Jan 02 Jan 02 SLACK REFUND $25.00",
    "Transactions",
    "Dec 28 Dec 29 GITHUB INC SAN FRANCISCO CA $45.00",
    "Jan 04 Jan 05 DELTA AIR LINES ATLANTA GA $320.00",
    "Total Transactions $365.00",
]

SAMPLE_GRASSHOPPER_CSV = """Grasshopper Business Checking - Activity Export
Account: Operating **9012

Posted Date,Transaction Date,Description,Debit,Credit,Category
2026-01-05,2026-01-05,ACH DEPOSIT ACME CORP INV-1001,,3000.00,Deposit
2026-01-09,2026-01-09,GUSTO FEE MONTHLY SUBSCRIPTION,45.00,,Software
2026-01-10,2026-01-10,GUSTO PAY 260110,3705.75,,Payroll
2026-01-10,2026-01-10,GUSTO TAX 260110,1315.50,,Payroll Tax
2026-01-12,2026-01-12,CAPITAL ONE AUTOPAY ACH,500.00,,Transfer
2026-01-15,2026-01-15,STRIPE TRANSFER WIDGET LABS,,1940.00,Deposit
"""

SAMPLE_HARVEST_CSV = (
    "\n".join(
        [
            "Invoice #,Client,Issue Date,Due Date,Paid Date,Status,Subtotal,Tax,Amount,Balance,"
            "Subject",
            "1001,Acme Corp,2025-12-20,2026-01-19,2026-01-05,Paid,3000.00,0.00,3000.00,0.00,"
            "December Consulting",
            "1002,Widget Labs,2026-01-02,2026-02-01,2026-01-15,Paid,2000.00,0.00,2000.00,0.00,"
            "January Sprint",
            "1003,Beta Partners,2026-01-10,2026-02-09,,Open,1500.00,0.00,1500.00,1500.00,"
            "Strategy Retainer",
        ]
    )
    + "\n"
)

SAMPLE_GUSTO_PAYROLL_CSV = (
    "\n".join(
        [
            "Company: Example Studio LLC",
            "Report: Payroll Journal",
            "Period: Jan 2026",
            "",
            "Employee,Check Date,Pay Period Start,Pay Period End,Gross Earnings,Reimbursements,"
            "Federal Income Tax,Employee Social Security,Employee Medicare,"
            "Employer Social Security,Employer Medicare,FUTA,Employee Deductions,"
            "Employer Contributions,Net Pay",
            "Alice Smith,2026-01-10,2026-01-01,2026-01-15,3000.00,150.00,350.00,186.00,43.50,"
            "186.00,43.50,18.00,0.00,0.00,2570.50",
            "Bob Jones,2026-01-10,2026-01-01,2026-01-15,1500.00,0.00,250.00,93.00,21.75,"
            "93.00,21.75,9.00,0.00,0.00,1135.25",
            "Totals,2026-01-10,,,4500.00,150.00,600.00,279.00,65.25,279.00,65.25,27.00,"
            "0.00,0.00,3705.75",
        ]
    )
    + "\n"
)

SAMPLE_GUSTO_CONTRACTOR_CSV = """Contractor,Payment Date,Payment Amount,Method
Dana Designer,2026-01-18,1200.00,Direct Deposit
"""

SAMPLE_OFX_SGML = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260120120000
<TRNAMT>-75.00
<FITID>OFX-GH-1
<NAME>GITHUB INC
<MEMO>SUBSCRIPTION
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260121120000
<TRNAMT>1500.00
<FITID>OFX-GH-2
<NAME>BETA PARTNERS ACH
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""

SAMPLE_CAPITAL_ONE_OFX_XML = """<?xml version="1.0" encoding="utf-8" ?>
<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
<DTSERVER>20260201120000.000</DTSERVER>
<LANGUAGE>ENG</LANGUAGE>
</SONRS>
</SIGNONMSGSRSV1>
<CREDITCARDMSGSRSV1>
<CCSTMTTRNRS>
<TRNUID>0</TRNUID>
<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
<CCSTMTRS>
<CURDEF>USD</CURDEF>
<CCACCTFROM>
<ACCTID>9999</ACCTID>
</CCACCTFROM>
<BANKTRANLIST>
<DTSTART>20260101000000.000</DTSTART>
<DTEND>20260131000000.000</DTEND>
<STMTTRN>
   <TRNTYPE>DEBIT</TRNTYPE>
   <DTPOSTED>20260110000000.000</DTPOSTED>
   <DTUSER>20260108000000.000</DTUSER>
   <TRNAMT>-42.50</TRNAMT>
   <FITID>FIT-C1-001</FITID>
   <NAME>CLOUD VENDOR LLC</NAME>
   <CCACCTTO>
      <ACCTID>8888</ACCTID>
   </CCACCTTO>
   <MEMO>CLOUD VENDOR LLC</MEMO>
</STMTTRN>
<STMTTRN>
   <TRNTYPE>CREDIT</TRNTYPE>
   <DTPOSTED>20260115000000.000</DTPOSTED>
   <DTUSER>20260115000000.000</DTUSER>
   <TRNAMT>500.00</TRNAMT>
   <FITID>FIT-C1-002</FITID>
   <NAME>CAPITAL ONE ONLINE PYMT</NAME>
   <MEMO>CAPITAL ONE ONLINE PYMT</MEMO>
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>-1234.56</BALAMT>
<DTASOF>20260201120000.000</DTASOF>
</LEDGERBAL>
<AVAILBAL>
<BALAMT>8765.44</BALAMT>
<DTASOF>20260201120000.000</DTASOF>
</AVAILBAL>
</CCSTMTRS>
</CCSTMTTRNRS>
</CREDITCARDMSGSRSV1>
</OFX>
"""


class MockWaveTransport:
    """In-memory mock GraphQL server simulating Wave Accounting validation rules."""

    def __init__(self, *, is_classic: bool = False) -> None:
        self.is_classic = is_classic
        self.calls: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = [
            self._acc("acc-gh", "Grasshopper Checking", "ASSET", "CASH_AND_BANK", "DEBIT"),
            self._acc("acc-co", "Capital One Credit Card", "LIABILITY", "CREDIT_CARD", "CREDIT"),
            self._acc("acc-rev", "Sales", "INCOME", "INCOME", "CREDIT"),
            self._acc(
                "acc-ar-clr", "Invoice Payments Clearing", "ASSET", "OTHER_CURRENT_ASSET", "DEBIT"
            ),
            self._acc("acc-fees", "Merchant Processing Fees", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-wages", "Payroll - Gross Wages", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-reimb", "Employee Reimbursements", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-ertax", "Payroll - Employer Taxes", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-erben", "Payroll - Employer Benefits", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc(
                "acc-taxpay",
                "Payroll Taxes Payable",
                "LIABILITY",
                "OTHER_CURRENT_LIABILITY",
                "CREDIT",
            ),
            self._acc(
                "acc-benpay",
                "Payroll Benefits Payable",
                "LIABILITY",
                "OTHER_CURRENT_LIABILITY",
                "CREDIT",
            ),
            self._acc("acc-payfee", "Payroll Processing Fees", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-contr", "Contractors", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc(
                "acc-cc-clr",
                "Credit Card Payment Clearing",
                "ASSET",
                "OTHER_CURRENT_ASSET",
                "DEBIT",
            ),
            self._acc("acc-soft", "Software & Subscriptions", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-cloud", "Cloud Hosting & Infrastructure", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-meals", "Meals & Entertainment", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-travel", "Travel", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-office", "Office Supplies", "EXPENSE", "EXPENSE", "DEBIT"),
            self._acc("acc-bankfee", "Bank Service Charges", "EXPENSE", "EXPENSE", "DEBIT"),
        ]
        self.customers: list[dict[str, Any]] = [
            {"id": "cust-acme", "name": "Acme Corp", "email": "ap@acme.example"}
        ]
        self.products: list[dict[str, Any]] = []
        self.invoices: list[dict[str, Any]] = []
        self.transactions: list[dict[str, Any]] = []
        self.seen_external_ids: set[str] = set()
        self.fail_next_tx_error: str | None = None
        self.rate_limit_once: bool = False

    @staticmethod
    def _acc(wave_id: str, name: str, acc_type: str, subtype: str, normal: str) -> dict[str, Any]:
        return {
            "id": wave_id,
            "name": name,
            "description": "",
            "type": {"name": acc_type, "value": acc_type, "normalBalanceType": normal},
            "subtype": {"name": subtype, "value": subtype},
            "isArchived": False,
        }

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        if self.rate_limit_once:
            self.rate_limit_once = False
            return HttpResponse(
                status=429,
                headers={"Retry-After": "0"},
                body=b'{"message":"Rate limit"}',
            )

        payload = json.loads((body or b"{}").decode("utf-8"))
        query = payload.get("query", "")
        variables = payload.get("variables") or {}
        self.calls.append({"url": url, "query": query, "variables": variables})

        if "GetBusiness" in query:
            return self._json_ok(
                {
                    "business": {
                        "id": variables["businessId"],
                        "name": "Example Studio LLC",
                        "isClassicAccounting": self.is_classic,
                        "currency": {"code": "USD"},
                    }
                }
            )
        if "ListAccounts" in query:
            return self._conn_ok("accounts", self.accounts)
        if "ListCustomers" in query:
            return self._conn_ok("customers", self.customers)
        if "ListProducts" in query:
            return self._conn_ok("products", self.products)
        if "CustomerCreate" in query:
            inp = variables["input"]
            node = {
                "id": f"cust-{len(self.customers) + 1}",
                "name": inp["name"],
                "email": inp.get("email", ""),
            }
            self.customers.append(node)
            return self._json_ok(
                {"customerCreate": {"didSucceed": True, "inputErrors": [], "customer": node}}
            )
        if "ProductCreate" in query:
            inp = variables["input"]
            node = {
                "id": f"prod-{len(self.products) + 1}",
                "name": inp["name"],
                "unitPrice": inp.get("unitPrice", "0.00"),
                "incomeAccount": {"id": inp["incomeAccountId"], "name": "Sales"},
            }
            self.products.append(node)
            return self._json_ok(
                {"productCreate": {"didSucceed": True, "inputErrors": [], "product": node}}
            )
        if "InvoiceCreate" in query:
            inp = variables["input"]
            for idx, item in enumerate(inp.get("items", [])):
                if "price" in item or "unitPrice" not in item:
                    return self._json_ok(
                        {
                            "invoiceCreate": {
                                "didSucceed": False,
                                "inputErrors": [
                                    {
                                        "path": ["input", "items", idx],
                                        "code": "INVALID_FIELD",
                                        "message": "InvoiceCreateItemInput requires unitPrice",
                                    }
                                ],
                                "invoice": None,
                            }
                        }
                    )
            node = {
                "id": f"winv-{len(self.invoices) + 1}",
                "invoiceNumber": inp.get("invoiceNumber", ""),
                "invoiceDate": inp.get("invoiceDate", ""),
                "dueDate": inp.get("dueDate", ""),
                "status": inp.get("status", "SAVED"),
                "total": {"value": "0.00"},
                "customer": {"id": inp["customerId"], "name": ""},
            }
            self.invoices.append(node)
            return self._json_ok(
                {"invoiceCreate": {"didSucceed": True, "inputErrors": [], "invoice": node}}
            )
        if "MoneyTransactionCreate" in query:
            inp = variables["input"]
            if self.fail_next_tx_error:
                msg = self.fail_next_tx_error
                self.fail_next_tx_error = None
                return self._json_ok(
                    {
                        "moneyTransactionCreate": {
                            "didSucceed": False,
                            "inputErrors": [
                                {"path": ["input"], "code": "REJECTED", "message": msg}
                            ],
                            "transaction": None,
                        }
                    }
                )
            if "status" in inp:
                return self._json_ok(
                    {
                        "moneyTransactionCreate": {
                            "didSucceed": False,
                            "inputErrors": [
                                {
                                    "path": ["input", "status"],
                                    "code": "UNKNOWN_FIELD",
                                    "message": "status is not valid",
                                }
                            ],
                            "transaction": None,
                        }
                    }
                )
            ext_id = inp["externalId"]
            if ext_id in self.seen_external_ids:
                return self._json_ok(
                    {
                        "moneyTransactionCreate": {
                            "didSucceed": False,
                            "inputErrors": [
                                {
                                    "path": ["input", "externalId"],
                                    "code": "DUPLICATE",
                                    "message": "Duplicate externalId",
                                }
                            ],
                            "transaction": None,
                        }
                    }
                )
            # Server-side double-entry check
            acc_normals = {a["id"]: a["type"]["normalBalanceType"] for a in self.accounts}
            anchor = inp["anchor"]
            anchor_amt = parse_money(str(anchor["amount"]))
            net_debit = ZERO
            for li in inp["lineItems"]:
                if li["accountId"] == anchor["accountId"]:
                    return self._json_ok(
                        {
                            "moneyTransactionCreate": {
                                "didSucceed": False,
                                "inputErrors": [
                                    {
                                        "path": ["input", "lineItems"],
                                        "code": "ANCHOR_IN_LINE",
                                        "message": "Anchor in lineItems",
                                    }
                                ],
                                "transaction": None,
                            }
                        }
                    )
                amt = parse_money(str(li["amount"]))
                normal = acc_normals[li["accountId"]]
                if normal == "DEBIT":
                    net_debit += amt if li["balance"] == "INCREASE" else -amt
                else:
                    net_debit += -amt if li["balance"] == "INCREASE" else amt

            expected = anchor_amt if anchor["direction"] == "WITHDRAWAL" else -anchor_amt
            if net_debit != expected:
                return self._json_ok(
                    {
                        "moneyTransactionCreate": {
                            "didSucceed": False,
                            "inputErrors": [
                                {
                                    "path": ["input", "lineItems"],
                                    "code": "UNBALANCED",
                                    "message": f"Expected {expected}, got {net_debit}",
                                }
                            ],
                            "transaction": None,
                        }
                    }
                )

            self.seen_external_ids.add(ext_id)
            node = {"id": f"wtx-{len(self.transactions) + 1}"}
            self.transactions.append({"node": node, "input": inp})
            return self._json_ok(
                {
                    "moneyTransactionCreate": {
                        "didSucceed": True,
                        "inputErrors": [],
                        "transaction": node,
                    }
                }
            )

        return HttpResponse(
            status=400, headers={}, body=b'{"errors":[{"message":"Unsupported query"}]}'
        )

    @staticmethod
    def _json_ok(data: dict[str, Any]) -> HttpResponse:
        return HttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=json.dumps({"data": data}).encode("utf-8"),
        )

    def _conn_ok(self, field: str, nodes: list[dict[str, Any]]) -> HttpResponse:
        return self._json_ok(
            {
                "business": {
                    "id": "biz-test-1",
                    field: {
                        "pageInfo": {"currentPage": 1, "totalPages": 1, "totalCount": len(nodes)},
                        "edges": [{"node": n} for n in nodes],
                    },
                }
            }
        )


HandlerFn = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


class RoutingMockTransport:
    """HTTP transport that answers every request with ``handler(method, url, headers, body)``."""

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


# One OFX statement entry: (DTPOSTED, DTUSER, signed TRNAMT, NAME, FITID). Like a real bank file,
# TRNTYPE is DEBIT for a negative amount and CREDIT for a positive one. Merchant categories are
# not part of OFX, so category-based rules (see #35) cannot match these entries.
OfxEntry = tuple[str, str, str, str, str]


def build_ofx_xml(section: str, acct_from: str, acct_id: str, entries: list[OfxEntry]) -> str:
    """Build an OFX 2.x statement. ``section`` is ``BANK`` or ``CC``; amounts are signed."""
    stmt_tag = "STMTRS" if section == "BANK" else "CCSTMTRS"
    trn_tag = "STMTTRNRS" if section == "BANK" else "CCSTMTTRNRS"
    body = "\n".join(
        "<STMTTRN>"
        f"<TRNTYPE>{'DEBIT' if amt.startswith('-') else 'CREDIT'}</TRNTYPE>"
        f"<DTPOSTED>{posted}000000</DTPOSTED><DTUSER>{user}000000</DTUSER>"
        f"<TRNAMT>{amt}</TRNAMT><FITID>{escape(fitid)}</FITID><NAME>{escape(name)}</NAME>"
        "</STMTTRN>"
        for posted, user, amt, name, fitid in entries
    )
    return f"""<?xml version="1.0" encoding="utf-8" ?>
<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
<{section}MSGSRSV1>
<{trn_tag}>
<TRNUID>0</TRNUID>
<{stmt_tag}>
<CURDEF>USD</CURDEF>
<{acct_from}><ACCTID>{acct_id}</ACCTID></{acct_from}>
<BANKTRANLIST>
{body}
</BANKTRANLIST>
</{stmt_tag}>
</{trn_tag}>
</{section}MSGSRSV1>
</OFX>
"""


# The e2e workflow data below mirrors SAMPLE_GRASSHOPPER_CSV, SAMPLE_CAPITAL_ONE_CSV and
# SAMPLE_CAPITAL_ONE_PDF_LINES (same dates, descriptions, amounts, directions and card last-4),
# expressed as OFX so the e2e test does not depend on the CSV/PDF adapters. The CSV and PDF
# merchant categories are dropped because OFX does not carry them.
E2E_GRASSHOPPER_OFX = build_ofx_xml(
    "BANK",
    "BANKACCTFROM",
    "9012",
    [
        ("20260105", "20260105", "3000.00", "ACH DEPOSIT ACME CORP INV-1001", "GH-1"),
        ("20260109", "20260109", "-45.00", "GUSTO FEE MONTHLY SUBSCRIPTION", "GH-2"),
        ("20260110", "20260110", "-3705.75", "GUSTO PAY 260110", "GH-3"),
        ("20260110", "20260110", "-1315.50", "GUSTO TAX 260110", "GH-4"),
        ("20260112", "20260112", "-500.00", "CAPITAL ONE AUTOPAY ACH", "GH-5"),
        ("20260115", "20260115", "1940.00", "STRIPE TRANSFER WIDGET LABS", "GH-6"),
    ],
)

E2E_CAPITAL_ONE_JAN_OFX = build_ofx_xml(
    "CC",
    "CCACCTFROM",
    "1234",
    [
        ("20260104", "20260103", "-142.50", "AMAZON WEB SERVICES", "C1-1"),
        ("20260105", "20260105", "-6.25", "BLUE BOTTLE COFFEE", "C1-2"),
        ("20260105", "20260105", "-6.25", "BLUE BOTTLE COFFEE", "C1-3"),
        ("20260107", "20260106", "15.00", "FIGMA.COM", "C1-4"),
        ("20260108", "20260108", "-88.00", "MYSTERY HARDWARE CO", "C1-5"),
        ("20260112", "20260112", "500.00", "CAPITAL ONE AUTOPAY PYMT", "C1-6"),
    ],
)

E2E_CAPITAL_ONE_STATEMENT_OFX = build_ofx_xml(
    "CC",
    "CCACCTFROM",
    "4321",
    [
        ("20251221", "20251220", "250.00", "CAPITAL ONE ONLINE PYMT", "C2-1"),
        ("20260102", "20260102", "25.00", "SLACK REFUND", "C2-2"),
        ("20251229", "20251228", "-45.00", "GITHUB INC SAN FRANCISCO CA", "C2-3"),
        ("20260105", "20260104", "-320.00", "DELTA AIR LINES ATLANTA GA", "C2-4"),
    ],
)

# Harvest API v2 payloads for the same three invoices as SAMPLE_HARVEST_CSV.
E2E_HARVEST_CLIENTS: dict[str, Any] = {
    "clients": [
        {"id": 1, "name": "Acme Corp", "currency": "USD", "is_active": True},
        {"id": 2, "name": "Widget Labs", "currency": "USD", "is_active": True},
        {"id": 3, "name": "Beta Partners", "currency": "USD", "is_active": True},
    ],
    "next_page": None,
}


def _harvest_invoice(
    inv_id: int,
    client_id: int,
    client: str,
    issue: str,
    due: str,
    paid: str | None,
    state: str,
    amount: str,
    due_amount: str,
    subject: str,
) -> dict[str, Any]:
    return {
        "id": inv_id,
        "number": str(1000 + inv_id),
        "client": {"id": client_id, "name": client},
        "issue_date": issue,
        "due_date": due,
        "paid_date": paid,
        "state": state,
        "currency": "USD",
        "subtotal": amount,
        "tax_amount": 0,
        "amount": amount,
        "due_amount": due_amount,
        "subject": subject,
        "line_items": [
            {
                "kind": "Service",
                "description": subject,
                "quantity": 1,
                "unit_price": amount,
                "amount": amount,
                "taxed": False,
            }
        ],
    }


# One payment each for the two paid invoices, served by /invoices/<id>/payments.
E2E_HARVEST_PAYMENTS: dict[int, dict[str, Any]] = {
    1: {
        "invoice_payments": [
            {"id": 501, "amount": "3000.00", "paid_date": "2026-01-05", "notes": "ACH"}
        ],
        "next_page": None,
    },
    2: {
        "invoice_payments": [
            {"id": 502, "amount": "2000.00", "paid_date": "2026-01-15", "notes": "Stripe"}
        ],
        "next_page": None,
    },
}

E2E_HARVEST_INVOICES: dict[str, Any] = {
    "invoices": [
        _harvest_invoice(
            1,
            1,
            "Acme Corp",
            "2025-12-20",
            "2026-01-19",
            "2026-01-05",
            "paid",
            "3000.00",
            "0.00",
            "December Consulting",
        ),
        _harvest_invoice(
            2,
            2,
            "Widget Labs",
            "2026-01-02",
            "2026-02-01",
            "2026-01-15",
            "paid",
            "2000.00",
            "0.00",
            "January Sprint",
        ),
        _harvest_invoice(
            3,
            3,
            "Beta Partners",
            "2026-01-10",
            "2026-02-09",
            None,
            "open",
            "1500.00",
            "1500.00",
            "Strategy Retainer",
        ),
    ],
    "next_page": None,
}
