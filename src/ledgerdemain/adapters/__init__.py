"""Pluggable source adapters for Harvest, Grasshopper, Capital One, and Gusto."""

from ledgerdemain.adapters.base import (
    normalize_description,
    parse_flexible_date,
    sha256_file,
)
from ledgerdemain.adapters.capital_one import CapitalOneCsvAdapter, CapitalOnePdfAdapter
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter, GrasshopperCsvAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter
from ledgerdemain.adapters.ofx import OfxParseError, OfxStatement, OfxTransactionParser

__all__ = [
    "CapitalOneCsvAdapter",
    "CapitalOnePdfAdapter",
    "GrasshopperApiAdapter",
    "GrasshopperCsvAdapter",
    "GustoPayrollCsvAdapter",
    "HarvestApiAdapter",
    "OfxParseError",
    "OfxStatement",
    "OfxTransactionParser",
    "normalize_description",
    "parse_flexible_date",
    "sha256_file",
]
