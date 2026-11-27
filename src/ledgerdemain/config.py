"""TOML configuration and categorization rules loader."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledgerdemain.money import Money, parse_money

DEFAULT_KEYCHAIN_SERVICE = "ledgerdemain"
_KEYCHAIN_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_PATTERN = re.compile(r"\$\{([^{}]+)\}")
_KEYCHAIN_PREFIX = "keychain:"
_KEYCHAIN_NOT_FOUND_RC = 44
_KEYCHAIN_TIMEOUT_SECONDS = 10


class ConfigError(ValueError):
    """Raised when configuration or rules files are invalid."""


def _validate_keychain_identifier(value: str, label: str) -> str:
    """Validate a macOS Keychain service or account identifier."""
    if not value or not _KEYCHAIN_IDENTIFIER_RE.fullmatch(value):
        raise ConfigError(
            f"Invalid macOS Keychain {label} {value!r}; allowed characters: [A-Za-z0-9._:/-]"
        )
    return value


def read_keychain_secret(service: str, account: str) -> str:
    """Read a secret from macOS Keychain via `/usr/bin/security`."""
    _validate_keychain_identifier(service, "service")
    _validate_keychain_identifier(account, "account")

    try:
        proc = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigError(
            f"Timed out after {_KEYCHAIN_TIMEOUT_SECONDS}s reading macOS Keychain item "
            f"service={service!r}, account={account!r} "
            "(Keychain may be locked or waiting for an interactive authorization prompt)"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"Failed to read macOS Keychain item service={service!r}, account={account!r}: "
            f"/usr/bin/security is unavailable ({exc})"
        ) from exc

    if proc.returncode == _KEYCHAIN_NOT_FOUND_RC:
        raise ConfigError(
            f"macOS Keychain item not found for service={service!r}, account={account!r}. "
            f"Store it with: security add-generic-password -U -s {service} -a {account} -w"
        )
    if proc.returncode != 0:
        detail = proc.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise ConfigError(
            f"Failed to read macOS Keychain item service={service!r}, account={account!r} "
            f"(security exited with code {proc.returncode}){suffix}"
        )

    secret = proc.stdout.rstrip("\r\n")
    if not secret:
        raise ConfigError(
            f"macOS Keychain item for service={service!r}, account={account!r} is empty"
        )
    return secret


def _resolve_keychain_service(raw_toml: dict[str, Any]) -> str:
    """Extract and validate literal general.keychain_service prior to Keychain lookups."""
    gen_raw = raw_toml.get("general", {})
    if not isinstance(gen_raw, dict):
        raise ConfigError("[general] must be a TOML table")
    raw_service = gen_raw.get("keychain_service", DEFAULT_KEYCHAIN_SERVICE)
    if not isinstance(raw_service, str):
        raise ConfigError("general.keychain_service must be a string")
    return _validate_keychain_identifier(raw_service.strip(), "service")


def _resolve_placeholders(
    value: str,
    env_vars: Mapping[str, str],
    keychain_service: str,
    keychain_cache: dict[tuple[str, str], str],
    *,
    resolve_keychain: bool,
) -> str:
    """Resolve ${keychain:NAME} and ${ENV_VAR} placeholders in a string."""

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if inner.startswith(_KEYCHAIN_PREFIX):
            account = _validate_keychain_identifier(inner[len(_KEYCHAIN_PREFIX) :], "account")
            if not resolve_keychain:
                return match.group(0)
            cache_key = (keychain_service, account)
            if cache_key not in keychain_cache:
                keychain_cache[cache_key] = read_keychain_secret(keychain_service, account)
            return keychain_cache[cache_key]
        if _ENV_NAME_RE.fullmatch(inner):
            return env_vars.get(inner, "")
        return match.group(0)

    return _PLACEHOLDER_PATTERN.sub(_replace, value)


def _resolve_value(
    val: Any,
    env_vars: Mapping[str, str],
    keychain_service: str,
    keychain_cache: dict[tuple[str, str], str],
    *,
    resolve_keychain: bool,
) -> Any:
    """Recursively resolve placeholders inside strings, dicts, and lists."""
    if isinstance(val, str):
        return _resolve_placeholders(
            val,
            env_vars,
            keychain_service,
            keychain_cache,
            resolve_keychain=resolve_keychain,
        )
    if isinstance(val, dict):
        return _resolve_dict_placeholders(
            val,
            env_vars,
            keychain_service,
            keychain_cache,
            resolve_keychain=resolve_keychain,
        )
    if isinstance(val, list):
        return [
            _resolve_value(
                item,
                env_vars,
                keychain_service,
                keychain_cache,
                resolve_keychain=resolve_keychain,
            )
            for item in val
        ]
    return val


def _resolve_dict_placeholders(
    data: dict[str, Any],
    env_vars: Mapping[str, str],
    keychain_service: str,
    keychain_cache: dict[tuple[str, str], str],
    *,
    resolve_keychain: bool,
) -> dict[str, Any]:
    """Recursively resolve placeholders in a dictionary."""
    return {
        key: _resolve_value(
            val,
            env_vars,
            keychain_service,
            keychain_cache,
            resolve_keychain=resolve_keychain,
        )
        for key, val in data.items()
    }


@dataclass(slots=True)
class GeneralConfig:
    """General storage and workspace settings."""

    db_path: Path
    inbox_dir: Path
    rules_path: Path
    currency: str = "USD"
    keychain_service: str = DEFAULT_KEYCHAIN_SERVICE


@dataclass(slots=True)
class AccountingConfig:
    """Accounting policy and reconciliation window settings."""

    deposit_match_window_days: int = 7
    payroll_match_window_days: int = 5
    transfer_match_window_days: int = 5
    auto_confirm_threshold: float = 0.90


@dataclass(slots=True)
class WaveConfig:
    """Wave GraphQL API credentials and defaults."""

    endpoint: str = "https://gql.waveapps.com/graphql/public"
    business_id: str = ""
    access_token: str = ""


@dataclass(slots=True)
class HarvestConfig:
    """Harvest REST API v2 credentials."""

    base_url: str = "https://api.harvestapp.com/v2"
    account_id: str = ""
    access_token: str = ""
    user_agent: str = "Ledgerdemain (accounting@example.com)"


@dataclass(slots=True)
class GrasshopperAccountConfig:
    """Configuration for a single Grasshopper Bank deposit account."""

    account_key: str
    account_id: str = ""


@dataclass(slots=True)
class GrasshopperConfig:
    """Grasshopper Bank Read-Only API credentials and configured deposit accounts."""

    base_url: str = "https://api.grasshopper.bank/v1"
    account_id: str = ""
    access_token: str = ""
    header_secret: str = ""
    header_secret_name: str = "X-API-Secret"
    accounts: list[GrasshopperAccountConfig] = field(default_factory=list)

    @property
    def configured_accounts(self) -> list[GrasshopperAccountConfig]:
        """Return configured Grasshopper accounts, falling back to legacy single account_id."""
        if self.accounts:
            return self.accounts
        if self.account_id:
            return [
                GrasshopperAccountConfig(
                    account_key="grasshopper_checking", account_id=self.account_id
                )
            ]
        return []


@dataclass(slots=True)
class PatternsConfig:
    """Compiled regex patterns used to classify payroll, interest, and CC payment transfers."""

    gusto_payroll: list[re.Pattern[str]] = field(default_factory=list)
    gusto_fee: list[re.Pattern[str]] = field(default_factory=list)
    grasshopper_cc_payment: list[re.Pattern[str]] = field(default_factory=list)
    capital_one_payment: list[re.Pattern[str]] = field(default_factory=list)
    bank_interest: list[re.Pattern[str]] = field(default_factory=list)


@dataclass(slots=True)
class AppConfig:
    """Root application configuration."""

    config_dir: Path
    general: GeneralConfig
    accounting: AccountingConfig
    wave: WaveConfig
    harvest: HarvestConfig
    grasshopper: GrasshopperConfig
    accounts: dict[str, str]
    patterns: PatternsConfig
    gusto_column_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CategorizationRule:
    """Single rule for categorizing bank or credit card transactions."""

    name: str
    priority: int
    account_key: str
    description_pattern: re.Pattern[str]
    source: str | None = None
    direction: str | None = None
    min_amount: Money | None = None
    max_amount: Money | None = None
    notes: str = ""


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else (base_dir / path)


def _compile_patterns(raw_list: list[str], fallback: list[str]) -> list[re.Pattern[str]]:
    items = raw_list if raw_list else fallback
    return [re.compile(pat) for pat in items]


def load_config(path: Path, *, resolve_keychain: bool = True) -> AppConfig:
    """Load and validate an AppConfig from a TOML file.

    Resolves explicit ``${keychain:NAME}`` placeholders via macOS Keychain (when
    ``resolve_keychain=True``) and ``${ENV_VAR}`` placeholders strictly from ``os.environ``.
    """
    config_path = path.resolve()
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    config_dir = config_path.parent
    with config_path.open("rb") as fh:
        raw_toml = tomllib.load(fh)
    env_vars = os.environ
    keychain_service = _resolve_keychain_service(raw_toml)
    keychain_cache: dict[tuple[str, str], str] = {}
    raw = _resolve_dict_placeholders(
        raw_toml,
        env_vars,
        keychain_service,
        keychain_cache,
        resolve_keychain=resolve_keychain,
    )
    gen_raw = raw.get("general", {})
    general = GeneralConfig(
        db_path=_resolve_path(config_dir, str(gen_raw.get("db_path", "data/ledgerdemain.db"))),
        inbox_dir=_resolve_path(config_dir, str(gen_raw.get("inbox_dir", "inbox"))),
        rules_path=_resolve_path(config_dir, str(gen_raw.get("rules_path", "rules.toml"))),
        currency=str(gen_raw.get("currency", "USD")),
        keychain_service=keychain_service,
    )

    acc_raw = raw.get("accounting", {})
    accounting = AccountingConfig(
        deposit_match_window_days=int(acc_raw.get("deposit_match_window_days", 7)),
        payroll_match_window_days=int(acc_raw.get("payroll_match_window_days", 5)),
        transfer_match_window_days=int(acc_raw.get("transfer_match_window_days", 5)),
        auto_confirm_threshold=float(acc_raw.get("auto_confirm_threshold", 0.90)),
    )

    wave_raw = raw.get("wave", {})
    wave = WaveConfig(
        endpoint=str(wave_raw.get("endpoint", "https://gql.waveapps.com/graphql/public")),
        business_id=str(wave_raw.get("business_id", "")),
        access_token=str(wave_raw.get("access_token", "")),
    )

    harvest_raw = raw.get("harvest", {})
    harvest = HarvestConfig(
        base_url=str(harvest_raw.get("base_url", "https://api.harvestapp.com/v2")).rstrip("/"),
        account_id=str(harvest_raw.get("account_id", "")),
        access_token=str(harvest_raw.get("access_token", "")),
        user_agent=str(harvest_raw.get("user_agent", "Ledgerdemain (accounting@example.com)")),
    )

    gh_raw = raw.get("grasshopper", {})
    gh_accounts: list[GrasshopperAccountConfig] = []
    for item in gh_raw.get("accounts", []):
        if isinstance(item, dict) and item.get("account_key"):
            gh_accounts.append(
                GrasshopperAccountConfig(
                    account_key=str(item["account_key"]).strip(),
                    account_id=str(item.get("account_id", "")).strip(),
                )
            )
    grasshopper = GrasshopperConfig(
        base_url=str(gh_raw.get("base_url", "https://api.grasshopper.bank/v1")).rstrip("/"),
        account_id=str(gh_raw.get("account_id", "")),
        access_token=str(gh_raw.get("access_token", "")),
        header_secret=str(gh_raw.get("header_secret", "")),
        header_secret_name=str(gh_raw.get("header_secret_name", "X-API-Secret")),
        accounts=gh_accounts,
    )

    accounts = {str(k): str(v) for k, v in raw.get("accounts", {}).items()}

    pat_raw = raw.get("patterns", {})
    patterns = PatternsConfig(
        gusto_payroll=_compile_patterns(list(pat_raw.get("gusto_payroll", [])), [r"(?i)\bgusto\b"]),
        gusto_fee=_compile_patterns(
            list(pat_raw.get("gusto_fee", [])), [r"(?i)gusto\s*(fee|sub|invoice|monthly)"]
        ),
        grasshopper_cc_payment=_compile_patterns(
            list(pat_raw.get("grasshopper_cc_payment", [])),
            [r"(?i)capital\s*one", r"(?i)cap\s*one", r"(?i)crcardpmt"],
        ),
        capital_one_payment=_compile_patterns(
            list(pat_raw.get("capital_one_payment", [])),
            [
                r"(?i)capital\s*one\s*(autopay|online|mobile)\s*pymt",
                r"(?i)payment\s*-?\s*thank\s*you",
                r"(?i)^autopay\b",
            ],
        ),
        bank_interest=_compile_patterns(
            list(pat_raw.get("bank_interest", [])),
            [r"(?i)\binterest\b"],
        ),
    )

    gusto_section = raw.get("gusto", {})
    overrides_raw = gusto_section.get("column_overrides", {})
    gusto_overrides = {str(k): str(v) for k, v in overrides_raw.items()}

    return AppConfig(
        config_dir=config_dir,
        general=general,
        accounting=accounting,
        wave=wave,
        harvest=harvest,
        grasshopper=grasshopper,
        accounts=accounts,
        patterns=patterns,
        gusto_column_overrides=gusto_overrides,
    )


def load_rules(path: Path) -> list[CategorizationRule]:
    """Load and compile categorization rules ordered by ascending priority."""
    if not path.exists():
        return []

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    rules: list[CategorizationRule] = []
    for idx, item in enumerate(raw.get("rules", [])):
        name = str(item.get("name") or f"Rule {idx + 1}")
        account_key = str(item.get("account_key", "")).strip()
        desc_regex = str(item.get("description_regex", "")).strip()
        if not account_key or not desc_regex:
            raise ConfigError(f"Rule {name!r} requires both 'account_key' and 'description_regex'")

        if "source_category_regex" in item:
            raise ConfigError(
                f"Rule {name!r} uses 'source_category_regex', which is no longer supported: "
                "Capital One OFX carries no merchant category. Remove the key (and the rule if "
                "it only matched on category)."
            )
        min_amt = item.get("min_amount")
        max_amt = item.get("max_amount")
        direction = item.get("direction")

        rules.append(
            CategorizationRule(
                name=name,
                priority=int(item.get("priority", (idx + 1) * 10)),
                account_key=account_key,
                description_pattern=re.compile(desc_regex),
                source=str(item["source"]) if item.get("source") else None,
                direction=str(direction).upper() if direction else None,
                min_amount=parse_money(min_amt) if min_amt is not None else None,
                max_amount=parse_money(max_amt) if max_amt is not None else None,
                notes=str(item.get("notes", "")),
            )
        )

    rules.sort(key=lambda r: (r.priority, r.name))
    return rules
