"""Command-line interface for Ledgerdemain."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ledgerdemain.adapters.base import sha256_file
from ledgerdemain.adapters.capital_one import CapitalOneCsvAdapter, CapitalOnePdfAdapter
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter, GrasshopperCsvAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter, HarvestCsvAdapter
from ledgerdemain.adapters.ofx import OfxStatement, OfxTransactionParser
from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import AppConfig, ConfigError, load_config, load_rules
from ledgerdemain.db import LedgerDB
from ledgerdemain.money import format_money
from ledgerdemain.planner import Planner, validate_double_entry_balance
from ledgerdemain.reconcile import Reconciler
from ledgerdemain.report import build_report, format_report_text
from ledgerdemain.wave.client import WaveGraphQLClient
from ledgerdemain.wave.sync import WaveSyncService

_DEFAULT_CONFIG_FALLBACK = """[general]
db_path = "data/ledgerdemain.db"
inbox_dir = "inbox"
rules_path = "rules.toml"
currency = "USD"

[accounting]
mirror_harvest_invoices = true
mirrored_invoice_deposit_strategy = "ar_clearing"
wave_invoice_create_status = "SAVED"
transfer_strategy = "manual_transfer"
deposit_match_window_days = 7
payroll_match_window_days = 5
transfer_match_window_days = 5
auto_confirm_threshold = 0.90

[wave]
endpoint = "https://gql.waveapps.com/graphql/public"
business_id = "${WAVE_BUSINESS_ID}"
access_token = "${WAVE_ACCESS_TOKEN}"
default_product_name = "Consulting Services"

[harvest]
base_url = "https://api.harvestapp.com/v2"
account_id = "${HARVEST_ACCOUNT_ID}"
access_token = "${HARVEST_ACCESS_TOKEN}"
user_agent = "Ledgerdemain (accounting@example.com)"

[grasshopper]
base_url = "https://api.grasshopper.bank/v1"
access_token = "${GRASSHOPPER_ACCESS_TOKEN}"
header_secret = "${GRASSHOPPER_HEADER_SECRET}"
header_secret_name = "X-API-Secret"

[[grasshopper.accounts]]
account_key = "grasshopper_savings"
account_id = "${GRASSHOPPER_SAVINGS_ACCOUNT_ID}"
acctid_last4 = "${GRASSHOPPER_SAVINGS_LAST4}"

[[grasshopper.accounts]]
account_key = "grasshopper_money_market"
account_id = "${GRASSHOPPER_MM_ACCOUNT_ID}"
acctid_last4 = "${GRASSHOPPER_MM_LAST4}"

[accounts]
grasshopper_checking = "Grasshopper Checking"
grasshopper_savings = "Grasshopper Savings"
grasshopper_money_market = "Grasshopper Money Market"
capital_one_card = "Capital One Credit Card"
revenue_default = "Sales"
interest_income = "Interest Income"
invoice_payments_clearing = "Invoice Payments Clearing"
merchant_fees = "Merchant Processing Fees"
payroll_gross_wages = "Payroll - Gross Wages"
payroll_reimbursements = "Employee Reimbursements"
payroll_employer_taxes = "Payroll - Employer Taxes"
payroll_employer_benefits = "Payroll - Employer Benefits"
officer_health_insurance = "Officer / Shareholder Health Insurance"
officer_hsa = "Officer / Shareholder HSA"
payroll_taxes_payable = "Payroll Taxes Payable"
payroll_benefits_payable = "Payroll Benefits Payable"
payroll_processing_fees = "Payroll Processing Fees"
contractors_expense = "Contractors"
cc_transfer_clearing = "Credit Card Payment Clearing"
bank_transfer_clearing = "Bank Transfer Clearing"
software_subscriptions = "Software & Subscriptions"
cloud_hosting = "Cloud Hosting & Infrastructure"
office_supplies = "Office Supplies"
travel = "Travel"
meals = "Meals & Entertainment"
bank_fees = "Bank Service Charges"
uncategorized_expense = "Uncategorized Expense"

