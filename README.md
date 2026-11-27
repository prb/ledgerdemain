# Ledgerdemain

A small command-line utility that gets one person's business transactions into
[Wave Accounting](https://www.waveapps.com/): Harvest invoices, Gusto payroll, a Capital One card and
a Grasshopper bank account go in, balanced Wave transactions come out.

## What this is (and isn't)

Ledgerdemain is a **personal-scale utility**: pico-SaaS, smaller than micro-SaaS. It is built for one
person doing one set of books on one machine.

- **One user, one machine.** State lives in a local SQLite file. There is no server, web UI,
  account system, or hosted service, and nothing runs in the background. You run it by hand when you
  do the books, typically once a month.
- **One stack, hardwired.** Harvest, Gusto, Capital One and Grasshopper Bank into Wave. Supporting
  another bank or tool means writing one adapter, not flipping a setting. Treat it as something to
  fork and adapt.
- **Fixed accounting treatment.** Cash basis, no strategy switches. The one alternative that was
  removed, accrual-style invoice mirroring, is described in
  [docs/FUTURE_FEATURES.md](docs/FUTURE_FEATURES.md).
- **Developed on macOS.** API tokens are read from the macOS Keychain. Python 3.13+ and
  [`uv`](https://docs.astral.sh/uv/); no runtime dependencies beyond the standard library.

## How it fits together

```
  Harvest API ------+
  Grasshopper API --+--> ingest --> SQLite --> reconcile --> plan
  Capital One OFX --+       ^                                   |
  Gusto CSV --------+       |                                   v
                    macOS Keychain              review / recategorize / approve
                    (API tokens)                                |
                                                                v
                                                push --dry-run, then push
                                                                |
                                                                v
                                                         Wave GraphQL API

  report --> checklist of transfers between your own accounts --> you, in the Wave UI
```

The Keychain is read whenever the configuration loads, so every command reads every
`${keychain:...}` token the config mentions, not just `ingest`.

Wave's API can't read back bank transactions, link deposits to invoices, or create transfers between
accounts, and `moneyTransactionCreate` insists on exact double-entry balance. So every transaction is
staged locally first: sources are cross-reconciled, each staged transaction is checked to balance to
the penny, and a deterministic fingerprint plus an `externalId` keep re-runs from creating duplicates.

## Quickstart

```bash
uv sync --extra dev                        # install the project and dev tools

uv run ledgerdemain init                   # 1. creates ledgerdemain.toml, rules.toml, data/, inbox/
# 2. store API tokens in the Keychain, then edit ledgerdemain.toml and rules.toml (see Setup)
uv run ledgerdemain wave sync-meta         # 3. cache your Wave accounts and customers

uv run ledgerdemain ingest all             # 4. Harvest and Grasshopper APIs, plus files in inbox/
uv run ledgerdemain reconcile --confirm-high-confidence   # 5. match deposits, payroll, transfers

uv run ledgerdemain plan                   # 6. build balanced Wave transactions
uv run ledgerdemain review --status needs_review
uv run ledgerdemain recategorize --tx 4 --account software_subscriptions

uv run ledgerdemain approve --all-staged   # 7. approve, preview, push
uv run ledgerdemain push --dry-run
uv run ledgerdemain push

uv run ledgerdemain report --period 2026-11   # 8. summary and the Wave UI checklist
```

## Setup

### Secrets

API tokens live in the macOS Keychain, never in a file. Store each one once:

```bash
security add-generic-password -U -s ledgerdemain -a WAVE_ACCESS_TOKEN -w
```

Do the same for `HARVEST_ACCESS_TOKEN`, `GRASSHOPPER_ACCESS_TOKEN` and `GRASSHOPPER_HEADER_SECRET`.
`ledgerdemain.toml` refers to them as `${keychain:NAME}`. Non-secret values such as account IDs use
`${ENV_VAR}` (read from the environment) or can be written directly in the file.

- Tokens must be printable ASCII.
- Every `${keychain:...}` item the config mentions must exist, or loading fails. Blank out
  (`""`) the credentials of any API you don't use and `ingest all` skips it.
- The Keychain service name is `[general] keychain_service` (default `ledgerdemain`).

### Wave

- Needs a **Wave Pro** (or Advisors) plan with the newer accounting (`isClassicAccounting = false`).
- Store `WAVE_ACCESS_TOKEN`. `ledgerdemain wave list-businesses` shows your business ID.
- `ledgerdemain wave sync-meta` caches your chart of accounts and customers. Then map your logical
  account names to Wave accounts under `[accounts]` in `ledgerdemain.toml`.
- `plan` stops and names any account key that has no cached Wave account and isn't one of the
  built-in accounts, so nothing is staged with a guessed debit/credit label. Run `wave sync-meta`
  first, and again after you add or rename accounts in Wave.

### Harvest (API)

- Create a personal access token and note your account ID at
  <https://id.getharvest.com/developers>.
- Store `HARVEST_ACCESS_TOKEN`; set `account_id` and a `user_agent` with your app name and contact
  email.

### Grasshopper Bank (API)

- In online banking, go to **Settings -> API Banking**, accept the terms, **whitelist your static IP
  address**, and store the bearer token (`GRASSHOPPER_ACCESS_TOKEN`) and header secret
  (`GRASSHOPPER_HEADER_SECRET`).
- `ledgerdemain grasshopper list-accounts` shows the account IDs. Map your checking and money market
  accounts under `[[grasshopper.accounts]]` (`grasshopper_checking`, `grasshopper_money_market`).
- On the first sync of a fresh database, pass `ingest grasshopper --from YYYY-MM-DD` to choose how
  far back to go.

### Capital One card (OFX file)

- Export **OFX/QFX/QBO** from Capital One and put the file in `inbox/capital_one/`. Overlapping
  downloads are safe: each transaction carries a unique `FITID`.
- Only OFX is supported. `ingest capital-one --file` rejects other file types.

### Gusto payroll (CSV files)

- Gusto limits its API to approved partner apps, so single-company owners use exports. From
  **Reports**, export the **Payroll journal** and **Contractor Payment** CSVs into `inbox/gusto/`.
  Gusto names the payroll file `<company>-payroll-summary-<dates>.csv`, but the report inside is
  titled "Payroll journal", and that is the one this reads.
- Preamble and `Totals` rows are skipped, lines are aggregated by check date, and the tax and
  benefit breakdowns are kept in the transaction notes.

## How it books things

The accounting treatment is fixed:

- **Harvest invoices are not created in Wave.** They are ingested and matched to Grasshopper
  deposits, and Wave only receives the deposit. A matched deposit posts to `revenue_default` as
  income, one line per invoice, with any payment processor fee going to `merchant_fees`. Income lines
  carry the Wave customer when the client name matches one cached by `wave sync-meta`; otherwise the
  customer is created at push time.
- **Transfers between your own accounts are entered by hand.** Wave's API has no transfer mutation.
  Matched transfers (Grasshopper to the Capital One card, checking to money market) are staged as
  `manual_transfer_required`, never pushed, and listed in the `report` checklist for you to enter in
  the Wave UI.
- **Payroll clearing accounts net to zero.** `Payroll Taxes Payable` and `Payroll Benefits Payable`
  clear to `$0.00` once the Gusto net pay, tax and benefit debits are matched.

## When things go wrong

**Unknown outcome.** If a Wave create request times out or comes back unreadable, Wave may have
applied it. `push` stops without sending anything else and marks the transaction `outcome_unknown`
(`ledgerdemain review --status outcome_unknown`). Re-planning and `approve --all-staged` leave it
alone, so it can't be sent twice by accident.

1. Look in Wave for the record. The error names the `externalId` (`ld:tx:...`); a customer is found
   by name.
2. Run `ledgerdemain wave sync-meta`, so a customer that was created is reused.
3. If the transaction exists in Wave, leave it alone. If it doesn't, re-queue it with
   `ledgerdemain approve --tx ID` and push again.

If Wave rejects a request outright (an HTTP or GraphQL error), `push` also stops, but nothing is
marked. Fix the cause and push again.

**Exit codes.**
- `ingest all` reports each unsupported or failed inbox file and each API failure on stderr, still
  ingests everything else, and exits 2 if anything failed. Re-running is safe: ingestion is
  idempotent.
- Single-command failures (`ingest harvest`, `wave sync-meta`, ...) print an `Error:` line and exit 2.
- `push` exits 1 if any record failed or the push stopped early.

## CLI reference

| Command | Purpose |
|---|---|
| `ledgerdemain init [--dir PATH]` | Create config, rules, inbox folders and the SQLite DB |
| `ledgerdemain wave list-businesses` | List the Wave businesses your token can see |
| `ledgerdemain wave sync-meta` | Cache Wave accounts and customers |
| `ledgerdemain grasshopper list-accounts` | List your Grasshopper Bank accounts |
| `ledgerdemain ingest harvest\|grasshopper\|capital-one\|gusto\|all` | Pull from the Harvest and Grasshopper APIs, or read Capital One OFX and Gusto CSV files (`all` also reports leftover files in `inbox/harvest/` and `inbox/grasshopper/`) |
| `ledgerdemain reconcile [--confirm-high-confidence]` | Match deposits, Gusto debits and card transfers |
| `ledgerdemain plan` | Stage balanced Wave transactions (transfers are staged for manual entry) |
| `ledgerdemain review [--status ...] [--json]` | Inspect staged, review-needed, manual or failed items |
| `ledgerdemain approve [--all-staged] [--tx ...] [--match ...]` | Confirm matches or approve staged transactions (`--tx` also re-queues a failed or `outcome_unknown` one) |
| `ledgerdemain recategorize --tx ID --account KEY [--note TEXT]` | Change a transaction's account and re-stage it |
| `ledgerdemain push [--dry-run]` | Preview payloads, or push approved transactions to Wave |
| `ledgerdemain report [--period YYYY-MM] [--format text\|json]` | Summary, AR status, payroll clearing check and the Wave UI checklist |

## Development

```bash
uv run --extra dev ruff format
uv run --extra dev ruff check
uv run --extra dev mypy
uv run --extra dev pytest
```

GitHub Actions runs the same four checks on every pull request.
