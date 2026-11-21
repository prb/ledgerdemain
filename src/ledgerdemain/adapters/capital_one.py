"""Capital One credit card CSV, PDF, and OFX adapters."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from ledgerdemain.adapters.base import normalize_description, parse_flexible_date
from ledgerdemain.adapters.ofx import OfxTransactionParser
from ledgerdemain.db import make_bank_fingerprint
from ledgerdemain.models import BankTransaction
from ledgerdemain.money import ZERO, parse_money
from ledgerdemain.pdf_extract import extract_pdf_text

_PERIOD_MONTH_NAME_RE = re.compile(
    r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})\s*(?:-|to|through|\u2013|\u2014)\s*"
    r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
    re.IGNORECASE,
)
_PERIOD_SLASH_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|to|through|\u2013|\u2014)\s*(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_CLOSING_DATE_RE = re.compile(
    r"(?:closing\s+date|statement\s+date)[:\s]+([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_CARD_LAST4_RE = re.compile(r"(?:card\s*(?:ending\s*in|#)|xxxx)\s*(\d{4})", re.IGNORECASE)
_MONTH_MAP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_TX_LINE_RE = re.compile(
    r"^(?P<tx_date>[A-Za-z]{3}\s+\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r"(?:\s+(?P<post_date>[A-Za-z]{3}\s+\d{1,2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?))?"
    r"\s+(?P<desc>.+?)\s+"
    r"(?P<amount>(?:-\s*)?\$?[\d,]+\.\d{2}(?:\s*CR)?)$",
    re.IGNORECASE,
)


class CapitalOneCsvAdapter:
    """Parse Capital One credit card CSV (or OFX/QBO) exports."""

    source_name = "capital_one"

    def parse_file(self, path: Path) -> list[BankTransaction]:
        """Parse Capital One CSV or OFX/QBO file into normalized BankTransactions."""
        if path.suffix.lower() in {".ofx", ".qfx", ".qbo"}:
            return OfxTransactionParser().parse_file(
                path, source="capital_one", account_key="capital_one_card"
            )

        seen_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        transactions: list[BankTransaction] = []

        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw_row in reader:
                row = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items() if k}
                tx_date_raw = row.get("transaction date") or row.get("date") or ""
                posted_raw = row.get("posted date") or tx_date_raw
                if not posted_raw:
                    continue

                tx_date = parse_flexible_date(tx_date_raw or posted_raw)
                posted_date = parse_flexible_date(posted_raw)
                desc = row.get("description") or "Capital One Transaction"
                norm_desc = normalize_description(desc)
                card_last4 = row.get("card no.") or row.get("card number") or ""
                category = row.get("category") or ""

                debit_raw = row.get("debit") or ""
                credit_raw = row.get("credit") or ""
                debit_amt = abs(parse_money(debit_raw, ZERO)) if debit_raw else ZERO
                credit_amt = abs(parse_money(credit_raw, ZERO)) if credit_raw else ZERO

                if debit_amt > ZERO:
                    direction = "WITHDRAWAL"
                    amount = debit_amt
                elif credit_amt > ZERO:
                    direction = "DEPOSIT"
                    amount = credit_amt
                else:
                    amt_raw = row.get("amount") or ""
                    if not amt_raw:
                        continue
                    signed = parse_money(amt_raw, ZERO)
                    if signed == ZERO:
                        continue
                    direction = "WITHDRAWAL" if signed > ZERO else "DEPOSIT"
                    amount = abs(signed)

                occ_key = (posted_date, direction, str(amount), norm_desc)
                occ_idx = seen_counts[occ_key]
                seen_counts[occ_key] += 1

                fingerprint = make_bank_fingerprint(
                    source="capital_one",
                    account_key="capital_one_card",
                    external_id=None,
                    posted_date=posted_date,
                    direction=direction,
                    amount=amount,
                    normalized_description=norm_desc,
                    occurrence_index=occ_idx,
                )

                transactions.append(
                    BankTransaction(
                        source="capital_one",
                        account_key="capital_one_card",
                        external_id=None,
                        fingerprint=fingerprint,
                        tx_date=tx_date,
                        posted_date=posted_date,
                        description=desc,
                        normalized_description=norm_desc,
                        amount=amount,
                        direction=direction,
                        source_category=category,
                        card_last4=card_last4,
                        raw=dict(raw_row),
                    )
                )

        return transactions


class CapitalOnePdfAdapter:
    """Parse Capital One monthly credit card PDF statements."""

    source_name = "capital_one"

    def parse_file(self, path: Path) -> list[BankTransaction]:
        """Extract and parse Capital One statement lines from PDF (or OFX)."""
        if path.suffix.lower() in {".ofx", ".qfx", ".qbo"}:
            return OfxTransactionParser().parse_file(
                path, source="capital_one", account_key="capital_one_card"
            )
        text = extract_pdf_text(path)
        return self.parse_text(text)

    def parse_text(self, text: str) -> list[BankTransaction]:
        """Parse extracted PDF statement text into normalized BankTransactions."""
        start_dt, end_dt = self._detect_statement_period(text)
        section_direction = "WITHDRAWAL"
        current_last4 = ""
        seen_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        transactions: list[BankTransaction] = []

        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue

            card_match = _CARD_LAST4_RE.search(line)
            if card_match:
                current_last4 = card_match.group(1)

            lowered = line.lower()
            if any(
                hdr in lowered
                for hdr in (
                    "payments, credits",
                    "payments and credits",
                    "payments & credits",
                    "credits and adjustments",
                )
            ):
                section_direction = "DEPOSIT"
                continue
            if any(
                hdr in lowered for hdr in ("transactions", "purchases", "fees", "interest charged")
            ) and not _TX_LINE_RE.match(line):
                section_direction = "WITHDRAWAL"
                continue

            if "total " in lowered or lowered.startswith("trans date"):
                continue

            tx_match = _TX_LINE_RE.match(line)
            if not tx_match:
                continue

            tx_date = self._resolve_statement_date(tx_match.group("tx_date"), start_dt, end_dt)
            post_raw = tx_match.group("post_date")
            posted_date = (
                self._resolve_statement_date(post_raw, start_dt, end_dt) if post_raw else tx_date
            )
            desc = tx_match.group("desc").strip()
            amt_str = tx_match.group("amount").strip()

            explicit_credit = amt_str.startswith("-") or amt_str.upper().endswith("CR")
            clean_amt = re.sub(r"CR$", "", amt_str, flags=re.IGNORECASE).strip()
            amount = abs(parse_money(clean_amt))
            if amount == ZERO:
                continue

            direction = "DEPOSIT" if explicit_credit else section_direction
            norm_desc = normalize_description(desc)
            occ_key = (posted_date, direction, str(amount), norm_desc)
            occ_idx = seen_counts[occ_key]
            seen_counts[occ_key] += 1

            fingerprint = make_bank_fingerprint(
                source="capital_one",
                account_key="capital_one_card",
                external_id=None,
                posted_date=posted_date,
                direction=direction,
                amount=amount,
                normalized_description=norm_desc,
                occurrence_index=occ_idx,
            )

            transactions.append(
                BankTransaction(
                    source="capital_one",
                    account_key="capital_one_card",
                    external_id=None,
                    fingerprint=fingerprint,
                    tx_date=tx_date,
                    posted_date=posted_date,
                    description=desc,
                    normalized_description=norm_desc,
                    amount=amount,
                    direction=direction,
                    source_category="Payment/Credit" if direction == "DEPOSIT" else "",
                    card_last4=current_last4,
                    raw={"pdf_line": line},
                )
            )

        return transactions

    @staticmethod
    def _detect_statement_period(text: str) -> tuple[date, date]:
        m = _PERIOD_MONTH_NAME_RE.search(text) or _PERIOD_SLASH_RE.search(text)
        if m:
            start_iso = parse_flexible_date(m.group(1))
            end_iso = parse_flexible_date(m.group(2))
            return date.fromisoformat(start_iso), date.fromisoformat(end_iso)

        closing = _CLOSING_DATE_RE.search(text)
        if closing:
            end_iso = parse_flexible_date(closing.group(1))
            end_dt = date.fromisoformat(end_iso)
            start_year = end_dt.year - 1 if end_dt.month == 1 else end_dt.year
            start_month = 12 if end_dt.month == 1 else end_dt.month - 1
            return date(start_year, start_month, 1), end_dt

        today = date.today()
        return date(today.year, 1, 1), today

    @staticmethod
    def _resolve_statement_date(token: str, start_dt: date, end_dt: date) -> str:
        tok = token.strip()
        if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", tok):
            return parse_flexible_date(tok)

        slash_short = re.match(r"^(\d{1,2})/(\d{1,2})$", tok)
        if slash_short:
            month = int(slash_short.group(1))
            day = int(slash_short.group(2))
        else:
            mon_match = re.match(r"^([A-Za-z]{3})\s+(\d{1,2})$", tok)
            if not mon_match:
                return parse_flexible_date(tok)
            month = _MONTH_MAP[mon_match.group(1).lower()]
            day = int(mon_match.group(2))

        if start_dt.year < end_dt.year:
            year = start_dt.year if month >= start_dt.month else end_dt.year
        else:
            year = end_dt.year
        return date(year, month, day).isoformat()
