# Future features

## Accrual-basis invoice mirroring (removed in #50)

Ledgerdemain used to mirror Harvest invoices into Wave so revenue was recognized when an invoice
was issued (accrual basis). It was removed because the working configuration never used it:
Harvest invoices are ingested and matched to deposits, deposits book to `revenue_default`, and
transfers between own accounts are recorded by hand in Wave. This page records how mirroring
worked so it can be restored.

### What it did

- **Invoice mirroring.** `plan` staged every non-draft Harvest invoice as a `StagedInvoice`
  (table `staged_invoices`, `externalId` `ld:inv:harvest:<harvest_id>`). `push` created each one in
  Wave with the `invoiceCreate` mutation. Each invoice used the Wave customer with the client's name
  (created with `customerCreate` if missing) and one line per Harvest line item, all pointing at a
  single default product (`[wave] default_product_name`, created with `productCreate` if missing).
  The invoice status on creation was `SAVED` or `DRAFT`.
- **Deposits against mirrored invoices.** A bank deposit matched to a Harvest invoice was staged
  according to `mirrored_invoice_deposit_strategy`:
  - `ar_clearing` (default): a money transaction crediting `invoice_payments_clearing` instead of
    revenue, so revenue is counted once, by the invoice. The clearing balance was then applied
    against the invoice or cleared in Wave.
  - `manual_ui_payment`: the deposit was not pushed. It was staged as
    `manual_invoice_payment_required` and listed in the report checklist, and you recorded the
    payment against the Wave invoice in the Wave UI.
  - `direct_income`: book to `revenue_default`. This is the behavior that remains.
- **Transfers.** `transfer_strategy = "clearing_account"` pushed both legs of a paired transfer
  (Grasshopper to Capital One card payment, or bank to bank) against `cc_transfer_clearing` or
  `bank_transfer_clearing`, so the clearing account netted to `0.00`. `manual_transfer` (the
  behavior that remains) stages them as `manual_transfer_required` for entry in the Wave UI.

### Configuration that selected it

```toml
[accounting]
mirror_harvest_invoices = true                       # default; false = no mirroring
mirrored_invoice_deposit_strategy = "ar_clearing"    # or "manual_ui_payment" or "direct_income"
wave_invoice_create_status = "SAVED"                 # or "DRAFT"
transfer_strategy = "manual_transfer"                # or "clearing_account"

[wave]
default_product_name = "Consulting Services"

[accounts]
invoice_payments_clearing = "Invoice Payments Clearing"
cc_transfer_clearing = "Credit Card Payment Clearing"
bank_transfer_clearing = "Bank Transfer Clearing"
```

### What was removed

- `staged_invoices` table (and `wave_products`), the `StagedInvoice` and `WaveProduct` models, and
  the matching `LedgerDB` methods (`upsert_staged_invoice`, `list_staged_invoices`,
  `delete_unpushed_staged_invoices`, `upsert_wave_products`, `find_wave_product_by_name`).
- Planner: `_build_staged_invoice`, the mirroring loop in `build_plan`, the `manual_ui_payment` and
  `invoice_payments_clearing` branches of `_plan_harvest_deposit`, the `clearing_account` branch of
  `_plan_transfer`, and the `manual_invoice_payment_required` status.
- Wave: the invoice push loop in `WaveSyncService.push`, `invoiceCreate`, default product creation
  and `sync-meta` product sync (`productCreate`, `list_products`), and the `outcome_unknown`
  handling for invoice and product creates.
- CLI: `approve --invoice`, `push --invoices-only` and `--transactions-only`, the invoice sections
  of `plan`, `review`, `approve`, `push` and `report` output.
- The config keys above, and the accounts `invoice_payments_clearing`, `cc_transfer_clearing` and
  `bank_transfer_clearing`.

Kept: Harvest invoice ingest and matching, Wave customers (`wave_customers`, `customerCreate`, and
the `customerId` on income lines), and the payroll clearing accounts.

### Where to find the code

- Git tag `accrual-mirroring-last` is the last commit that has it. For example:
  `git show accrual-mirroring-last:src/ledgerdemain/planner.py`,
  `git show accrual-mirroring-last:src/ledgerdemain/wave/sync.py`,
  `git show accrual-mirroring-last:src/ledgerdemain/db.py`.
- Pull request #59 (which closes #50) removed it. Its diff is the list of
  pieces to put back.

### Restoring it

1. Bring back the table, model, DB methods and Wave client calls listed above from the tag, plus
   the product cache, and the config keys with their validation.
2. Reinstate the planner branches and the invoice push loop, and add the three clearing accounts
   back to `DEFAULT_NORMAL_BALANCE`, `[accounts]` and the example config.
3. Restore the CLI flags and output sections, and reword the README "Accounting Workflows" section.
4. Restore the tests that went with each piece. The mock Wave transport in `tests/fixtures.py`
   needs its `InvoiceCreate` and `ProductCreate` handlers back.

### Gotchas

- **Double-counted revenue.** Mirroring invoices and also booking deposits to `revenue_default`
  (`direct_income`) counts revenue twice: once by the invoice and once by the deposit. The old
  config validation rejected `mirror_harvest_invoices = true` together with `direct_income`.
  Keep that check. With mirroring on, deposits must book to the clearing account (or be recorded
  against the invoice in the Wave UI).
- **Clearing accounts need clearing.** `invoice_payments_clearing` only nets to zero if each
  deposit is applied against its invoice in Wave. Nothing in the API does that, so it stays a
  manual step.
- **Unknown outcomes.** Invoice creation was already protected against an unknown outcome (a
  timeout or an unreadable response may mean Wave applied the create). The push stopped, marked the
  invoice `outcome_unknown`, never resent it on its own, and only `approve --invoice ID` re-queued
  it after you checked Wave. Customer and product creates told you to run `wave sync-meta` before
  pushing again so the created record was reused. Any restored create mutation needs the same
  treatment; the transaction handling in `WaveSyncService.push` shows the pattern.
- **Draft invoices** (Harvest state `draft`) were not mirrored.
