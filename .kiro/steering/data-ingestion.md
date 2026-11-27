---
inclusion: always
description: Conventions for rigorous external file and payload parsing.
---

# Data Ingestion & Parsing

Apply these rules whenever parsing external files or API payloads (CSV, OFX/QFX/QBO, PDF, JSON).

- **Fail fast and loudly on drift or corruption.** Raise an explicit domain parse error (a `ValueError` subclass named for the adapter, e.g., `<Source>ParseError`) on empty or unrecognizable files, missing required columns/tags, truncated envelopes, or unparseable dates and amounts. Never silently return `[]`, skip malformed records, or substitute defaults for required fields (such as `0.00` or today's date). Optional fields may use explicit defaults when documented at the field or helper definition.
- **Never write a partial file.** Parse and validate the entire file before any database write so a parse failure leaves no records and no `ingestion_batches` row, and insert the file's records in a single transaction. Single-file (`--file`) runs fail immediately; multi-file inbox runs log per-file failures to `stderr`, continue processing remaining files (relying on record-level idempotency), and exit non-zero if any file failed.
- **Validate structure and completeness.** Check envelope/header integrity and record-count parity, and explicitly distinguish valid zero-activity exports (e.g., a well-formed statement with no transactions) from garbage, HTML error pages, or truncated downloads.
- **Cross-check redundant invariants.** Whenever a source provides totals or metadata (net pay vs. gross/taxes/benefits, employer cost, statement currency, account last-4), assert they reconcile so renamed or dropped fields cannot silently disappear. Use exact equality by default; when a source cannot produce exact totals, express each tolerance as a named constant with a comment explaining its rationale (e.g., per-row or section rounding drift).
- **Keep format fallbacks narrow.** Only fall back between formats (e.g., XML vs. SGML, multi-section vs. flat CSV) when the input unambiguously matches the fallback format—never use a looser parser to swallow truncation or syntax errors in a stricter one.
- **Require negative and drift tests.** Every new or changed parser must have unit tests covering empty/unrecognizable input, truncated/malformed payloads, missing or renamed required fields, violated source invariants, and valid zero-activity edge cases.
