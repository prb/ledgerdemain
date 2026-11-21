"""Tests for .env auto-loading, multi-account Grasshopper support, bank transfers, and discovery."""

from __future__ import annotations

import contextlib
import io
import json
from decimal import Decimal
from pathlib import Path

import pytest

from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter
from ledgerdemain.categorize import Categorizer
from ledgerdemain.cli import main
from ledgerdemain.config import (
    GrasshopperAccountConfig,
    GrasshopperConfig,
    load_config,
    load_rules,
)
from ledgerdemain.db import LedgerDB
from ledgerdemain.http_client import HttpClient, HttpResponse
from ledgerdemain.models import ZERO, BankTransaction, HarvestInvoice
from ledgerdemain.planner import Planner
from ledgerdemain.reconcile import Reconciler
from ledgerdemain.wave.client import WaveGraphQLClient
from tests.fixtures import write_test_workspace_config


class _StubTransport:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        del method, headers, body, timeout
        self.urls.append(url)
        for prefix, payload in sorted(
            self.responses.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if prefix in url:
                return HttpResponse(200, {}, json.dumps(payload).encode("utf-8"))
        raise AssertionError(f"Unexpected URL: {url}")


def test_dotenv_loading_and_env_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[general]
db_path = "data/ledgerdemain.db"

[wave]
business_id = "${TEST_WAVE_BIZ}"
access_token = "${TEST_WAVE_TOK}"

[grasshopper]
access_token = "${TEST_GH_TOK}"

[[grasshopper.accounts]]
account_key = "grasshopper_savings"
account_id = "${TEST_GH_SAV}"
acctid_last4 = "1111"

[[grasshopper.accounts]]
account_key = "grasshopper_money_market"
account_id = "${TEST_GH_MM}"
acctid_last4 = "2222"
""",
        encoding="utf-8",
    )

    (tmp_path / ".env").write_text(
        """# Comment line
export TEST_WAVE_BIZ="biz-from-dotenv"
TEST_WAVE_TOK='tok-from-dotenv'
TEST_GH_TOK=gh-tok-dotenv # inline comment
TEST_GH_SAV=sav-id-1
TEST_GH_MM=mm-id-2
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("TEST_WAVE_TOK", "tok-from-environ")
    monkeypatch.delenv("TEST_WAVE_BIZ", raising=False)
    monkeypatch.delenv("TEST_GH_TOK", raising=False)
    monkeypatch.delenv("TEST_GH_SAV", raising=False)
    monkeypatch.delenv("TEST_GH_MM", raising=False)

    cfg = load_config(cfg_path)
    assert cfg.wave.business_id == "biz-from-dotenv"
    assert cfg.wave.access_token == "tok-from-environ"
    assert cfg.grasshopper.access_token == "gh-tok-dotenv"
    assert len(cfg.grasshopper.configured_accounts) == 2
    assert cfg.grasshopper.configured_accounts[0].account_key == "grasshopper_savings"
    assert cfg.grasshopper.configured_accounts[0].account_id == "sav-id-1"
    assert cfg.grasshopper.configured_accounts[1].account_key == "grasshopper_money_market"
    assert cfg.grasshopper.configured_accounts[1].account_id == "mm-id-2"


def test_grasshopper_list_accounts_and_multi_account_fetch() -> None:
    transport = _StubTransport(
        {
            "/accounts/sav-1/transactions": {
                "transactions": [
                    {
                        "id": "tx-sav-1",
                        "posted_date": "2026-11-05",
                        "description": "INTERNAL TRANSFER TO MM",
                        "amount": "2500.00",
                        "direction": "DEBIT",
                    }
                ]
            },
            "/accounts/mm-1/transactions": {
                "transactions": [
                    {
                        "id": "tx-mm-1",
                        "posted_date": "2026-11-05",
                        "description": "INTERNAL TRANSFER FROM SAVINGS",
                        "amount": "2500.00",
                        "direction": "CREDIT",
                    }
                ]
            },
            "/accounts": {
                "accounts": [
                    {
                        "id": "sav-1",
                        "name": "Business Savings",
                        "type": "SAVINGS",
                        "account_number": "00001111",
                        "current_balance": "10000.00",
                        "status": "ACTIVE",
                    },
                    {
                        "id": "mm-1",
                        "name": "Money Market",
                        "type": "MONEY_MARKET",
                        "account_number": "00002222",
                        "current_balance": "25000.00",
                        "status": "ACTIVE",
                    },
                ]
            },
        }
    )

    gh_cfg = GrasshopperConfig(
        access_token="gh-tok",
        header_secret="sec",
        accounts=[
            GrasshopperAccountConfig("grasshopper_savings", "sav-1", "1111"),
            GrasshopperAccountConfig("grasshopper_money_market", "mm-1", "2222"),
        ],
    )
    adapter = GrasshopperApiAdapter(
        gh_cfg, http_client=HttpClient(transport=transport, sleep_fn=lambda _: None)
    )

    accounts = adapter.list_accounts()
    assert len(accounts) == 2
    assert accounts[0]["id"] == "sav-1"
    assert accounts[0]["account_number_last4"] == "1111"
    assert accounts[1]["id"] == "mm-1"
    assert accounts[1]["account_number_last4"] == "2222"

    by_acct = adapter.fetch_all_accounts()
    assert set(by_acct) == {"grasshopper_savings", "grasshopper_money_market"}
    assert by_acct["grasshopper_savings"][0].account_key == "grasshopper_savings"
    assert by_acct["grasshopper_savings"][0].direction == "WITHDRAWAL"
    assert by_acct["grasshopper_money_market"][0].account_key == "grasshopper_money_market"
    assert by_acct["grasshopper_money_market"][0].direction == "DEPOSIT"


def test_grasshopper_live_narmi_schema_cents_settled_at_and_links_pagination() -> None:
    transport = _StubTransport(
        {
            "/v1/accounts/chk-synth-1/transactions?cursor=p2": {
                "links": {"next": None},
                "transactions": [
                    {
                        "id": "tx-synth-2",
                        "settled_at": "2026-11-10T09:00:00Z",
                        "created_at": "2026-11-10T09:00:00Z",
                        "amount": 1900000,
                        "type": "credit",
                        "description": "Transfer",
                        "raw_description": "SYNTHETIC TRANSFER FROM MM",
                    },
                    {
                        "id": "tx-synth-pending",
                        "settled_at": None,
                        "created_at": "2026-11-12T11:30:00Z",
                        "amount": 450,
                        "type": "credit",
                        "description": "Interest Payment",
                    },
                ],
            },
            "/v1/accounts/chk-synth-1/transactions": {
                "links": {"next": "/v1/accounts/chk-synth-1/transactions?cursor=p2"},
                "meta": {"total": 3},
                "transactions": [
                    {
                        "id": "tx-synth-old",
                        "settled_at": "2026-10-01T10:00:00Z",
                        "created_at": "2026-10-01T10:00:00Z",
                        "amount": -5000,
                        "type": "debit",
                        "raw_description": "OUT OF RANGE CHARGE",
                    },
                    {
                        "id": "tx-synth-1",
                        "settled_at": "2026-11-09T14:22:00Z",
                        "created_at": "2026-11-08T18:05:00Z",
                        "amount": -21182,
                        "type": "debit",
                        "description": "Card Payment",
                        "raw_description": "SYNTHETIC CARD ONLINE PMT",
                        "category": "Credit Card Payment",
                    },
                ],
            },
            "/v1/accounts": {
                "accounts": [
                    {
                        "id": "chk-synth-1",
                        "name": "Synthetic Checking",
                        "number": "****4101",
                        "type": "deposit",
                        "product": {"type": "checking"},
                        "state": "active",
                        "balances": {"primary": 125050, "available": 120000, "ledger": 125050},
                    },
                    {
                        "id": "mm-synth-2",
                        "name": "Synthetic Money Market",
                        "number": "****4202",
                        "type": "deposit",
                        "product": {"type": "money_market"},
                        "state": "active",
                        "balances": {"primary": 5000000, "available": 5000000, "ledger": 5000000},
                    },
                ]
            },
        }
    )

    gh_cfg = GrasshopperConfig(
        base_url="https://api.grasshopper.bank/v1",
        access_token="gh-synth-tok",
        accounts=[
            GrasshopperAccountConfig("grasshopper_checking", "chk-synth-1", "4101"),
            GrasshopperAccountConfig("grasshopper_money_market", "mm-synth-2", "4202"),
        ],
    )
    adapter = GrasshopperApiAdapter(
        gh_cfg, http_client=HttpClient(transport=transport, sleep_fn=lambda _: None)
    )

    accounts = adapter.list_accounts()
    assert len(accounts) == 2
    assert accounts[0]["type"] == "checking"
    assert accounts[0]["account_number_last4"] == "4101"
    assert accounts[0]["balance"] == "1250.50"
    assert accounts[0]["status"] == "active"
    assert accounts[1]["type"] == "money_market"
    assert accounts[1]["account_number_last4"] == "4202"
    assert accounts[1]["balance"] == "50000.00"

    txs = adapter.fetch_transactions(
        from_date="2026-11-05",
        to_date="2026-11-15",
        account_id="chk-synth-1",
        account_key="grasshopper_checking",
    )
    assert all("page=1" not in u for u in transport.urls)
    assert any(u.endswith("?cursor=p2") for u in transport.urls)
    assert len(txs) == 3

    assert txs[0].external_id == "tx-synth-1"
    assert txs[0].posted_date == "2026-11-09"
    assert txs[0].tx_date == "2026-11-08"
    assert txs[0].amount == Decimal("211.82")
    assert txs[0].direction == "WITHDRAWAL"
    assert txs[0].description == "SYNTHETIC CARD ONLINE PMT"

    assert txs[1].external_id == "tx-synth-2"
    assert txs[1].amount == Decimal("19000.00")
    assert txs[1].direction == "DEPOSIT"

    assert txs[2].external_id == "tx-synth-pending"
    assert txs[2].posted_date == "2026-11-12"
    assert txs[2].amount == Decimal("4.50")
    assert txs[2].direction == "DEPOSIT"


def test_grasshopper_ofx_auto_resolves_account_key_by_acctid_last4(tmp_path: Path) -> None:
    cfg_path = write_test_workspace_config(tmp_path, include_api_creds=False)
    with cfg_path.open("a", encoding="utf-8") as fh:
        fh.write(
            """
[[grasshopper.accounts]]
account_key = "grasshopper_savings"
acctid_last4 = "1111"

[[grasshopper.accounts]]
account_key = "grasshopper_money_market"
acctid_last4 = "2222"
"""
        )

    sav_ofx = tmp_path / "export_a.ofx"
    sav_ofx.write_text(
        """<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>000<ACCTID>00001111<ACCTTYPE>SAVINGS</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261105<TRNAMT>-1500.00<FITID>S-1<NAME>TRANSFER TO MM</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>""",
        encoding="utf-8",
    )
    mm_ofx = tmp_path / "export_b.ofx"
    mm_ofx.write_text(
        """<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>USD
<BANKACCTFROM><BANKID>000<ACCTID>00002222<ACCTTYPE>SAVINGS</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20261105<TRNAMT>1500.00<FITID>M-1<NAME>TRANSFER FROM SAV</STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>""",
        encoding="utf-8",
    )

    stdout_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf):
        assert (
            main(["--config", str(cfg_path), "ingest", "grasshopper", "--file", str(sav_ofx)]) == 0
        )
        assert (
            main(["--config", str(cfg_path), "ingest", "grasshopper", "--file", str(mm_ofx)]) == 0
        )

    cfg = load_config(cfg_path)
    with LedgerDB(cfg.general.db_path) as db:
        txs = db.list_bank_transactions(source="grasshopper")
    assert {tx.account_key for tx in txs} == {
        "grasshopper_savings",
        "grasshopper_money_market",
    }