[patterns]
gusto_payroll = ["(?i)\\\\bgusto\\\\b"]
gusto_fee = ["(?i)gusto\\\\s*(fee|sub|invoice|monthly)"]
grasshopper_cc_payment = ["(?i)capital\\\\s*one", "(?i)cap\\\\s*one", "(?i)crcardpmt"]
capital_one_payment = [
  "(?i)capital\\\\s*one\\\\s*(autopay|online|mobile)\\\\s*pymt",
  "(?i)payment\\\\s*-?\\\\s*thank\\\\s*you",
  "(?i)^autopay\\\\b",
]
bank_interest = ["(?i)\\\\binterest\\\\b"]
"""

_DEFAULT_RULES_FALLBACK = """[[rules]]
name = "Gusto Monthly Fee"
priority = 10
source = "grasshopper"
direction = "WITHDRAWAL"
description_regex = "(?i)gusto\\\\s*(fee|sub|invoice|monthly)"
account_key = "payroll_processing_fees"

[[rules]]
name = "Bank Interest Earned"
priority = 15
source = "grasshopper"
direction = "DEPOSIT"
description_regex = "(?i)\\\\binterest\\\\b"
account_key = "interest_income"

[[rules]]
name = "Officer / Shareholder Health Insurance (COBRA)"
priority = 17
source = "grasshopper"
direction = "WITHDRAWAL"
description_regex = "(?i)inspirafinancial"
account_key = "officer_health_insurance"
notes = "S-Corp officer/shareholder health insurance (COBRA)"

[[rules]]
name = "Officer / Shareholder HSA Contributions"
priority = 18
source = "grasshopper"
direction = "WITHDRAWAL"
description_regex = "(?i)(hsa\\\\s*con\\\\s*tribution|hsa\\\\s*cont\\\\s*ribution)"
account_key = "officer_hsa"
notes = "S-Corp officer/shareholder HSA contribution"

[[rules]]
name = "Cloud Hosting Vendors"
priority = 20
direction = "WITHDRAWAL"
description_regex = "(?i)(aws\\\\b|amazon\\\\s*web\\\\s*services|gcp\\\\b|google\\\\s*cloud|github)"
account_key = "cloud_hosting"

[[rules]]
name = "Software Subscriptions"
priority = 30
direction = "WITHDRAWAL"
description_regex = "(?i)(figma|slack|notion|linear|1password|openai|anthropic|zoom)"
account_key = "software_subscriptions"

[[rules]]
name = "Software Refunds"
priority = 35
source = "capital_one"
direction = "DEPOSIT"
description_regex = "(?i)(figma|slack|notion|linear|github|aws)"
account_key = "software_subscriptions"

