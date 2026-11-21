"""Priority-ordered rule-based transaction categorizer."""

from __future__ import annotations

from dataclasses import dataclass

from ledgerdemain.config import CategorizationRule
from ledgerdemain.models import BankTransaction


@dataclass(slots=True)
class CategorizationResult:
    """Matched categorization rule result."""

    account_key: str
    rule_name: str
    notes: str = ""


class Categorizer:
    """Evaluates ordered rules against normalized bank/credit card transactions."""

    def __init__(self, rules: list[CategorizationRule]) -> None:
        self.rules = sorted(rules, key=lambda r: (r.priority, r.name))

    def categorize(self, tx: BankTransaction) -> CategorizationResult | None:
        """Return the first matching CategorizationResult, or None if unmatched."""
        for rule in self.rules:
            if rule.source and rule.source != tx.source:
                continue
            if rule.direction and rule.direction != tx.direction:
                continue
            if rule.min_amount is not None and tx.amount < rule.min_amount:
                continue
            if rule.max_amount is not None and tx.amount > rule.max_amount:
                continue
            if rule.source_category_pattern is not None and not rule.source_category_pattern.search(
                tx.source_category or ""
            ):
                continue
            if not rule.description_pattern.search(tx.description):
                continue

            return CategorizationResult(
                account_key=rule.account_key,
                rule_name=rule.name,
                notes=rule.notes,
            )
        return None
