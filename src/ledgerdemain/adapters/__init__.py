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
from ledgerdemain.adapters.capital_one import CapitalOneCsvAdapter, CapitalOnePdfAdapter
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter, HarvestCsvAdapter
from ledgerdemain.adapters.ofx import OfxParseError, OfxStatement, OfxTransactionParser

__all__ = [
    "AdapterRegistry",
    "CapitalOneCsvAdapter",
    "CapitalOnePdfAdapter",
    "GrasshopperApiAdapter",
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
