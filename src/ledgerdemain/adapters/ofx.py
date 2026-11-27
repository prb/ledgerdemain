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
    r"<STMTTRN(?:\s[^>]*)?>(.*?)"
    r"(?:</STMTTRN\s*>|(?=<STMTTRN(?:\s[^>]*)?>|</BANKTRANLIST\s*>|<LEDGERBAL\b|<AVAILBAL\b|</STMTRS\s*>|</CCSTMTRS\s*>|</OFX\s*>|$))",
    re.DOTALL | re.IGNORECASE,
)
_STMTTRN_OPEN_RE = re.compile(r"<STMTTRN(?:\s[^>]*)?/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<([A-Za-z0-9_]+)>\s*([^<\r\n]+)", re.IGNORECASE)
_ACCTFROM_BLK_RE = re.compile(
    r"<(?:CC|BANK)ACCTFROM>(.*?)(?:</(?:CC|BANK)ACCTFROM>|<BANKTRANLIST\b|<STMTTRN\b|$)",
    re.DOTALL | re.IGNORECASE,
)
_ACCTID_RE = re.compile(r"<ACCTID>\s*([^<\r\n]+)", re.IGNORECASE)
_CURDEF_SGML_RE = re.compile(r"<CURDEF>\s*([^<\r\n]*)", re.IGNORECASE)
_LEDGERBAL_RE = re.compile(
    r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|(?=<AVAILBAL\b|</CCSTMTRS>|</STMTRS>|</OFX>|$))",
    re.DOTALL | re.IGNORECASE,
)
_AVAILBAL_RE = re.compile(
    r"<AVAILBAL>(.*?)(?:</AVAILBAL>|(?=<LEDGERBAL\b|</CCSTMTRS>|</STMTRS>|</OFX>|$))",
    re.DOTALL | re.IGNORECASE,
)
_OFX_OPEN_RE = re.compile(r"<OFX(?:\s[^>]*)?>", re.IGNORECASE)
_OFX_CLOSE_RE = re.compile(r"</OFX\s*>", re.IGNORECASE)
_BANKTRANLIST_OPEN_RE = re.compile(r"<BANKTRANLIST(?:\s+[^>/]*)?(?<!/)>", re.IGNORECASE)
_BANKTRANLIST_SELF_RE = re.compile(r"<BANKTRANLIST(?:\s[^>]*)?/>", re.IGNORECASE)
_BANKTRANLIST_CLOSE_RE = re.compile(r"</BANKTRANLIST\s*>", re.IGNORECASE)
_XML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_XML_PI_RE = re.compile(r"<\?.*?\?>", re.DOTALL)
_BARE_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9A-Fa-f]+);)")
_XML_DECL_RE = re.compile(r"<\?(?:xml|ofx)\b", re.IGNORECASE)
_SGML_HEADER_RE = re.compile(r"(?:^|\n)\s*(?:OFXHEADER|DATA)\s*:", re.IGNORECASE)
_XML_LEAF_CLOSE_RE = re.compile(
    r"</(?:DTPOSTED|DTUSER|TRNAMT|TRNTYPE|FITID|NAME|MEMO|CURDEF|BALAMT|DTASOF|ACCTID)\s*>",
    re.IGNORECASE,
)
_COMPACT_OFX_DATE_RE = re.compile(r"^\d{8}(?:\d{4,6}(?:\.\d+)?)?(?:\[[^\]]+\])?$")
_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


class OfxParseError(ValueError):
    """Raised when OFX, QFX, or QBO content is malformed, truncated, or fails validation."""


def _parse_ofx_date(raw: str, *, field_name: str = "date") -> str | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    candidate = cleaned[:8] if _COMPACT_OFX_DATE_RE.match(cleaned) else cleaned
    try:
        return parse_flexible_date(candidate)
    except ValueError as exc:
        raise OfxParseError(f"Invalid OFX date in <{field_name}>: {raw!r}") from exc


def _parse_ofx_money(raw: str, *, field_name: str) -> Money:
    try:
        return parse_money(raw)
    except (ValueError, TypeError) as exc:
        raise OfxParseError(f"Invalid OFX monetary amount in <{field_name}>: {raw!r}") from exc