def test_bank_transfer_reconciliation_and_harvest_isolation(tmp_path: Path) -> None:
    for strat, expected_status in (
        ("manual_transfer", "manual_transfer_required"),
        ("clearing_account", "staged"),
    ):
        sub_dir = tmp_path / strat
        sub_dir.mkdir()
        cfg_path = write_test_workspace_config(sub_dir, transfer_strategy=strat)
        with (sub_dir / "rules.toml").open("a", encoding="utf-8") as fh:
            fh.write(
                """
[[rules]]
name = "Bank Interest Earned"
priority = 15
source = "grasshopper"
direction = "DEPOSIT"
description_regex = "(?i)\\\\binterest\\\\b"
account_key = "interest_income"
"""
            )
        text = cfg_path.read_text(encoding="utf-8").replace(
            "\n[accounts]\n",
            '\n[accounts]\ngrasshopper_savings = "Grasshopper Savings"\n'
            'grasshopper_money_market = "Grasshopper Money Market"\n'
            'interest_income = "Interest Income"\n'
            'bank_transfer_clearing = "Bank Transfer Clearing"\n',
        )
        cfg_path.write_text(text, encoding="utf-8")
        cfg = load_config(cfg_path)
        rules = load_rules(cfg.general.rules_path)

        with LedgerDB(cfg.general.db_path) as db:
            db.init_schema()
            # Open Harvest invoice of the exact same amount as the transfer AND interest amount
            db.upsert_harvest_invoices(
                [
                    HarvestInvoice(
                        "inv-1",
                        "1001",
                        "c1",
                        "Acme Corp",
                        "2026-11-01",
                        "2026-12-01",
                        None,
                        "open",
                        "USD",
                        Decimal("2500.00"),
                        ZERO,
                        Decimal("2500.00"),
                        Decimal("2500.00"),
                    ),
                    HarvestInvoice(
                        "inv-2",
                        "1002",
                        "c1",
                        "Acme Corp",
                        "2026-11-01",
                        "2026-12-01",
                        None,
                        "open",
                        "USD",
                        Decimal("42.15"),
                        ZERO,
                        Decimal("42.15"),
                        Decimal("42.15"),
                    ),
                ]
            )
            db.upsert_bank_transactions(
                [
                    BankTransaction(
                        source="grasshopper",
                        account_key="grasshopper_savings",
                        external_id=None,
                        fingerprint="fp-sav-out",
                        tx_date="2026-11-05",
                        posted_date="2026-11-05",
                        description="INTERNAL TRANSFER TO MM",
                        normalized_description="INTERNAL TRANSFER TO MM",
                        amount=Decimal("2500.00"),
                        direction="WITHDRAWAL",
                    ),
                    BankTransaction(
                        source="grasshopper",
                        account_key="grasshopper_money_market",
                        external_id=None,
                        fingerprint="fp-mm-in",
                        tx_date="2026-11-05",
                        posted_date="2026-11-05",
                        description="INTERNAL TRANSFER FROM SAV",
                        normalized_description="INTERNAL TRANSFER FROM SAV",
                        amount=Decimal("2500.00"),
                        direction="DEPOSIT",
                    ),
                    BankTransaction(
                        source="grasshopper",
                        account_key="grasshopper_money_market",
                        external_id=None,
                        fingerprint="fp-mm-int",
                        tx_date="2026-11-10",
                        posted_date="2026-11-10",
                        description="Interest Payment",
                        normalized_description="INTEREST PAYMENT",
                        amount=Decimal("42.15"),
                        direction="DEPOSIT",
                    ),
                ]
            )

            summary = Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
            assert summary.bank_transfer_matches == 1
            assert summary.harvest_matches == 0

            Planner(db, cfg, Categorizer(rules)).build_plan()
            staged = db.list_staged_transactions()
            xfer_legs = [t for t in staged if t.anchor_amount == Decimal("2500.00")]
            assert len(xfer_legs) == 2
            assert all(t.status == expected_status for t in xfer_legs)
            if strat == "manual_transfer":
                assert (
                    "grasshopper_savings -> grasshopper_money_market" in xfer_legs[0].review_reason
                )
            else:
                assert xfer_legs[0].line_items[0].account_key == "bank_transfer_clearing"
                assert xfer_legs[0].line_items[0].balance == "INCREASE"
                assert xfer_legs[1].line_items[0].account_key == "bank_transfer_clearing"
                assert xfer_legs[1].line_items[0].balance == "DECREASE"

            interest_tx = next(t for t in staged if t.anchor_amount == Decimal("42.15"))
            assert interest_tx.status == "staged"
            assert interest_tx.line_items[0].account_key == "interest_income"
            assert interest_tx.line_items[0].balance == "INCREASE"


