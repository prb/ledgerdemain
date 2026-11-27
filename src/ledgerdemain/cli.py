"""Command-line interface for Ledgerdemain."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ledgerdemain.adapters.base import sha256_file
from ledgerdemain.adapters.grasshopper import GrasshopperApiAdapter
from ledgerdemain.adapters.gusto import GustoPayrollCsvAdapter
from ledgerdemain.adapters.harvest import HarvestApiAdapter
from ledgerdemain.adapters.ofx import OfxStatement, OfxTransactionParser
from ledgerdemain.categorize import Categorizer
from ledgerdemain.config import AppConfig, ConfigError, load_config, load_rules
from ledgerdemain.db import LedgerDB
from ledgerdemain.http_client import GraphQLError, HttpError
from ledgerdemain.money import format_money
from ledgerdemain.planner import Planner, validate_double_entry_balance
from ledgerdemain.reconcile import Reconciler
from ledgerdemain.report import build_report, format_report_text
from ledgerdemain.wave.client import WaveApiError, WaveGraphQLClient
from ledgerdemain.wave.sync import WaveSyncService

_DEFAULT_CONFIG_FALLBACK = """[general]
db_path = "data/ledgerdemain.db"
inbox_dir = "inbox"
rules_path = "rules.toml"
currency = "USD"
keychain_service = "ledgerdemain"

[accounting]
deposit_match_window_days = 7
payroll_match_window_days = 5
transfer_match_window_days = 5
auto_confirm_threshold = 0.90

[wave]
endpoint = "https://gql.waveapps.com/graphql/public"
business_id = "${WAVE_BUSINESS_ID}"
access_token = "${keychain:WAVE_ACCESS_TOKEN}"

[harvest]
base_url = "https://api.harvestapp.com/v2"
account_id = "${HARVEST_ACCOUNT_ID}"
access_token = "${keychain:HARVEST_ACCESS_TOKEN}"
user_agent = "Ledgerdemain (accounting@example.com)"

[grasshopper]
base_url = "https://api.grasshopper.bank/v1"
access_token = "${keychain:GRASSHOPPER_ACCESS_TOKEN}"
header_secret = "${keychain:GRASSHOPPER_HEADER_SECRET}"
header_secret_name = "X-API-Secret"

[[grasshopper.accounts]]
account_key = "grasshopper_checking"
account_id = "${GRASSHOPPER_CHECKING_ACCOUNT_ID}"

[[grasshopper.accounts]]
account_key = "grasshopper_money_market"
account_id = "${GRASSHOPPER_MM_ACCOUNT_ID}"

[accounts]
grasshopper_checking = "Grasshopper Checking"
grasshopper_money_market = "Grasshopper Money Market"
capital_one_card = "Capital One Credit Card"
revenue_default = "Sales"
interest_income = "Interest Income"
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
description_regex = "(?i)hsa\\\\s*con\\\\s*t\\\\s*ribution"
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
        "inbox/capital_one",
        "inbox/gusto",
    ):
        (target_dir / sub).mkdir(parents=True, exist_ok=True)

    cfg_path = target_dir / "ledgerdemain.toml"
    rules_path = target_dir / "rules.toml"
    _copy_template_or_write("ledgerdemain.example.toml", cfg_path, _DEFAULT_CONFIG_FALLBACK)
    _copy_template_or_write("rules.example.toml", rules_path, _DEFAULT_RULES_FALLBACK)

    cfg = load_config(cfg_path, resolve_keychain=False)
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

    return db.ingest_harvest_batch(
        invoices,
        clients=clients,
        payments=payments,
        sync_cursor_source="harvest_invoices" if max_date else None,
        sync_cursor_value=max_date or None,
    )


def _ingest_grasshopper_api(
    db: LedgerDB,
    cfg: AppConfig,
    from_date: str | None,
    to_date: str | None,
) -> tuple[int, int]:
    adapter = GrasshopperApiAdapter(cfg.grasshopper)
    accounts = [acct for acct in cfg.grasshopper.configured_accounts if acct.account_id]
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
        latest = max(tx.posted_date for tx in txs) if txs else None
        read_cnt, ins_cnt = db.ingest_bank_transactions_batch(
            txs,
            source="grasshopper",
            input_kind="api",
            sync_cursor_source=cursor_name if latest else None,
            sync_cursor_value=latest,
        )
        total_read += read_cnt
        total_inserted += ins_cnt
        print(f"  Grasshopper API ({acct.account_key}): read {read_cnt}, inserted {ins_cnt}.")
    return total_read, total_inserted