def _last4(account_id: str) -> str:
    cleaned = account_id.strip()
    return cleaned[-4:] if len(cleaned) > 4 else cleaned


def _normalize_acct_chars(value: str) -> str:
    return _ALNUM_RE.sub("", value).upper()


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

    def parse_file(
        self,
        path: Path,
        source: str,
        account_key: str,
        expected_currency: str | None = None,
        expected_account_last4: str | None = None,
    ) -> list[BankTransaction]:
        """Parse an OFX/QFX/QBO file into normalized BankTransaction records."""
        return self.parse_statement_file(
            path,
            source=source,
            account_key=account_key,
            expected_currency=expected_currency,
            expected_account_last4=expected_account_last4,
        ).transactions

    def parse_text(
        self,
        text: str,
        source: str,
        account_key: str,
        expected_currency: str | None = None,
        expected_account_last4: str | None = None,
    ) -> list[BankTransaction]:
        """Parse raw OFX SGML or XML string into normalized BankTransaction objects."""
        return self.parse_statement_text(
            text,
            source=source,
            account_key=account_key,
            expected_currency=expected_currency,
            expected_account_last4=expected_account_last4,
        ).transactions

    def parse_statement_file(
        self,
        path: Path,
        source: str,
        account_key: str,
        expected_currency: str | None = None,
        expected_account_last4: str | None = None,
    ) -> OfxStatement:
        """Parse an OFX/QFX/QBO file into an OfxStatement with metadata and transactions."""
        raw_text = path.read_text(encoding="utf-8", errors="replace")
        return self.parse_statement_text(
            raw_text,
            source=source,
            account_key=account_key,
            expected_currency=expected_currency,
            expected_account_last4=expected_account_last4,
        )

    def parse_statement_text(
        self,
        text: str,
        source: str,
        account_key: str,
        expected_currency: str | None = None,
        expected_account_last4: str | None = None,
    ) -> OfxStatement:
        """Parse raw OFX SGML or XML string into an OfxStatement.

        When ``expected_currency`` is provided, it is normalized uppercase and verified against
        ``<CURDEF>`` (or the default ``USD`` when a minimal legacy SGML export omits ``<CURDEF>``).
        When ``expected_account_last4`` is provided, the extracted statement ``account_id`` must
        be non-empty and end with the normalized expected characters.
        """
        clean_text = text.lstrip("\ufeff")
        if "\x00" in clean_text:
            raise OfxParseError("OFX content contains NUL bytes")
        if not clean_text.strip():
            raise OfxParseError("OFX content is empty")

        ofx_open = _OFX_OPEN_RE.search(clean_text)
        if ofx_open is None:
            raise OfxParseError("Missing <OFX> root element")

        preamble = clean_text[: ofx_open.start()]
        if re.search(r"<!DOCTYPE\b|<(?![?!])", preamble, re.IGNORECASE):
            raise OfxParseError("Unexpected markup before <OFX> root element")

        ofx_closes = list(_OFX_CLOSE_RE.finditer(clean_text, ofx_open.end()))
        if not ofx_closes:
            raise OfxParseError("Truncated OFX content: missing closing </OFX> tag")

        text_no_comments = _XML_COMMENT_RE.sub("", clean_text)
        if (
            len(_OFX_OPEN_RE.findall(text_no_comments)) != 1
            or len(_OFX_CLOSE_RE.findall(text_no_comments)) != 1
        ):
            raise OfxParseError("Malformed OFX content: expected exactly one <OFX>...</OFX> root")

        ofx_body = clean_text[ofx_open.start() : ofx_closes[-1].end()]
        trailing = clean_text[ofx_closes[-1].end() :]
        if _XML_COMMENT_RE.sub("", trailing).strip():
            raise OfxParseError("Unexpected trailing content after closing </OFX> tag")

        parsed_xml = self._extract_xml_entries(clean_text)
        if parsed_xml is not None:
            meta, entries = parsed_xml
        else:
            self._validate_sgml_envelope(ofx_body)
            meta, entries = self._extract_sgml_entries(ofx_body)

        body_no_comments = _XML_COMMENT_RE.sub("", ofx_body)
        raw_stmttrn_count = len(_STMTTRN_OPEN_RE.findall(body_no_comments))
        if len(entries) != raw_stmttrn_count:
            raise OfxParseError(
                f"OFX <STMTTRN> count mismatch: found {raw_stmttrn_count} <STMTTRN> block(s) "
                f"but extracted {len(entries)}"
            )

        has_curdef = "CURDEF" in meta
        raw_currency = meta.get("CURDEF", "").strip()
        if has_curdef and not raw_currency:
            raise OfxParseError("OFX <CURDEF> element is empty")
        statement_currency = (raw_currency or "USD").upper()
        if expected_currency is not None and expected_currency.strip():
            expected_cur = expected_currency.strip().upper()
            if statement_currency != expected_cur:
                raise OfxParseError(
                    f"OFX currency mismatch: expected {expected_cur}, got {statement_currency}"
                )

        stmt_account_id = meta.get("ACCOUNT_ID", "").strip()
        if expected_account_last4 is not None and expected_account_last4.strip():
            norm_expected = _normalize_acct_chars(expected_account_last4)
            norm_actual = _normalize_acct_chars(stmt_account_id)
            if not norm_expected or not norm_actual or not norm_actual.endswith(norm_expected):
                raise OfxParseError(
                    f"OFX account mismatch: expected account ending in "
                    f"{expected_account_last4.strip()!r}, got {stmt_account_id!r}"
                )

        seen_counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
        transactions: list[BankTransaction] = []

        for idx, entry in enumerate(entries, start=1):
            dt_posted = entry.get("DTPOSTED", "").strip()
            if not dt_posted:
                raise OfxParseError(f"OFX <STMTTRN> #{idx} is missing required <DTPOSTED>")
            posted_date = _parse_ofx_date(dt_posted, field_name="DTPOSTED")
            if posted_date is None:
                raise OfxParseError(f"OFX <STMTTRN> #{idx} has empty <DTPOSTED>")

            trnamt_raw = entry.get("TRNAMT", "").strip()
            if not trnamt_raw:
                raise OfxParseError(f"OFX <STMTTRN> #{idx} is missing required <TRNAMT>")
            signed_amount = _parse_ofx_money(trnamt_raw, field_name="TRNAMT")

            dt_user_raw = entry.get("DTUSER", "").strip()
            tx_date = (
                _parse_ofx_date(dt_user_raw, field_name="DTUSER") if dt_user_raw else None
            ) or posted_date

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
            currency=statement_currency,
            start_date=_parse_ofx_date(meta.get("DTSTART", ""), field_name="DTSTART"),
            end_date=_parse_ofx_date(meta.get("DTEND", ""), field_name="DTEND"),
            ledger_balance=(
                _parse_ofx_money(ledger_raw, field_name="LEDGERBAL/BALAMT") if ledger_raw else None
            ),
            ledger_balance_as_of=_parse_ofx_date(
                meta.get("LEDGER_DTASOF", ""), field_name="LEDGERBAL/DTASOF"
            ),
            available_balance=(
                _parse_ofx_money(avail_raw, field_name="AVAILBAL/BALAMT") if avail_raw else None
            ),
            transactions=transactions,
        )

    @staticmethod
    def _validate_sgml_envelope(ofx_body: str) -> None:
        """Validate required <BANKTRANLIST> envelope tags in SGML OFX."""
        opens = list(_BANKTRANLIST_OPEN_RE.finditer(ofx_body))
        self_closed = list(_BANKTRANLIST_SELF_RE.finditer(ofx_body))
        closes = list(_BANKTRANLIST_CLOSE_RE.finditer(ofx_body))
        if not opens and not self_closed:
            raise OfxParseError("Missing <BANKTRANLIST> element in OFX content")
        if len(closes) < len(opens):
            raise OfxParseError("Truncated OFX content: missing closing </BANKTRANLIST> tag")
        if len(closes) > len(opens):
            raise OfxParseError("Malformed OFX content: unexpected closing </BANKTRANLIST> tag")
        for open_m, close_m in zip(opens, closes, strict=True):
            if close_m.start() < open_m.end():
                raise OfxParseError(
                    "Malformed OFX content: </BANKTRANLIST> appears before <BANKTRANLIST>"
                )

    @staticmethod
    def _extract_xml_entries(
        text: str,
    ) -> tuple[dict[str, str], list[dict[str, str]]] | None:
        """Parse OFX 2.x XML using case-insensitive <OFX> detection; return None for SGML."""
        clean_text = text.lstrip("\ufeff")
        ofx_open = _OFX_OPEN_RE.search(clean_text)
        if ofx_open is None:
            return None

        preamble = clean_text[: ofx_open.start()]
        is_explicit_xml = bool(_XML_DECL_RE.search(preamble))
        has_sgml_header = bool(_SGML_HEADER_RE.search(preamble))
        if is_explicit_xml:
            leftover_preamble = _XML_PI_RE.sub("", _XML_COMMENT_RE.sub("", preamble)).strip()
            if leftover_preamble:
                raise OfxParseError("Malformed XML preamble before <OFX> root element")

        xml_slice = clean_text[ofx_open.start() :]
        root: ET.Element | None = None
        parse_exc: ET.ParseError | None = None
        try:
            root = ET.fromstring(xml_slice)
        except ET.ParseError as exc:
            parse_exc = exc

        if root is None:
            is_xml_like = is_explicit_xml or (
                not has_sgml_header and bool(_XML_LEAF_CLOSE_RE.search(xml_slice))
            )
            if not is_xml_like:
                return None
            sanitized = _BARE_AMP_RE.sub("&amp;", xml_slice)
            try:
                root = ET.fromstring(sanitized)
            except ET.ParseError as exc:
                raise OfxParseError(f"Malformed XML in OFX content: {parse_exc}") from exc

        if root.tag.split("}")[-1].upper() != "OFX":
            raise OfxParseError(f"Expected <OFX> root element, got <{root.tag}>")

        meta: dict[str, str] = {}
        entries: list[dict[str, str]] = []
        found_banktranlist = False
        tx_currency_elems = {
            id(child)
            for parent in root.iter()
            if parent.tag.split("}")[-1].upper() in {"CURRENCY", "ORIGCURRENCY"}
            for child in parent
        }

        for elem in root.iter():
            tag = elem.tag.split("}")[-1].upper()
            if tag == "CURDEF" and id(elem) not in tx_currency_elems:
                meta.setdefault("CURDEF", (elem.text or "").strip())
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
            elif tag == "BANKTRANLIST":
                found_banktranlist = True
                for child in elem:
                    if child.tag.split("}")[-1].upper() != "STMTTRN":
                        continue
                    record: dict[str, str] = {}
                    for sub in child:
                        stag = sub.tag.split("}")[-1].upper()
                        if stag in {"CCACCTTO", "BANKACCTTO"}:
                            for acct_sub in sub:
                                atag = acct_sub.tag.split("}")[-1].upper()
                                if atag == "ACCTID" and acct_sub.text and acct_sub.text.strip():
                                    record["CARD_ACCTID"] = acct_sub.text.strip()
                        elif sub.text and sub.text.strip():
                            record[stag] = sub.text.strip()
                    entries.append(record)

        if not found_banktranlist:
            raise OfxParseError("Missing <BANKTRANLIST> element in OFX XML content")

        return meta, entries

    @staticmethod
    def _extract_sgml_entries(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
        """Parse OFX 1.x SGML statement metadata and <STMTTRN> blocks."""
        meta: dict[str, str] = {}
        acct_blk = _ACCTFROM_BLK_RE.search(text)
        if acct_blk:
            acct_match = _ACCTID_RE.search(acct_blk.group(1))
            if acct_match:
                meta["ACCOUNT_ID"] = acct_match.group(1).strip()

        curdef_m = _CURDEF_SGML_RE.search(text)
        if curdef_m:
            meta["CURDEF"] = html.unescape(curdef_m.group(1).strip())

        for tag in ("DTSTART", "DTEND"):
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
            entries.append(record)

        return meta, entries
