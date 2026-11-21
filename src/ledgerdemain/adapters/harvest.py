"""Harvest REST API v2 and CSV adapters."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ledgerdemain.adapters.base import parse_flexible_date
from ledgerdemain.config import ConfigError, HarvestConfig
from ledgerdemain.http_client import HttpClient
from ledgerdemain.models import (
    HarvestClient,
    HarvestInvoice,
    HarvestInvoiceLineItem,
    HarvestPayment,
)
from ledgerdemain.money import ZERO, parse_money


class HarvestApiAdapter:
    """Adapter for Harvest API v2 using a Personal Access Token."""

    source_name = "harvest_api"

    def __init__(self, config: HarvestConfig, http_client: HttpClient | None = None) -> None:
        self.config = config
        self.http = http_client or HttpClient()

    def _headers(self) -> dict[str, str]:
        if not self.config.access_token or not self.config.account_id:
            raise ConfigError(
                "Harvest API requires harvest.access_token and harvest.account_id to be configured"
            )
        return {
            "Authorization": f"Bearer {self.config.access_token}",
            "Harvest-Account-Id": self.config.account_id,
            "User-Agent": self.config.user_agent or "Ledgerdemain (accounting@example.com)",
            "Accept": "application/json",
        }

    def fetch_clients(self) -> list[HarvestClient]:
        """Fetch all clients from Harvest API v2 with pagination."""
        clients: list[HarvestClient] = []
        page = 1
        while True:
            data = self.http.request_json(
                "GET",
                f"{self.config.base_url}/clients",
                headers=self._headers(),
                params={"page": page, "per_page": 100},
            )
            for item in data.get("clients", []):
                clients.append(
                    HarvestClient(
                        harvest_id=str(item["id"]),
                        name=str(item.get("name", "")).strip(),
                        currency=str(item.get("currency") or "USD"),
                        is_active=bool(item.get("is_active", True)),
                        raw=dict(item),
                    )
                )
            next_page = data.get("next_page")
            if not next_page:
                break
            page = int(next_page)
        return clients

    def fetch_invoices(self, updated_since: str | None = None) -> list[HarvestInvoice]:
        """Fetch Harvest invoices, optionally filtering by updated_since ISO timestamp/date."""
        invoices: list[HarvestInvoice] = []
        since_param: str | None = None
        if updated_since:
            since_param = (
                f"{updated_since}T00:00:00Z" if len(updated_since) == 10 else updated_since
            )

        page = 1
        while True:
            params: dict[str, Any] = {"page": page, "per_page": 100}
            if since_param:
                params["updated_since"] = since_param
            data = self.http.request_json(
                "GET",
                f"{self.config.base_url}/invoices",
                headers=self._headers(),
                params=params,
            )
            for item in data.get("invoices", []):
                invoices.append(self._parse_api_invoice(item))
            next_page = data.get("next_page")
            if not next_page:
                break
            page = int(next_page)
        return invoices

    def fetch_payments_for_invoice(self, invoice_id: str) -> list[HarvestPayment]:
        """Fetch recorded payments for a given Harvest invoice ID."""
        payments: list[HarvestPayment] = []
        page = 1
        while True:
            data = self.http.request_json(
                "GET",
                f"{self.config.base_url}/invoices/{invoice_id}/payments",
                headers=self._headers(),
                params={"page": page, "per_page": 100},
            )
            for item in data.get("invoice_payments", []):
                paid_raw = item.get("paid_date") or item.get("paid_at") or ""
                paid_date = parse_flexible_date(str(paid_raw))
                gateway = item.get("payment_gateway")
                gateway_name = (
                    str(gateway.get("name", ""))
                    if isinstance(gateway, dict)
                    else str(gateway or "")
                )
                payments.append(
                    HarvestPayment(
                        harvest_id=str(item["id"]),
                        invoice_harvest_id=str(invoice_id),
                        paid_date=paid_date,
                        amount=parse_money(item.get("amount"), ZERO),
                        payment_gateway=gateway_name,
                        notes=str(item.get("notes") or ""),
                        raw=dict(item),
                    )
                )
            next_page = data.get("next_page")
            if not next_page:
                break
            page = int(next_page)
        return payments

    @staticmethod
    def _parse_api_invoice(item: dict[str, Any]) -> HarvestInvoice:
        client = item.get("client") or {}
        total_amount = parse_money(item.get("amount"), ZERO)
        tax1 = parse_money(item.get("tax_amount"), ZERO)
        tax2 = parse_money(item.get("tax2_amount"), ZERO)
        tax_amount = tax1 + tax2
        subtotal = (
            parse_money(item.get("subtotal"))
            if item.get("subtotal") is not None
            else (total_amount - tax_amount)
        )
        due_amount = parse_money(item.get("due_amount"), ZERO)

        paid_raw = item.get("paid_date") or item.get("paid_at")
        paid_date = parse_flexible_date(str(paid_raw)) if paid_raw else None

        line_items: list[HarvestInvoiceLineItem] = []
        for li in item.get("line_items") or []:
            raw_qty = li.get("quantity")
            qty = "1" if raw_qty is None else str(raw_qty)
            unit_price = parse_money(li.get("unit_price"), ZERO)
            li_amount = parse_money(li.get("amount"), ZERO)
            line_items.append(
                HarvestInvoiceLineItem(
                    kind=str(li.get("kind") or "Service"),
                    description=str(li.get("description") or ""),
                    quantity=qty,
                    unit_price=unit_price,
                    amount=li_amount,
                    taxed=bool(li.get("taxed", False)),
                )
            )

        issue_raw = item.get("issue_date") or item.get("created_at") or ""
        due_raw = item.get("due_date") or issue_raw
        return HarvestInvoice(
            harvest_id=str(item["id"]),
            invoice_number=str(item.get("number") or item["id"]),
            client_harvest_id=str(client.get("id") or ""),
            client_name=str(client.get("name") or "").strip(),
            issue_date=parse_flexible_date(str(issue_raw)),
            due_date=parse_flexible_date(str(due_raw)),
            paid_date=paid_date,
            state=str(item.get("state") or "open").lower(),
            currency=str(item.get("currency") or "USD"),
            subtotal=subtotal,
            tax_amount=tax_amount,
            total_amount=total_amount,
            due_amount=due_amount,
            subject=str(item.get("subject") or ""),
            line_items=line_items,
            raw=dict(item),
        )


class HarvestCsvAdapter:
    """Parse exported Harvest Invoice CSV reports."""

    source_name = "harvest_csv"

    def parse_file(self, path: Path) -> list[HarvestInvoice]:
        """Parse a Harvest CSV export into HarvestInvoice records."""
        invoices: list[HarvestInvoice] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                norm_row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                inv_num = (
                    norm_row.get("invoice #")
                    or norm_row.get("invoice number")
                    or norm_row.get("number")
                    or norm_row.get("invoice id")
                    or ""
                )
                client_name = norm_row.get("client") or norm_row.get("client name") or ""
                if not inv_num or not client_name:
                    continue

                issue_raw = norm_row.get("issue date") or norm_row.get("sent date") or ""
                if not issue_raw:
                    continue
                issue_date = parse_flexible_date(issue_raw)
                due_raw = norm_row.get("due date") or issue_raw
                due_date = parse_flexible_date(due_raw)
                paid_raw = norm_row.get("paid date") or norm_row.get("date paid") or ""
                paid_date = parse_flexible_date(paid_raw) if paid_raw else None

                state = (norm_row.get("status") or norm_row.get("state") or "open").lower()
                total_raw = (
                    norm_row.get("amount")
                    or norm_row.get("total")
                    or norm_row.get("invoice amount")
                    or "0.00"
                )
                total_amount = parse_money(total_raw, ZERO)
                tax_amount = parse_money(
                    norm_row.get("tax") or norm_row.get("tax amount") or "0.00", ZERO
                )
                subtotal = (
                    parse_money(norm_row["subtotal"], ZERO)
                    if norm_row.get("subtotal")
                    else (total_amount - tax_amount)
                )
                due_raw_amt = (
                    norm_row.get("balance")
                    or norm_row.get("due amount")
                    or norm_row.get("amount due")
                )
                if due_raw_amt:
                    due_amount = parse_money(due_raw_amt, ZERO)
                else:
                    due_amount = ZERO if state == "paid" else total_amount

                harvest_id = norm_row.get("id") or f"csv:{inv_num}"
                subject = norm_row.get("subject") or f"Harvest Invoice #{inv_num}"
                line_items = [
                    HarvestInvoiceLineItem(
                        kind="Service",
                        description=subject,
                        quantity="1",
                        unit_price=subtotal,
                        amount=subtotal,
                        taxed=tax_amount > ZERO,
                    )
                ]
                invoices.append(
                    HarvestInvoice(
                        harvest_id=harvest_id,
                        invoice_number=inv_num,
                        client_harvest_id=f"csv-client:{client_name.lower()}",
                        client_name=client_name,
                        issue_date=issue_date,
                        due_date=due_date,
                        paid_date=paid_date,
                        state=state,
                        currency=norm_row.get("currency") or "USD",
                        subtotal=subtotal,
                        tax_amount=tax_amount,
                        total_amount=total_amount,
                        due_amount=due_amount,
                        subject=subject,
                        line_items=line_items,
                        raw=dict(row),
                    )
                )
        return invoices
