---
inclusion: always
description: Conventions for rigorous external file and payload parsing.
---

# Data Ingestion & Parsing

Apply these rules whenever parsing external files or API payloads (CSV, OFX/QFX/QBO, PDF, JSON).

- **Fail fast and loudly on drift or corruption.** Raise an explicit domain parse error (a `ValueError` subclass such as `GustoParseError` or `OfxParseError`) on empty or unrecognizable files, missing required columns/tags, truncated envelopes, or unparseable dates and amounts. Never silently return `[]`, skip malformed records, or substitute defaults like `0.00` or today's date.
- **Validate structure and completeness.** Check envelope/header integrity and record-count parity, and explicitly distinguish valid zero-activity exports (e.g., a well-formed statement with no transactions) from garbage, HTML error pages, or truncated downloads.
- **Cross-check redundant invariants.** Whenever a source provides totals or metadata (net pay vs. gross/taxes/benefits, employer cost, statement currency, account last-4), assert they reconcile within documented tolerance so renamed or dropped fields cannot silently disappear.
- **Keep format fallbacks narrow.** Only fall back between formats (e.g., XML vs. SGML, multi-section vs. flat CSV) when the input unambiguously matches the fallback format—never use a looser parser to swallow truncation or syntax errors in a stricter one.
- **Require negative and drift tests.** Every parser must have unit tests covering empty/unrecognizable input, truncated/malformed payloads, missing or renamed required fields, violated source invariants, and valid zero-activity edge cases.