def _print_ofx_summary(path: Path, stmt: OfxStatement) -> None:
    date_range = f"{stmt.start_date or '?'}..{stmt.end_date or '?'}"
    bal = (
        f" | ledger_bal=${format_money(stmt.ledger_balance)}"
        if stmt.ledger_balance is not None
        else ""
    )
    print(f"  OFX ({path.name}): {date_range}{bal}")


def _ingest_capital_one_file(
    db: LedgerDB,
    path: Path,
    expected_currency: str | None = "USD",
) -> tuple[int, int]:
    suffix = path.suffix.lower()
    if suffix in {".ofx", ".qfx", ".qbo"}:
        stmt = OfxTransactionParser().parse_statement_file(
            path,
            source="capital_one",
            account_key="capital_one_card",
            expected_currency=expected_currency,
        )
        txs = stmt.transactions
        kind = "ofx"
        _print_ofx_summary(path, stmt)
    else:
        raise ValueError(
            f"Unsupported Capital One file type {path.suffix!r} ({path.name}); "
            "export OFX, QFX, or QBO instead."
        )

    return db.ingest_bank_transactions_batch(
        txs,
        source="capital_one",
        input_kind=kind,
        file_path=str(path),
        file_sha256=sha256_file(path),
    )


def _ingest_gusto_file(db: LedgerDB, cfg: AppConfig, path: Path, run_type: str) -> tuple[int, int]:
    if path.suffix.lower() != ".csv":
        raise ValueError(
            f"Unsupported Gusto file type {path.suffix!r} ({path.name}); use a CSV export."
        )
    effective_type = (
        "contractor" if run_type == "contractor" or "contractor" in path.name.lower() else run_type
    )
    runs = GustoPayrollCsvAdapter(cfg.gusto_column_overrides).parse_file(
        path, run_type=effective_type
    )
    return db.ingest_gusto_payroll_batch(
        runs,
        file_path=str(path),
        file_sha256=sha256_file(path),
    )


def _api_only_inbox_rejecter(source_label: str) -> Callable[[Path], tuple[int, int]]:
    """Return an ingester that fails every file left in the inbox of an API-only source."""

    def reject(path: Path) -> tuple[int, int]:
        raise ValueError(
            f"{source_label} is ingested through the API; remove or move the file ({path.name})."
        )

    return reject


def _inbox_files(inbox_dir: Path, subdir: str) -> list[Path]:
    """List candidate files in an inbox folder, leaving the supported-type check to the ingester.

    Dotfiles (such as ``.DS_Store``) and subdirectories are ignored by name. Every other file is
    returned so that an unsupported one is reported as a failure instead of silently skipped.
    """
    folder = inbox_dir / subdir
    if not folder.exists():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith("."))


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
        f"{summary.customers_synced} customers."
    )
    for warn in summary.warnings:
        print(f"WARNING: {warn}", file=sys.stderr)
    return 0


def _ingest_inbox_files(
    source_label: str,
    files: Sequence[Path],
    ingest_fn: Callable[[Path], tuple[int, int]],
    failures: list[tuple[Path, str]],
    *,
    per_file_prefix: str | None = None,
    per_file_suffix: str = "",
) -> tuple[int, int, int]:
    """Ingest inbox files individually, recording per-file failures without aborting.

    ``ValueError`` (unparseable or invalid content) and ``OSError`` (unreadable file) are
    logged to stderr and appended to ``failures``. ``sqlite3.Error`` is deliberately not caught
    because a database fault is likely to affect every file, so it aborts the run.
    """
    succeeded = total_r = total_i = 0
    for f in files:
        try:
            r, ins = ingest_fn(f)
        except (ValueError, OSError) as exc:
            print(f"Error ingesting {source_label} ({f.name}): {exc}", file=sys.stderr)
            failures.append((f, str(exc)))
            continue
        succeeded += 1
        total_r += r
        total_i += ins
        if per_file_prefix is not None:
            print(f"{per_file_prefix} ({f.name}): read {r}, inserted {ins}{per_file_suffix}.")
    return succeeded, total_r, total_i


