"""Tests for priority-ordered rule-based transaction categorization."""

from __future__ import annotations

from pathlib import Path

from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import load_rules
from ledgerdemain.models import BankTransaction
from ledgerdemain.money import Money
from tests.fixtures import write_test_workspace_config

_DEFAULT_AMOUNT = Money(2500)


def _tx(
    *,
    source: str = "capital_one",
    account_key: str = "capital_one_card",
    description: str = "SOME MERCHANT",
    amount: Money = _DEFAULT_AMOUNT,
    direction: str = "WITHDRAWAL",
    source_category: str = "",
) -> BankTransaction:
    return BankTransaction(
        source=source,
        account_key=account_key,
        external_id=None,
        fingerprint="fp-1",
        tx_date="2026-01-05",
        posted_date="2026-01-05",
        description=description,
        normalized_description=description.upper(),
        amount=amount,
        direction=direction,
        source_category=source_category,
    )


def test_rule_priority_source_direction_category_and_bounds(tmp_path: Path) -> None:
    write_test_workspace_config(tmp_path)
    rules = load_rules(tmp_path / "rules.toml")
    cat = Categorizer(rules)

    # Gusto fee matches Grasshopper withdrawal rule
    res_fee = cat.categorize(
        _tx(
            source="grasshopper",
            account_key="grasshopper_checking",
            description="GUSTO FEE MONTHLY",
        )
    )
    assert res_fee is not None
    assert res_fee.account_key == "payroll_processing_fees"

    # Cloud hosting
    res_aws = cat.categorize(_tx(description="AMAZON WEB SERVICES"))
    assert res_aws is not None
    assert res_aws.account_key == "cloud_hosting"

    # Refund on Capital One DEPOSIT
    res_refund = cat.categorize(_tx(description="FIGMA.COM", direction="DEPOSIT"))
    assert res_refund is not None
    assert res_refund.account_key == "software_subscriptions"

    # Source category fallback
    res_dining = cat.categorize(_tx(description="CORNER BISTRO", source_category="Dining"))
    assert res_dining is not None
    assert res_dining.account_key == "meals"

    # Unmatched returns None
    assert (
        cat.categorize(_tx(description="MYSTERY HARDWARE CO", source_category="Merchandise"))
        is None
    )
