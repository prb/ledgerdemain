"""SQLite persistence layer for staging, reconciliation, and Wave sync state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ledgerdemain.models import (
    BankTransaction,
    GustoPayrollRun,
    HarvestClient,
    HarvestInvoice,
    HarvestInvoiceLineItem,
    HarvestPayment,
    ReconciliationMatch,
    StagedLineItem,
    StagedTransaction,
    WaveAccount,
    WaveCustomer,
)
from ledgerdemain.money import Money, format_money, parse_money

SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def make_bank_fingerprint(
    source: str,
    account_key: str,
    external_id: str | None,
    posted_date: str,
    direction: str,
    amount: Money,
    normalized_description: str,
    occurrence_index: int = 0,
) -> str:
    """Compute a deterministic SHA-256 fingerprint for bank/card transaction deduplication."""
    if external_id:
        raw = f"{source}|{account_key}|ext:{external_id.strip()}"
    else:
        raw = (
            f"{source}|{account_key}|{posted_date}|{direction}|"
            f"{format_money(amount)}|{normalized_description}|{occurrence_index}"
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LedgerDB:
    """SQLite database manager for Ledgerdemain."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._tx_depth = 0

    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()

    def __enter__(self) -> LedgerDB:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run operations inside an atomic transaction, joining any active outer one.

        Nested ``transaction()`` blocks participate in the outermost SQLite transaction
        rather than committing early, and all ``LedgerDB`` DML write methods compose
        safely inside ``transaction()`` (only ``init_schema()`` runs DDL via
        ``executescript()`` and cannot run inside an active transaction). Because SQLite
        rolls back the entire outer transaction on an unhandled exception, callers must
        not catch and suppress database errors inside a nested ``transaction()`` block.
        """
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return

        self._tx_depth = 1
        try:
            with self.conn:
                yield
        finally:
            self._tx_depth = 0

    def init_schema(self) -> None:
        """Create all schema tables and indexes if they do not exist."""
        # executescript() issues an implicit COMMIT for DDL, so this stays on self.conn.
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    source TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingestion_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    input_kind TEXT NOT NULL,
                    file_path TEXT,
                    file_sha256 TEXT,
                    records_read INTEGER NOT NULL DEFAULT 0,
                    records_inserted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bank_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES ingestion_batches(id),
                    source TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    external_id TEXT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    tx_date TEXT NOT NULL,
                    posted_date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    normalized_description TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    card_last4 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'posted',
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bank_tx_source_date
                    ON bank_transactions(source, posted_date);

                CREATE TABLE IF NOT EXISTS harvest_clients (
                    harvest_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harvest_invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    harvest_id TEXT NOT NULL UNIQUE,
                    invoice_number TEXT NOT NULL,
                    client_harvest_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    issue_date TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    paid_date TEXT,
                    state TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    subtotal TEXT NOT NULL,
                    tax_amount TEXT NOT NULL,
                    total_amount TEXT NOT NULL,
                    due_amount TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    line_items_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS harvest_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    harvest_id TEXT NOT NULL UNIQUE,
                    invoice_harvest_id TEXT NOT NULL,
                    paid_date TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    payment_gateway TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS gusto_payroll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER REFERENCES ingestion_batches(id),
                    fingerprint TEXT NOT NULL UNIQUE,
                    check_date TEXT NOT NULL,
                    pay_period_start TEXT NOT NULL,
                    pay_period_end TEXT NOT NULL,
                    run_type TEXT NOT NULL,
                    gross_wages TEXT NOT NULL,
                    reimbursements TEXT NOT NULL,
                    employee_taxes TEXT NOT NULL,
                    employer_taxes TEXT NOT NULL,
                    employee_benefits TEXT NOT NULL,
                    employer_benefits TEXT NOT NULL,
                    net_pay TEXT NOT NULL,
                    tax_breakdown_json TEXT NOT NULL,
                    employee_count INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reconciliation_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    match_type TEXT NOT NULL,
                    bank_tx_id INTEGER NOT NULL REFERENCES bank_transactions(id),
                    counterparty_bank_tx_id INTEGER REFERENCES bank_transactions(id),
                    harvest_invoice_id INTEGER REFERENCES harvest_invoices(id),
                    harvest_payment_id INTEGER REFERENCES harvest_payments(id),
                    gusto_run_id INTEGER REFERENCES gusto_payroll_runs(id),
                    matched_amount TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_reconcile_tuple
                    ON reconciliation_matches(
                        match_type,
                        bank_tx_id,
                        COALESCE(counterparty_bank_tx_id, -1),
                        COALESCE(harvest_invoice_id, -1),
                        COALESCE(harvest_payment_id, -1),
                        COALESCE(gusto_run_id, -1)
                    );

                CREATE TABLE IF NOT EXISTS wave_accounts (
                    wave_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    account_subtype TEXT NOT NULL,
                    normal_balance_type TEXT NOT NULL,
                    is_archived INTEGER NOT NULL DEFAULT 0,
                    description TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wave_customers (
                    wave_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS staged_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT NOT NULL UNIQUE,
                    bank_tx_id INTEGER NOT NULL UNIQUE REFERENCES bank_transactions(id),
                    tx_date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    anchor_account_key TEXT NOT NULL,
                    anchor_wave_account_id TEXT,
                    anchor_amount TEXT NOT NULL,
                    anchor_direction TEXT NOT NULL,
                    line_items_json TEXT NOT NULL,
                    category_rule TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'staged',
                    review_reason TEXT NOT NULL DEFAULT '',
                    wave_transaction_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now_iso()),
            )

    # ------------------------------------------------------------------
    # Sync cursors & ingestion batches
    # ------------------------------------------------------------------

    def get_sync_cursor(self, source: str) -> str | None:
        """Return the last persisted sync cursor for a source, if any."""
        row = self.conn.execute(
            "SELECT cursor FROM sync_state WHERE source = ?", (source,)
        ).fetchone()
        return str(row["cursor"]) if row else None

    def set_sync_cursor(self, source: str, cursor: str) -> None:
        """Persist the latest sync cursor for a source."""
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO sync_state (source, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    cursor = excluded.cursor,
                    updated_at = excluded.updated_at
                """,
                (source, cursor, utc_now_iso()),
            )

    def record_ingestion_batch(
        self,
        source: str,
        input_kind: str,
        file_path: str | None = None,
        file_sha256: str | None = None,
        records_read: int = 0,
        records_inserted: int = 0,
    ) -> int:
        """Record an ingestion batch audit row and return its batch ID."""
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO ingestion_batches (
                    source, input_kind, file_path, file_sha256,
                    records_read, records_inserted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    input_kind,
                    file_path,
                    file_sha256,
                    records_read,
                    records_inserted,
                    utc_now_iso(),
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def update_batch_counts(self, batch_id: int, records_read: int, records_inserted: int) -> None:
        """Update record counts for an existing ingestion batch."""
        with self.transaction():
            self.conn.execute(
                """
                UPDATE ingestion_batches
                SET records_read = ?, records_inserted = ?
                WHERE id = ?
                """,
                (records_read, records_inserted, batch_id),
            )

    # ------------------------------------------------------------------
    # Atomic ingestion batches
    # ------------------------------------------------------------------

    def _ingest_tracked_batch(
        self,
        *,
        source: str,
        input_kind: str,
        file_path: str | None,
        file_sha256: str | None,
        upsert_fn: Callable[[int], tuple[int, int]],
        sync_cursor_source: str | None = None,
        sync_cursor_value: str | None = None,
    ) -> tuple[int, int]:
        with self.transaction():
            batch_id = self.record_ingestion_batch(
                source=source,
                input_kind=input_kind,
                file_path=file_path,
                file_sha256=file_sha256,
            )
            read_cnt, ins_cnt = upsert_fn(batch_id)
            self.update_batch_counts(batch_id, read_cnt, ins_cnt)
            if sync_cursor_source and sync_cursor_value:
                self.set_sync_cursor(sync_cursor_source, sync_cursor_value)
        return read_cnt, ins_cnt

    def ingest_bank_transactions_batch(
        self,
        transactions: list[BankTransaction],
        *,
        source: str,
        input_kind: str,
        file_path: str | None = None,
        file_sha256: str | None = None,
        sync_cursor_source: str | None = None,
        sync_cursor_value: str | None = None,
    ) -> tuple[int, int]:
        """Atomically record a batch, upsert bank transactions, and update counts/cursor."""
        return self._ingest_tracked_batch(
            source=source,
            input_kind=input_kind,
            file_path=file_path,
            file_sha256=file_sha256,
            upsert_fn=lambda batch_id: self.upsert_bank_transactions(
                transactions, batch_id=batch_id
            ),
            sync_cursor_source=sync_cursor_source,
            sync_cursor_value=sync_cursor_value,
        )

    def ingest_gusto_payroll_batch(
        self,
        runs: list[GustoPayrollRun],
        *,
        source: str = "gusto",
        input_kind: str = "csv",
        file_path: str | None = None,
        file_sha256: str | None = None,
    ) -> tuple[int, int]:
        """Atomically record a batch, upsert Gusto payroll runs, and update counts."""
        return self._ingest_tracked_batch(
            source=source,
            input_kind=input_kind,
            file_path=file_path,
            file_sha256=file_sha256,
            upsert_fn=lambda batch_id: self.upsert_gusto_payroll_runs(runs, batch_id=batch_id),
        )

    def ingest_harvest_batch(
        self,
        invoices: list[HarvestInvoice],
        *,
        clients: list[HarvestClient] | None = None,
        payments: list[HarvestPayment] | None = None,
        sync_cursor_source: str | None = None,
        sync_cursor_value: str | None = None,
    ) -> tuple[int, int, int]:
        """Atomically upsert Harvest entities, record the ingestion batch, and update cursor."""
        with self.transaction():
            client_cnt = self.upsert_harvest_clients(clients) if clients is not None else 0
            inv_cnt = self.upsert_harvest_invoices(invoices)
            pay_cnt = self.upsert_harvest_payments(payments) if payments is not None else 0
            self.record_ingestion_batch(
                source="harvest",
                input_kind="api",
                records_read=inv_cnt,
                records_inserted=inv_cnt,
            )
            if sync_cursor_source and sync_cursor_value:
                self.set_sync_cursor(sync_cursor_source, sync_cursor_value)
        return client_cnt, inv_cnt, pay_cnt

    # ------------------------------------------------------------------
    # Bank transactions
    # ------------------------------------------------------------------

    def upsert_bank_transactions(
        self, transactions: list[BankTransaction], batch_id: int | None = None
    ) -> tuple[int, int]:
        """Insert bank transactions idempotently by fingerprint. Returns (read, inserted)."""
        now = utc_now_iso()
        inserted = 0
        with self.transaction():
            for tx in transactions:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO bank_transactions (
                        batch_id, source, account_key, external_id, fingerprint,
                        tx_date, posted_date, description, normalized_description,
                        amount, direction, card_last4, status,
                        raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        tx.source,
                        tx.account_key,
                        tx.external_id,
                        tx.fingerprint,
                        tx.tx_date,
                        tx.posted_date,
                        tx.description,
                        tx.normalized_description,
                        format_money(tx.amount),
                        tx.direction,
                        tx.card_last4,
                        tx.status,
                        json.dumps(tx.raw, sort_keys=True),
                        now,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
        return len(transactions), inserted

    @staticmethod
    def _row_to_bank_tx(row: sqlite3.Row) -> BankTransaction:
        return BankTransaction(
            id=int(row["id"]),
            source=str(row["source"]),
            account_key=str(row["account_key"]),
            external_id=row["external_id"],
            fingerprint=str(row["fingerprint"]),
            tx_date=str(row["tx_date"]),
            posted_date=str(row["posted_date"]),
            description=str(row["description"]),
            normalized_description=str(row["normalized_description"]),
            amount=parse_money(row["amount"]),
            direction=str(row["direction"]),
            card_last4=str(row["card_last4"]),
            status=str(row["status"]),
            raw=json.loads(row["raw_json"]),
        )

    def list_bank_transactions(
        self,
        source: str | None = None,
        direction: str | None = None,
        period_prefix: str | None = None,
    ) -> list[BankTransaction]:
        """List bank transactions optionally filtered by source, direction, or YYYY-MM prefix."""
        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if period_prefix:
            clauses.append("posted_date LIKE ?")
            params.append(f"{period_prefix}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM bank_transactions {where} ORDER BY posted_date ASC, id ASC",
            params,
        ).fetchall()
        return [self._row_to_bank_tx(row) for row in rows]

    def get_bank_transaction(self, bank_tx_id: int) -> BankTransaction | None:
        """Fetch a single bank transaction by primary key."""
        row = self.conn.execute(
            "SELECT * FROM bank_transactions WHERE id = ?", (bank_tx_id,)
        ).fetchone()
        return self._row_to_bank_tx(row) if row else None

    # ------------------------------------------------------------------
    # Harvest clients, invoices, payments
    # ------------------------------------------------------------------

    def upsert_harvest_clients(self, clients: list[HarvestClient]) -> int:
        """Upsert Harvest clients and return count processed."""
        now = utc_now_iso()
        with self.transaction():
            for client in clients:
                self.conn.execute(
                    """
                    INSERT INTO harvest_clients (
                        harvest_id, name, currency, is_active, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(harvest_id) DO UPDATE SET
                        name = excluded.name,
                        currency = excluded.currency,
                        is_active = excluded.is_active,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        client.harvest_id,
                        client.name,
                        client.currency,
                        1 if client.is_active else 0,
                        json.dumps(client.raw, sort_keys=True),
                        now,
                    ),
                )
        return len(clients)

    def upsert_harvest_invoices(self, invoices: list[HarvestInvoice]) -> int:
        """Upsert Harvest invoices and return count processed."""
        now = utc_now_iso()
        with self.transaction():
            for inv in invoices:
                line_items_json = json.dumps([item.to_dict() for item in inv.line_items])
                self.conn.execute(
                    """
                    INSERT INTO harvest_invoices (
                        harvest_id, invoice_number, client_harvest_id, client_name,
                        issue_date, due_date, paid_date, state, currency,
                        subtotal, tax_amount, total_amount, due_amount, subject,
                        line_items_json, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(harvest_id) DO UPDATE SET
                        invoice_number = excluded.invoice_number,
                        client_harvest_id = excluded.client_harvest_id,
                        client_name = excluded.client_name,
                        issue_date = excluded.issue_date,
                        due_date = excluded.due_date,
                        paid_date = excluded.paid_date,
                        state = excluded.state,
                        currency = excluded.currency,
                        subtotal = excluded.subtotal,
                        tax_amount = excluded.tax_amount,
                        total_amount = excluded.total_amount,
                        due_amount = excluded.due_amount,
                        subject = excluded.subject,
                        line_items_json = excluded.line_items_json,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        inv.harvest_id,
                        inv.invoice_number,
                        inv.client_harvest_id,
                        inv.client_name,
                        inv.issue_date,
                        inv.due_date,
                        inv.paid_date,
                        inv.state,
                        inv.currency,
                        format_money(inv.subtotal),
                        format_money(inv.tax_amount),
                        format_money(inv.total_amount),
                        format_money(inv.due_amount),
                        inv.subject,
                        line_items_json,
                        json.dumps(inv.raw, sort_keys=True),
                        now,
                    ),
                )
        return len(invoices)

    def upsert_harvest_payments(self, payments: list[HarvestPayment]) -> int:
        """Upsert Harvest payments and return count processed."""
        now = utc_now_iso()
        with self.transaction():
            for pay in payments:
                self.conn.execute(
                    """
                    INSERT INTO harvest_payments (
                        harvest_id, invoice_harvest_id, paid_date, amount,
                        payment_gateway, notes, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(harvest_id) DO UPDATE SET
                        invoice_harvest_id = excluded.invoice_harvest_id,
                        paid_date = excluded.paid_date,
                        amount = excluded.amount,
                        payment_gateway = excluded.payment_gateway,
                        notes = excluded.notes,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        pay.harvest_id,
                        pay.invoice_harvest_id,
                        pay.paid_date,
                        format_money(pay.amount),
                        pay.payment_gateway,
                        pay.notes,
                        json.dumps(pay.raw, sort_keys=True),
                        now,
                    ),
                )
        return len(payments)

    @staticmethod
    def _row_to_harvest_invoice(row: sqlite3.Row) -> HarvestInvoice:
        items_raw = json.loads(row["line_items_json"])
        return HarvestInvoice(
            id=int(row["id"]),
            harvest_id=str(row["harvest_id"]),
            invoice_number=str(row["invoice_number"]),
            client_harvest_id=str(row["client_harvest_id"]),
            client_name=str(row["client_name"]),
            issue_date=str(row["issue_date"]),
            due_date=str(row["due_date"]),
            paid_date=row["paid_date"],
            state=str(row["state"]),
            currency=str(row["currency"]),
            subtotal=parse_money(row["subtotal"]),
            tax_amount=parse_money(row["tax_amount"]),
            total_amount=parse_money(row["total_amount"]),
            due_amount=parse_money(row["due_amount"]),
            subject=str(row["subject"]),
            line_items=[HarvestInvoiceLineItem.from_dict(item) for item in items_raw],
            raw=json.loads(row["raw_json"]),
        )

    def list_harvest_invoices(self, period_prefix: str | None = None) -> list[HarvestInvoice]:
        """List stored Harvest invoices."""
        if period_prefix:
            rows = self.conn.execute(
                """
                SELECT * FROM harvest_invoices
                WHERE issue_date LIKE ?
                ORDER BY issue_date ASC, id ASC
                """,
                (f"{period_prefix}%",),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM harvest_invoices ORDER BY issue_date ASC, id ASC"
            ).fetchall()
        return [self._row_to_harvest_invoice(row) for row in rows]

    def get_harvest_invoice(self, invoice_id: int) -> HarvestInvoice | None:
        """Get a Harvest invoice by SQLite row ID."""
        row = self.conn.execute(
            "SELECT * FROM harvest_invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        return self._row_to_harvest_invoice(row) if row else None

    def list_harvest_payments(self) -> list[HarvestPayment]:
        """List all stored Harvest payments."""
        rows = self.conn.execute(
            "SELECT * FROM harvest_payments ORDER BY paid_date ASC, id ASC"
        ).fetchall()
        return [
            HarvestPayment(
                id=int(row["id"]),
                harvest_id=str(row["harvest_id"]),
                invoice_harvest_id=str(row["invoice_harvest_id"]),
                paid_date=str(row["paid_date"]),
                amount=parse_money(row["amount"]),
                payment_gateway=str(row["payment_gateway"]),
                notes=str(row["notes"]),
                raw=json.loads(row["raw_json"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Gusto payroll runs
    # ------------------------------------------------------------------

    def upsert_gusto_payroll_runs(
        self, runs: list[GustoPayrollRun], batch_id: int | None = None
    ) -> tuple[int, int]:
        """Insert Gusto payroll runs idempotently by fingerprint. Returns (read, inserted)."""
        now = utc_now_iso()
        inserted = 0
        with self.transaction():
            for run in runs:
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO gusto_payroll_runs (
                        batch_id, fingerprint, check_date, pay_period_start, pay_period_end,
                        run_type, gross_wages, reimbursements, employee_taxes, employer_taxes,
                        employee_benefits, employer_benefits, net_pay, tax_breakdown_json,
                        employee_count, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        run.fingerprint,
                        run.check_date,
                        run.pay_period_start,
                        run.pay_period_end,
                        run.run_type,
                        format_money(run.gross_wages),
                        format_money(run.reimbursements),
                        format_money(run.employee_taxes),
                        format_money(run.employer_taxes),
                        format_money(run.employee_benefits),
                        format_money(run.employer_benefits),
                        format_money(run.net_pay),
                        json.dumps(run.tax_breakdown, sort_keys=True),
                        run.employee_count,
                        json.dumps(run.raw, sort_keys=True),
                        now,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
        return len(runs), inserted

    @staticmethod
    def _row_to_gusto_run(row: sqlite3.Row) -> GustoPayrollRun:
        return GustoPayrollRun(
            id=int(row["id"]),
            fingerprint=str(row["fingerprint"]),
            check_date=str(row["check_date"]),
            pay_period_start=str(row["pay_period_start"]),
            pay_period_end=str(row["pay_period_end"]),
            run_type=str(row["run_type"]),
            gross_wages=parse_money(row["gross_wages"]),
            reimbursements=parse_money(row["reimbursements"]),
            employee_taxes=parse_money(row["employee_taxes"]),
            employer_taxes=parse_money(row["employer_taxes"]),
            employee_benefits=parse_money(row["employee_benefits"]),
            employer_benefits=parse_money(row["employer_benefits"]),
            net_pay=parse_money(row["net_pay"]),
            tax_breakdown=json.loads(row["tax_breakdown_json"]),
            employee_count=int(row["employee_count"]),
            raw=json.loads(row["raw_json"]),
        )

    def list_gusto_payroll_runs(self, period_prefix: str | None = None) -> list[GustoPayrollRun]:
        """List stored Gusto payroll runs."""
        if period_prefix:
            rows = self.conn.execute(
                """
                SELECT * FROM gusto_payroll_runs
                WHERE check_date LIKE ?
                ORDER BY check_date ASC, id ASC
                """,
                (f"{period_prefix}%",),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM gusto_payroll_runs ORDER BY check_date ASC, id ASC"
            ).fetchall()
        return [self._row_to_gusto_run(row) for row in rows]

    def get_gusto_payroll_run(self, gusto_run_id: int) -> GustoPayrollRun | None:
        """Fetch a Gusto payroll run by primary key."""
        row = self.conn.execute(
            "SELECT * FROM gusto_payroll_runs WHERE id = ?", (gusto_run_id,)
        ).fetchone()
        return self._row_to_gusto_run(row) if row else None

    # ------------------------------------------------------------------
    # Reconciliation matches
    # ------------------------------------------------------------------

    def upsert_match(self, match: ReconciliationMatch) -> int:
        """Insert or update a reconciliation match without overwriting confirmed/rejected status."""
        now = utc_now_iso()
        existing = self.conn.execute(
            """
            SELECT id, status FROM reconciliation_matches
            WHERE match_type = ?
              AND bank_tx_id = ?
              AND COALESCE(counterparty_bank_tx_id, -1) = COALESCE(?, -1)
              AND COALESCE(harvest_invoice_id, -1) = COALESCE(?, -1)
              AND COALESCE(harvest_payment_id, -1) = COALESCE(?, -1)
              AND COALESCE(gusto_run_id, -1) = COALESCE(?, -1)
            """,
            (
                match.match_type,
                match.bank_tx_id,
                match.counterparty_bank_tx_id,
                match.harvest_invoice_id,
                match.harvest_payment_id,
                match.gusto_run_id,
            ),
        ).fetchone()

        if existing:
            match_id = int(existing["id"])
            current_status = str(existing["status"])
            new_status = (
                current_status if current_status in {"confirmed", "rejected"} else match.status
            )
            with self.transaction():
                self.conn.execute(
                    """
                    UPDATE reconciliation_matches
                    SET matched_amount = ?, confidence = ?, reason = ?,
                        status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        format_money(match.matched_amount),
                        match.confidence,
                        match.reason,
                        new_status,
                        now,
                        match_id,
                    ),
                )
            return match_id

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO reconciliation_matches (
                    match_type, bank_tx_id, counterparty_bank_tx_id,
                    harvest_invoice_id, harvest_payment_id, gusto_run_id,
                    matched_amount, confidence, reason, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match.match_type,
                    match.bank_tx_id,
                    match.counterparty_bank_tx_id,
                    match.harvest_invoice_id,
                    match.harvest_payment_id,
                    match.gusto_run_id,
                    format_money(match.matched_amount),
                    match.confidence,
                    match.reason,
                    match.status,
                    now,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    @staticmethod
    def _row_to_match(row: sqlite3.Row) -> ReconciliationMatch:
        return ReconciliationMatch(
            id=int(row["id"]),
            match_type=str(row["match_type"]),
            bank_tx_id=int(row["bank_tx_id"]),
            counterparty_bank_tx_id=(
                int(row["counterparty_bank_tx_id"])
                if row["counterparty_bank_tx_id"] is not None
                else None
            ),
            harvest_invoice_id=(
                int(row["harvest_invoice_id"]) if row["harvest_invoice_id"] is not None else None
            ),
            harvest_payment_id=(
                int(row["harvest_payment_id"]) if row["harvest_payment_id"] is not None else None
            ),
            gusto_run_id=(int(row["gusto_run_id"]) if row["gusto_run_id"] is not None else None),
            matched_amount=parse_money(row["matched_amount"]),
            confidence=float(row["confidence"]),
            reason=str(row["reason"]),
            status=str(row["status"]),
        )

    def list_matches(
        self, status: str | None = None, match_type: str | None = None
    ) -> list[ReconciliationMatch]:
        """List reconciliation matches filtered optionally by status and match_type."""
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if match_type:
            clauses.append("match_type = ?")
            params.append(match_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM reconciliation_matches {where} ORDER BY id ASC",
            params,
        ).fetchall()
        return [self._row_to_match(row) for row in rows]

    def set_match_status(self, match_id: int, status: str) -> bool:
        """Update status ('proposed', 'confirmed', 'rejected') for a match."""
        with self.transaction():
            cur = self.conn.execute(
                "UPDATE reconciliation_matches SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now_iso(), match_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Wave metadata cache
    # ------------------------------------------------------------------

    def upsert_wave_accounts(self, accounts: list[WaveAccount]) -> int:
        """Replace/upsert cached Wave chart of accounts."""
        now = utc_now_iso()
        with self.transaction():
            for acc in accounts:
                self.conn.execute(
                    """
                    INSERT INTO wave_accounts (
                        wave_id, name, account_type, account_subtype,
                        normal_balance_type, is_archived, description, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wave_id) DO UPDATE SET
                        name = excluded.name,
                        account_type = excluded.account_type,
                        account_subtype = excluded.account_subtype,
                        normal_balance_type = excluded.normal_balance_type,
                        is_archived = excluded.is_archived,
                        description = excluded.description,
                        updated_at = excluded.updated_at
                    """,
                    (
                        acc.wave_id,
                        acc.name,
                        acc.account_type,
                        acc.account_subtype,
                        acc.normal_balance_type,
                        1 if acc.is_archived else 0,
                        acc.description,
                        now,
                    ),
                )
        return len(accounts)

    def upsert_wave_customers(self, customers: list[WaveCustomer]) -> int:
        """Upsert cached Wave customers."""
        now = utc_now_iso()
        with self.transaction():
            for cust in customers:
                self.conn.execute(
                    """
                    INSERT INTO wave_customers (wave_id, name, email, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(wave_id) DO UPDATE SET
                        name = excluded.name,
                        email = excluded.email,
                        updated_at = excluded.updated_at
                    """,
                    (cust.wave_id, cust.name, cust.email, now),
                )
        return len(customers)

    def list_wave_accounts(self) -> list[WaveAccount]:
        """List all cached Wave accounts."""
        rows = self.conn.execute("SELECT * FROM wave_accounts ORDER BY name ASC").fetchall()
        return [
            WaveAccount(
                wave_id=str(row["wave_id"]),
                name=str(row["name"]),
                account_type=str(row["account_type"]),
                account_subtype=str(row["account_subtype"]),
                normal_balance_type=str(row["normal_balance_type"]),
                is_archived=bool(row["is_archived"]),
                description=str(row["description"]),
            )
            for row in rows
        ]

    def resolve_wave_account(
        self, account_key: str, account_map: dict[str, str] | None = None
    ) -> WaveAccount | None:
        """Resolve a logical account_key, Wave ID, or Wave account name from cache."""
        target = account_map.get(account_key, account_key) if account_map else account_key
        row = self.conn.execute(
            "SELECT * FROM wave_accounts WHERE wave_id = ?", (target,)
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM wave_accounts WHERE LOWER(name) = LOWER(?) AND is_archived = 0",
                (target,),
            ).fetchone()
        if row is None:
            return None
        return WaveAccount(
            wave_id=str(row["wave_id"]),
            name=str(row["name"]),
            account_type=str(row["account_type"]),
            account_subtype=str(row["account_subtype"]),
            normal_balance_type=str(row["normal_balance_type"]),
            is_archived=bool(row["is_archived"]),
            description=str(row["description"]),
        )

    def find_wave_customer_by_name(self, name: str) -> WaveCustomer | None:
        """Lookup cached Wave customer case-insensitively by name."""
        row = self.conn.execute(
            "SELECT * FROM wave_customers WHERE LOWER(name) = LOWER(?)", (name.strip(),)
        ).fetchone()
        if not row:
            return None
        return WaveCustomer(
            wave_id=str(row["wave_id"]), name=str(row["name"]), email=str(row["email"])
        )

    # ------------------------------------------------------------------
    # Staged transactions
    # ------------------------------------------------------------------

    def upsert_staged_transaction(self, tx: StagedTransaction) -> int:
        """Upsert a staged transaction unless it has already been pushed or skipped."""
        now = utc_now_iso()
        existing = self.conn.execute(
            "SELECT id, status, category_rule FROM staged_transactions WHERE bank_tx_id = ?",
            (tx.bank_tx_id,),
        ).fetchone()
        line_items_json = json.dumps([item.to_dict() for item in tx.line_items])

        if existing:
            existing_id = int(existing["id"])
            existing_status = str(existing["status"])
            existing_rule = str(existing["category_rule"])
            if existing_status in {"pushed", "skipped", "outcome_unknown"}:
                return existing_id
            if existing_rule == "manual_recategorize" and existing_status in {"staged", "approved"}:
                return existing_id

            with self.transaction():
                self.conn.execute(
                    """
                    UPDATE staged_transactions
                    SET external_id = ?, tx_date = ?, description = ?, notes = ?,
                        anchor_account_key = ?, anchor_wave_account_id = ?,
                        anchor_amount = ?, anchor_direction = ?, line_items_json = ?,
                        category_rule = ?, status = ?, review_reason = ?,
                        last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        tx.external_id,
                        tx.tx_date,
                        tx.description,
                        tx.notes,
                        tx.anchor_account_key,
                        tx.anchor_wave_account_id,
                        format_money(tx.anchor_amount),
                        tx.anchor_direction,
                        line_items_json,
                        tx.category_rule,
                        tx.status,
                        tx.review_reason,
                        now,
                        existing_id,
                    ),
                )
            return existing_id

        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO staged_transactions (
                    external_id, bank_tx_id, tx_date, description, notes,
                    anchor_account_key, anchor_wave_account_id, anchor_amount,
                    anchor_direction, line_items_json, category_rule, status,
                    review_reason, wave_transaction_id, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tx.external_id,
                    tx.bank_tx_id,
                    tx.tx_date,
                    tx.description,
                    tx.notes,
                    tx.anchor_account_key,
                    tx.anchor_wave_account_id,
                    format_money(tx.anchor_amount),
                    tx.anchor_direction,
                    line_items_json,
                    tx.category_rule,
                    tx.status,
                    tx.review_reason,
                    tx.wave_transaction_id,
                    tx.last_error,
                    now,
                    now,
                ),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    @staticmethod
    def _row_to_staged_tx(row: sqlite3.Row) -> StagedTransaction:
        items_raw = json.loads(row["line_items_json"])
        return StagedTransaction(
            id=int(row["id"]),
            external_id=str(row["external_id"]),
            bank_tx_id=int(row["bank_tx_id"]),
            tx_date=str(row["tx_date"]),
            description=str(row["description"]),
            notes=str(row["notes"]),
            anchor_account_key=str(row["anchor_account_key"]),
            anchor_wave_account_id=row["anchor_wave_account_id"],
            anchor_amount=parse_money(row["anchor_amount"]),
            anchor_direction=str(row["anchor_direction"]),
            line_items=[StagedLineItem.from_dict(item) for item in items_raw],
            category_rule=str(row["category_rule"]),
            status=str(row["status"]),
            review_reason=str(row["review_reason"]),
            wave_transaction_id=row["wave_transaction_id"],
            last_error=row["last_error"],
        )

    def list_staged_transactions(
        self, status: str | None = None, period_prefix: str | None = None
    ) -> list[StagedTransaction]:
        """List staged transactions optionally filtered by status or YYYY-MM prefix."""
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if period_prefix:
            clauses.append("tx_date LIKE ?")
            params.append(f"{period_prefix}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM staged_transactions {where} ORDER BY tx_date ASC, id ASC",
            params,
        ).fetchall()
        return [self._row_to_staged_tx(row) for row in rows]

    def get_staged_transaction(self, staged_tx_id: int) -> StagedTransaction | None:
        """Fetch a single staged transaction by ID."""
        row = self.conn.execute(
            "SELECT * FROM staged_transactions WHERE id = ?", (staged_tx_id,)
        ).fetchone()
        return self._row_to_staged_tx(row) if row else None

    def approve_staged(self, all_staged: bool = False, tx_ids: list[int] | None = None) -> int:
        """Approve staged transactions. Returns the number approved."""
        now = utc_now_iso()
        tx_count = 0
        with self.transaction():
            if all_staged:
                cur = self.conn.execute(
                    """
                    UPDATE staged_transactions
                    SET status = 'approved', updated_at = ?
                    WHERE status = 'staged'
                    """,
                    (now,),
                )
                tx_count += cur.rowcount

            if tx_ids:
                placeholders = ",".join("?" for _ in tx_ids)
                cur = self.conn.execute(
                    f"""
                    UPDATE staged_transactions
                    SET status = 'approved', updated_at = ?
                    WHERE id IN ({placeholders})
                      AND status IN ('staged', 'needs_review', 'failed', 'outcome_unknown')
                    """,
                    [now, *tx_ids],
                )
                tx_count += cur.rowcount

        return tx_count

    def recategorize_staged_tx(
        self,
        staged_tx_id: int,
        account_key: str,
        balance: str,
        wave_account_id: str | None = None,
        note: str | None = None,
    ) -> StagedTransaction:
        """Replace line items on a staged transaction and mark it staged for approval."""
        tx = self.get_staged_transaction(staged_tx_id)
        if tx is None:
            raise KeyError(f"Staged transaction {staged_tx_id} not found")
        if tx.status == "pushed":
            raise ValueError(f"Staged transaction {staged_tx_id} is already pushed")
        if tx.status == "outcome_unknown":
            raise ValueError(
                f"Staged transaction {staged_tx_id} may already exist in Wave (outcome unknown). "
                f"Check Wave for externalId {tx.external_id}; re-send it with "
                f"'approve --tx {staged_tx_id}' only if it is missing."
            )

        new_line = StagedLineItem(
            account_key=account_key,
            wave_account_id=wave_account_id,
            amount=tx.anchor_amount,
            balance=balance,
            description=tx.description,
        )
        new_notes = f"{tx.notes}; {note}".strip("; ") if note else tx.notes
        now = utc_now_iso()
        with self.transaction():
            self.conn.execute(
                """
                UPDATE staged_transactions
                SET line_items_json = ?, notes = ?, category_rule = 'manual_recategorize',
                    status = 'staged', review_reason = '', last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps([new_line.to_dict()]), new_notes, now, staged_tx_id),
            )
        updated = self.get_staged_transaction(staged_tx_id)
        assert updated is not None
        return updated

    def mark_staged_pushed(self, staged_id: int, wave_id: str) -> None:
        """Mark a staged transaction as pushed with its remote Wave ID."""
        with self.transaction():
            self.conn.execute(
                """
                UPDATE staged_transactions
                SET status = 'pushed', wave_transaction_id = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (wave_id, utc_now_iso(), staged_id),
            )

    def mark_staged_failed(self, staged_id: int, error: str) -> None:
        """Mark a staged transaction as failed with an error message."""
        self._mark_staged_error(staged_id, "failed", error)

    def mark_staged_outcome_unknown(self, staged_id: int, error: str) -> None:
        """Mark a staged transaction whose Wave create may or may not have been applied.

        Re-planning, ``approve --all-staged`` and recategorizing leave it alone; only an
        explicit ``approve --tx`` re-queues it, after the user has checked Wave.
        """
        self._mark_staged_error(staged_id, "outcome_unknown", error)

    def _mark_staged_error(self, staged_id: int, status: str, error: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE staged_transactions SET status = ?, last_error = ?, updated_at = ?"
                " WHERE id = ?",
                (status, error, utc_now_iso(), staged_id),
            )