def _run_api_step(
    label: str,
    step: Callable[[], None],
    api_failures: list[tuple[str, str]],
) -> None:
    """Run one API ingest step, recording a failure instead of aborting the whole run.

    ``HttpError`` (network or HTTP failure, including socket errors) and ``ValueError`` (a
    malformed payload, raised by the adapters as a ``<Source>ParseError``) are logged to stderr
    and appended to ``api_failures``. ``sqlite3.Error`` is not caught, for the same reason as in
    ``_ingest_inbox_files``.
    """
    try:
        step()
    except (HttpError, ValueError) as exc:
        print(f"Error ingesting {label}: {exc}", file=sys.stderr)
        api_failures.append((label, str(exc)))


def _cmd_ingest(args: argparse.Namespace) -> int:
    cfg, db = _open_context(Path(args.config))
    failures: list[tuple[Path, str]] = []
    api_failures: list[tuple[str, str]] = []
    with db:
        target = args.ingest_target
        if target == "harvest":
            c, i, p = _ingest_harvest_api(db, cfg, args.since)
            print(f"Harvest API: synced {c} client(s), {i} invoice(s), {p} payment(s).")

        elif target == "grasshopper":
            r, ins = _ingest_grasshopper_api(db, cfg, args.from_date, args.to_date)
            print(f"Grasshopper API: read {r}, inserted {ins} new transaction(s).")

        elif target == "capital-one":
            if args.file:
                succeeded = 1
                total_r, total_i = _ingest_capital_one_file(
                    db, Path(args.file), expected_currency=cfg.general.currency
                )
            else:
                files = _inbox_files(cfg.general.inbox_dir, "capital_one")
                succeeded, total_r, total_i = _ingest_inbox_files(
                    "Capital One",
                    files,
                    lambda f: _ingest_capital_one_file(
                        db, f, expected_currency=cfg.general.currency
                    ),
                    failures,
                )
            print(
                f"Capital One: {succeeded} file(s), read {total_r}, inserted {total_i} new tx(s)."
            )

        elif target == "gusto":
            if args.file:
                r, ins = _ingest_gusto_file(db, cfg, Path(args.file), args.run_type)
                print(f"Gusto: 1 file(s), read {r}, inserted {ins} payroll run(s).")
            else:
                succeeded, total_r, total_i = _ingest_inbox_files(
                    "Gusto",
                    _inbox_files(cfg.general.inbox_dir, "gusto"),
                    lambda f: _ingest_gusto_file(db, cfg, f, args.run_type),
                    failures,
                )
                print(
                    f"Gusto: {succeeded} file(s), read {total_r}, "
                    f"inserted {total_i} payroll run(s)."
                )

        elif target == "all":
            if cfg.harvest.access_token and cfg.harvest.account_id:

                def _harvest_api_step() -> None:
                    c, i, p = _ingest_harvest_api(db, cfg, None)
                    print(f"Harvest API: {c} clients, {i} invoices, {p} payments.")

                _run_api_step("Harvest API", _harvest_api_step, api_failures)

            has_gh_api = bool(
                cfg.grasshopper.access_token
                and any(a.account_id for a in cfg.grasshopper.configured_accounts)
            )
            if has_gh_api:

                def _grasshopper_api_step() -> None:
                    r, ins = _ingest_grasshopper_api(db, cfg, None, None)
                    print(f"Grasshopper API: read {r}, inserted {ins}.")

                _run_api_step("Grasshopper API", _grasshopper_api_step, api_failures)
            _ingest_inbox_files(
                "Grasshopper",
                _inbox_files(cfg.general.inbox_dir, "grasshopper"),
                _api_only_inbox_rejecter("Grasshopper"),
                failures,
            )

            _ingest_inbox_files(
                "Capital One",
                _inbox_files(cfg.general.inbox_dir, "capital_one"),
                lambda f: _ingest_capital_one_file(db, f, expected_currency=cfg.general.currency),
                failures,
                per_file_prefix="Capital One",
            )

            _ingest_inbox_files(
                "Gusto",
                _inbox_files(cfg.general.inbox_dir, "gusto"),
                lambda f: _ingest_gusto_file(db, cfg, f, "regular"),
                failures,
                per_file_prefix="Gusto",
                per_file_suffix=" run(s)",
            )

            _ingest_inbox_files(
                "Harvest",
                _inbox_files(cfg.general.inbox_dir, "harvest"),
                _api_only_inbox_rejecter("Harvest"),
                failures,
            )
    if failures:
        names = ", ".join(path.name for path, _ in failures)
        print(
            f"Ingest completed with {len(failures)} failed file(s): {names}",
            file=sys.stderr,
        )
    if api_failures:
        labels = ", ".join(label for label, _ in api_failures)
        print(
            f"Ingest completed with {len(api_failures)} failed API step(s): {labels}",
            file=sys.stderr,
        )
    if failures or api_failures:
        return 2
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
        f"Plan built: {summary.staged_transactions} staged tx(s), "
        f"{summary.needs_review} needing review, "
        f"{summary.manual_transfers} manual transfer(s)."
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    _, db = _open_context(Path(args.config))
    with db:
        txs = db.list_staged_transactions(status=args.status)
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
        tx_cnt = db.approve_staged(all_staged=args.all_staged, tx_ids=args.tx or [])
    print(f"Approved: {tx_cnt} transaction(s), {match_count} match(es).")
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
        summary = WaveSyncService(db, cfg).push(dry_run=args.dry_run)

    if args.dry_run:
        output: dict[str, Any] = {
            "dry_run": True,
            "transactions_ready": summary.transactions_pushed,
            "would_create_customers": summary.would_create_customers,
            "warnings": summary.warnings,
            "payloads": summary.dry_run_payloads,
        }
        print(json.dumps(output, indent=2))
        return 0 if not summary.errors else 1

    print(
        f"Push complete: transactions pushed={summary.transactions_pushed} "
        f"failed={summary.transactions_failed}"
    )
    for err in summary.errors:
        print(f"ERROR: {err}", file=sys.stderr)
    return 0 if not summary.errors else 1


