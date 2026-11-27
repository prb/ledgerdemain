"""Gusto Payroll Journal and Contractor CSV adapter."""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ledgerdemain.adapters.base import parse_flexible_date
from ledgerdemain.models import GustoPayrollRun
from ledgerdemain.money import ZERO, Money, format_money, parse_money


class GustoParseError(ValueError):
    """Raised when a Gusto CSV export cannot be parsed or fails accounting checks."""


_TOLERANCE = Money(2)

_SUMMARY_EXACT = frozenset(
    {
        "total",
        "totals",
        "company total",
        "company totals",
        "grand total",
        "grand totals",
        "all employees",
        "all contractors",
        "payroll totals",
        "payroll total",
        "subtotal",
        "subtotals",
    }
)
_SUMMARY_PREFIX_STARTS = (
    "total ",
    "totals ",
    "total:",
    "totals:",
    "payroll total",
    "company total",
    "grand total",
)

_IGNORED_JOURNAL_TITLES = frozenset(
    {"payroll journal", "payroll journal summary by employee", "employee earnings"}
)

_SECTION_PERIOD_KEYS = frozenset({"pay period"})
_SECTION_DATE_KEYS = frozenset({"pay day", "check date", "pay date"})
_SECTION_META_KEYS = _SECTION_PERIOD_KEYS | _SECTION_DATE_KEYS
_PERIOD_SEP_RE = re.compile(r"\s+(?:-|to)\s+", re.IGNORECASE)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "check_date": ("check date", "pay date", "payment date", "debit date"),
    "pay_period_start": ("pay period start", "period start", "start date"),
    "pay_period_end": ("pay period end", "period end", "end date"),
    "employee_name": ("employee", "employee name", "full name", "name"),
    "gross_wages": ("gross earnings", "gross pay", "gross wages", "total gross"),
    "reimbursements": ("reimbursements", "reimbursement", "expense reimbursements"),
    "employee_taxes": (
        "employee taxes",
        "total employee taxes",
        "ee taxes",
        "employee tax withholdings",
    ),
    "employer_taxes": ("employer taxes", "total employer taxes", "er taxes", "company taxes"),
    "employee_benefits": (
        "employee deductions",
        "employee benefits",
        "ee deductions",
        "total deductions",
        "post-tax deductions",
        "pre-tax deductions",
    ),
    "employer_benefits": (
        "employer contributions",
        "employer benefits",
        "er contributions",
        "company contributions",
        "company benefits",
    ),
    "employer_cost": ("employer cost", "total employer cost"),
    "net_pay": ("net pay", "check amount", "direct deposit", "take home pay"),
}

_EE_TAX_ITEM_COLS = (
    (
        "federal_income_tax",
        ("federal income tax (employee)", "federal income tax", "fit", "fed income tax"),
    ),
    (
        "state_income_tax",
        ("state income tax (employee)", "state income tax", "sit", "state tax"),
    ),
    ("local_tax", ("local tax (employee)", "local tax", "local income tax")),
    (
        "ee_social_security",
        ("social security (employee)", "employee social security", "ee social security", "ee ss"),
    ),
    ("ee_medicare", ("medicare (employee)", "employee medicare", "ee medicare")),
    ("ee_additional_medicare", ("additional medicare (employee)", "employee additional medicare")),
)

_ER_TAX_ITEM_COLS = (
    (
        "er_social_security",
        ("social security (employer)", "employer social security", "er social security", "er ss"),
    ),
    ("er_medicare", ("medicare (employer)", "employer medicare", "er medicare")),
    ("futa", ("futa (employer)", "futa", "federal unemployment")),
    ("suta", ("suta (employer)", "suta", "state unemployment")),
)

_CONTRACTOR_AMOUNT_COLS = ("amount", "payment amount", "total paid", "gross pay", "net pay")


