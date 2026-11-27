"""Tests for explicit ${keychain:NAME} secret resolution and config validation."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

import ledgerdemain.config as config_mod
from ledgerdemain import cli
from ledgerdemain.config import ConfigError, load_config, read_keychain_secret


def _fake_security_factory(
    secrets: dict[tuple[str, str], str],
    calls: list[tuple[list[str], dict[str, Any]]],
) -> Any:
    """Return a fake `subprocess.run` stub that resolves secrets from an in-memory map."""

    def _fake_run(
        cmd: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        assert cmd[:3] == ["/usr/bin/security", "find-generic-password", "-s"]
        assert cmd[4] == "-a"
        assert cmd[6] == "-w"
        service = cmd[3]
        account = cmd[5]
        if (service, account) not in secrets:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=44,
                stdout="",
                stderr=(
                    "security: SecKeychainSearchCopyNext: "
                    "The specified item could not be found in the keychain.\n"
                ),
            )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=secrets[(service, account)],
            stderr="",
        )

    return _fake_run


def test_keychain_placeholders_resolved_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[general]
db_path = "data/ledgerdemain.db"

[wave]
business_id = "dup:${keychain:WAVE_ACCESS_TOKEN}"
access_token = "${keychain:WAVE_ACCESS_TOKEN}"

[harvest]
account_id = "999999"
access_token = "${keychain:HARVEST_ACCESS_TOKEN}"

[grasshopper]
access_token = "Bearer ${keychain:GRASSHOPPER_ACCESS_TOKEN}"
header_secret = "${keychain:GRASSHOPPER_HEADER_SECRET}"

[[grasshopper.accounts]]
account_key = "grasshopper_savings"
account_id = "${keychain:GH_SAV_ID}"
""",
        encoding="utf-8",
    )

    calls: list[tuple[list[str], dict[str, Any]]] = []
    secrets = {
        ("ledgerdemain", "WAVE_ACCESS_TOKEN"): "wave-tok\r\n",
        ("ledgerdemain", "HARVEST_ACCESS_TOKEN"): "harv-tok\n",
        ("ledgerdemain", "GRASSHOPPER_ACCESS_TOKEN"): "gh-tok\n",
        ("ledgerdemain", "GRASSHOPPER_HEADER_SECRET"): "gh-sec\r\n",
        ("ledgerdemain", "GH_SAV_ID"): "sav-acct-123\n",
    }
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory(secrets, calls),
    )

    cfg = load_config(cfg_path)
    assert cfg.general.keychain_service == "ledgerdemain"
    assert cfg.wave.access_token == "wave-tok"
    assert cfg.wave.business_id == "dup:wave-tok"
    assert cfg.harvest.access_token == "harv-tok"
    assert cfg.grasshopper.access_token == "Bearer gh-tok"
    assert cfg.grasshopper.header_secret == "gh-sec"
    assert cfg.grasshopper.configured_accounts[0].account_id == "sav-acct-123"

    # Duplicate ${keychain:WAVE_ACCESS_TOKEN} reference only invoked `security` once.
    assert len(calls) == 5
    for cmd, kwargs in calls:
        assert cmd[:4] == ["/usr/bin/security", "find-generic-password", "-s", "ledgerdemain"]
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "check": False,
        }


