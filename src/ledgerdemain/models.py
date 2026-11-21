"""Core domain models and exact decimal money helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")

_PARENS_RE = re.compile(r"^\((.+)\)$")


def to_decimal(
    value: Decimal | int | float | str | None,
    default: Decimal | None = None,
) -> Decimal:
    """Parse a numeric or currency string into a 2-decimal-place Decimal."""
    if value is None:
        if default is not None:
            return default.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        raise ValueError("Cannot parse None into Decimal without default")

    if isinstance(value, Decimal):
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if isinstance(value, int):
        return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    if isinstance(value, float):
        return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    text = value.strip()
    if not text or text in {"-", "--", "N/A", "n/a"}:
        if default is not None:
            return default.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        raise ValueError(f"Empty monetary value: {value!r}")

    negative = False
    parens_match = _PARENS_RE.match(text)
    if parens_match:
        negative = True
        text = parens_match.group(1).strip()

    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    elif text.startswith("+"):
        text = text[1:]

    try:
        dec = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary value: {value!r}") from exc

    if negative:
        dec = -dec
    return dec.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def fmt_decimal(value: Decimal) -> str:
    """Format a Decimal cleanly as a 2-decimal string."""
    return f"{value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP):.2f}"


@dataclass(slots=True)
class BankTransaction:
    """Normalized bank or credit card transaction."""

    source: str
    account_key: str
    external_id: str | None
    fingerprint: str
    tx_date: str
    posted_date: str
    description: str
    normalized_description: str
    amount: Decimal
    direction: str  # "DEPOSIT" or "WITHDRAWAL"
    source_category: str = ""
    card_last4: str = ""
    status: str = "posted"
    raw: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass(slots=True)
class HarvestClient:
    """Harvest customer/client record."""

    harvest_id: str
    name: str
    currency: str = "USD"
    is_active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HarvestInvoiceLineItem:
    """Single line item on a Harvest invoice."""

    kind: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    taxed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize line item for JSON storage."""
        return {
            "kind": self.kind,
            "description": self.description,
            "quantity": str(self.quantity),
            "unit_price": fmt_decimal(self.unit_price),
            "amount": fmt_decimal(self.amount),
            "taxed": self.taxed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HarvestInvoiceLineItem:
        """Deserialize line item from stored JSON."""
        return cls(
            kind=str(data.get("kind", "")),
            description=str(data.get("description", "")),
            quantity=Decimal(str(data.get("quantity", "1"))),
            unit_price=to_decimal(data.get("unit_price", "0.00")),
            amount=to_decimal(data.get("amount", "0.00")),
            taxed=bool(data.get("taxed", False)),
        )


@dataclass(slots=True)
class HarvestInvoice:
    """Harvest invoice record."""

    harvest_id: str
    invoice_number: str
    client_harvest_id: str
    client_name: str
    issue_date: str
    due_date: str
    paid_date: str | None
    state: str
    currency: str
    subtotal: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    due_amount: Decimal
    subject: str = ""
    line_items: list[HarvestInvoiceLineItem] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass(slots=True)
class HarvestPayment:
    """Harvest payment recorded against an invoice."""

    harvest_id: str
    invoice_harvest_id: str
    paid_date: str
    amount: Decimal
    payment_gateway: str = ""
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass(slots=True)
class GustoPayrollRun:
    """Aggregated Gusto payroll or contractor run."""

    fingerprint: str
    check_date: str
    pay_period_start: str
    pay_period_end: str
    run_type: str  # "regular", "off_cycle", "contractor"
    gross_wages: Decimal
    reimbursements: Decimal
    employee_taxes: Decimal
    employer_taxes: Decimal
    employee_benefits: Decimal
    employer_benefits: Decimal
    net_pay: Decimal
    tax_breakdown: dict[str, str] = field(default_factory=dict)
    employee_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    @property
    def net_disbursement(self) -> Decimal:
        """Total direct deposit / check cash paid to employees or contractors."""
        return (self.net_pay + self.reimbursements).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    @property
    def total_tax_debit(self) -> Decimal:
        """Combined employee + employer tax debit pulled by Gusto."""
        return (self.employee_taxes + self.employer_taxes).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

    @property
    def total_benefits_debit(self) -> Decimal:
        """Combined employee + employer benefit debit."""
        return (self.employee_benefits + self.employer_benefits).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

    @property
    def combined_net_and_tax_debit(self) -> Decimal:
        """Combined cash + tax withdrawal when Gusto drafts a single lump sum."""
        return (self.net_disbursement + self.total_tax_debit).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )

    @property
    def total_all_in_debit(self) -> Decimal:
        """All-in cash + tax + benefits withdrawal."""
        return (self.net_disbursement + self.total_tax_debit + self.total_benefits_debit).quantize(
            TWO_PLACES, rounding=ROUND_HALF_UP
        )


