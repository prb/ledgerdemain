"""End-to-end CLI workflow tests across all 5 platforms."""

from __future__ import annotations

import contextlib
import io
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from ledgerdemain.cli import main
from ledgerdemain.http_client import HttpClient, HttpResponse
from tests.fixtures import (
    E2E_CAPITAL_ONE_JAN_OFX,
    E2E_CAPITAL_ONE_STATEMENT_OFX,
    E2E_GRASSHOPPER_ACCOUNT_ID,
    E2E_GRASSHOPPER_TRANSACTIONS,
    E2E_HARVEST_CLIENTS,
    E2E_HARVEST_INVOICES,
    E2E_HARVEST_PAYMENTS,
    SAMPLE_GUSTO_PAYROLL_CSV,
    RoutingMockTransport,
    write_test_workspace_config,
)


def _run_cli(cfg_path: Path, argv: list[str]) -> tuple[int, str, str]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    full_argv = ["--config", str(cfg_path), *argv]
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = main(full_argv)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _mock_api_handler(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    """Serve Harvest and Grasshopper checking from fixtures; 404 on any other request."""
    del method, headers, body
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    payload: dict[str, Any] | None = None
    if parsed.netloc == "api.harvestapp.com":
        payments = re.fullmatch(r"/v2/invoices/(\d+)/payments", path)
        if path == "/v2/clients":
            payload = E2E_HARVEST_CLIENTS
        elif path == "/v2/invoices":
            payload = E2E_HARVEST_INVOICES
        elif payments:
            payload = E2E_HARVEST_PAYMENTS.get(
                int(payments.group(1)), {"invoice_payments": [], "next_page": None}
            )
    elif parsed.netloc == "api.grasshopper.bank" and path == (
        f"/v1/accounts/{E2E_GRASSHOPPER_ACCOUNT_ID}/transactions"
    ):
        payload = E2E_GRASSHOPPER_TRANSACTIONS
    if payload is None:
        return HttpResponse(404, {}, b"{}")
    return HttpResponse(200, {}, json.dumps(payload).encode("utf-8"))


def _no_wave_requests(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> HttpResponse:
    raise AssertionError(f"Unexpected Wave request in an offline test: {method} {url}")


def test_full_offline_cli_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"

    # 1. init
    code, out, err = _run_cli(cfg_path, ["init", "--dir", str(tmp_path)])
    assert code == 0, err
    assert "Initialized Ledgerdemain" in out

    # Overwrite with deterministic test config/rules. API creds are set so `ingest all` pulls
    # Harvest through the mock transport below; no real network is used.
    write_test_workspace_config(tmp_path, include_api_creds=True)

    def mock_http_client(*_args: object, **_kwargs: object) -> HttpClient:
        return HttpClient(
            transport=RoutingMockTransport(_mock_api_handler), sleep_fn=lambda _: None
        )

    def no_wave_http_client(*_args: object, **_kwargs: object) -> HttpClient:
        return HttpClient(
            transport=RoutingMockTransport(_no_wave_requests), sleep_fn=lambda _: None
        )

    monkeypatch.setattr("ledgerdemain.adapters.harvest.HttpClient", mock_http_client)
    monkeypatch.setattr("ledgerdemain.adapters.grasshopper.HttpClient", mock_http_client)
    # The config also carries Wave credentials; make sure nothing in this test can reach Wave.
    monkeypatch.setattr("ledgerdemain.wave.client.HttpClient", no_wave_http_client)

    # Drop Capital One OFX and Gusto CSV files into the inbox (Harvest and Grasshopper come from
    # the mocked APIs)
    (tmp_path / "inbox/capital_one/jan.ofx").write_text(E2E_CAPITAL_ONE_JAN_OFX, encoding="utf-8")
    (tmp_path / "inbox/capital_one/dec_jan_statement.ofx").write_text(
        E2E_CAPITAL_ONE_STATEMENT_OFX, encoding="utf-8"
    )
    (tmp_path / "inbox/gusto/payroll_jan.csv").write_text(
        SAMPLE_GUSTO_PAYROLL_CSV, encoding="utf-8"
    )

    # 2. ingest all
    code, out, err = _run_cli(cfg_path, ["ingest", "all"])
    assert code == 0, err
    assert "Harvest API: 3 clients, 3 invoices, 2 payments." in out

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
    # The two BLUE BOTTLE COFFEE rows are not categorized: OFX carries no merchant category, so
    # the category-based fallback rules were removed (#35).
    assert dry_json["transactions_ready"] == 10
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