def test_cli_discovery_wave_businesses_and_grasshopper_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = write_test_workspace_config(tmp_path)

    def fake_list_businesses(
        self: WaveGraphQLClient, page_size: int = 50
    ) -> list[dict[str, object]]:
        del self, page_size
        return [
            {
                "id": "biz-1",
                "name": "Example Studio LLC",
                "isClassicAccounting": False,
                "currency": {"code": "USD"},
            }
        ]

    def fake_list_accounts(self: GrasshopperApiAdapter) -> list[dict[str, object]]:
        del self
        return [
            {
                "id": "gh-sav-1",
                "name": "Savings",
                "type": "SAVINGS",
                "account_number_last4": "1111",
                "balance": "12345.67",
            },
            {
                "id": "gh-mm-1",
                "name": "Money Market",
                "type": "MONEY_MARKET",
                "account_number_last4": "2222",
                "balance": "54321.00",
            },
        ]

    monkeypatch.setattr(WaveGraphQLClient, "list_businesses", fake_list_businesses)
    monkeypatch.setattr(GrasshopperApiAdapter, "list_accounts", fake_list_accounts)

    wave_out = io.StringIO()
    with contextlib.redirect_stdout(wave_out):
        assert main(["--config", str(cfg_path), "wave", "list-businesses"]) == 0
    assert "biz-1" in wave_out.getvalue()
    assert "Example Studio LLC" in wave_out.getvalue()

    gh_out = io.StringIO()
    with contextlib.redirect_stdout(gh_out):
        assert main(["--config", str(cfg_path), "grasshopper", "list-accounts"]) == 0
    assert "gh-sav-1" in gh_out.getvalue()
    assert "gh-mm-1" in gh_out.getvalue()