@dataclass(slots=True)
class ReconciliationMatch:
    """Cross-source match tying bank transactions to invoices, payroll, or transfers."""

    match_type: str
    bank_tx_id: int
    counterparty_bank_tx_id: int | None = None
    harvest_invoice_id: int | None = None
    harvest_payment_id: int | None = None
    gusto_run_id: int | None = None
    matched_amount: Decimal = ZERO
    confidence: float = 0.0
    reason: str = ""
    status: str = "proposed"  # "proposed", "confirmed", "rejected"
    id: int | None = None


@dataclass(slots=True)
class WaveAccount:
    """Cached Wave Chart of Accounts entry."""

    wave_id: str
    name: str
    account_type: str
    account_subtype: str
    normal_balance_type: str  # "DEBIT" or "CREDIT"
    is_archived: bool = False
    description: str = ""


@dataclass(slots=True)
class WaveCustomer:
    """Cached Wave customer."""

    wave_id: str
    name: str
    email: str = ""


@dataclass(slots=True)
class WaveProduct:
    """Cached Wave product/service."""

    wave_id: str
    name: str
    unit_price: Decimal = ZERO
    income_account_id: str = ""


@dataclass(slots=True)
class StagedLineItem:
    """Single categorization leg of a staged Wave money transaction."""

    account_key: str
    wave_account_id: str | None
    amount: Decimal
    balance: str  # "INCREASE" or "DECREASE"
    description: str = ""
    customer_name: str | None = None
    wave_customer_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize line item for SQLite JSON column."""
        return {
            "account_key": self.account_key,
            "wave_account_id": self.wave_account_id,
            "amount": fmt_decimal(self.amount),
            "balance": self.balance,
            "description": self.description,
            "customer_name": self.customer_name,
            "wave_customer_id": self.wave_customer_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StagedLineItem:
        """Deserialize line item from SQLite JSON column."""
        return cls(
            account_key=str(data["account_key"]),
            wave_account_id=data.get("wave_account_id"),
            amount=to_decimal(data["amount"]),
            balance=str(data["balance"]),
            description=str(data.get("description", "")),
            customer_name=data.get("customer_name"),
            wave_customer_id=data.get("wave_customer_id"),
        )


@dataclass(slots=True)
class StagedTransaction:
    """Staged Wave moneyTransactionCreate payload awaiting review/approval/push."""

    external_id: str
    bank_tx_id: int
    tx_date: str
    description: str
    notes: str
    anchor_account_key: str
    anchor_wave_account_id: str | None
    anchor_amount: Decimal
    anchor_direction: str  # "DEPOSIT" or "WITHDRAWAL"
    line_items: list[StagedLineItem]
    category_rule: str = ""
    status: str = "staged"
    review_reason: str = ""
    wave_transaction_id: str | None = None
    last_error: str | None = None
    id: int | None = None


@dataclass(slots=True)
class StagedInvoice:
    """Staged Wave invoiceCreate payload mirroring a Harvest invoice."""

    external_id: str
    harvest_invoice_id: int
    invoice_number: str
    customer_name: str
    wave_customer_id: str | None
    invoice_date: str
    due_date: str
    wave_status: str
    items: list[dict[str, Any]]
    total_amount: Decimal
    status: str = "staged"
    wave_invoice_id: str | None = None
    last_error: str | None = None
    id: int | None = None
