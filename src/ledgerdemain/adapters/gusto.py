"""Gusto Payroll Journal and Contractor CSV adapter."""

from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledgerdemain.adapters.base import parse_flexible_date
from ledgerdemain.models import ZERO, GustoPayrollRun, fmt_decimal, to_decimal

_SUMMARY_PREFIXES = (
    "total",
    "totals",
    "company total",
    "grand total",
    "all employees",
    "payroll totals",
    "payroll total",
)

_IGNORED_JOURNAL_TITLES = frozenset(
    {"payroll journal", "payroll journal summary by employee", "employee earnings"}
)

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

        if journal_runs := self._parse_payroll_journal_sections(rows, default_run_type=run_type):
            return journal_runs

        header_idx = self._find_header_row(rows)
        if header_idx is None:
            return []

        headers = [h.strip().lower() for h in rows[header_idx]]
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
            check_date = parse_flexible_date(check_raw)
            pp_start_raw = self._get_field(row, "pay_period_start") or check_raw
            pp_end_raw = self._get_field(row, "pay_period_end") or check_raw
            pp_start = parse_flexible_date(pp_start_raw)
            pp_end = parse_flexible_date(pp_end_raw)
            grouped[(check_date, pp_start, pp_end)].append(row)

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

        header_idx = self._find_header_row(rows, contractor=True)
        if header_idx is None:
            return []

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
            pay_date = parse_flexible_date(pay_raw)
            grouped[pay_date].append(row)

        runs: list[GustoPayrollRun] = []
        for pay_date, group_rows in sorted(grouped.items()):
            total_paid = ZERO
            for row in group_rows:
                amt = self._first_decimal(row, _CONTRACTOR_AMOUNT_COLS)
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
        header_markers = set(_FIELD_ALIASES["gross_wages"]) | set(_FIELD_ALIASES["net_pay"])
        if override := self.column_overrides.get("gross_wages"):
            header_markers.add(override)
        if override := self.column_overrides.get("net_pay"):
            header_markers.add(override)

        def reset_section() -> None:
            nonlocal section_title, check_date, pp_start, pp_end, headers, section_rows
            section_title = ""
            check_date = None
            pp_start = None
            pp_end = None
            headers = None
            section_rows = []

        def flush_section() -> None:
            if check_date and section_rows:
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

            first = raw_row[0].strip().lower() if raw_row else ""
            if first == "pay period" and len(raw_row) >= 2 and raw_row[1].strip():
                period_text = raw_row[1].strip()
                if " - " in period_text:
                    start_str, end_str = period_text.split(" - ", 1)
                    pp_start = parse_flexible_date(start_str)
                    pp_end = parse_flexible_date(end_str)
                continue

            if first in {"pay day", "check date", "pay date"} and len(raw_row) >= 2:
                val = raw_row[1].strip()
                if val:
                    check_date = parse_flexible_date(val)
                continue

            if check_date is not None:
                lowered_cells = [cell.strip().lower() for cell in raw_row]
                if any(col in header_markers for col in lowered_cells if col):
                    headers = lowered_cells
                    continue

            if check_date is None and len(non_empty) == 1:
                candidate = non_empty[0]
                if candidate.lower() not in _IGNORED_JOURNAL_TITLES:
                    section_title = candidate

        flush_section()
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
        raw_net_pay = ZERO
        tax_breakdown: dict[str, Decimal] = defaultdict(lambda: ZERO)

        for row in rows:
            gross_wages += self._get_decimal(row, "gross_wages")
            reimbursements += self._get_decimal(row, "reimbursements")
            ee_ben_summary = self._get_decimal_or_none(row, "employee_benefits")
            if ee_ben_summary is not None:
                employee_benefits += ee_ben_summary
            else:
                for col, cell_val in row.items():
                    if cell_val and col.endswith("(employee deduction)"):
                        employee_benefits += to_decimal(cell_val, ZERO)

            er_ben_summary = self._get_decimal_or_none(row, "employer_benefits")
            if er_ben_summary is not None:
                employer_benefits += er_ben_summary
            else:
                for col, cell_val in row.items():
                    if cell_val and (
                        col.endswith("(company contribution)")
                        or col.endswith("(employer contribution)")
                    ):
                        employer_benefits += to_decimal(cell_val, ZERO)

            raw_net_pay += self._get_decimal(row, "net_pay")

            ee_itemized = ZERO
            for key, aliases in _EE_TAX_ITEM_COLS:
                val = self._first_decimal(row, aliases)
                if val != ZERO:
                    tax_breakdown[key] += val
                    ee_itemized += val

            er_itemized = ZERO
            for key, aliases in _ER_TAX_ITEM_COLS:
                val = self._first_decimal(row, aliases)
                if val != ZERO:
                    tax_breakdown[key] += val
                    er_itemized += val

            ee_summary = self._get_decimal_or_none(row, "employee_taxes")
            er_summary = self._get_decimal_or_none(row, "employer_taxes")
            employee_taxes += ee_summary if ee_summary is not None else ee_itemized
            employer_taxes += er_summary if er_summary is not None else er_itemized

        # Normalize net_pay vs reimbursements depending on report convention:
        # Either raw_net_pay = gross - ee_tax - ee_ben (excluding reimbursements),
        # or raw_net_pay = gross + reimbursements - ee_tax - ee_ben (including reimbursements).
        expected_without_reimb = gross_wages - employee_taxes - employee_benefits
        expected_with_reimb = expected_without_reimb + reimbursements
        if (
            reimbursements > ZERO
            and abs(raw_net_pay - expected_with_reimb) <= Decimal("0.02")
            and abs(raw_net_pay - expected_without_reimb) > Decimal("0.02")
        ):
            net_pay = raw_net_pay - reimbursements
        else:
            net_pay = raw_net_pay

        non_cash_imputed = (
            net_pay == ZERO
            and reimbursements == ZERO
            and employee_taxes == ZERO
            and employer_taxes == ZERO
            and employee_benefits == ZERO
            and employer_benefits > ZERO
            and abs(gross_wages - employer_benefits) <= Decimal("0.01")
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
            tax_breakdown={k: fmt_decimal(v) for k, v in sorted(tax_breakdown.items())},
            employee_count=len(rows),
            raw=raw_payload,
        )

    def _find_header_row(self, rows: list[list[str]], contractor: bool = False) -> int | None:
        date_candidates = set(_FIELD_ALIASES["check_date"])
        if override := self.column_overrides.get("check_date"):
            date_candidates.add(override)
        if contractor:
            date_candidates.add("date")

        for idx, row in enumerate(rows[:30]):
            lowered = {cell.strip().lower() for cell in row if cell.strip()}
            has_date = any(col in date_candidates for col in lowered)
            if contractor:
                has_amt = any(col in _CONTRACTOR_AMOUNT_COLS for col in lowered)
            else:
                has_amt = any(
                    col in set(_FIELD_ALIASES["gross_wages"]) | set(_FIELD_ALIASES["net_pay"])
                    for col in lowered
                )
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

    def _get_decimal(self, row: dict[str, str], canonical: str) -> Decimal:
        val = self._get_field(row, canonical)
        return to_decimal(val, ZERO) if val else ZERO

    def _get_decimal_or_none(self, row: dict[str, str], canonical: str) -> Decimal | None:
        val = self._get_field(row, canonical)
        return to_decimal(val, ZERO) if val else None

    @staticmethod
    def _first_decimal(row: dict[str, str], aliases: tuple[str, ...]) -> Decimal:
        for alias in aliases:
            if row.get(alias):
                return to_decimal(row[alias], ZERO)
        return ZERO

    def _is_summary_row(self, row: dict[str, str]) -> bool:
        for key in (
            "last name",
            "employee",
            "employee name",
            "full name",
            "name",
            "contractor",
            "check date",
            "pay date",
        ):
            val = row.get(key, "").strip().lower()
            if val in _SUMMARY_PREFIXES or val.startswith("total "):
                return True
        if override := self.column_overrides.get("employee_name"):
            val = row.get(override, "").strip().lower()
            if val in _SUMMARY_PREFIXES or val.startswith("total "):
                return True
        return False

    @staticmethod
    def _run_fingerprint(
        check_date: str,
        pp_start: str,
        pp_end: str,
        run_type: str,
        gross: Decimal,
        emp_tax: Decimal,
        er_tax: Decimal,
        net_pay: Decimal,
    ) -> str:
        payload = (
            f"gusto|{run_type}|{check_date}|{pp_start}|{pp_end}|"
            f"{fmt_decimal(gross)}|{fmt_decimal(emp_tax)}|"
            f"{fmt_decimal(er_tax)}|{fmt_decimal(net_pay)}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
