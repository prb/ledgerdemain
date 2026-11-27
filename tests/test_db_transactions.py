"""Tests for atomic database ingestion batches and transaction rollback behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ledgerdemain.adapters.capital_one import CapitalOneCsvAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.cli import (
    _ingest_capital_one_file,
    _ingest_grasshopper_file,
    _ingest_gusto_file,
    _ingest_harvest_file,
)
from ledgerdemain.db import LedgerDB
from ledgerdemain.models import (
    HarvestClient,
    HarvestInvoice,
    HarvestInvoiceLineItem,
    HarvestPayment,
)
from ledgerdemain.money import Money
from tests.fixtures import (
    SAMPLE_CAPITAL_ONE_OFX_XML,
    SAMPLE_GRASSHOPPER_CSV,
    SAMPLE_GUSTO_PAYROLL_CSV,
    SAMPLE_HARVEST_CSV,
    make_test_config,
)

_ALLOWED_TABLES = frozenset(
    {
        "bank_transactions",
        "gusto_payroll_runs",
        "harvest_clients",
        "harvest_invoices",
        "harvest_payments",
        "ingestion_batches",
        "sync_state",
    }
)


def _count(db: LedgerDB, table: str) -> int:
    assert table in _ALLOWED_TABLES
    row = db.conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()
    assert row is not None
    return int(row["cnt"])


def _install_abort_trigger(
    db: LedgerDB,
    *,
    name: str,
    timing: str,
    table: str,
    when_clause: str | None = None,
) -> None:
    assert timing in {"BEFORE INSERT", "BEFORE UPDATE"}
    assert table in _ALLOWED_TABLES
    when_sql = f"WHEN {when_clause} " if when_clause else ""
    db.conn.execute(
        f"""
        CREATE TRIGGER {name} {timing} ON {table}
        {when_sql}BEGIN
            SELECT RAISE(ABORT, 'simulated write failure on {table}');
        END
        """
    )


def test_failed_bank_file_ingest_rolls_back_batch_row(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    gh_csv = tmp_path / "grasshopper.csv"
    gh_csv.write_text(SAMPLE_GRASSHOPPER_CSV, encoding="utf-8")
    co_ofx = tmp_path / "capital_one.ofx"
    co_ofx.write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        _install_abort_trigger(
            db,
            name="fail_second_bank_tx",
            timing="BEFORE INSERT",
            table="bank_transactions",
            when_clause="(SELECT COUNT(*) FROM bank_transactions) >= 1",
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            _ingest_grasshopper_file(db, cfg, gh_csv)
        assert _count(db, "bank_transactions") == 0
        assert _count(db, "ingestion_batches") == 0

        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            _ingest_capital_one_file(db, co_ofx, expected_currency="USD")
        assert _count(db, "bank_transactions") == 0
        assert _count(db, "ingestion_batches") == 0


def test_failed_batch_count_update_rolls_back_transactions_and_preserves_prior_data(
    tmp_path: Path,
) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    gh_csv = tmp_path / "grasshopper.csv"
    gh_csv.write_text(SAMPLE_GRASSHOPPER_CSV, encoding="utf-8")
    co_ofx = tmp_path / "capital_one.ofx"
    co_ofx.write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        read_cnt, ins_cnt = _ingest_grasshopper_file(db, cfg, gh_csv)
        assert read_cnt == 6
        assert ins_cnt == 6
        assert _count(db, "bank_transactions") == 6
        assert _count(db, "ingestion_batches") == 1

        _install_abort_trigger(
            db,
            name="fail_batch_count_update",
            timing="BEFORE UPDATE",
            table="ingestion_batches",
        )
        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            _ingest_capital_one_file(db, co_ofx, expected_currency="USD")

        # Capital One rows and the new batch row must roll back cleanly while Grasshopper remains
        assert _count(db, "bank_transactions") == 6
        assert _count(db, "ingestion_batches") == 1


def test_failed_gusto_file_ingest_rolls_back_batch_row(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    gusto_csv = tmp_path / "payroll.csv"
    gusto_csv.write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        _install_abort_trigger(
            db,
            name="fail_gusto_insert",
            timing="BEFORE INSERT",
            table="gusto_payroll_runs",
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            _ingest_gusto_file(db, cfg, gusto_csv, "regular")

        assert _count(db, "gusto_payroll_runs") == 0
        assert _count(db, "ingestion_batches") == 0


def test_failed_harvest_file_batch_insert_rolls_back_invoices(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    harvest_csv = tmp_path / "harvest.csv"
    harvest_csv.write_text(SAMPLE_HARVEST_CSV, encoding="utf-8")

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        _install_abort_trigger(
            db,
            name="fail_harvest_batch_row",
            timing="BEFORE INSERT",
            table="ingestion_batches",
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            _ingest_harvest_file(db, harvest_csv)

        assert _count(db, "harvest_invoices") == 0
        assert _count(db, "ingestion_batches") == 0


def test_failed_harvest_api_batch_rolls_back_all_entities_and_cursor(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    client = HarvestClient(harvest_id="c1", name="Acme Corp", currency="USD", is_active=True)
    invoice = HarvestInvoice(
        harvest_id="inv1",
        invoice_number="1001",
        client_harvest_id="c1",
        client_name="Acme Corp",
        issue_date="2026-01-05",
        due_date="2026-02-04",
        paid_date="2026-01-10",
        state="paid",
        currency="USD",
        subtotal=Money(300000),
        tax_amount=Money(0),
        total_amount=Money(300000),
        due_amount=Money(0),
        subject="Retainer",
        line_items=[
            HarvestInvoiceLineItem(
                kind="Service",
                description="Consulting",
                quantity="1",
                unit_price=Money(300000),
                amount=Money(300000),
            )
        ],
    )
    payment = HarvestPayment(
        harvest_id="pay1",
        invoice_harvest_id="inv1",
        paid_date="2026-01-10",
        amount=Money(300000),
    )

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        _install_abort_trigger(
            db,
            name="fail_harvest_payment_insert",
            timing="BEFORE INSERT",
            table="harvest_payments",
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            db.ingest_harvest_batch(
                [invoice],
                input_kind="api",
                clients=[client],
                payments=[payment],
                sync_cursor_source="harvest_invoices",
                sync_cursor_value="2026-01-05",
            )

        assert _count(db, "harvest_clients") == 0
        assert _count(db, "harvest_invoices") == 0
        assert _count(db, "harvest_payments") == 0
        assert _count(db, "ingestion_batches") == 0
        assert _count(db, "sync_state") == 0
        assert db.get_sync_cursor("harvest_invoices") is None


def test_failed_sync_cursor_update_rolls_back_bank_batch(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    co_csv = tmp_path / "cap.csv"
    co_csv.write_text(
        "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
        "2026-01-03,2026-01-04,1234,AMAZON WEB SERVICES,Merchandise,142.50,\n",
        encoding="utf-8",
    )
    txs = CapitalOneCsvAdapter().parse_file(co_csv)

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()
        _install_abort_trigger(
            db,
            name="fail_sync_state_insert",
            timing="BEFORE INSERT",
            table="sync_state",
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
            db.ingest_bank_transactions_batch(
                txs,
                source="capital_one",
                input_kind="api",
                sync_cursor_source="grasshopper:grasshopper_checking",
                sync_cursor_value="2026-01-04",
            )

        assert _count(db, "bank_transactions") == 0
        assert _count(db, "ingestion_batches") == 0
        assert _count(db, "sync_state") == 0


def test_atomic_batches_commit_counts_links_cursors_and_stay_idempotent(tmp_path: Path) -> None:
    cfg = make_test_config(tmp_path, include_api_creds=False)
    co_ofx = tmp_path / "capital_one.ofx"
    co_ofx.write_text(SAMPLE_CAPITAL_ONE_OFX_XML, encoding="utf-8")
    gusto_csv = tmp_path / "payroll.csv"
    gusto_csv.write_text(SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8")

    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()

        read1, ins1 = _ingest_capital_one_file(db, co_ofx, expected_currency="USD")
        assert (read1, ins1) == (2, 2)
        read2, ins2 = _ingest_capital_one_file(db, co_ofx, expected_currency="USD")
        assert (read2, ins2) == (2, 0)

        runs = GustoPayrollCsvAdapter().parse_file(gusto_csv)
        g_read, g_ins = db.ingest_gusto_payroll_batch(runs, file_path=str(gusto_csv))
        assert (g_read, g_ins) == (1, 1)

        batches = db.conn.execute("SELECT * FROM ingestion_batches ORDER BY id ASC").fetchall()
        assert len(batches) == 3
        assert (
            batches[0]["source"],
            batches[0]["records_read"],
            batches[0]["records_inserted"],
        ) == (
            "capital_one",
            2,
            2,
        )
        assert (batches[1]["records_read"], batches[1]["records_inserted"]) == (2, 0)
        assert (
            batches[2]["source"],
            batches[2]["records_read"],
            batches[2]["records_inserted"],
        ) == (
            "gusto",
            1,
            1,
        )

        tx_batch_ids = {
            row["batch_id"]
            for row in db.conn.execute("SELECT batch_id FROM bank_transactions").fetchall()
        }
        gusto_batch_ids = {
            row["batch_id"]
            for row in db.conn.execute("SELECT batch_id FROM gusto_payroll_runs").fetchall()
        }
        assert tx_batch_ids == {int(batches[0]["id"])}
        assert gusto_batch_ids == {int(batches[2]["id"])}

        # Standalone cursor write outside transaction() still auto-commits cleanly
        db.set_sync_cursor("manual_check", "2026-01-31")
        assert db.get_sync_cursor("manual_check") == "2026-01-31"
