"""OFX 1.x (SGML) and OFX 2.x (XML) statement parser."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ledgerdemain.adapters.base import normalize_description, parse_flexible_date
from ledgerdemain.db import make_bank_fingerprint
from ledgerdemain.models import BankTransaction
from ledgerdemain.money import ZERO, Money, parse_money

_STMTTRN_RE = re.compile(
    r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>|</BANKTRANLIST>))", re.DOTALL | re.IGNORECASE
)
_TAG_RE = re.compile(r"<([A-Za-z0-9_]+)>\s*([^<\r\n]+)", re.IGNORECASE)
_ACCTFROM_RE = re.compile(
    r"<(?:CC|BANK)ACCTFROM>.*?<ACCTID>\s*([^<\r\n]+)", re.DOTALL | re.IGNORECASE
)
_LEDGERBAL_RE = re.compile(
    r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|</CCSTMTRS>|</STMTRS>|</OFX>)",
    re.DOTALL | re.IGNORECASE,
)
_AVAILBAL_RE = re.compile(
    r"<AVAILBAL>(.*?)(?:</AVAILBAL>|</CCSTMTRS>|</STMTRS>|</OFX>)",
    re.DOTALL | re.IGNORECASE,
)


def _parse_ofx_date(raw: str) -> str | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    return parse_flexible_date(cleaned[:8] if len(cleaned) >= 8 else cleaned)


def _last4(account_id: str) -> str:
    cleaned = account_id.strip()
    return cleaned[-4:] if len(cleaned) > 4 else cleaned


@dataclass(slots=True)
class OfxStatement:
    """Parsed OFX statement metadata alongside normalized transactions."""

    account_id: str = ""
    currency: str = "USD"
    start_date: str | None = None
    end_date: str | None = None
    ledger_balance: Money | None = None
    ledger_balance_as_of: str | None = None
    available_balance: Money | None = None
    transactions: list[BankTransaction] = field(default_factory=list)


class OfxTransactionParser:
    """Parse OFX / QFX / QBO bank and credit card statement files."""

    def parse_file(self, path: Path, source: str, account_key: str) -> list[BankTransaction]:
        """Parse an OFX/QFX/QBO file into normalized BankTransaction records."""
        return self.parse_statement_file(path, source=source, account_key=account_key).transactions

    def parse_text(self, text: str, source: str, account_key: str) -> list[BankTransaction]:
        """Parse raw OFX SGML or XML string into normalized BankTransaction objects."""
        return self.parse_statement_text(text, source=source, account_key=account_key).transactions

    def parse_statement_file(self, path: Path, source: str, account_key: str) -> OfxStatement:
        """Parse an OFX/QFX/QBO file into an OfxStatement with metadata and transactions."""
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        return self.parse_statement_text(raw_text, source=source, account_key=account_key)

    def parse_statement_text(self, text: str, source: str, account_key: str) -> OfxStatement:
        """Parse raw OFX SGML or XML string into an OfxStatement."""
        parsed_xml = self._extract_xml_entries(text)
        if parsed_xml is not None:
            meta, entries = parsed_xml
        else:
            meta, entries = self._extract_sgml_entries(text)

        stmt_account_id = meta.get("ACCOUNT_ID", "").strip()
        seen_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        transactions: list[BankTransaction] = []

        for entry in entries:
            dt_posted = entry.get("DTPOSTED", "").strip()
            trnamt_raw = entry.get("TRNAMT", "").strip()
            if not dt_posted or not trnamt_raw:
                continue

            posted_date = _parse_ofx_date(dt_posted)
            if posted_date is None:
                continue
            tx_date = _parse_ofx_date(entry.get("DTUSER", "")) or posted_date

            signed_amount = parse_money(trnamt_raw)
            if signed_amount == ZERO:
                continue

            direction = "DEPOSIT" if signed_amount > ZERO else "WITHDRAWAL"
            amount = abs(signed_amount)

            name = entry.get("NAME", "").strip()
            memo = entry.get("MEMO", "").strip()
            if name and memo and memo.lower() not in name.lower():
                description = f"{name} - {memo}"
            else:
                description = name or memo or entry.get("TRNTYPE", "OFX Transaction")

            norm_desc = normalize_description(description)
            fitid = entry.get("FITID", "").strip() or None
            occ_key = (posted_date, direction, str(amount), norm_desc)
            occ_idx = seen_counts[occ_key]
            seen_counts[occ_key] += 1

            fingerprint = make_bank_fingerprint(
                source=source,
                account_key=account_key,
                external_id=fitid,
                posted_date=posted_date,
                direction=direction,
                amount=amount,
                normalized_description=norm_desc,
                occurrence_index=occ_idx,
            )

            raw_card = entry.get("CARD_ACCTID") or stmt_account_id
            transactions.append(
                BankTransaction(
                    source=source,
                    account_key=account_key,
                    external_id=fitid,
                    fingerprint=fingerprint,
                    tx_date=tx_date,
                    posted_date=posted_date,
                    description=description,
                    normalized_description=norm_desc,
                    amount=amount,
                    direction=direction,
                    source_category=entry.get("TRNTYPE", "").strip(),
                    card_last4=_last4(raw_card),
                    raw=dict(entry),
                )
            )

        ledger_raw = meta.get("LEDGER_BALAMT", "").strip()
        avail_raw = meta.get("AVAIL_BALAMT", "").strip()
        return OfxStatement(
            account_id=stmt_account_id,
            currency=meta.get("CURDEF", "USD").strip() or "USD",
            start_date=_parse_ofx_date(meta.get("DTSTART", "")),
            end_date=_parse_ofx_date(meta.get("DTEND", "")),
            ledger_balance=parse_money(ledger_raw) if ledger_raw else None,
            ledger_balance_as_of=_parse_ofx_date(meta.get("LEDGER_DTASOF", "")),
            available_balance=parse_money(avail_raw) if avail_raw else None,
            transactions=transactions,
        )

    @staticmethod
    def _extract_xml_entries(
        text: str,
    ) -> tuple[dict[str, str], list[dict[str, str]]] | None:
        """Attempt strict XML parse for OFX 2.x files; return None to allow SGML fallback."""
        stripped = text.lstrip()
        if not stripped.startswith("<?xml") and "<OFX>" not in text:
            return None
        xml_start = text.find("<OFX>")
        if xml_start == -1:
            return None
        try:
            root = ET.fromstring(text[xml_start:])
        except ET.ParseError:
            return None

        meta: dict[str, str] = {}
        entries: list[dict[str, str]] = []

        for elem in root.iter():
            tag = elem.tag.split("}")[-1].upper()
            if tag == "CURDEF" and elem.text and elem.text.strip():
                meta.setdefault("CURDEF", elem.text.strip())
            elif tag in {"CCACCTFROM", "BANKACCTFROM"}:
                for child in elem:
                    ctag = child.tag.split("}")[-1].upper()
                    if ctag == "ACCTID" and child.text and child.text.strip():
                        meta["ACCOUNT_ID"] = child.text.strip()
            elif tag == "DTSTART" and elem.text and elem.text.strip():
                meta.setdefault("DTSTART", elem.text.strip())
            elif tag == "DTEND" and elem.text and elem.text.strip():
                meta.setdefault("DTEND", elem.text.strip())
            elif tag == "LEDGERBAL":
                for child in elem:
                    ctag = child.tag.split("}")[-1].upper()
                    if ctag == "BALAMT" and child.text and child.text.strip():
                        meta["LEDGER_BALAMT"] = child.text.strip()
                    elif ctag == "DTASOF" and child.text and child.text.strip():
                        meta["LEDGER_DTASOF"] = child.text.strip()
            elif tag == "AVAILBAL":
                for child in elem:
                    ctag = child.tag.split("}")[-1].upper()
                    if ctag == "BALAMT" and child.text and child.text.strip():
                        meta["AVAIL_BALAMT"] = child.text.strip()
            elif tag == "STMTTRN":
                record: dict[str, str] = {}
                for child in elem:
                    ctag = child.tag.split("}")[-1].upper()
                    if ctag in {"CCACCTTO", "BANKACCTTO"}:
                        for sub in child:
                            stag = sub.tag.split("}")[-1].upper()
                            if stag == "ACCTID" and sub.text and sub.text.strip():
                                record["CARD_ACCTID"] = sub.text.strip()
                    elif child.text and child.text.strip():
                        record[ctag] = child.text.strip()
                if record:
                    entries.append(record)

        return meta, entries

    @staticmethod
    def _extract_sgml_entries(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
        """Parse OFX 1.x SGML or fallback XML with unescaped entities."""
        meta: dict[str, str] = {}
        acct_match = _ACCTFROM_RE.search(text)
        if acct_match:
            meta["ACCOUNT_ID"] = acct_match.group(1).strip()

        for tag in ("CURDEF", "DTSTART", "DTEND"):
            m = re.search(rf"<{tag}>\s*([^<\r\n]+)", text, re.IGNORECASE)
            if m:
                meta[tag] = m.group(1).strip()

        ledger_match = _LEDGERBAL_RE.search(text)
        if ledger_match:
            blk = ledger_match.group(1)
            bal_m = re.search(r"<BALAMT>\s*([^<\r\n]+)", blk, re.IGNORECASE)
            asof_m = re.search(r"<DTASOF>\s*([^<\r\n]+)", blk, re.IGNORECASE)
            if bal_m:
                meta["LEDGER_BALAMT"] = bal_m.group(1).strip()
            if asof_m:
                meta["LEDGER_DTASOF"] = asof_m.group(1).strip()

        avail_match = _AVAILBAL_RE.search(text)
        if avail_match:
            bal_m = re.search(r"<BALAMT>\s*([^<\r\n]+)", avail_match.group(1), re.IGNORECASE)
            if bal_m:
                meta["AVAIL_BALAMT"] = bal_m.group(1).strip()

        entries: list[dict[str, str]] = []
        for match in _STMTTRN_RE.finditer(text):
            block = match.group(1)
            record: dict[str, str] = {}
            for tag_match in _TAG_RE.finditer(block):
                tag = tag_match.group(1).upper()
                val = html.unescape(tag_match.group(2).strip())
                if not val:
                    continue
                if tag == "ACCTID":
                    record["CARD_ACCTID"] = val
                else:
                    record[tag] = val
            if record:
                entries.append(record)

        return meta, entries