def test_custom_keychain_service_literal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    secrets = {("ledgerdemain:prod", "WAVE_ACCESS_TOKEN"): "prod-wave\n"}
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory(secrets, calls),
    )

    literal_cfg = tmp_path / "literal.toml"
    literal_cfg.write_text(
        """[general]
keychain_service = "ledgerdemain:prod"

[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )
    cfg = load_config(literal_cfg)
    assert cfg.general.keychain_service == "ledgerdemain:prod"
    assert cfg.wave.access_token == "prod-wave"
    assert len(calls) == 1
    assert calls[0][0][3] == "ledgerdemain:prod"


def test_default_keychain_service_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[general]
db_path = "data/ledgerdemain.db"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("security should not run without keychain refs"),
    )

    cfg = load_config(cfg_path)
    assert cfg.general.keychain_service == "ledgerdemain"


def test_keychain_item_not_found_raises_actionable_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[general]
keychain_service = "ledgerdemain:staging"

[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory({}, calls),
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path)

    msg = str(exc_info.value)
    assert "ledgerdemain:staging" in msg
    assert "WAVE_ACCESS_TOKEN" in msg
    assert "security add-generic-password -U -s ledgerdemain:staging -a WAVE_ACCESS_TOKEN -w" in msg


def test_keychain_other_error_reports_stderr_without_leaking_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )

    def _locked_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=36,
            stdout="SUPER-SECRET-NEVER-LEAK",
            stderr="User interaction is not allowed.\n",
        )

    monkeypatch.setattr("ledgerdemain.config.subprocess.run", _locked_run)

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path)

    msg = str(exc_info.value)
    assert "36" in msg
    assert "User interaction is not allowed." in msg
    assert "SUPER-SECRET-NEVER-LEAK" not in msg


def test_keychain_missing_security_binary_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )

    def _missing_binary(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("/usr/bin/security")

    monkeypatch.setattr("ledgerdemain.config.subprocess.run", _missing_binary)

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path)

    msg = str(exc_info.value)
    assert "/usr/bin/security" in msg
    assert "ledgerdemain" in msg
    assert "WAVE_ACCESS_TOKEN" in msg


def test_keychain_timeout_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )

    def _timed_out(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

    monkeypatch.setattr("ledgerdemain.config.subprocess.run", _timed_out)

    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_path)

    msg = str(exc_info.value)
    assert "Timed out" in msg
    assert "10s" in msg
    assert "ledgerdemain" in msg
    assert "WAVE_ACCESS_TOKEN" in msg


def test_keychain_empty_secret_raises_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory({("ledgerdemain", "WAVE_ACCESS_TOKEN"): "\n"}, calls),
    )

    with pytest.raises(ConfigError, match="empty"):
        load_config(cfg_path)


@pytest.mark.parametrize(
    "placeholder",
    ["${keychain:}", "${keychain:bad name}", "${keychain:foo$bar}"],
)
def test_malformed_keychain_placeholders_fail_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        f"""[wave]
access_token = "{placeholder}"
""",
        encoding="utf-8",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory({}, calls),
    )

    with pytest.raises(ConfigError):
        load_config(cfg_path)
    assert calls == []


@pytest.mark.parametrize(
    "service_toml",
    ['""', '"   "', '"bad service"', '"${keychain:SVC}"', '"${TEST_KC_SVC}"', "123"],
)
def test_invalid_keychain_service_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service_toml: str
) -> None:
    monkeypatch.setenv("TEST_KC_SVC", "ledgerdemain:prod")
    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        f"""[general]
keychain_service = {service_toml}

[wave]
access_token = "${{keychain:WAVE_ACCESS_TOKEN}}"
""",
        encoding="utf-8",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory({}, calls),
    )

    with pytest.raises(ConfigError):
        load_config(cfg_path)
    assert calls == []


@pytest.mark.parametrize(
    ("service", "account"),
    [
        ("", "WAVE_ACCESS_TOKEN"),
        ("bad svc", "WAVE_ACCESS_TOKEN"),
        ("ledgerdemain", ""),
        ("ledgerdemain", "bad acct"),
    ],
)
def test_read_keychain_secret_rejects_invalid_identifiers(
    monkeypatch: pytest.MonkeyPatch, service: str, account: str
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        _fake_security_factory({}, calls),
    )

    with pytest.raises(ConfigError):
        read_keychain_secret(service, account)
    assert calls == []


def test_non_env_placeholder_preserved_and_load_dotenv_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert not hasattr(config_mod, "load_dotenv")
    monkeypatch.setenv("TEST_ENV_BIZ", "biz-123")
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("security should not run without keychain refs"),
    )

    cfg_path = tmp_path / "ledgerdemain.toml"
    cfg_path.write_text(
        """[wave]
business_id = "${TEST_ENV_BIZ}"
default_product_name = "Literal ${foo-bar}"
""",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.wave.business_id == "biz-123"
    assert cfg.wave.default_product_name == "Literal ${foo-bar}"


def test_example_config_and_cli_fallback_use_keychain_placeholders() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config" / "ledgerdemain.example.toml"
    parsed_example = tomllib.loads(example_path.read_text(encoding="utf-8"))
    parsed_fallback = tomllib.loads(cli._DEFAULT_CONFIG_FALLBACK)

    for parsed in (parsed_example, parsed_fallback):
        assert parsed["general"]["keychain_service"] == "ledgerdemain"
        assert parsed["wave"]["access_token"] == "${keychain:WAVE_ACCESS_TOKEN}"
        assert parsed["harvest"]["access_token"] == "${keychain:HARVEST_ACCESS_TOKEN}"
        assert parsed["grasshopper"]["access_token"] == "${keychain:GRASSHOPPER_ACCESS_TOKEN}"
        assert parsed["grasshopper"]["header_secret"] == "${keychain:GRASSHOPPER_HEADER_SECRET}"


def test_load_config_resolve_keychain_false_validates_syntax_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("security must not run when resolve_keychain=False"),
    )

    valid_cfg = tmp_path / "valid.toml"
    valid_cfg.write_text(
        """[general]
keychain_service = "ledgerdemain"

[wave]
access_token = "${keychain:WAVE_ACCESS_TOKEN}"
""",
        encoding="utf-8",
    )
    cfg = load_config(valid_cfg, resolve_keychain=False)
    assert cfg.general.keychain_service == "ledgerdemain"
    assert cfg.wave.access_token == "${keychain:WAVE_ACCESS_TOKEN}"

    for bad_toml in (
        '[wave]\naccess_token = "${keychain:}"\n',
        '[wave]\naccess_token = "${keychain:bad name}"\n',
        '[general]\nkeychain_service = "bad service"\n',
    ):
        bad_cfg = tmp_path / "bad.toml"
        bad_cfg.write_text(bad_toml, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(bad_cfg, resolve_keychain=False)


def test_cli_init_succeeds_without_invoking_security(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "ledgerdemain.config.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("init must not invoke /usr/bin/security"),
    )

    rc = cli.main(["init", "--dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "Initialized Ledgerdemain workspace" in captured.out

    generated = (tmp_path / "ledgerdemain.toml").read_text(encoding="utf-8")
    assert 'keychain_service = "ledgerdemain"' in generated
    assert 'access_token = "${keychain:WAVE_ACCESS_TOKEN}"' in generated