[[rules]]
name = "Bank & Wire Fees"
priority = 40
source = "grasshopper"
direction = "WITHDRAWAL"
description_regex = "(?i)(wire\\\\s*fee|service\\\\s*charge|bank\\\\s*fee)"
account_key = "bank_fees"
"""


def _copy_template_or_write(template_name: str, dest: Path, fallback: str) -> None:
    if dest.exists():
        return
    repo_template = Path(__file__).resolve().parents[2] / "config" / template_name
    if repo_template.exists():
        shutil.copy2(repo_template, dest)
    else:
        dest.write_text(fallback, encoding="utf-8")


def _cmd_init(args: argparse.Namespace) -> int:
    target_dir = Path(args.dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    for sub in (
        "data",
        "inbox/grasshopper",
        "inbox/capital_one",
        "inbox/gusto",
        "inbox/harvest",
    ):
        (target_dir / sub).mkdir(parents=True, exist_ok=True)

    cfg_path = target_dir / "ledgerdemain.toml"
    rules_path = target_dir / "rules.toml"
    _copy_template_or_write("ledgerdemain.example.toml", cfg_path, _DEFAULT_CONFIG_FALLBACK)
    _copy_template_or_write("rules.example.toml", rules_path, _DEFAULT_RULES_FALLBACK)

    cfg = load_config(cfg_path)
    with LedgerDB(cfg.general.db_path) as db:
        db.init_schema()

    print(f"Initialized Ledgerdemain workspace at {target_dir}")
    print(f"  Config: {cfg_path}")
    print(f"  Rules:  {rules_path}")
    print(f"  DB:     {cfg.general.db_path}")
    return 0


def _open_context(config_path: Path) -> tuple[AppConfig, LedgerDB]:
    cfg = load_config(config_path)
    db = LedgerDB(cfg.general.db_path)
    db.init_schema()
    return cfg, db


def _ingest_harvest_api(db: LedgerDB, cfg: AppConfig, since: str | None) -> tuple[int, int, int]:
    adapter = HarvestApiAdapter(cfg.harvest)
    cursor = since or db.get_sync_cursor("harvest_invoices")
    clients = adapter.fetch_clients()
    invoices = adapter.fetch_invoices(updated_since=cursor)
    payments = []
    max_date = cursor or ""
    for inv in invoices:
        if inv.issue_date > max_date:
            max_date = inv.issue_date
        if inv.state in {"paid", "partial"} or inv.paid_date:
            payments.extend(adapter.fetch_payments_for_invoice(inv.harvest_id))

    db.upsert_harvest_clients(clients)
    db.upsert_harvest_invoices(invoices)
    db.upsert_harvest_payments(payments)
    db.record_ingestion_batch(
        source="harvest",
        input_kind="api",
        records_read=len(invoices),
        records_inserted=len(invoices),
    )
    if max_date:
        db.set_sync_cursor("harvest_invoices", max_date)
    return len(clients), len(invoices), len(payments)


def _ingest_harvest_file(db: LedgerDB, path: Path) -> int:
    invoices = HarvestCsvAdapter().parse_file(path)
    db.upsert_harvest_invoices(invoices)
    db.record_ingestion_batch(
        source="harvest",
        input_kind="csv",
        file_path=str(path),
        file_sha256=sha256_file(path),
        records_read=len(invoices),
        records_inserted=len(invoices),
    )
    return len(invoices)


def _ingest_grasshopper_api(
    db: LedgerDB,
    cfg: AppConfig,
    from_date: str | None,
    to_date: str | None,
    account_key_filter: str | None = None,
) -> tuple[int, int]:
    adapter = GrasshopperApiAdapter(cfg.grasshopper)
    accounts = [
        acct
        for acct in cfg.grasshopper.configured_accounts
        if acct.account_id
        and (account_key_filter is None or acct.account_key == account_key_filter)
    ]
    if not accounts:
        raise ConfigError(
            "Grasshopper API requires grasshopper.account_id or [[grasshopper.accounts]] "
            "with a populated account_id"
        )

    total_read = 0
    total_inserted = 0
    for acct in accounts:
        cursor_name = f"grasshopper:{acct.account_key}"
        effective_from = (
            from_date or db.get_sync_cursor(cursor_name) or db.get_sync_cursor("grasshopper")
        )
        txs = adapter.fetch_transactions(
            from_date=effective_from,
            to_date=to_date,
            account_id=acct.account_id,
            account_key=acct.account_key,
        )
        batch_id = db.record_ingestion_batch(source="grasshopper", input_kind="api")
        read_cnt, ins_cnt = db.upsert_bank_transactions(txs, batch_id=batch_id)
        db.update_batch_counts(batch_id, read_cnt, ins_cnt)
        if txs:
            latest = max(tx.posted_date for tx in txs)
            db.set_sync_cursor(cursor_name, latest)
        total_read += read_cnt
        total_inserted += ins_cnt
        print(f"  Grasshopper API ({acct.account_key}): read {read_cnt}, inserted {ins_cnt}.")
    return total_read, total_inserted


def _print_ofx_summary(path: Path, stmt: OfxStatement, account_key: str = "") -> None:
    date_range = f"{stmt.start_date or '?'}..{stmt.end_date or '?'}"
    acct_tag = f" [{account_key}]" if account_key else ""
    bal = (
        f" | ledger_bal=${format_money(stmt.ledger_balance)}"
        if stmt.ledger_balance is not None
        else ""
    )
    print(f"  OFX ({path.name}){acct_tag}: {date_range}{bal}")


def _ingest_grasshopper_file(
    db: LedgerDB,
    cfg: AppConfig,
    path: Path,
    account_key_override: str | None = None,
) -> tuple[int, int]:
    if path.suffix.lower() in {".ofx", ".qfx", ".qbo"}:
        probe = OfxTransactionParser().parse_statement_file(
            path, source="grasshopper", account_key="grasshopper_checking"
        )
        acct_key = account_key_override or cfg.grasshopper.resolve_account_key(
            ofx_account_id=probe.account_id, filename=path.name
        )
        stmt = OfxTransactionParser().parse_statement_file(
            path, source="grasshopper", account_key=acct_key
        )
        txs = stmt.transactions
        kind = "ofx"
        _print_ofx_summary(path, stmt, account_key=acct_key)
    else:
        acct_key = account_key_override or cfg.grasshopper.resolve_account_key(filename=path.name)
        txs = GrasshopperCsvAdapter().parse_file(path, account_key=acct_key)
        kind = "csv"
    batch_id = db.record_ingestion_batch(
        source="grasshopper",
        input_kind=kind,
        file_path=str(path),
        file_sha256=sha256_file(path),
    )
    read_cnt, ins_cnt = db.upsert_bank_transactions(txs, batch_id=batch_id)
    db.update_batch_counts(batch_id, read_cnt, ins_cnt)
    return read_cnt, ins_cnt
    db.update_batch_counts(batch_id, read_cnt, ins_cnt)
    return read_cnt, ins_cnt


def _ingest_capital_one_file(db: LedgerDB, path: Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        txs = CapitalOnePdfAdapter().parse_file(path)
        kind = "pdf"
    elif suffix in {".ofx", ".qfx", ".qbo"}:
        stmt = OfxTransactionParser().parse_statement_file(
            path, source="capital_one", account_key="capital_one_card"
        )
        txs = stmt.transactions
        kind = "ofx"
        _print_ofx_summary(path, stmt)
    else:
        txs = CapitalOneCsvAdapter().parse_file(path)
        kind = "csv"

    batch_id = db.record_ingestion_batch(
        source="capital_one",
        input_kind=kind,
        file_path=str(path),
        file_sha256=sha256_file(path),
    )
    read_cnt, ins_cnt = db.upsert_bank_transactions(txs, batch_id=batch_id)
    db.update_batch_counts(batch_id, read_cnt, ins_cnt)
    return read_cnt, ins_cnt


def _ingest_gusto_file(db: LedgerDB, cfg: AppConfig, path: Path, run_type: str) -> tuple[int, int]:
    effective_type = (
        "contractor" if run_type == "contractor" or "contractor" in path.name.lower() else run_type
    )
    runs = GustoPayrollCsvAdapter(cfg.gusto_column_overrides).parse_file(
        path, run_type=effective_type
    )
    batch_id = db.record_ingestion_batch(
        source="gusto",
        input_kind="csv",
        file_path=str(path),
        file_sha256=sha256_file(path),
    )
    read_cnt, ins_cnt = db.upsert_gusto_payroll_runs(runs, batch_id=batch_id)
    db.update_batch_counts(batch_id, read_cnt, ins_cnt)
    return read_cnt, ins_cnt


def _inbox_files(inbox_dir: Path, subdir: str, extensions: tuple[str, ...]) -> list[Path]:
    folder = inbox_dir / subdir
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in extensions)


def _cmd_wave_list_businesses(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    if not cfg.wave.access_token:
        raise ConfigError("Wave list-businesses requires wave.access_token to be configured")
    businesses = WaveGraphQLClient(cfg.wave.endpoint, cfg.wave.access_token).list_businesses()
    print(f"Wave businesses ({len(businesses)}):")
    for biz in businesses:
        curr = (biz.get("currency") or {}).get("code", "")
        print(
            f"  id={biz.get('id')} name={biz.get('name')!r} "
            f"currency={curr} isClassicAccounting={biz.get('isClassicAccounting')}"
        )
    return 0


def _cmd_grasshopper_list_accounts(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    accounts = GrasshopperApiAdapter(cfg.grasshopper).list_accounts()
    print(f"Grasshopper accounts ({len(accounts)}):")
    for acct in accounts:
        print(
            f"  id={acct['id']} name={acct['name']!r} type={acct['type']!r} "
            f"last4={acct['account_number_last4']!r} balance={acct['balance']!r}"
        )
    return 0


def _cmd_wave_sync_meta(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    with db:
        summary = WaveSyncService(db, cfg).sync_metadata()
    print(
        f"Synced Wave metadata for {summary.business_name!r}: "
        f"{summary.accounts_synced} accounts, "
        f"{summary.customers_synced} customers, "
        f"{summary.products_synced} products."
    )
    for warn in summary.warnings:
        print(f"WARNING: {warn}", file=sys.stderr)
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    with db:
        target = args.ingest_target
        if target == "harvest":
            if args.file:
                cnt = _ingest_harvest_file(db, Path(args.file))
                print(f"Harvest CSV: ingested {cnt} invoice(s).")
            elif args.api or (cfg.harvest.access_token and cfg.harvest.account_id):
                c, i, p = _ingest_harvest_api(db, cfg, args.since)
                print(f"Harvest API: synced {c} client(s), {i} invoice(s), {p} payment(s).")
            else:
                files = _inbox_files(cfg.general.inbox_dir, "harvest", (".csv",))
                total = sum(_ingest_harvest_file(db, f) for f in files)
                print(f"Harvest inbox: processed {len(files)} file(s), {total} invoice(s).")

        elif target == "grasshopper":
            has_gh_api = bool(
                cfg.grasshopper.access_token
                and any(a.account_id for a in cfg.grasshopper.configured_accounts)
            )
            acct_override = getattr(args, "account_key", None)
            if args.file:
                r, ins = _ingest_grasshopper_file(
                    db, cfg, Path(args.file), account_key_override=acct_override
                )
                print(f"Grasshopper file: read {r}, inserted {ins} new transaction(s).")
            elif args.api or (not args.inbox and has_gh_api):
                r, ins = _ingest_grasshopper_api(
                    db, cfg, args.from_date, args.to_date, account_key_filter=acct_override
                )
                print(f"Grasshopper API: read {r}, inserted {ins} new transaction(s).")
            else:
                files = _inbox_files(
                    cfg.general.inbox_dir, "grasshopper", (".csv", ".ofx", ".qfx", ".qbo")
                )
                total_r = total_i = 0
                for f in files:
                    r, ins = _ingest_grasshopper_file(
                        db, cfg, f, account_key_override=acct_override
                    )
                    total_r += r
                    total_i += ins
                print(
                    f"Grasshopper inbox: {len(files)} file(s), read {total_r}, inserted {total_i}."
                )

        elif target == "capital-one":
            files = (
                [Path(args.file)]
                if args.file
                else _inbox_files(
                    cfg.general.inbox_dir,
                    "capital_one",
                    (".csv", ".pdf", ".ofx", ".qfx", ".qbo"),
                )
            )
            total_r = total_i = 0
            for f in files:
                r, ins = _ingest_capital_one_file(db, f)
                total_r += r
                total_i += ins
            print(
                f"Capital One: {len(files)} file(s), read {total_r}, inserted {total_i} new tx(s)."
            )

        elif target == "gusto":
            files = (
                [Path(args.file)]
                if args.file
                else _inbox_files(cfg.general.inbox_dir, "gusto", (".csv",))
            )
            total_r = total_i = 0
            for f in files:
                r, ins = _ingest_gusto_file(db, cfg, f, args.run_type)
                total_r += r
                total_i += ins
            print(
                f"Gusto: {len(files)} file(s), read {total_r}, inserted {total_i} payroll run(s)."
            )

        elif target == "all":
            if cfg.harvest.access_token and cfg.harvest.account_id:
                c, i, p = _ingest_harvest_api(db, cfg, None)
                print(f"Harvest API: {c} clients, {i} invoices, {p} payments.")
            for f in _inbox_files(cfg.general.inbox_dir, "harvest", (".csv",)):
                cnt = _ingest_harvest_file(db, f)
                print(f"Harvest CSV ({f.name}): {cnt} invoices.")

            has_gh_api = bool(
                cfg.grasshopper.access_token
                and any(a.account_id for a in cfg.grasshopper.configured_accounts)
            )
            if has_gh_api:
                r, ins = _ingest_grasshopper_api(db, cfg, None, None)
                print(f"Grasshopper API: read {r}, inserted {ins}.")
            for f in _inbox_files(
                cfg.general.inbox_dir, "grasshopper", (".csv", ".ofx", ".qfx", ".qbo")
            ):
                r, ins = _ingest_grasshopper_file(db, cfg, f)
                print(f"Grasshopper file ({f.name}): read {r}, inserted {ins}.")

            for f in _inbox_files(
                cfg.general.inbox_dir, "capital_one", (".csv", ".pdf", ".ofx", ".qfx", ".qbo")
            ):
                r, ins = _ingest_capital_one_file(db, f)
                print(f"Capital One ({f.name}): read {r}, inserted {ins}.")

            for f in _inbox_files(cfg.general.inbox_dir, "gusto", (".csv",)):
                r, ins = _ingest_gusto_file(db, cfg, f, "regular")
                print(f"Gusto ({f.name}): read {r}, inserted {ins} run(s).")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    with db:
        summary = Reconciler(db, cfg).reconcile_all(
            confirm_high_confidence=args.confirm_high_confidence
        )
    print(
        f"Reconciliation complete: {summary.cc_transfer_matches} CC transfer(s), "
        f"{summary.bank_transfer_matches} bank transfer(s), "
        f"{summary.gusto_matches} Gusto payroll match(es), "
        f"{summary.harvest_matches} Harvest deposit match(es) | "
        f"confirmed={summary.auto_confirmed}, proposed={summary.total_proposed}."
    )
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    rules = load_rules(cfg.general.rules_path)
    with db:
        summary = Planner(db, cfg, Categorizer(rules)).build_plan()
    print(
        f"Plan built: {summary.staged_invoices} staged invoice(s), "
        f"{summary.staged_transactions} staged tx(s), "
        f"{summary.needs_review} needing review, "
        f"{summary.manual_transfers} manual transfer(s), "
        f"{summary.manual_invoice_payments} manual invoice payment(s)."
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    _, db = _open_context(Path(args.config))
    with db:
        txs = db.list_staged_transactions(status=args.status)
        invs = db.list_staged_invoices(status=args.status)
        matches = (
            db.list_matches(status="proposed") if args.status in {"all", "needs_review"} else []
        )

    if args.json:
        payload = {
            "staged_transactions": [
                {
                    "id": tx.id,
                    "tx_date": tx.tx_date,
                    "anchor_account_key": tx.anchor_account_key,
                    "anchor_direction": tx.anchor_direction,
                    "anchor_amount": format_money(tx.anchor_amount),
                    "description": tx.description,
                    "status": tx.status,
                    "category_rule": tx.category_rule,
                    "review_reason": tx.review_reason,
                    "last_error": tx.last_error,
                    "line_items": [li.to_dict() for li in tx.line_items],
                }
                for tx in txs
            ],
            "staged_invoices": [
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "customer_name": inv.customer_name,
                    "invoice_date": inv.invoice_date,
                    "total_amount": format_money(inv.total_amount),
                    "status": inv.status,
                }
                for inv in invs
            ],
            "proposed_matches": [
                {
                    "id": m.id,
                    "match_type": m.match_type,
                    "bank_tx_id": m.bank_tx_id,
                    "matched_amount": format_money(m.matched_amount),
                    "confidence": m.confidence,
                    "reason": m.reason,
                }
                for m in matches
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"Staged Transactions (filter={args.status}): {len(txs)}")
    for tx in txs:
        cats = ", ".join(
            f"{li.account_key}:{li.balance}=${format_money(li.amount)}" for li in tx.line_items
        )
        reason = (
            f" | {tx.last_error or tx.review_reason}" if (tx.last_error or tx.review_reason) else ""
        )
        print(
            f"  [tx #{tx.id}] {tx.tx_date} [{tx.status}] {tx.anchor_account_key} "
            f"{tx.anchor_direction} ${format_money(tx.anchor_amount)} {tx.description!r} "
            f"-> [{cats}]{reason}"
        )

    print(f"\nStaged Invoices (filter={args.status}): {len(invs)}")
    for inv in invs:
        print(
            f"  [inv #{inv.id}] #{inv.invoice_number} [{inv.status}] "
            f"{inv.customer_name} {inv.invoice_date} ${format_money(inv.total_amount)}"
        )

    if matches:
        print(f"\nProposed Matches awaiting confirmation: {len(matches)}")
        for m in matches:
            print(
                f"  [match #{m.id}] {m.match_type} bank_tx=#{m.bank_tx_id} "
                f"${format_money(m.matched_amount)} conf={m.confidence:.2f} — {m.reason}"
            )
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    _, db = _open_context(Path(args.config))
    with db:
        match_count = 0
        for mid in args.match or []:
            if db.set_match_status(mid, "confirmed"):
                match_count += 1
        tx_cnt, inv_cnt = db.approve_staged(
            all_staged=args.all_staged,
            tx_ids=args.tx or [],
            invoice_ids=args.invoice or [],
        )
    print(f"Approved: {tx_cnt} transaction(s), {inv_cnt} invoice(s), {match_count} match(es).")
    return 0


def _cmd_recategorize(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    with db:
        stx = db.get_staged_transaction(args.tx)
        if stx is None:
            print(f"Error: staged transaction #{args.tx} not found", file=sys.stderr)
            return 1

        planner = Planner(db, cfg, Categorizer([]))
        desired_effect = "DEBIT" if stx.anchor_direction == "WITHDRAWAL" else "CREDIT"
        normal = planner.normal_balance_for(args.account)
        if desired_effect == "DEBIT":
            balance = "INCREASE" if normal == "DEBIT" else "DECREASE"
        else:
            balance = "INCREASE" if normal == "CREDIT" else "DECREASE"

        cached_acc = db.resolve_wave_account(args.account, cfg.accounts)
        updated = db.recategorize_staged_tx(
            staged_tx_id=args.tx,
            account_key=args.account,
            balance=balance,
            wave_account_id=cached_acc.wave_id if cached_acc else None,
            note=args.note,
        )
        validate_double_entry_balance(
            anchor_account_key=updated.anchor_account_key,
            anchor_direction=updated.anchor_direction,
            anchor_amount=updated.anchor_amount,
            line_items=updated.line_items,
            normal_balance_lookup=planner.normal_balance_for,
            anchor_wave_account_id=updated.anchor_wave_account_id,
        )
    print(f"Recategorized staged tx #{updated.id} -> {args.account} ({balance}), status='staged'.")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    with db:
        summary = WaveSyncService(db, cfg).push(
            dry_run=args.dry_run,
            invoices_only=args.invoices_only,
            transactions_only=args.transactions_only,
        )

    if args.dry_run:
        output: dict[str, Any] = {
            "dry_run": True,
            "invoices_ready": summary.invoices_pushed,
            "transactions_ready": summary.transactions_pushed,
            "would_create_customers": summary.would_create_customers,
            "would_create_products": summary.would_create_products,
            "warnings": summary.warnings,
            "payloads": summary.dry_run_payloads,
        }
        print(json.dumps(output, indent=2))
        return 0 if not summary.errors else 1

    print(
        f"Push complete: invoices pushed={summary.invoices_pushed} "
        f"failed={summary.invoices_failed} | "
        f"transactions pushed={summary.transactions_pushed} failed={summary.transactions_failed}"
    )
    for err in summary.errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 0 if not summary.errors else 1


def _cmd_report(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    with db:
        report = build_report(db, cfg, period=args.period)
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_report_text(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="ledgerdemain",
        description=(
            "Integrate Harvest, Gusto, Capital One, and Grasshopper Bank with Wave Accounting."
        ),
    )
    parser.add_argument(
        "--config",
        default="ledgerdemain.toml",
        help="Path to TOML configuration file (default: ledgerdemain.toml)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = subparsers.add_parser(
        "init", help="Initialize workspace directories, config, and SQLite DB"
    )
    p_init.add_argument(
        "--dir", default=".", help="Workspace directory (default: current directory)"
    )
    p_init.set_defaults(func=_cmd_init)

    # wave
    p_wave = subparsers.add_parser("wave", help="Wave Accounting metadata operations")
    wave_sub = p_wave.add_subparsers(dest="wave_cmd", required=True)
    p_list_biz = wave_sub.add_parser(
        "list-businesses", help="List Wave businesses accessible to your token"
    )
    p_list_biz.set_defaults(func=_cmd_wave_list_businesses)
    p_sync_meta = wave_sub.add_parser(
        "sync-meta", help="Sync Wave Chart of Accounts, customers, and products"
    )
    p_sync_meta.set_defaults(func=_cmd_wave_sync_meta)

    # grasshopper discovery
    p_gh = subparsers.add_parser("grasshopper", help="Grasshopper Bank discovery operations")
    gh_sub = p_gh.add_subparsers(dest="grasshopper_cmd", required=True)
    p_gh_accts = gh_sub.add_parser(
        "list-accounts", help="List Grasshopper Bank accounts accessible to your API token"
    )
    p_gh_accts.set_defaults(func=_cmd_grasshopper_list_accounts)

    # ingest
    p_ingest = subparsers.add_parser(
        "ingest", help="Ingest data from Harvest, Grasshopper, Capital One, or Gusto"
    )
    ingest_sub = p_ingest.add_subparsers(dest="ingest_target", required=True)

    p_ing_harv = ingest_sub.add_parser(
        "harvest", help="Ingest Harvest invoices/payments via API or CSV"
    )
    p_ing_harv.add_argument("--api", action="store_true", help="Force Harvest API sync")
    p_ing_harv.add_argument("--file", help="Path to exported Harvest invoice CSV")
    p_ing_harv.add_argument("--since", help="Incremental updated_since date (YYYY-MM-DD)")
    p_ing_harv.set_defaults(func=_cmd_ingest)

    p_ing_gh = ingest_sub.add_parser(
        "grasshopper", help="Ingest Grasshopper transactions via API or CSV/OFX"
    )
    p_ing_gh.add_argument("--api", action="store_true", help="Force Grasshopper API sync")
    p_ing_gh.add_argument("--file", help="Path to Grasshopper CSV/OFX/QBO export")
    p_ing_gh.add_argument("--inbox", action="store_true", help="Scan inbox/grasshopper/")
    p_ing_gh.add_argument(
        "--account-key",
        help="Logical account key override (e.g. grasshopper_savings, grasshopper_money_market)",
    )
    p_ing_gh.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD)")
    p_ing_gh.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD)")
    p_ing_gh.set_defaults(func=_cmd_ingest)

    p_ing_co = ingest_sub.add_parser(
        "capital-one", help="Ingest Capital One credit card CSV/PDF/OFX"
    )
    p_ing_co.add_argument("--file", help="Path to Capital One CSV, PDF, or OFX/QBO file")
    p_ing_co.add_argument("--inbox", action="store_true", help="Scan inbox/capital_one/")
    p_ing_co.set_defaults(func=_cmd_ingest)

    p_ing_gu = ingest_sub.add_parser("gusto", help="Ingest Gusto payroll journal or contractor CSV")
    p_ing_gu.add_argument("--file", help="Path to Gusto CSV export")
    p_ing_gu.add_argument("--inbox", action="store_true", help="Scan inbox/gusto/")
    p_ing_gu.add_argument(
        "--type",
        dest="run_type",
        choices=["regular", "off_cycle", "contractor"],
        default="regular",
        help="Gusto run type (default: regular)",
    )
    p_ing_gu.set_defaults(func=_cmd_ingest)

    p_ing_all = ingest_sub.add_parser("all", help="Run configured APIs and scan all inbox folders")
    p_ing_all.set_defaults(func=_cmd_ingest)

    # reconcile
    p_rec = subparsers.add_parser(
        "reconcile", help="Match deposits, Gusto payroll debits, and CC transfers"
    )
    p_rec.add_argument(
        "--confirm-high-confidence",
        action="store_true",
        help="Automatically confirm matches >= accounting.auto_confirm_threshold",
    )
    p_rec.set_defaults(func=_cmd_reconcile)

    # plan
    p_plan = subparsers.add_parser(
        "plan", help="Stage balanced Wave transactions and mirrored invoices"
    )
    p_plan.set_defaults(func=_cmd_plan)

    # review
    p_rev = subparsers.add_parser("review", help="Inspect staged items and items needing review")
    p_rev.add_argument(
        "--status",
        default="needs_review",
        choices=[
            "needs_review",
            "staged",
            "approved",
            "manual_transfer_required",
            "manual_invoice_payment_required",
            "failed",
            "pushed",
            "all",
        ],
    )
    p_rev.add_argument("--json", action="store_true", help="Output JSON")
    p_rev.set_defaults(func=_cmd_review)

    # approve
    p_app = subparsers.add_parser(
        "approve", help="Approve staged transactions, invoices, or matches"
    )
    p_app.add_argument(
        "--all-staged", action="store_true", help="Approve all staged transactions and invoices"
    )
    p_app.add_argument("--tx", type=int, nargs="*", help="Staged transaction IDs to approve")
    p_app.add_argument("--invoice", type=int, nargs="*", help="Staged invoice IDs to approve")
    p_app.add_argument("--match", type=int, nargs="*", help="Reconciliation match IDs to confirm")
    p_app.set_defaults(func=_cmd_approve)

    # recategorize
    p_recat = subparsers.add_parser(
        "recategorize", help="Assign an account key to a staged transaction"
    )
    p_recat.add_argument("--tx", type=int, required=True, help="Staged transaction ID")
    p_recat.add_argument("--account", required=True, help="Logical account key from [accounts]")
    p_recat.add_argument("--note", help="Optional memo note to append")
    p_recat.set_defaults(func=_cmd_recategorize)

    # push
    p_push = subparsers.add_parser(
        "push", help="Dry-run or push approved entries to Wave Accounting"
    )
    p_push.add_argument(
        "--dry-run", action="store_true", help="Validate and output payloads without mutating Wave"
    )
    scope = p_push.add_mutually_exclusive_group()
    scope.add_argument("--invoices-only", action="store_true", help="Only push approved invoices")
    scope.add_argument(
        "--transactions-only", action="store_true", help="Only push approved transactions"
    )
    p_push.set_defaults(func=_cmd_push)

    # report
    p_rep = subparsers.add_parser(
        "report", help="Show period summary, payroll tax clearing, and Wave UI checklist"
    )
    p_rep.add_argument("--period", help="Optional YYYY-MM prefix filter")
    p_rep.add_argument("--format", choices=["text", "json"], default="text")
    p_rep.set_defaults(func=_cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
