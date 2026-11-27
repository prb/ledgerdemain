---
inclusion: always
description: Conventions for modeling financial values and accounting transactions.
---

# Financial Modeling

Apply these conventions whenever storing, parsing, calculating, serializing, or
validating monetary amounts and accounting transactions in Ledgerdemain.

- **Use fixed-point integer cents (`Money`), never `float` or `Decimal`.** Represent
  all monetary values with `ledgerdemain.money.Money` (an immutable value object
  backed by exact integer `cents: int`). Do not introduce `float` or floating-point
  `decimal.Decimal` for currency math.
- **Enforce strict monetary type boundaries.** Keep arithmetic and comparisons strictly
  between `Money` instances (`+`, `-`, ordering); do not implicitly coerce raw `int`,
  `float`, or `bool` values into money. Keep non-monetary quantities (such as invoice
  line-item `quantity` strings or reconciliation `confidence` scores) distinct from
  `Money`.
- **Parse and round at ingestion boundaries only.** Convert external strings, numbers,
  and API payloads into `Money` immediately at adapter/deserialization boundaries using
  `parse_money` / `Money.parse` (exact string parsing with half-up rounding) or
  `Money.from_cents` for integer-cent APIs (e.g., Grasshopper/Narmi). Finite `float`
  values from JSON/APIs may be converted via `Money.parse` (which uses `repr` and rounds
  half-up) at these boundaries, while `bool`, `NaN`, and `Inf` are rejected; never store
  or compute monetary values as `float` inside the domain.
- **Use exact rational scaling and explicit remainder handling for splits.** Use
  `Money.mul_ratio(numerator, denominator)` (exact integer arithmetic with half-up
  rounding) for rational scaling such as percentage bounds or thresholds (e.g., fee
  windows in `reconcile.py`) rather than floating-point division or multiplication.
  Because `mul_ratio` rounds each scaled value independently and there is no built-in
  allocation helper, any multi-way split must assign the rounding remainder explicitly
  (for example, to the last leg or via deterministic cent distribution) so that the
  parts sum exactly to the original whole amount with no lost cents.
- **Store unsigned magnitudes with explicit direction or balance enums.** On
  `BankTransaction` and `StagedTransaction` anchors, store positive `Money` amounts
  paired with `direction` (`DEPOSIT` or `WITHDRAWAL`). On `StagedLineItem`, store
  positive `Money` amounts paired with `balance` (`INCREASE` or `DECREASE`). Reserve
  signed `Money` for net totals, deltas, and report summaries.
- **Enforce exact double-entry balancing.** Every `StagedTransaction` must pass
  `validate_double_entry_balance`: all amounts must be strictly positive, the anchor
  account must never appear in its own `line_items`, and the signed net debit of all
  line items (using each account's normal balance `DEBIT` or `CREDIT`) must equal
  `+anchor_amount` for a `WITHDRAWAL` and `-anchor_amount` for a `DEPOSIT` with zero
  imbalance.
- **Zero out clearing accounts.** Clearing accounts (`payroll_taxes_payable`,
  `payroll_benefits_payable`) must net to `Money.ZERO` (`0.00`) once all corresponding
  legs are matched and posted.
- **Serialize canonically for storage, APIs, and fingerprints.** Format `Money` with
  `format_money(value)` (`Money.format()`, e.g. `"-1234.56"`, fixed 2 decimal places,
  no currency symbols or thousands separators) for SQLite persistence, JSON payloads,
  Wave GraphQL inputs, and deterministic SHA-256 fingerprints (`make_bank_fingerprint`
  and `GustoPayrollCsvAdapter._run_fingerprint` in `adapters/gusto.py`).
  Do not add thousands separators or currency symbols to any of these outputs
  (CLI and report output also use `format_money`). Preserve canonical formatting so
  re-ingesting overlapping exports remains idempotent.
- **Verify financial invariants with property-based tests.** Cover parsing roundtrips,
  fixed-point algebra, rational scaling, double-entry balance checks, payroll clearing
  identities, and fingerprint determinism with Hypothesis tests in
  `tests/test_properties.py`.
