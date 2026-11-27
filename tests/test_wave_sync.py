"""Tests for Wave GraphQL sync, dry-run, idempotency, error handling, and 429 backoff."""

from __future__ import annotations

import http.client
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import load_rules
from ledgerdemain.db import LedgerDB
from ledgerdemain.http_client import HttpClient, HttpResponse
from ledgerdemain.models import (
    BankTransaction,
    HarvestInvoice,
    StagedLineItem,
    StagedTransaction,
)
from ledgerdemain.money import ZERO, Money
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
                "c2",
                "New Client LLC",
                "2026-01-01",
                "2026-01-31",
                "2026-01-05",
                "paid",
                "USD",
                Money.parse("1500.00"),
                ZERO,
                Money.parse("1500.00"),
                ZERO,
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
                description="ACH DEPOSIT NEW CLIENT LLC",
                normalized_description="ACH DEPOSIT NEW CLIENT LLC",
                amount=Money.parse("1500.00"),
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
                amount=Money.parse("142.50"),
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
        assert meta.accounts_synced == 18
        assert meta.customers_synced == 1
        assert not meta.is_classic_accounting

        # Dry-run produces payloads and zero mutations
        calls_before = len(transport.calls)
        dry = svc.push(dry_run=True)
        assert len(transport.calls) == calls_before
        assert dry.transactions_pushed == 2
        assert "New Client LLC" in dry.would_create_customers

        # Live push creates the missing customer and the txs (no status)
        live = svc.push(dry_run=False)
        assert live.transactions_pushed == 2
        assert live.transactions_failed == 0
        assert len(transport.transactions) == 2
        assert [c["name"] for c in transport.customers].count("New Client LLC") == 1

        for sent in transport.transactions:
            assert "status" not in sent["input"]
            assert sent["input"]["externalId"].startswith("ld:tx:")

        # Re-running push makes zero new GraphQL calls
        calls_after_push = len(transport.calls)
        second = svc.push(dry_run=False)
        assert len(transport.calls) == calls_after_push
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
        res = svc.push(dry_run=False)
        assert res.transactions_failed == 1
        assert res.transactions_pushed == 1
        failed = db.list_staged_transactions(status="failed")
        assert len(failed) == 1
        assert "Simulated Wave validation failure" in (failed[0].last_error or "")


class _LossyWaveTransport:
    """Mock Wave transport where one mutation is applied but its response is then lost."""

    def __init__(
        self, inner: MockWaveTransport, fail_op: str, failure: Exception | int | bytes
    ) -> None:
        self.inner = inner
        self.fail_op = fail_op
        self.failure = failure
        self.failed = False

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        resp = self.inner.request(method, url, headers, body, timeout)
        if not self.failed and self.fail_op in self.inner.calls[-1]["query"]:
            self.failed = True
            if isinstance(self.failure, Exception):
                raise self.failure
            if isinstance(self.failure, bytes):
                return HttpResponse(status=200, headers={}, body=self.failure)
            return HttpResponse(status=self.failure, headers={}, body=b"gateway timeout")
        return resp


def _op_calls(transport: MockWaveTransport, op: str) -> list[dict[str, Any]]:
    return [c for c in transport.calls if op in c["query"]]


def _lossy_service(
    tmp_path: Path, db: LedgerDB, fail_op: str, failure: Exception | int | bytes
) -> tuple[WaveSyncService, MockWaveTransport]:
    cfg = make_test_config(tmp_path)
    inner = MockWaveTransport()
    http = HttpClient(
        transport=_LossyWaveTransport(inner, fail_op, failure), sleep_fn=lambda _: None
    )
    _setup_staged_db(tmp_path, db)
    svc = WaveSyncService(
        db, cfg, WaveGraphQLClient(cfg.wave.endpoint, cfg.wave.access_token, http)
    )
    svc.sync_metadata()
    return svc, inner


