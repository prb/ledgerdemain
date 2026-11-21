"""Tests for Wave GraphQL sync, dry-run, idempotency, error handling, and 429 backoff."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import load_rules
from ledgerdemain.db import LedgerDB
from ledgerdemain.http_client import HttpClient
from ledgerdemain.models import BankTransaction, HarvestInvoice
from ledgerdemain.planner import Planner
from ledgerdemain.reconcile import Reconciler
from ledgerdemain.wave.client import WaveGraphQLClient
from ledgerdemain.wave.sync import WaveSyncService
from tests.fixtures import MockWaveTransport, make_test_config


def _setup_staged_db(tmp: Path, db: LedgerDB) -> None:
    cfg = make_test_config(tmp)
    rules = load_rules(cfg.general.rules_path)
    db.init_schema()
    db.upsert_harvest_invoices(
        [
            HarvestInvoice(
                "inv-1001",
                "1001",
                "c1",
                "Acme Corp",
                "2026-01-01",
                "2026-01-31",
                "2026-01-05",
                "paid",
                "USD",
                Decimal("3000.00"),
                Decimal("0.00"),
                Decimal("3000.00"),
                Decimal("0.00"),
                subject="Jan Retainer",
            ),
            HarvestInvoice(
                "inv-1002",
                "1002",
                "c2",
                "New Client LLC",
                "2026-01-10",
                "2026-02-09",
                None,
                "open",
                "USD",
                Decimal("1500.00"),
                Decimal("0.00"),
                Decimal("1500.00"),
                Decimal("1500.00"),
                subject="Advisory",
            ),
        ]
    )
    db.upsert_bank_transactions(
        [
            BankTransaction(
                source="grasshopper",
                account_key="grasshopper_checking",
                external_id=None,
                fingerprint="fp-gh-dep",
                tx_date="2026-01-05",
                posted_date="2026-01-05",
                description="ACH DEPOSIT ACME CORP",
                normalized_description="ACH DEPOSIT ACME CORP",
                amount=Decimal("3000.00"),
                direction="DEPOSIT",
            ),
            BankTransaction(
                source="capital_one",
                account_key="capital_one_card",
                external_id=None,
                fingerprint="fp-co-aws",
                tx_date="2026-01-06",
                posted_date="2026-01-07",
                description="AMAZON WEB SERVICES",
                normalized_description="AMAZON WEB SERVICES",
                amount=Decimal("142.50"),
                direction="WITHDRAWAL",
            ),
        ]
    )
    Reconciler(db, cfg).reconcile_all(confirm_high_confidence=True)
    Planner(db, cfg, Categorizer(rules)).build_plan()
    db.approve_staged(all_staged=True)


def test_sync_metadata_dry_run_live_push_and_idempotency(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    transport = MockWaveTransport()
    sleep_calls: list[float] = []
    http = HttpClient(transport=transport, sleep_fn=sleep_calls.append)
    wave_client = WaveGraphQLClient(cfg.wave.endpoint, cfg.wave.access_token, http)

    with LedgerDB(cfg.general.db_path) as db:
        _setup_staged_db(tmp_path, db)
        svc = WaveSyncService(db, cfg, wave_client)

        # Metadata sync + 429 retry-after backoff
        transport.rate_limit_once = True
        meta = svc.sync_metadata()
        assert sleep_calls == [0.0]
        assert meta.accounts_synced == 20
        assert meta.customers_synced == 1
        assert not meta.is_classic_accounting

        # Dry-run produces payloads and zero mutations
        calls_before = len(transport.calls)
        dry = svc.push(dry_run=True)
        assert len(transport.calls) == calls_before
        assert dry.invoices_pushed == 2
        assert dry.transactions_pushed == 2
        assert "New Client LLC" in dry.would_create_customers
        assert "Consulting Services" in dry.would_create_products

        # Live push creates missing customer & product, invoices (unitPrice), and txs (no status)
        live = svc.push(dry_run=False)
        assert live.invoices_pushed == 2
        assert live.transactions_pushed == 2
        assert live.invoices_failed == 0
        assert live.transactions_failed == 0
        assert len(transport.invoices) == 2
        assert len(transport.transactions) == 2

        for sent in transport.transactions:
            assert "status" not in sent["input"]
            assert sent["input"]["externalId"].startswith("ld:tx:")

        # Re-running push makes zero new GraphQL calls
        calls_after_push = len(transport.calls)
        second = svc.push(dry_run=False)
        assert len(transport.calls) == calls_after_push
        assert second.invoices_pushed == 0
        assert second.transactions_pushed == 0


def test_wave_input_errors_and_classic_accounting_warning(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    transport = MockWaveTransport(is_classic=True)
    http = HttpClient(transport=transport, sleep_fn=lambda _: None)
    wave_client = WaveGraphQLClient(cfg.wave.endpoint, cfg.wave.access_token, http)

    with LedgerDB(cfg.general.db_path) as db:
        _setup_staged_db(tmp_path, db)
        svc = WaveSyncService(db, cfg, wave_client)
        meta = svc.sync_metadata()
        assert meta.is_classic_accounting
        assert any("isClassicAccounting" in w for w in meta.warnings)

        transport.fail_next_tx_error = "Simulated Wave validation failure"
        res = svc.push(dry_run=False, transactions_only=True)
        assert res.transactions_failed == 1
        assert res.transactions_pushed == 1
        failed = db.list_staged_transactions(status="failed")
        assert len(failed) == 1
        assert "Simulated Wave validation failure" in (failed[0].last_error or "")