def _cmd_report(args: argparse.Namespace) -> int:
    _cfg, db = _open_context(Path(args.config))
    with db:
        report = build_report(db, period=args.period)
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
    p_sync_meta = wave_sub.add_parser("sync-meta", help="Sync Wave Chart of Accounts and customers")
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

    p_ing_harv = ingest_sub.add_parser("harvest", help="Ingest Harvest invoices/payments via API")
    p_ing_harv.add_argument("--since", help="Incremental updated_since date (YYYY-MM-DD)")
    p_ing_harv.set_defaults(func=_cmd_ingest)

    p_ing_gh = ingest_sub.add_parser("grasshopper", help="Ingest Grasshopper transactions via API")
    p_ing_gh.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD)")
    p_ing_gh.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD)")
    p_ing_gh.set_defaults(func=_cmd_ingest)

    p_ing_co = ingest_sub.add_parser("capital-one", help="Ingest Capital One credit card OFX")
    p_ing_co.add_argument("--file", help="Path to Capital One OFX/QFX/QBO file")
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
    p_plan = subparsers.add_parser("plan", help="Stage balanced Wave transactions")
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
            "failed",
            "outcome_unknown",
            "pushed",
            "all",
        ],
    )
    p_rev.add_argument("--json", action="store_true", help="Output JSON")
    p_rev.set_defaults(func=_cmd_review)

    # approve
    p_app = subparsers.add_parser("approve", help="Approve staged transactions or matches")
    p_app.add_argument("--all-staged", action="store_true", help="Approve all staged transactions")
    p_app.add_argument("--tx", type=int, nargs="*", help="Staged transaction IDs to approve")
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
    except (ConfigError, ValueError, KeyError, HttpError, GraphQLError, WaveApiError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
