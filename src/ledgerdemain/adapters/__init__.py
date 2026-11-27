"""Pluggable source adapters for Harvest, Grasshopper, Capital One, and Gusto."""

from ledgerdemain.adapters.base import (
    AdapterRegistry,
    InvoiceSourceAdapter,
    PayrollSourceAdapter,
    TransactionSourceAdapter,
    normalize_description,
    parse_flexible_date,
    sha256_file,
)
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter, GrasshopperCsvAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter, HarvestCsvAdapter
from ledgerdemain.adapters.ofx import OfxParseError, OfxStatement, OfxTransactionParser

__all__ = [
    "AdapterRegistry",
    "GrasshopperApiAdapter",
    "GrasshopperCsvAdapter",
    "GustoPayrollCsvAdapter",
    "HarvestApiAdapter",
    "HarvestCsvAdapter",
    "InvoiceSourceAdapter",
    "OfxParseError",
    "OfxStatement",
    "OfxTransactionParser",
    "PayrollSourceAdapter",
    "TransactionSourceAdapter",
    "normalize_description",
    "parse_flexible_date",
    "sha256_file",
]
