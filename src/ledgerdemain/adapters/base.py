"""Base adapter protocols, registry, and normalization helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    HarvestClient,
    HarvestInvoice,
    HarvestPayment,
)

_WHITESPACE_RE = re.compile(r"\s+")
_ISO_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T\s].*)?$")
_COMPACT_DATE_RE = re.compile(r"^(\d{8})(?:\d{6}.*)?$")

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y/%m/%d",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
)


def normalize_description(text: str) -> str:
    """Normalize transaction descriptions for stable deduplication and matching."""
    return _WHITESPACE_RE.sub(" ", text).strip().upper()


def parse_flexible_date(val: str) -> str:
    """Parse common date formats into ISO YYYY-MM-DD."""
    cleaned = _WHITESPACE_RE.sub(" ", val).strip()
    if not cleaned:
        raise ValueError("Empty date string")

    iso_match = _ISO_DATE_PREFIX_RE.match(cleaned)
    if iso_match:
        datetime.strptime(iso_match.group(1), "%Y-%m-%d")
        return iso_match.group(1)

    compact_match = _COMPACT_DATE_RE.match(cleaned)
    if compact_match:
        dt = datetime.strptime(compact_match.group(1), "%Y%m%d")
        return dt.strftime("%Y-%m-%d")

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"Unrecognized date format: {val!r}")


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@runtime_checkable
class TransactionSourceAdapter(Protocol):
    """Protocol for adapters producing normalized bank or card transactions."""

    source_name: str

    def fetch_or_parse(self, *args: Any, **kwargs: Any) -> list[BankTransaction]: ...


@runtime_checkable
class InvoiceSourceAdapter(Protocol):
    """Protocol for adapters producing clients, invoices, and payments."""

    source_name: str

    def fetch_invoices(self, *args: Any, **kwargs: Any) -> list[HarvestInvoice]: ...

    def fetch_clients(self, *args: Any, **kwargs: Any) -> list[HarvestClient]: ...

    def fetch_payments_for_invoice(
        self, invoice_id: str, *args: Any, **kwargs: Any
    ) -> list[HarvestPayment]: ...


@runtime_checkable
class PayrollSourceAdapter(Protocol):
    """Protocol for adapters producing aggregated payroll runs."""

    source_name: str

    def parse_file(self, path: Path, *args: Any, **kwargs: Any) -> list[GustoPayrollRun]: ...


class AdapterRegistry:
    """Simple registry mapping source identifiers to adapter instances/factories."""

    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}

    def register(self, name: str, adapter: Any) -> None:
        """Register an adapter under a unique key."""
        self._adapters[name] = adapter

    def get(self, name: str) -> Any:
        """Retrieve an adapter by key or raise KeyError."""
        if name not in self._adapters:
            raise KeyError(f"No adapter registered for {name!r}")
        return self._adapters[name]

    def keys(self) -> list[str]:
        """List registered adapter keys."""
        return sorted(self._adapters)
