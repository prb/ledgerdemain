# Ledgerdemain

**Ledgerdemain** integrates **Harvest** (invoices & payments), **Gusto** (payroll & tax breakdowns), **Capital One** (credit card OFX statements), and **Grasshopper Bank** (Read-Only API) with **Wave Accounting** (GraphQL API) through a local SQLite staging, reconciliation, review, and dry-run/live push pipeline.

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

# 1. Initialize workspace (creates ledgerdemain.toml, rules.toml, data/, inbox/)
uv run ledgerdemain init

# 2. Store API tokens in macOS Keychain & customize ledgerdemain.toml / rules.toml
# 3. Sync your Wave Chart of Accounts and customers
uv run ledgerdemain wave sync-meta

# 4. Ingest from the Harvest and Grasshopper APIs, plus Capital One OFX and Gusto CSV files
#    dropped into inbox/capital_one/ and inbox/gusto/
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

### Secrets & macOS Keychain
- API tokens (`WAVE_ACCESS_TOKEN`, `HARVEST_ACCESS_TOKEN`, `GRASSHOPPER_ACCESS_TOKEN`, `GRASSHOPPER_HEADER_SECRET`) use explicit `${keychain:NAME}` placeholders in `ledgerdemain.toml`, resolved at runtime via `/usr/bin/security` under literal `[general] keychain_service` (default `"ledgerdemain"`, or e.g. `"ledgerdemain:prod"` to namespace environments):
  ```bash
  security add-generic-password -U -s ledgerdemain -a <NAME> -w
  ```
- Store printable ASCII tokens only (`security -w` prints values with non-printable bytes hex-encoded).
- Every referenced `${keychain:NAME}` placeholder is read when loading configuration and fails if missing; blank out (`""`) or remove placeholders for APIs you do not use so `ingest all` skips them.
- Non-secret IDs and environment overrides use `${ENV_VAR}` placeholders resolved strictly from process environment variables (`os.environ`); `.env` files are not loaded.

### 1. Wave Accounting (GraphQL API)
- Requires an active **Wave Pro** (or Advisors) plan with newer accounting (`isClassicAccounting = false`).
- Store `WAVE_ACCESS_TOKEN` in macOS Keychain under service `ledgerdemain` (`${keychain:WAVE_ACCESS_TOKEN}`), and set `WAVE_BUSINESS_ID` in `os.environ` (or directly in `ledgerdemain.toml`).
- Run `uv run ledgerdemain wave list-businesses` if you need to look up your `WAVE_BUSINESS_ID`.
- Run `uv run ledgerdemain wave sync-meta` to cache your Chart of Accounts and customers, then map your account names/IDs under `[accounts]` in `ledgerdemain.toml`.

### 2. Harvest (REST API v2)
- Generate a Personal Access Token and Account ID in Harvest Developers (`https://id.getharvest.com/developers`).
- Store `HARVEST_ACCESS_TOKEN` in macOS Keychain (`${keychain:HARVEST_ACCESS_TOKEN}`), and set `HARVEST_ACCOUNT_ID` (via `os.environ` or `ledgerdemain.toml`) plus `harvest.user_agent` with your app name and contact email.

### 3. Grasshopper Bank (Multi-Account Read-Only API)
- In Grasshopper online banking, go to **Settings -> API Banking**, accept the terms, **whitelist your static IP address**, and store both the Bearer Token (`GRASSHOPPER_ACCESS_TOKEN`) and Header Secret (`GRASSHOPPER_HEADER_SECRET`) in macOS Keychain (`${keychain:GRASSHOPPER_ACCESS_TOKEN}`, `${keychain:GRASSHOPPER_HEADER_SECRET}`).
- Run `uv run ledgerdemain grasshopper list-accounts` to retrieve the `id` for both your **Checking** and **Money Market** accounts, then map them under `[[grasshopper.accounts]]` in `ledgerdemain.toml` (`grasshopper_checking`, `grasshopper_money_market`).
- Grasshopper is ingested only through the API. Internal Checking <-> Money Market transfers reconcile automatically. On the first sync in a fresh database, pass `ingest grasshopper --from YYYY-MM-DD` to set how far back to go. Files left in `inbox/grasshopper/` are reported as failures by `ingest all`.

### 4. Capital One Credit Card (OFX)
- Export **OFX (OFX 2.x XML / Quicken / QFX / QBO)** from Capital One and place it in `inbox/capital_one/` (or pass `--file`). Capital One OFX exports include unique `<FITID>` IDs for duplicate-safe overlapping downloads, both transaction date (`<DTUSER>`) and posted date (`<DTPOSTED>`), card last-4 (`<ACCTID>`), and statement `<LEDGERBAL>`.
- Only OFX/QFX/QBO is supported; `ingest capital-one --file` rejects other file types, and inbox runs report them as failed files (exit 2) and still ingest the supported files.