@pytest.mark.parametrize("failure", [http.client.IncompleteRead(b"{"), 502])
def test_money_transaction_create_with_unknown_outcome_stops_push(
    tmp_path: Path, failure: Exception | int
) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        svc, wave = _lossy_service(tmp_path, db, "MoneyTransactionCreate", failure)

        res = svc.push(dry_run=False)

        assert len(_op_calls(wave, "MoneyTransactionCreate")) == 1
        assert res.transactions_failed == 1
        assert res.transactions_pushed == 0
        assert any(e.startswith("Push stopped") for e in res.errors)
        failed = db.list_staged_transactions(status="outcome_unknown")
        assert len(failed) == 1
        assert "Outcome unknown" in (failed[0].last_error or "")
        assert failed[0].external_id in (failed[0].last_error or "")
        # The other approved transaction was not sent and is still approved.
        assert len(db.list_staged_transactions(status="approved")) == 1


def test_customer_create_with_unknown_outcome_is_reused_after_sync_meta(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        svc, wave = _lossy_service(tmp_path, db, "CustomerCreate", TimeoutError("timed out"))

        first = svc.push(dry_run=False)
        assert any("sync-meta" in e for e in first.errors)
        assert len(_op_calls(wave, "CustomerCreate")) == 1
        # The transaction needing the new customer was not sent and stays approved.
        assert len(db.list_staged_transactions(status="approved")) == 2

        svc.sync_metadata()
        second = svc.push(dry_run=False)

        assert second.errors == []
        assert second.transactions_pushed == 2
        assert len(_op_calls(wave, "CustomerCreate")) == 1
        new_client = next(c for c in wave.customers if c["name"] == "New Client LLC")
        deposit = next(t for t in wave.transactions if t["input"]["lineItems"][0].get("customerId"))
        assert deposit["input"]["lineItems"][0]["customerId"] == new_client["id"]


def _replan_and_approve(tmp_path: Path, db: LedgerDB) -> None:
    cfg = make_test_config(tmp_path)
    Planner(db, cfg, Categorizer(load_rules(cfg.general.rules_path))).build_plan()
    db.approve_staged(all_staged=True)


def test_replan_and_approve_all_do_not_resend_unknown_outcome_transaction(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        svc, wave = _lossy_service(
            tmp_path, db, "MoneyTransactionCreate", http.client.IncompleteRead(b"{")
        )
        svc.push(dry_run=False)
        [unknown] = db.list_staged_transactions(status="outcome_unknown")
        assert unknown.id is not None

        # The routine plan -> approve --all-staged -> push cycle must not revive it.
        _replan_and_approve(tmp_path, db)
        second = svc.push(dry_run=False)

        assert second.errors == []
        sent = [
            c["variables"]["input"]["externalId"] for c in _op_calls(wave, "MoneyTransactionCreate")
        ]
        assert sent.count(unknown.external_id) == 1
        assert len(set(sent)) == len(sent) == 2
        still = db.get_staged_transaction(unknown.id)
        assert still is not None and still.status == "outcome_unknown"

        # Recategorizing can't reset it either; only an explicit approve re-queues it.
        with pytest.raises(ValueError, match="outcome unknown"):
            db.recategorize_staged_tx(unknown.id, "meals", "INCREASE")
        assert db.approve_staged(tx_ids=[unknown.id]) == 1


def test_rejected_create_stops_push_without_marking_records(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        svc, wave = _lossy_service(tmp_path, db, "MoneyTransactionCreate", 401)
        res = svc.push(dry_run=False)

        assert any(e.startswith("Push stopped: Wave rejected") for e in res.errors)
        assert len(_op_calls(wave, "MoneyTransactionCreate")) == 1
        assert db.list_staged_transactions(status="outcome_unknown") == []
        assert db.list_staged_transactions(status="failed") == []
        assert len(db.list_staged_transactions(status="approved")) == 2


_MUTATION_ENTITIES = {
    "customerCreate": "customer",
    "moneyTransactionCreate": "transaction",
}


def _every_mutation(result: Callable[[str], dict[str, Any]]) -> bytes:
    """A body whose result for each create mutation is ``result(entity_name)``."""
    data = {op: result(entity) for op, entity in _MUTATION_ENTITIES.items()}
    return json.dumps({"data": data}).encode("utf-8")


_UNUSABLE_200_BODIES = [
    b"",
    b"not json",
    b"[]",
    b"{}",
    b'{"data": {}}',
    # The mutation's own result is present but has no success flag, or reports success
    # without the created entity or its id: Wave may have applied it.
    _every_mutation(lambda entity: {}),
    _every_mutation(lambda entity: {"didSucceed": None, "inputErrors": []}),
    _every_mutation(lambda entity: {"didSucceed": True, "inputErrors": []}),
    _every_mutation(lambda entity: {"didSucceed": True, entity: None}),
    _every_mutation(lambda entity: {"didSucceed": True, entity: {}}),
]


@pytest.mark.parametrize("body", _UNUSABLE_200_BODIES)
def test_unusable_200_on_money_transaction_create_is_unknown_and_not_resent(
    tmp_path: Path, body: bytes
) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        svc, wave = _lossy_service(tmp_path, db, "MoneyTransactionCreate", body)

        first = svc.push(dry_run=False)

        assert len(_op_calls(wave, "MoneyTransactionCreate")) == 1
        assert first.transactions_pushed == 0
        assert any(e.startswith("Push stopped") for e in first.errors)
        [unknown] = db.list_staged_transactions(status="outcome_unknown")
        assert db.list_staged_transactions(status="failed") == []

        _replan_and_approve(tmp_path, db)
        second = svc.push(dry_run=False)

        assert second.errors == []
        sent = [
            c["variables"]["input"]["externalId"] for c in _op_calls(wave, "MoneyTransactionCreate")
        ]
        assert sent.count(unknown.external_id) == 1
        assert len(wave.transactions) == len(sent) == 2


@pytest.mark.parametrize("body", _UNUSABLE_200_BODIES)
def test_unusable_200_on_customer_create_is_reused_after_sync_meta(
    tmp_path: Path, body: bytes
) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        svc, wave = _lossy_service(tmp_path, db, "CustomerCreate", body)

        first = svc.push(dry_run=False)

        assert any("sync-meta" in e for e in first.errors)
        assert len(_op_calls(wave, "CustomerCreate")) == 1
        assert db.list_staged_transactions(status="outcome_unknown") == []
        assert db.list_staged_transactions(status="failed") == []
        assert len(db.list_staged_transactions(status="approved")) == 2

        svc.sync_metadata()
        second = svc.push(dry_run=False)

        assert second.errors == []
        assert second.transactions_pushed == 2
        assert len(_op_calls(wave, "CustomerCreate")) == 1


def test_dry_run_reports_an_unresolved_account_instead_of_aborting(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path)
    with LedgerDB(cfg.general.db_path) as db:
        _setup_staged_db(tmp_path, db)
        ready = WaveSyncService(db, cfg).push(dry_run=True).transactions_pushed
        assert ready == 2
        db.upsert_bank_transactions(
            [
                BankTransaction(
                    source="grasshopper",
                    account_key="grasshopper_checking",
                    external_id=None,
                    fingerprint="fp-mystery",
                    tx_date="2026-01-20",
                    posted_date="2026-01-20",
                    description="MYSTERY",
                    normalized_description="MYSTERY",
                    amount=Money.parse("10.00"),
                    direction="WITHDRAWAL",
                )
            ]
        )
        bank_id = next(t.id for t in db.list_bank_transactions() if t.fingerprint == "fp-mystery")
        assert bank_id is not None
        staged_id = db.upsert_staged_transaction(
            StagedTransaction(
                external_id="ld:tx:mystery",
                bank_tx_id=bank_id,
                tx_date="2026-01-20",
                description="MYSTERY",
                notes="",
                anchor_account_key="grasshopper_checking",
                anchor_wave_account_id=None,
                anchor_amount=Money.parse("10.00"),
                anchor_direction="WITHDRAWAL",
                line_items=[
                    StagedLineItem("mystery_account", None, Money.parse("10.00"), "INCREASE")
                ],
                status="approved",
            )
        )

        result = WaveSyncService(db, cfg).push(dry_run=True)

        assert result.transactions_pushed == ready
        assert result.transactions_failed == 1
        assert any("mystery_account" in e for e in result.errors)
        row = db.get_staged_transaction(staged_id)
        assert row is not None
        assert row.status == "approved"
