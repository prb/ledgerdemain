"""Sync orchestrator translating approved staged records into Wave GraphQL mutations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ledgerdemain.config import AppConfig, ConfigError
from ledgerdemain.db import LedgerDB
from ledgerdemain.http_client import GraphQLError, HttpError, OutcomeUnknownError
from ledgerdemain.models import StagedTransaction
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
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PushSummary:
    """Summary returned by Wave dry-run or live push."""

    dry_run: bool
    transactions_pushed: int = 0
    transactions_failed: int = 0
    would_create_customers: list[str] = field(default_factory=list)
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
        """Pull Wave business info, accounts and customers into SQLite cache."""
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

        self.db.upsert_wave_accounts(accounts)
        self.db.upsert_wave_customers(customers)

        return MetadataSyncSummary(
            business_name=str(biz.get("name") or biz_id),
            is_classic_accounting=is_classic,
            accounts_synced=len(accounts),
            customers_synced=len(customers),
            warnings=warnings,
        )

    def push(self, dry_run: bool = False) -> PushSummary:
        """Validate and push approved staged transactions to Wave.

        If a create request fails with an unknown outcome (it may have been applied in Wave),
        the push stops so no further records are sent. A staged transaction in that state is
        marked ``outcome_unknown``, which only an explicit ``approve --tx`` re-queues.
        If Wave rejects a request outright (an HTTP or GraphQL error), the push stops and
        nothing is marked, so the same records can be pushed again once the cause is fixed.
        """
        summary = PushSummary(dry_run=dry_run)
        try:
            self._push_approved(summary, dry_run)
        except OutcomeUnknownError as err:
            summary.errors.append(
                f"Push stopped after a Wave request with an unknown outcome ({err}). "
                "Remaining approved records were not sent. Run 'ledgerdemain wave sync-meta' "
                "before pushing again so a customer that was created is reused."
            )
        except (HttpError, GraphQLError) as err:
            summary.errors.append(
                f"Push stopped: Wave rejected a request ({err}). Nothing was marked for the "
                "rejected record; fix the cause and push again."
            )
        return summary

    def _push_approved(self, summary: PushSummary, dry_run: bool) -> None:
        if not dry_run:
            self._require_client()

        approved_txs = self.db.list_staged_transactions(status="approved")

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
            approved_txs=approved_txs,
            allow_placeholders=dry_run and not has_cached_accounts,
        )

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
                    self.db.mark_staged_failed(tx.id, str(err))
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
                self.db.mark_staged_pushed(tx.id, str(created_tx["id"]))
                summary.transactions_pushed += 1
            except WaveApiError as err:
                self.db.mark_staged_failed(tx.id, str(err))
                summary.transactions_failed += 1
                summary.errors.append(f"Staged tx #{tx.id} ({tx.description}): {err}")
            except OutcomeUnknownError as err:
                msg = (
                    f"Outcome unknown, transaction may exist in Wave; check for externalId "
                    f"{tx.external_id} and run 'approve --tx {tx.id}' only if it is missing: "
                    f"{err}"
                )
                self.db.mark_staged_outcome_unknown(tx.id, msg)
                summary.transactions_failed += 1
                summary.errors.append(f"Staged tx #{tx.id} ({tx.description}): {msg}")
                raise

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
        approved_txs: list[StagedTransaction],
        allow_placeholders: bool,
    ) -> dict[str, str]:
        required_keys: set[str] = set()
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
