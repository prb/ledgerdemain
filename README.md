# Ledgerdemain

**Ledgerdemain** integrates **Harvest** (invoices & payments), **Gusto** (payroll & tax breakdowns), **Capital One** (credit card CSV/PDF/OFX statements), and **Grasshopper Bank** (Read-Only API + CSV/OFX exports) with **Wave Accounting** (GraphQL API) through a local SQLite staging, reconciliation, review, and dry-run/live push pipeline.

## Why a Local Staging & Reconciliation Ledger?

Wave's public GraphQL API has specific constraints:
- It cannot query back full raw bank transactions, link bank deposits directly to Wave invoices, or create inter-account bank/credit card transfers via API.
- `moneyTransactionCreate` enforces strict double-entry balancing between an **anchor** account (bank or credit card) and categorization **lineItems** (using positive `Decimal` amounts and `INCREASE`/`DECREASE` balance enums).

Ledgerdemain bridges these gaps by staging every transaction locally in SQLite, cross-reconciling sources before pushing, validating double-entry balances to the penny, preventing duplicate entries via deterministic fingerprints + `externalId`s, and generating a Wave UI checklist for the operations Wave's API cannot perform directly (such as Grasshopper -> Capital One transfers).

---

## Quickstart

Requires **Python 3.13+** and [`uv`](https://docs.astral.sh/uv/) (zero mandatory external runtime dependencies).

```bash
# Install project and dev tools (pytest, hypothesis, ruff, mypy):
uv sync --extra dev

# Optional PDF library extras for complex Capital One PDFs:
uv sync --extra pdf

# 1. Initialize workspace (creates ledgerdemain.toml, rules.toml, data/, inbox/)
uv run ledgerdemain init

# 2. Configure API credentials in your environment & customize ledgerdemain.toml / rules.toml
# 3. Sync your Wave Chart of Accounts, customers, and products
uv run ledgerdemain wave sync-meta

# 4. Ingest from APIs and/or drop CSV/PDF/OFX files into inbox/<source>/
uv run ledgerdemain ingest all

# 5. Reconcile Harvest deposits, Gusto payroll debits, and Capital One transfers
uv run ledgerdemain reconcile --confirm-high-confidence

# 6. Build balanced Wave payloads & review anything uncategorized
uv run ledgerdemain plan
uv run ledgerdemain review --status needs_review
uv run ledgerdemain recategorize --tx 4 --account software_subscriptions

# 7. Approve, preview payloads with --dry-run, and push live
uv run ledgerdemain approve --all-staged
uv run ledgerdemain push --dry-run
uv run ledgerdemain push

# 8. View period report, payroll tax clearing balance, and Wave UI checklist
uv run ledgerdemain report --period 2026-11
```

## Development Checks

```bash
uv run --extra dev ruff format
uv run --extra dev ruff check
uv run --extra dev mypy
uv run --extra dev pytest -v
```

---

## Source Setup

### 1. Wave Accounting (GraphQL API)
- Requires an active **Wave Pro** (or Advisors) plan with newer accounting (`isClassicAccounting = false`).
- Put `WAVE_ACCESS_TOKEN` (and `WAVE_BUSINESS_ID`) in `.env` (auto-loaded from the config directory).
- Run `uv run ledgerdemain wave list-businesses` if you need to look up your `WAVE_BUSINESS_ID`.
- Run `uv run ledgerdemain wave sync-meta` to cache your Chart of Accounts, customers, and products, then map your account names/IDs under `[accounts]` in `ledgerdemain.toml`.

### 2. Harvest (REST API v2 or CSV)
- Generate a Personal Access Token and Account ID in Harvest Developers (`https://id.getharvest.com/developers`).
- Set `HARVEST_ACCOUNT_ID` and `HARVEST_ACCESS_TOKEN` in `.env`, plus `harvest.user_agent` with your app name and contact email.
- Alternatively, drop exported Harvest invoice CSVs into `inbox/harvest/` or pass `--file`.

### 3. Grasshopper Bank (Multi-Account Read-Only API or OFX/CSV)
- In Grasshopper online banking, go to **Settings -> API Banking**, accept the terms, **whitelist your static IP address**, and save both the Bearer Token (`GRASSHOPPER_ACCESS_TOKEN`) and Header Secret (`GRASSHOPPER_HEADER_SECRET`) in `.env`.
- Run `uv run ledgerdemain grasshopper list-accounts` to retrieve the `id` and `last4` for both your **Savings** and **Money Market** accounts, then map them under `[[grasshopper.accounts]]` in `ledgerdemain.toml` (`grasshopper_savings`, `grasshopper_money_market`).
- Or export OFX/CSV/QBO statements to `inbox/grasshopper/`; OFX exports automatically match each account via `<BANKACCTFROM>/<ACCTID>`, and internal Savings <-> Money Market transfers reconcile automatically.

### 4. Capital One Credit Card (OFX / CSV / PDF)
- **Recommended:** Export **OFX (OFX 2.x XML / Quicken / QFX / QBO)** from Capital One and place it in `inbox/capital_one/` (or pass `--file`). Capital One OFX exports include unique `<FITID>` IDs for duplicate-safe overlapping downloads, both transaction date (`<DTUSER>`) and posted date (`<DTPOSTED>`), card last-4 (`<ACCTID>`), and statement `<LEDGERBAL>`.
- CSV and monthly PDF exports are also supported in `inbox/capital_one/` (the PDF parser resolves `MMM DD` dates across December -> January statement rollovers and distinguishes `Payments, Credits` from `Purchases / Fees`).

### 5. Gusto Payroll (Payroll Journal & Contractor CSVs)
- Gusto restricts direct OAuth API access to approved multi-company SaaS partners, so single-company owners export **Payroll Journal** and **Contractor Payment** CSVs from **Reports** and drop them into `inbox/gusto/`.
- Ledgerdemain skips Gusto's report preamble rows and `Totals` rows automatically, aggregates per-employee lines by check date, preserves itemized federal/state/FICA/FUTA/SUTA breakdowns in transaction notes, and clears `Payroll Taxes Payable` and `Payroll Benefits Payable` to `$0.00` when Net Pay + Tax + Benefit debits are matched.

---

## Accounting Workflows

### Harvest Invoices: Accrual vs Cash Basis
- **Accrual / Mirrored Invoices (`mirror_harvest_invoices = true`, default)**:
  - `invoiceCreate` mirrors non-draft Harvest invoices into Wave.
  - `mirrored_invoice_deposit_strategy = "ar_clearing"` (default) posts matched Grasshopper deposits against `invoice_payments_clearing` (so revenue is **never** double-counted!).
  - `mirrored_invoice_deposit_strategy = "manual_ui_payment"` holds matched deposits in a checklist so you record the payment directly against the Wave invoice in the UI.
- **Cash Basis (`mirror_harvest_invoices = false`, `mirrored_invoice_deposit_strategy = "direct_income"`)**:
  - Matched Grasshopper deposits post directly to `revenue_default` (and split any payment processor fee to `merchant_fees`).

### Credit Card Payments (`transfer_strategy`)
- Wave's GraphQL API does not provide a native bank-to-credit-card transfer mutation.
- `transfer_strategy = "manual_transfer"` (default) pairs Grasshopper's Capital One payment withdrawal with Capital One's payment credit, prevents double-posting them as expenses/income, and lists them in `report`'s Wave Manual Action Checklist.
- `transfer_strategy = "clearing_account"` posts both legs against `cc_transfer_clearing` so the clearing account nets to `$0.00`.

---

## CLI Reference

| Command | Purpose |
|---|---|
| `ledgerdemain init [--dir PATH]` | Initialize config, rules, inbox folders, and SQLite DB |
| `ledgerdemain wave list-businesses` | List Wave businesses accessible to your token |
| `ledgerdemain wave sync-meta` | Cache Wave Chart of Accounts, customers, and products |
| `ledgerdemain grasshopper list-accounts` | List Grasshopper Bank accounts accessible to your token |
| `ledgerdemain ingest harvest\|grasshopper\|capital-one\|gusto\|all` | Ingest APIs or local OFX/CSV/PDF files |
| `ledgerdemain reconcile [--confirm-high-confidence]` | Match deposits, Gusto debits, and CC transfers |
| `ledgerdemain plan` | Stage balanced Wave transactions and mirrored invoices |
| `ledgerdemain review [--status ...] [--json]` | Inspect staged, review-needed, manual, or failed items |
| `ledgerdemain approve [--all-staged] [--tx ...] [--invoice ...] [--match ...]` | Confirm matches or approve staged items |
| `ledgerdemain recategorize --tx ID --account KEY [--note TEXT]` | Reassign account category and re-stage |
| `ledgerdemain push [--dry-run] [--invoices-only \| --transactions-only]` | Preview JSON payloads or push live to Wave |
| `ledgerdemain report [--period YYYY-MM] [--format text\|json]` | Summary, AR status, Gusto clearing check, Wave checklist |
| `ledgerdemain solo401k [--year YYYY] [--employee-deferrals AMT] [--format text\|json]` | YTD Solo 401(k) employer profit-sharing limit and room |
