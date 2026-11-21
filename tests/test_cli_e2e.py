"""End-to-end CLI workflow tests across all 5 platforms."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

from ledgerdemain.cli import main
from tests.fixtures import (
    SAMPLE_CAPITAL_ONE_CSV,
    SAMPLE_CAPITAL_ONE_PDF_LINES,
    SAMPLE_GRASSHOPPER_CSV,
    SAMPLE_GUSTO_PAYROLL_CSV,
    SAMPLE_HARVEST_CSV,
    create_synthetic_pdf,
    write_test_workspace_config,
)


def _run_cli(cfg_path: Path, argv: list[str]) -> tuple[int, str, str]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    full_argv = ["--config", str(cfg_path), *argv]
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = main(full_argv)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def test_full_offline_cli_workflow(tmp_path: Path) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"

    # 1. init
    code, out, err = _run_cli(cfg_path, ["init", "--dir", str(tmp_path)])
    assert code == 0, err
    assert "Initialized Ledgerdemain" in out

    # Overwrite with deterministic offline test config/rules
    write_test_workspace_config(tmp_path, include_api_creds=False)

    # Drop CSVs & PDFs into inbox
    (tmp_path / "inbox/harvest/invoices.csv").write_text(SAMPLE_HARVEST_CSV, encoding="utf-8")
    (tmp_path / "inbox/grasshopper/checking.csv").write_text(
        SAMPLE_GRASSHOPPER_CSV, encoding="utf-8"
    )
    (tmp_path / "inbox/capital_one/jan.csv").write_text(SAMPLE_CAPITAL_ONE_CSV, encoding="utf-8")
    create_synthetic_pdf(
        SAMPLE_CAPITAL_ONE_PDF_LINES,
        tmp_path / "inbox/capital_one/dec_jan_statement.pdf",
    )
    (tmp_path / "inbox/gusto/payroll_jan.csv").write_text(
        SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8"
    )

    # 2. ingest all
    code, _, err = _run_cli(cfg_path, ["ingest", "all"])
    assert code == 0, err

    # 3. reconcile
    code, out, err = _run_cli(cfg_path, ["reconcile", "--confirm-high-confidence"])
    assert code == 0, err
    assert "1 CC transfer(s)" in out
    assert "2 Gusto payroll match(es)" in out

    # 4. plan
    code, _, err = _run_cli(cfg_path, ["plan"])
    assert code == 0, err

    # 5. review --json
    code, out, err = _run_cli(cfg_path, ["review", "--status", "needs_review", "--json"])
    assert code == 0, err
    review_data: dict[str, Any] = json.loads(out)
    needs_review = review_data["staged_transactions"]
    mystery = next(tx for tx in needs_review if "MYSTERY HARDWARE" in tx["description"])

    # 6. recategorize MYSTERY HARDWARE CO
    code, _, err = _run_cli(
        cfg_path,
        [
            "recategorize",
            "--tx",
            str(mystery["id"]),
            "--account",
            "office_supplies",
            "--note",
            "Monitor stand",
        ],
    )
    assert code == 0, err

    # 7. approve --all-staged
    code, _, err = _run_cli(cfg_path, ["approve", "--all-staged"])
    assert code == 0, err

    # 8. push --dry-run
    code, out, err = _run_cli(cfg_path, ["push", "--dry-run"])
    assert code == 0, err
    dry_json: dict[str, Any] = json.loads(out)
    assert dry_json["dry_run"] is True
    assert dry_json["invoices_ready"] == 3
    assert dry_json["transactions_ready"] == 11
    for p in dry_json["payloads"]:
        if p["operation"] == "moneyTransactionCreate":
            assert "status" not in p["input"]
        elif p["operation"] == "invoiceCreate":
            for item in p["input"]["items"]:
                assert "unitPrice" in item

    # 9. report --format json
    code, out, err = _run_cli(cfg_path, ["report", "--format", "json"])
    assert code == 0, err
    rep: dict[str, Any] = json.loads(out)
    assert rep["gusto_payroll"]["payroll_taxes_payable_net"] == "0.00"
    assert rep["gusto_payroll"]["payroll_benefits_payable_net"] == "0.00"
    assert len(rep["wave_manual_checklist"]["transfers"]) == 2