### 5. Gusto Payroll (Payroll Journal & Contractor CSVs)
- Gusto restricts direct OAuth API access to approved multi-company SaaS partners, so single-company owners export **Payroll Journal** and **Contractor Payment** CSVs from **Reports** and drop them into `inbox/gusto/`.
- Ledgerdemain skips Gusto's report preamble rows and `Totals` rows automatically, aggregates per-employee lines by check date, preserves itemized federal/state/FICA/FUTA/SUTA breakdowns in transaction notes, and clears `Payroll Taxes Payable` and `Payroll Benefits Payable` to `$0.00` when Net Pay + Tax + Benefit debits are matched.

---

## Accounting Workflows

The accounting treatment is fixed; there are no strategy settings.

- **Harvest invoices are not created in Wave.** They are ingested and matched to Grasshopper deposits, but Wave only receives the deposit. A matched deposit posts directly to `revenue_default` as income (one line per invoice, with any payment processor fee split to `merchant_fees`). Income lines carry the Wave customer when the client name matches a customer cached by `wave sync-meta`; otherwise the customer is created at push time.
- **Transfers between your own accounts are recorded by hand.** Wave's GraphQL API has no transfer mutation. Matched transfers (Grasshopper to Capital One card payments, and Checking <-> Money Market) are staged as `manual_transfer_required`, never pushed, and listed in the `report` Wave Manual Action Checklist so you can enter them as Transfers in the Wave UI.
- **Payroll clearing accounts net to zero.** `Payroll Taxes Payable` and `Payroll Benefits Payable` clear to `$0.00` once the Gusto net pay, tax, and benefit debits are matched.

Invoice mirroring (accrual basis) was removed. See [docs/FUTURE_FEATURES.md](docs/FUTURE_FEATURES.md) for how it worked and how to bring it back.

---

## If a push stops with an unknown outcome

If a Wave create request times out or returns a response that cannot be read, Wave may have applied it. `push` stops without sending anything further and marks the staged transaction `outcome_unknown` (visible with `ledgerdemain review --status outcome_unknown`). Re-planning and `approve --all-staged` leave it alone so it is never sent twice by accident.

1. Check Wave for the record. The error message names the `externalId` (`ld:tx:...`) to look for; a customer is found by name.
2. Run `ledgerdemain wave sync-meta` so a customer that was created is reused instead of created again.
3. If the transaction exists in Wave, leave it as is. If it is missing, re-queue it with `ledgerdemain approve --tx ID` and push again.

If Wave rejects a request outright (an HTTP or GraphQL error), `push` also stops, but nothing is marked. Fix the cause and push again.

## Exit codes and failures

- `ingest all` reports every unsupported or failed inbox file (any source) and every API failure (Harvest or Grasshopper) per item on stderr, still ingests everything else, and exits 2 if anything failed. Rerunning is safe because ingestion is idempotent.
- Single-source commands (`ingest harvest`, `wave sync-meta`, and so on) print an `Error:` line and exit 2 on configuration errors, HTTP errors, GraphQL errors, and Wave API errors.
- `push` exits 1 if any record failed or the push stopped early.

---

## CLI Reference

| Command | Purpose |
|---|---|
| `ledgerdemain init [--dir PATH]` | Initialize config, rules, inbox folders, and SQLite DB |
| `ledgerdemain wave list-businesses` | List Wave businesses accessible to your token |
| `ledgerdemain wave sync-meta` | Cache Wave Chart of Accounts and customers |
| `ledgerdemain grasshopper list-accounts` | List Grasshopper Bank accounts accessible to your token |
| `ledgerdemain ingest harvest\|grasshopper\|capital-one\|gusto\|all` | Ingest from the Harvest and Grasshopper APIs, or from Capital One OFX and Gusto CSV files (`all` also reports leftover files in `inbox/harvest/` and `inbox/grasshopper/`) |
| `ledgerdemain reconcile [--confirm-high-confidence]` | Match deposits, Gusto debits, and CC transfers |
| `ledgerdemain plan` | Stage balanced Wave transactions (transfers between own accounts are staged for manual entry) |
| `ledgerdemain review [--status ...] [--json]` | Inspect staged, review-needed, manual, or failed items |
| `ledgerdemain approve [--all-staged] [--tx ...] [--match ...]` | Confirm matches or approve staged transactions (`--tx` also re-queues a failed or `outcome_unknown` one) |
| `ledgerdemain recategorize --tx ID --account KEY [--note TEXT]` | Reassign account category and re-stage |
| `ledgerdemain push [--dry-run]` | Preview JSON payloads or push approved transactions live to Wave |
| `ledgerdemain report [--period YYYY-MM] [--format text\|json]` | Summary, AR status, Gusto clearing check, Wave checklist |