class GustoPayrollCsvAdapter:
    """Parse Gusto Payroll Journal and Contractor Payment CSV exports."""

    source_name = "gusto"

    def __init__(self, column_overrides: dict[str, str] | None = None) -> None:
        self.column_overrides = {k: v.strip().lower() for k, v in (column_overrides or {}).items()}

    def parse_file(self, path: Path, run_type: str = "regular") -> list[GustoPayrollRun]:
        """Parse a Gusto payroll journal or contractor CSV file."""
        if run_type == "contractor":
            return self.parse_contractor_csv(path)

        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))

        if not any(cell.strip() for row in rows for cell in row):
            raise GustoParseError(f"Empty Gusto payroll CSV: {path}")

        if journal_runs := self._parse_payroll_journal_sections(rows, default_run_type=run_type):
            return journal_runs

        header_idx = self._find_header_row(rows)
        if header_idx is None:
            raise GustoParseError(
                f"Unrecognized Gusto payroll CSV format (missing required columns): {path}"
            )

        headers = [h.strip().lower() for h in rows[header_idx]]
        self._validate_payroll_headers(headers, context=str(path), require_date=True)
        grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)

        for raw_row in rows[header_idx + 1 :]:
            if not any(cell.strip() for cell in raw_row):
                continue
            row = {
                headers[i]: raw_row[i].strip() if i < len(raw_row) else ""
                for i in range(len(headers))
                if headers[i]
            }
            if self._is_summary_row(row):
                continue

            check_raw = self._get_field(row, "check_date")
            if not check_raw:
                continue
            check_date = self._parse_csv_date(check_raw, context="check_date")
            pp_start_raw = self._get_field(row, "pay_period_start") or check_raw
            pp_end_raw = self._get_field(row, "pay_period_end") or check_raw
            pp_start = self._parse_csv_date(pp_start_raw, context="pay_period_start")
            pp_end = self._parse_csv_date(pp_end_raw, context="pay_period_end")
            grouped[(check_date, pp_start, pp_end)].append(row)

        if not grouped:
            raise GustoParseError(f"No payroll rows found in Gusto CSV: {path}")

        runs: list[GustoPayrollRun] = []
        for (check_date, pp_start, pp_end), group_rows in sorted(grouped.items()):
            runs.append(
                self._aggregate_payroll_group(
                    check_date=check_date,
                    pp_start=pp_start,
                    pp_end=pp_end,
                    run_type=run_type,
                    rows=group_rows,
                )
            )
        return runs

    def parse_contractor_csv(self, path: Path) -> list[GustoPayrollRun]:
        """Parse a Gusto contractor payments CSV export into contractor runs."""
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))

        if not any(cell.strip() for row in rows for cell in row):
            raise GustoParseError(f"Empty Gusto contractor CSV: {path}")

        header_idx = self._find_header_row(rows, contractor=True)
        if header_idx is None:
            raise GustoParseError(
                "Unrecognized Gusto contractor CSV format "
                f"(missing payment date or amount column): {path}"
            )

        headers = [h.strip().lower() for h in rows[header_idx]]
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)

        for raw_row in rows[header_idx + 1 :]:
            if not any(cell.strip() for cell in raw_row):
                continue
            row = {
                headers[i]: raw_row[i].strip() if i < len(raw_row) else ""
                for i in range(len(headers))
                if headers[i]
            }
            if self._is_summary_row(row):
                continue

            pay_raw = self._get_field(row, "check_date") or row.get("date") or ""
            if not pay_raw:
                continue
            pay_date = self._parse_csv_date(pay_raw, context="contractor payment")
            grouped[pay_date].append(row)

        if not grouped:
            raise GustoParseError(f"No contractor payment rows found in Gusto CSV: {path}")

        runs: list[GustoPayrollRun] = []
        for pay_date, group_rows in sorted(grouped.items()):
            total_paid = ZERO
            for row in group_rows:
                amt = self._first_money(row, _CONTRACTOR_AMOUNT_COLS)
                total_paid += amt
            fp = self._run_fingerprint(
                check_date=pay_date,
                pp_start=pay_date,
                pp_end=pay_date,
                run_type="contractor",
                gross=total_paid,
                emp_tax=ZERO,
                er_tax=ZERO,
                net_pay=total_paid,
            )
            runs.append(
                GustoPayrollRun(
                    fingerprint=fp,
                    check_date=pay_date,
                    pay_period_start=pay_date,
                    pay_period_end=pay_date,
                    run_type="contractor",
                    gross_wages=total_paid,
                    reimbursements=ZERO,
                    employee_taxes=ZERO,
                    employer_taxes=ZERO,
                    employee_benefits=ZERO,
                    employer_benefits=ZERO,
                    net_pay=total_paid,
                    tax_breakdown={},
                    employee_count=len(group_rows),
                    raw={"rows": group_rows},
                )
            )
        return runs

    def _parse_payroll_journal_sections(
        self, rows: list[list[str]], default_run_type: str = "regular"
    ) -> list[GustoPayrollRun]:
        """Parse Gusto's multi-section Payroll Journal CSV export."""
        runs: list[GustoPayrollRun] = []
        section_title = ""
        check_date: str | None = None
        pp_start: str | None = None
        pp_end: str | None = None
        headers: list[str] | None = None
        section_rows: list[dict[str, str]] = []
        seen_metadata = False
        in_employee_earnings = False

        header_markers = self._candidates_for("gross_wages") | self._candidates_for("net_pay")

        def reset_section() -> None:
            nonlocal section_title, check_date, pp_start, pp_end, headers
            nonlocal section_rows, seen_metadata
            section_title = ""
            check_date = None
            pp_start = None
            pp_end = None
            headers = None
            section_rows = []
            seen_metadata = False

        def flush_section() -> None:
            has_banner = in_employee_earnings and bool(section_title)
            if seen_metadata or headers is not None or has_banner:
                if not check_date:
                    raise GustoParseError(
                        f"Gusto payroll section {section_title!r} is missing a valid Pay day"
                    )
                if headers is None:
                    raise GustoParseError(
                        f"Gusto payroll section {section_title!r} ({check_date}) "
                        "is missing a payroll table header"
                    )
                if not section_rows:
                    raise GustoParseError(
                        f"Gusto payroll section {section_title!r} ({check_date}) "
                        "contains zero employee rows"
                    )
                title_lower = section_title.lower()
                if default_run_type == "regular" and (
                    "off-cycle" in title_lower or "off cycle" in title_lower
                ):
                    effective_run_type = "off_cycle"
                else:
                    effective_run_type = default_run_type
                runs.append(
                    self._aggregate_payroll_group(
                        check_date=check_date,
                        pp_start=pp_start or check_date,
                        pp_end=pp_end or check_date,
                        run_type=effective_run_type,
                        rows=section_rows,
                        section_title=section_title,
                    )
                )
            reset_section()

        for raw_row in rows:
            non_empty = [cell.strip() for cell in raw_row if cell.strip()]
            if not non_empty:
                flush_section()
                continue

            if headers is not None:
                row_dict = {
                    headers[i]: raw_row[i].strip() if i < len(raw_row) else ""
                    for i in range(len(headers))
                    if headers[i]
                }
                if not self._is_summary_row(row_dict):
                    section_rows.append(row_dict)
                continue

            if (meta := self._extract_section_metadata(non_empty, header_markers)) is not None:
                meta_key, meta_val = meta
                seen_metadata = True
                if meta_key in _SECTION_PERIOD_KEYS:
                    pp_start, pp_end = self._parse_pay_period(meta_val)
                else:
                    if not meta_val:
                        raise GustoParseError(
                            f"Gusto payroll section {section_title!r} has empty {meta_key!r}"
                        )
                    check_date = self._parse_csv_date(meta_val, context=meta_key)
                continue

            lowered_cells = [cell.strip().lower() for cell in raw_row]
            if len(non_empty) > 1 and any(col in header_markers for col in lowered_cells if col):
                if seen_metadata or in_employee_earnings:
                    if check_date is None:
                        raise GustoParseError(
                            f"Gusto payroll section {section_title!r} is missing a valid Pay day"
                        )
                    self._validate_payroll_headers(
                        lowered_cells,
                        context=f"section {section_title!r} ({check_date})",
                        require_date=False,
                    )
                    headers = lowered_cells
                continue

            if check_date is None and not seen_metadata and len(non_empty) == 1:
                candidate = non_empty[0]
                cand_lower = candidate.lower()
                if cand_lower == "employee earnings":
                    in_employee_earnings = True
                elif cand_lower == "payroll journal summary by employee":
                    in_employee_earnings = False
                elif cand_lower not in _IGNORED_JOURNAL_TITLES:
                    section_title = candidate

        flush_section()
        if in_employee_earnings and not runs:
            raise GustoParseError(
                "Gusto Payroll Journal Employee Earnings section produced zero payroll runs"
            )
        return sorted(
            runs,
            key=lambda r: (r.check_date, r.pay_period_start, r.pay_period_end, r.fingerprint),
        )

    def _aggregate_payroll_group(
        self,
        check_date: str,
        pp_start: str,
        pp_end: str,
        run_type: str,
        rows: list[dict[str, str]],
        *,
        section_title: str = "",
    ) -> GustoPayrollRun:
        gross_wages = ZERO
        reimbursements = ZERO
        employee_taxes = ZERO
        employer_taxes = ZERO
        employee_benefits = ZERO
        employer_benefits = ZERO
        imputed_benefits = ZERO
        s_corp_employer_benefits = ZERO
        raw_net_pay = ZERO
        reported_employer_cost = ZERO
        has_employer_cost = False
        tax_breakdown: dict[str, Money] = defaultdict(lambda: ZERO)

        for row in rows:
            gross_wages += self._get_money(row, "gross_wages")
            reimbursements += self._get_money(row, "reimbursements")
            ee_ben_summary = self._get_money_or_none(row, "employee_benefits")
            if ee_ben_summary is not None:
                employee_benefits += ee_ben_summary
            else:
                for col, cell_val in row.items():
                    if cell_val and col.endswith("(employee deduction)"):
                        employee_benefits += self._parse_csv_money(cell_val, field=col)

            er_ben_summary = self._get_money_or_none(row, "employer_benefits")
            if er_ben_summary is not None:
                employer_benefits += er_ben_summary
            else:
                for col, cell_val in row.items():
                    if cell_val and (
                        col.endswith("(company contribution)")
                        or col.endswith("(employer contribution)")
                    ):
                        amt = self._parse_csv_money(cell_val, field=col)
                        employer_benefits += amt
                        if "s-corp" in col or "s-corporation" in col:
                            s_corp_employer_benefits += amt

            for col, cell_val in row.items():
                if cell_val and col.endswith("(imputed)"):
                    imputed_benefits += self._parse_csv_money(cell_val, field=col)

            if (er_cost := self._get_money_or_none(row, "employer_cost")) is not None:
                has_employer_cost = True
                reported_employer_cost += er_cost

            raw_net_pay += self._get_money(row, "net_pay")

            ee_itemized = ZERO
            for key, aliases in _EE_TAX_ITEM_COLS:
                val = self._first_money(row, aliases)
                if val != ZERO:
                    tax_breakdown[key] += val
                    ee_itemized += val

            er_itemized = ZERO
            for key, aliases in _ER_TAX_ITEM_COLS:
                val = self._first_money(row, aliases)
                if val != ZERO:
                    tax_breakdown[key] += val
                    er_itemized += val

            ee_summary = self._get_money_or_none(row, "employee_taxes")
            er_summary = self._get_money_or_none(row, "employer_taxes")
            employee_taxes += ee_summary if ee_summary is not None else ee_itemized
            employer_taxes += er_summary if er_summary is not None else er_itemized

        # Normalize net_pay vs reimbursements depending on report convention:
        # Either raw_net_pay = gross - ee_tax - ee_ben (excluding reimbursements),
        # or raw_net_pay = gross + reimbursements - ee_tax - ee_ben (including reimbursements).
        expected_without_reimb = gross_wages - employee_taxes - employee_benefits
        expected_with_reimb = expected_without_reimb + reimbursements
        if (
            reimbursements > ZERO
            and abs(raw_net_pay - expected_with_reimb) <= _TOLERANCE
            and abs(raw_net_pay - expected_without_reimb) > _TOLERANCE
        ):
            net_pay = raw_net_pay - reimbursements
        else:
            net_pay = raw_net_pay

        non_cash_imputed = (
            abs(net_pay) <= _TOLERANCE
            and reimbursements == ZERO
            and employee_taxes == ZERO
            and employee_benefits == ZERO
            and employer_benefits > ZERO
            and abs(gross_wages - employer_benefits) <= _TOLERANCE
        )
        self._validate_run_accounting(
            check_date=check_date,
            gross_wages=gross_wages,
            reimbursements=reimbursements,
            employee_taxes=employee_taxes,
            employer_taxes=employer_taxes,
            employee_benefits=employee_benefits,
            employer_benefits=employer_benefits,
            imputed_benefits=imputed_benefits,
            s_corp_employer_benefits=s_corp_employer_benefits,
            net_pay=net_pay,
            non_cash_imputed=non_cash_imputed,
            has_employer_cost=has_employer_cost,
            reported_employer_cost=reported_employer_cost,
        )

        raw_payload: dict[str, Any] = {
            "rows": rows,
            "non_cash_imputed_benefits": non_cash_imputed,
        }
        if section_title:
            raw_payload["section_title"] = section_title

        fp = self._run_fingerprint(
            check_date=check_date,
            pp_start=pp_start,
            pp_end=pp_end,
            run_type=run_type,
            gross=gross_wages,
            emp_tax=employee_taxes,
            er_tax=employer_taxes,
            net_pay=net_pay,
        )
        return GustoPayrollRun(
            fingerprint=fp,
            check_date=check_date,
            pay_period_start=pp_start,
            pay_period_end=pp_end,
            run_type=run_type,
            gross_wages=gross_wages,
            reimbursements=reimbursements,
            employee_taxes=employee_taxes,
            employer_taxes=employer_taxes,
            employee_benefits=employee_benefits,
            employer_benefits=employer_benefits,
            net_pay=net_pay,
            tax_breakdown={k: format_money(v) for k, v in sorted(tax_breakdown.items())},
            employee_count=len(rows),
            raw=raw_payload,
        )

    @staticmethod
    def _validate_run_accounting(
        *,
        check_date: str,
        gross_wages: Money,
        reimbursements: Money,
        employee_taxes: Money,
        employer_taxes: Money,
        employee_benefits: Money,
        employer_benefits: Money,
        imputed_benefits: Money,
        s_corp_employer_benefits: Money,
        net_pay: Money,
        non_cash_imputed: bool,
        has_employer_cost: bool,
        reported_employer_cost: Money,
    ) -> None:
        if non_cash_imputed:
            if abs(net_pay) > _TOLERANCE:
                raise GustoParseError(
                    f"Gusto imputed run {check_date} has non-zero net pay: {format_money(net_pay)}"
                )
            if employer_benefits <= ZERO or abs(gross_wages - employer_benefits) > _TOLERANCE:
                raise GustoParseError(
                    f"Gusto imputed run {check_date} gross/employer_benefits mismatch: "
                    f"gross={format_money(gross_wages)}, "
                    f"employer_benefits={format_money(employer_benefits)}"
                )
            if imputed_benefits > ZERO and abs(imputed_benefits - employer_benefits) > _TOLERANCE:
                raise GustoParseError(
                    f"Gusto imputed run {check_date} imputed/employer_benefits mismatch: "
                    f"imputed={format_money(imputed_benefits)}, "
                    f"employer_benefits={format_money(employer_benefits)}"
                )
            if has_employer_cost:
                expected_imputed_cost = employer_benefits + employer_taxes
                if abs(reported_employer_cost - expected_imputed_cost) > _TOLERANCE:
                    raise GustoParseError(
                        f"Gusto imputed run {check_date} employer cost mismatch: "
                        f"reported={format_money(reported_employer_cost)}, "
                        f"expected={format_money(expected_imputed_cost)}"
                    )
            return

        if imputed_benefits > ZERO or (
            s_corp_employer_benefits > ZERO
            and abs(net_pay) <= _TOLERANCE
            and abs(gross_wages - employer_benefits) > _TOLERANCE
        ):
            raise GustoParseError(
                f"Gusto imputed run {check_date} failed consistency check: "
                f"gross={format_money(gross_wages)}, "
                f"employer_benefits={format_money(employer_benefits)}, "
                f"net_pay={format_money(net_pay)}"
            )

        expected_net = gross_wages - employee_taxes - employee_benefits
        if abs(net_pay - expected_net) > _TOLERANCE:
            raise GustoParseError(
                f"Gusto run {check_date} net pay mismatch: "
                f"net_pay={format_money(net_pay)}, expected={format_money(expected_net)}"
            )

        if has_employer_cost:
            expected_er_cost = gross_wages + employer_taxes + employer_benefits
            expected_er_cost_with_reimb = expected_er_cost + reimbursements
            matches_no_reimb = abs(reported_employer_cost - expected_er_cost) <= _TOLERANCE
            matches_with_reimb = (
                reimbursements > ZERO
                and abs(reported_employer_cost - expected_er_cost_with_reimb) <= _TOLERANCE
            )
            if not matches_no_reimb and not matches_with_reimb:
                raise GustoParseError(
                    f"Gusto run {check_date} employer cost mismatch: "
                    f"reported={format_money(reported_employer_cost)}, "
                    f"expected={format_money(expected_er_cost)}"
                )

    def _candidates_for(self, canonical: str) -> set[str]:
        candidates = set(_FIELD_ALIASES.get(canonical, ()))
        if override := self.column_overrides.get(canonical):
            candidates.add(override)
        return candidates

    def _has_column(self, lowered_cols: set[str] | list[str], canonical: str) -> bool:
        candidates = self._candidates_for(canonical)
        return any(col in candidates for col in lowered_cols if col)

    def _validate_payroll_headers(
        self,
        headers: list[str],
        *,
        context: str,
        require_date: bool,
    ) -> None:
        required = (
            ["check_date", "gross_wages", "net_pay"] if require_date else ["gross_wages", "net_pay"]
        )
        missing = [field for field in required if not self._has_column(headers, field)]
        if missing:
            raise GustoParseError(
                f"Gusto payroll CSV ({context}) missing required column(s): {', '.join(missing)}"
            )

    @staticmethod
    def _parse_csv_date(val: str, *, context: str) -> str:
        try:
            return parse_flexible_date(val)
        except ValueError as exc:
            raise GustoParseError(f"Invalid Gusto {context} date {val!r}: {exc}") from exc

    @staticmethod
    def _parse_csv_money(val: str, *, field: str) -> Money:
        try:
            return parse_money(val, ZERO)
        except ValueError as exc:
            raise GustoParseError(
                f"Invalid Gusto monetary value for {field} ({val!r}): {exc}"
            ) from exc

    @classmethod
    def _parse_pay_period(cls, period_text: str) -> tuple[str, str]:
        cleaned = period_text.strip()
        if not cleaned:
            raise GustoParseError("Empty Gusto pay period value")
        parts = _PERIOD_SEP_RE.split(cleaned, maxsplit=1)
        if len(parts) != 2 and cleaned.count("-") == 1:
            parts = cleaned.split("-", 1)
        if len(parts) != 2:
            raise GustoParseError(f"Unrecognized Gusto pay period format: {period_text!r}")
        pp_start = cls._parse_csv_date(parts[0], context="pay period start")
        pp_end = cls._parse_csv_date(parts[1], context="pay period end")
        return pp_start, pp_end

    @staticmethod
    def _extract_section_metadata(
        non_empty: list[str],
        header_markers: set[str],
    ) -> tuple[str, str] | None:
        if len(non_empty) == 1:
            cell = non_empty[0]
            if ":" in cell:
                key, val = cell.split(":", 1)
                key_norm = key.strip().lower()
                if key_norm in _SECTION_META_KEYS:
                    return key_norm, val.strip()
                return None
            key_norm = cell.strip().lower()
            if key_norm in _SECTION_META_KEYS:
                return key_norm, ""
            return None

        if len(non_empty) == 2:
            key_norm = non_empty[0].rstrip(":").strip().lower()
            val = non_empty[1].strip()
            if key_norm in _SECTION_META_KEYS and val.lower() not in header_markers:
                return key_norm, val
        return None

    def _find_header_row(self, rows: list[list[str]], contractor: bool = False) -> int | None:
        date_candidates = self._candidates_for("check_date")
        if contractor:
            date_candidates.add("date")
            amt_candidates = set(_CONTRACTOR_AMOUNT_COLS)
        else:
            amt_candidates = self._candidates_for("gross_wages") | self._candidates_for("net_pay")

        for idx, row in enumerate(rows[:30]):
            lowered = {cell.strip().lower() for cell in row if cell.strip()}
            has_date = any(col in date_candidates for col in lowered)
            has_amt = any(col in amt_candidates for col in lowered)
            if has_date and has_amt:
                return idx
        return None

    def _get_field(self, row: dict[str, str], canonical: str) -> str:
        override = self.column_overrides.get(canonical)
        if override and override in row and row[override]:
            return row[override]
        for alias in _FIELD_ALIASES.get(canonical, ()):
            if row.get(alias):
                return row[alias]
        return ""

    def _get_money(self, row: dict[str, str], canonical: str) -> Money:
        val = self._get_field(row, canonical)
        return self._parse_csv_money(val, field=canonical) if val else ZERO

    def _get_money_or_none(self, row: dict[str, str], canonical: str) -> Money | None:
        val = self._get_field(row, canonical)
        return self._parse_csv_money(val, field=canonical) if val else None

    @classmethod
    def _first_money(cls, row: dict[str, str], aliases: tuple[str, ...]) -> Money:
        for alias in aliases:
            if row.get(alias):
                return cls._parse_csv_money(row[alias], field=alias)
        return ZERO

    def _is_summary_row(self, row: dict[str, str]) -> bool:
        keys = [
            "last name",
            "first name",
            "employee",
            "employee name",
            "full name",
            "name",
            "contractor",
            "check date",
            "pay date",
            "payment date",
        ]
        if override := self.column_overrides.get("employee_name"):
            keys.append(override)
        for key in keys:
            val = row.get(key, "").strip().lower()
            if (
                val in _SUMMARY_EXACT
                or val.rstrip(":") in _SUMMARY_EXACT
                or val.startswith(_SUMMARY_PREFIX_STARTS)
            ):
                return True
        return False

    @staticmethod
    def _run_fingerprint(
        check_date: str,
        pp_start: str,
        pp_end: str,
        run_type: str,
        gross: Money,
        emp_tax: Money,
        er_tax: Money,
        net_pay: Money,
    ) -> str:
        payload = (
            f"gusto|{run_type}|{check_date}|{pp_start}|{pp_end}|"
            f"{format_money(gross)}|{format_money(emp_tax)}|"
            f"{format_money(er_tax)}|{format_money(net_pay)}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
