"""Grasshopper Bank Read-Only API adapter."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any
from urllib.parse import urljoin

from ledgerdemain.adapters.base import normalize_description, parse_flexible_date
from ledgerdemain.config import ConfigError, GrasshopperConfig
from ledgerdemain.db import make_bank_fingerprint
from ledgerdemain.http_client import HttpClient
from ledgerdemain.models import BankTransaction
from ledgerdemain.money import ZERO, Money, format_money, parse_money

_INTEGRAL_CENTS_STR_RE = re.compile(r"^([+-]?\d+)(?:\.0+)?$")


class GrasshopperParseError(ValueError):
    """Raised when a Grasshopper API payload is not shaped as expected."""


def _payload_items(payload: Any, keys: tuple[str, ...]) -> list[Any]:
    """Return the item list from a list-or-object payload, rejecting any other shape.

    An object must carry at least one of ``keys``: an error body, a renamed key or an empty
    response (which ``request_json`` returns as ``{}``) is rejected rather than read as zero
    items. Each item must be an object.
    """
    if isinstance(payload, list):
        items: Any = payload
    elif isinstance(payload, dict):
        present = [k for k in keys if k in payload]
        if not present:
            got = ", ".join(sorted(map(str, payload))[:10]) or "none"
            raise GrasshopperParseError(
                f"Grasshopper response has none of {', '.join(keys)} (got keys: {got})"
            )
        items = next((payload[k] for k in present if payload[k]), payload[present[0]])
    else:
        raise GrasshopperParseError(
            f"Grasshopper response must be a list or object: {payload!r:.80}"
        )
    if not isinstance(items, list):
        raise GrasshopperParseError(f"Grasshopper items must be a list, got {type(items).__name__}")
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise GrasshopperParseError(
                f"Grasshopper item #{idx} must be an object, got {type(item).__name__}"
            )
    return items


def _parse_cents_field(value: Any) -> Money:
    """Parse an amount_cents field, accepting ints/integral floats and rejecting fractions."""
    if isinstance(value, bool):
        raise TypeError("amount_cents does not accept bool")
    if isinstance(value, int):
        return Money.from_cents(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"Non-integral amount_cents value: {value!r}")
        return Money.from_cents(int(value))
    if isinstance(value, str):
        text = value.strip()
        match = _INTEGRAL_CENTS_STR_RE.match(text)
        if not match:
            raise ValueError(f"Invalid integer-cents value: {value!r}")
        return Money.from_cents(int(match.group(1)))
    raise TypeError(f"Unsupported amount_cents type: {type(value).__name__}")


def _extract_account_balance(item: dict[str, Any]) -> str:
    """Extract formatted balance from Narmi balances dict or legacy scalar fields."""
    balances = item.get("balances")
    if isinstance(balances, dict):
        for key in ("ledger", "available", "primary"):
            val = balances.get(key)
            if val is None:
                continue
            if isinstance(val, int) and not isinstance(val, bool):
                return Money.from_cents(val).format()
            return format_money(parse_money(val, ZERO))
    bal = item.get("available_balance") or item.get("current_balance") or item.get("balance") or ""
    return str(bal)


class GrasshopperApiAdapter:
    """Adapter for Grasshopper Bank's Read-Only API."""

    source_name = "grasshopper"

    def __init__(self, config: GrasshopperConfig, http_client: HttpClient | None = None) -> None:
        self.config = config
        self.http = http_client or HttpClient()

    def _headers(self, *, require_account: bool = False) -> dict[str, str]:
        if not self.config.access_token:
            raise ConfigError("Grasshopper API requires grasshopper.access_token to be configured")
        if require_account and not self.config.account_id and not self.config.accounts:
            raise ConfigError(
                "Grasshopper API requires grasshopper.account_id or [[grasshopper.accounts]]"
            )
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.header_secret:
            secret_header = self.config.header_secret_name or "X-API-Secret"
            headers[secret_header] = self.config.header_secret
        return headers

    def list_accounts(self) -> list[dict[str, Any]]:
        """List accessible Grasshopper Bank accounts via Read-Only API."""
        url = f"{self.config.base_url}/accounts"
        payload = self.http.request_json("GET", url, headers=self._headers(require_account=False))
        items = _payload_items(payload, ("accounts", "data", "items"))

        accounts: list[dict[str, Any]] = []
        for item in items:
            acct_num = str(
                item.get("account_number_last4")
                or item.get("last4")
                or item.get("account_number")
                or item.get("number")
                or ""
            ).strip()
            last4 = acct_num[-4:] if len(acct_num) > 4 else acct_num
            product = item.get("product")
            product_type = product.get("type") if isinstance(product, dict) else None
            acct_type = str(product_type or item.get("type") or item.get("account_type") or "")
            accounts.append(
                {
                    "id": str(item.get("id") or item.get("account_id") or ""),
                    "name": str(item.get("name") or item.get("nickname") or ""),
                    "type": acct_type,
                    "account_number_last4": last4,
                    "balance": _extract_account_balance(item),
                    "status": str(item.get("state") or item.get("status") or ""),
                    "raw": dict(item),
                }
            )
        return accounts

    def fetch_transactions(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
        *,
        account_id: str | None = None,
        account_key: str = "grasshopper_checking",
    ) -> list[BankTransaction]:
        """Fetch transactions for a Grasshopper account."""
        target_account_id = account_id or self.config.account_id
        if not target_account_id:
            raise ConfigError(
                "Grasshopper fetch_transactions requires an account_id to be configured"
            )

        norm_from = parse_flexible_date(from_date) if from_date else None
        norm_to = parse_flexible_date(to_date) if to_date else None

        transactions: list[BankTransaction] = []
        cursor: str | None = None
        page: int | None = None
        next_url: str | None = None
        visited_urls: set[str] = set()
        seen_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        base_tx_url = f"{self.config.base_url}/accounts/{target_account_id}/transactions"

        while True:
            if next_url:
                request_url = next_url
                params: dict[str, Any] | None = None
            else:
                request_url = base_tx_url
                query_params: dict[str, Any] = {}
                if from_date:
                    query_params["from_date"] = from_date
                if to_date:
                    query_params["to_date"] = to_date
                if cursor:
                    query_params["cursor"] = cursor
                elif page is not None:
                    query_params["page"] = page
                params = query_params or None

            if request_url in visited_urls and params is None:
                break
            visited_urls.add(request_url)

            payload = self.http.request_json(
                "GET", request_url, headers=self._headers(require_account=False), params=params
            )

            items = _payload_items(payload, ("transactions", "data", "items"))
            if isinstance(payload, list):
                raw_next = None
                next_cursor = None
                next_page = None
            else:
                links = payload.get("links")
                raw_next = links.get("next") if isinstance(links, dict) else None
                next_cursor = payload.get("next_cursor")
                next_page = payload.get("next_page")

            for item in items:
                try:
                    tx = self._normalize_api_item(item, seen_counts, account_key=account_key)
                except (KeyError, TypeError, AttributeError) as exc:
                    raise GrasshopperParseError(
                        f"Malformed Grasshopper transaction: {exc}"
                    ) from exc
                if tx is None:
                    continue
                if norm_from and tx.posted_date < norm_from:
                    continue
                if norm_to and tx.posted_date > norm_to:
                    continue
                transactions.append(tx)

            if raw_next:
                resolved_next = urljoin(request_url, str(raw_next))
                if resolved_next == request_url or resolved_next in visited_urls:
                    break
                next_url = resolved_next
                cursor = None
                page = None
            elif next_cursor:
                next_url = None
                cursor = str(next_cursor)
                page = None
            elif next_page:
                next_url = None
                cursor = None
                page = int(next_page)
            else:
                break

        return transactions

    def _normalize_api_item(
        self,
        item: dict[str, Any],
        seen_counts: dict[tuple[str, str, str, str], int],
        *,
        account_key: str = "grasshopper_checking",
    ) -> BankTransaction | None:
        raw_date = (
            item.get("settled_at")
            or item.get("posted_date")
            or item.get("posted_at")
            or item.get("date")
            or item.get("created_at")
            or item.get("transaction_date")
        )
        if not raw_date:
            return None
        posted_date = parse_flexible_date(str(raw_date))
        tx_date_raw = (
            item.get("created_at")
            or item.get("transaction_date")
            or item.get("tx_date")
            or raw_date
        )
        tx_date = parse_flexible_date(str(tx_date_raw))

        raw_cents = item.get("amount_cents")
        raw_amount_val = item.get("amount")
        if raw_cents is not None and raw_amount_val is None:
            raw_amt = _parse_cents_field(raw_cents)
        elif isinstance(raw_amount_val, int) and not isinstance(raw_amount_val, bool):
            raw_amt = Money.from_cents(raw_amount_val)
        else:
            raw_amt = parse_money(raw_amount_val, ZERO)

        if raw_amt == ZERO:
            return None

        dir_raw = str(item.get("direction") or item.get("type") or "").strip().upper()
        if dir_raw in {"DEBIT", "WITHDRAWAL", "OUTFLOW", "DR"}:
            direction = "WITHDRAWAL"
        elif dir_raw in {"CREDIT", "DEPOSIT", "INFLOW", "CR"}:
            direction = "DEPOSIT"
        else:
            direction = "WITHDRAWAL" if raw_amt < ZERO else "DEPOSIT"

        amount = abs(raw_amt)
        desc = str(
            item.get("raw_description")
            or item.get("description")
            or item.get("merchant_name")
            or item.get("memo")
            or "Grasshopper Transaction"
        ).strip()
        norm_desc = normalize_description(desc)
        ext_id = str(item.get("id") or item.get("transaction_id") or "").strip() or None

        occ_key = (posted_date, direction, str(amount), norm_desc)
        occ_idx = seen_counts[occ_key]
        seen_counts[occ_key] += 1

        fingerprint = make_bank_fingerprint(
            source="grasshopper",
            account_key=account_key,
            external_id=ext_id,
            posted_date=posted_date,
            direction=direction,
            amount=amount,
            normalized_description=norm_desc,
            occurrence_index=occ_idx,
        )

        return BankTransaction(
            source="grasshopper",
            account_key=account_key,
            external_id=ext_id,
            fingerprint=fingerprint,
            tx_date=tx_date,
            posted_date=posted_date,
            description=desc,
            normalized_description=norm_desc,
            amount=amount,
            direction=direction,
            source_category=str(item.get("category") or ""),
            status=str(item.get("status") or "posted").lower(),
            raw=dict(item),
        )
