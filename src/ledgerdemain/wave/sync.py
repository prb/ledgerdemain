"""Sync orchestrator translating approved staged records into Wave GraphQL mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ledgerdemain.config import AppConfig, ConfigError
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    StagedInvoice,
    StagedTransaction,
)
from ledgerdemain.money import format_money
from ledgerdemain.planner import DEFAULT_NORMAL_BALANCE, BalanceError, validate_double_entry_balance
from ledgerdemain.wave.client import WaveApiError, WaveGraphQLClient


@dataclass(slots=True)
class MetadataSyncSummary:
    """Summary returned by Wave metadata synchronization."""

    business_name: str
    is_classic_accounting: bool
    accounts_synced: int
    customers_synced: int
    products_synced: int
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PushSummary:
    """Summary returned by Wave dry-run or live push."""

    dry_run: bool
    invoices_pushed: int = 0
    invoices_failed: int = 0
    transactions_pushed: int = 0
    transactions_failed: int = 0
    would_create_customers: list[str] = field(default_factory=list)
    would_create_products: list[str] = field(default_factory=list)
    dry_run_payloads: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class WaveSyncService:
    """Coordinates Wave metadata caching and idempotent mutation pushes."""

    def __init__(
        self,
        db: LedgerDB,
        config: AppConfig,
        wave_client: WaveGraphQLClient | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.client = wave_client

    def _require_client(self) -> WaveGraphQLClient:
        if not self.config.wave.business_id or not self.config.wave.access_token:
            raise ConfigError(
                "Wave sync requires wave.business_id and wave.access_token to be configured"
            )
        if self.client is None:
            self.client = WaveGraphQLClient(
                endpoint=self.config.wave.endpoint,
                access_token=self.config.wave.access_token,
            )
        return self.client

    def sync_metadata(self) -> MetadataSyncSummary:
        """Pull Wave business info, accounts, customers, and products into SQLite cache."""
        client = self._require_client()
        biz_id = self.config.wave.business_id
        biz = client.get_business(biz_id)
        is_classic = bool(biz.get("isClassicAccounting", False))
        warnings: list[str] = []
        if is_classic:
            warnings.append(
                "Wave business has isClassicAccounting=true; moneyTransactionCreate requires "
                "newer Wave accounting (isClassicAccounting=false)."
            )

        accounts = client.list_accounts(biz_id, is_archived=False)
        customers = client.list_customers(biz_id)
        products = client.list_products(biz_id)

        self.db.upsert_wave_accounts(accounts)
        self.db.upsert_wave_customers(customers)
        self.db.upsert_wave_products(products)

        return MetadataSyncSummary(
            business_name=str(biz.get("name") or biz_id),
            is_classic_accounting=is_classic,
            accounts_synced=len(accounts),
            customers_synced=len(customers),
            products_synced=len(products),
            warnings=warnings,
        )

    def push(
        self,
        dry_run: bool = False,
        invoices_only: bool = False,
        transactions_only: bool = False,
    ) -> PushSummary:
        """Validate and push approved staged invoices and/or transactions to Wave."""
        if invoices_only and transactions_only:
            raise ValueError("Cannot pass both invoices_only and transactions_only")

        summary = PushSummary(dry_run=dry_run)
        if not dry_run:
            self._require_client()

        approved_invoices = (
            [] if transactions_only else self.db.list_staged_invoices(status="approved")
        )
        approved_txs = [] if invoices_only else self.db.list_staged_transactions(status="approved")

        cached_accounts = self.db.list_wave_accounts()
        has_cached_accounts = len(cached_accounts) > 0
        if not has_cached_accounts:
            if not dry_run:
                raise ConfigError(
                    "Wave accounts cache is empty. Run 'ledgerdemain wave sync-meta' first."
                )
            summary.warnings.append(
                "Wave accounts not yet synced; using placeholder account IDs for dry-run. "
                "Run 'ledgerdemain wave sync-meta' before live push."
            )

        account_id_map = self._resolve_required_accounts(
            approved_invoices=approved_invoices,
            approved_txs=approved_txs,
            allow_placeholders=dry_run and not has_cached_accounts,
        )

        # Ensure default product when pushing invoices
        default_product_id: str | None = None
        if approved_invoices:
            prod_name = self.config.wave.default_product_name
            existing_prod = self.db.find_wave_product_by_name(prod_name)
            if existing_prod is not None:
                default_product_id = existing_prod.wave_id
            elif dry_run:
                default_product_id = f"<WOULD_CREATE_PRODUCT:{prod_name}>"
                if prod_name not in summary.would_create_products:
                    summary.would_create_products.append(prod_name)
            else:
                rev_id = account_id_map["revenue_default"]
                created_prod = self._require_client().create_product(
                    business_id=self.config.wave.business_id,
                    name=prod_name,
                    income_account_id=rev_id,
                )
                self.db.upsert_wave_products([created_prod])
                default_product_id = created_prod.wave_id

        # Push approved invoices
        for inv in approved_invoices:
            assert inv.id is not None
            cust_id = self._resolve_or_create_customer(
                customer_name=inv.customer_name,
                dry_run=dry_run,
                summary=summary,
            )
            items_payload = [
                {
                    "productId": default_product_id,
                    "description": str(item.get("description") or ""),
                    "quantity": str(item.get("quantity") or "1"),
                    "unitPrice": str(item.get("unitPrice") or "0.00"),
                }
                for item in inv.items
            ]
            if dry_run:
                summary.dry_run_payloads.append(
                    {
                        "operation": "invoiceCreate",
                        "staged_invoice_id": inv.id,
                        "input": {
                            "businessId": self.config.wave.business_id or "<WAVE_BUSINESS_ID>",
                            "customerId": cust_id,
                            "invoiceNumber": inv.invoice_number,
                            "invoiceDate": inv.invoice_date,
                            "dueDate": inv.due_date,
                            "status": inv.wave_status,
                            "items": items_payload,
                        },
                    }
                )
                summary.invoices_pushed += 1
                continue

            try:
                created = self._require_client().create_invoice(
                    business_id=self.config.wave.business_id,
                    customer_id=cust_id,
                    invoice_number=inv.invoice_number,
                    invoice_date=inv.invoice_date,
                    due_date=inv.due_date,
                    items=items_payload,
                    status=inv.wave_status,
                )
                self.db.mark_staged_pushed("invoice", inv.id, str(created["id"]))
                summary.invoices_pushed += 1
            except WaveApiError as err:
                self.db.mark_staged_failed("invoice", inv.id, str(err))
                summary.invoices_failed += 1
                summary.errors.append(f"Invoice #{inv.invoice_number}: {err}")

        # Push approved money transactions
        for tx in approved_txs:
            assert tx.id is not None
            try:
                validate_double_entry_balance(
                    anchor_account_key=tx.anchor_account_key,
                    anchor_direction=tx.anchor_direction,
                    anchor_amount=tx.anchor_amount,
                    line_items=tx.line_items,
                    normal_balance_lookup=self._normal_balance_lookup,
                    anchor_wave_account_id=account_id_map[tx.anchor_account_key],
                )
            except BalanceError as err:
                if not dry_run:
                    self.db.mark_staged_failed("transaction", tx.id, str(err))
                summary.transactions_failed += 1
                summary.errors.append(f"Staged tx #{tx.id} ({tx.description}): {err}")
                continue

            line_items_payload: list[dict[str, Any]] = []
            for li in tx.line_items:
                entry: dict[str, Any] = {
                    "accountId": account_id_map[li.account_key],
                    "amount": format_money(li.amount),
                    "balance": li.balance,
                }
                if li.description:
                    entry["description"] = li.description
                if li.wave_customer_id:
                    entry["customerId"] = li.wave_customer_id
                elif li.customer_name:
                    entry["customerId"] = self._resolve_or_create_customer(
                        customer_name=li.customer_name,
                        dry_run=dry_run,
                        summary=summary,
                    )
                line_items_payload.append(entry)

            tx_input: dict[str, Any] = {
                "businessId": self.config.wave.business_id or "<WAVE_BUSINESS_ID>",
                "externalId": tx.external_id,
                "date": tx.tx_date,
                "description": tx.description,
                "anchor": {
                    "accountId": account_id_map[tx.anchor_account_key],
                    "amount": format_money(tx.anchor_amount),
                    "direction": tx.anchor_direction,
                },
                "lineItems": line_items_payload,
            }
            if tx.notes:
                tx_input["notes"] = tx.notes

            if dry_run:
                summary.dry_run_payloads.append(
                    {
                        "operation": "moneyTransactionCreate",
                        "staged_tx_id": tx.id,
                        "input": tx_input,
                    }
                )
                summary.transactions_pushed += 1
                continue

            try:
                created_tx = self._require_client().create_money_transaction(tx_input)
                self.db.mark_staged_pushed("transaction", tx.id, str(created_tx["id"]))
                summary.transactions_pushed += 1
            except WaveApiError as err:
                self.db.mark_staged_failed("transaction", tx.id, str(err))
                summary.transactions_failed += 1
                summary.errors.append(f"Staged tx #{tx.id} ({tx.description}): {err}")

        return summary

    def _normal_balance_lookup(self, account_key: str) -> str:
        cached = self.db.resolve_wave_account(account_key, self.config.accounts)
        if cached is not None:
            return cached.normal_balance_type.upper()
        if account_key in DEFAULT_NORMAL_BALANCE:
            return DEFAULT_NORMAL_BALANCE[account_key]
        if account_key.endswith("_payable") or account_key.startswith("revenue"):
            return "CREDIT"
        return "DEBIT"

    def _resolve_required_accounts(
        self,
        approved_invoices: list[StagedInvoice],
        approved_txs: list[StagedTransaction],
        allow_placeholders: bool,
    ) -> dict[str, str]:
        required_keys: set[str] = set()
        if approved_invoices:
            required_keys.add("revenue_default")
        for tx in approved_txs:
            required_keys.add(tx.anchor_account_key)
            for li in tx.line_items:
                required_keys.add(li.account_key)

        resolved: dict[str, str] = {}
        missing: list[str] = []
        for key in sorted(required_keys):
            cached = self.db.resolve_wave_account(key, self.config.accounts)
            if cached is not None:
                resolved[key] = cached.wave_id
            elif allow_placeholders:
                resolved[key] = f"<UNSYNCED_WAVE_ACCOUNT:{key}>"
            else:
                mapped = self.config.accounts.get(key, key)
                missing.append(f"{key!r} (configured as {mapped!r})")

        if missing:
            raise ConfigError(
                "Missing Wave accounts in local cache for keys: "
                + ", ".join(missing)
                + ". Run 'ledgerdemain wave sync-meta' or update [accounts] in config."
            )
        return resolved

    def _resolve_or_create_customer(
        self,
        customer_name: str,
        dry_run: bool,
        summary: PushSummary,
    ) -> str:
        cached = self.db.find_wave_customer_by_name(customer_name)
        if cached is not None:
            return cached.wave_id
        if dry_run:
            if customer_name not in summary.would_create_customers:
                summary.would_create_customers.append(customer_name)
            return f"<WOULD_CREATE_CUSTOMER:{customer_name}>"
        created = self._require_client().create_customer(
            business_id=self.config.wave.business_id,
            name=customer_name,
        )
        self.db.upsert_wave_customers([created])
        return created.wave_id
