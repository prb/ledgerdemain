"""TOML configuration and categorization rules loader."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ledgerdemain.money import Money, parse_money

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Raised when configuration or rules files are invalid."""


def load_dotenv(env_path: Path) -> dict[str, str]:
    """Parse a .env file into a key-value dict without mutating os.environ."""
    if not env_path.exists():
        return {}
    env_map: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        raw_key, raw_val = line.split("=", 1)
        key = raw_key.strip()
        val = raw_val.strip()
        if not key:
            continue
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        env_map[key] = val
    return env_map


def _resolve_env(value: str, env_vars: dict[str, str]) -> str:
    """Replace ${ENV_VAR} tokens using resolved dotenv + process environment variables."""

    def _replace(match: re.Match[str]) -> str:
        return env_vars.get(match.group(1), "")

    return _ENV_PATTERN.sub(_replace, value)


def _resolve_dict_envs(data: dict[str, Any], env_vars: dict[str, str]) -> dict[str, Any]:
    """Recursively resolve environment variable placeholders in a dictionary."""
    resolved: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, str):
            resolved[key] = _resolve_env(val, env_vars)
        elif isinstance(val, dict):
            resolved[key] = _resolve_dict_envs(val, env_vars)
        elif isinstance(val, list):
            resolved[key] = [
                _resolve_dict_envs(item, env_vars)
                if isinstance(item, dict)
                else (_resolve_env(item, env_vars) if isinstance(item, str) else item)
                for item in val
            ]
        else:
            resolved[key] = val
    return resolved


@dataclass(slots=True)
class GeneralConfig:
    """General storage and workspace settings."""

    db_path: Path
    inbox_dir: Path
    rules_path: Path
    currency: str = "USD"


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


def load_config(path: Path) -> AppConfig:
    """Load and validate an AppConfig from a TOML file (resolving .env + process env)."""
    config_path = path.resolve()
    if not config_path.exists():
        raise ConfigError(f"Configuration file not found: {config_path}")

    config_dir = config_path.parent
    env_vars = {**load_dotenv(config_dir / ".env"), **os.environ}
    with config_path.open("rb") as fh:
        raw = _resolve_dict_envs(tomllib.load(fh), env_vars)
    gen_raw = raw.get("general", {})
    general = GeneralConfig(
        db_path=_resolve_path(config_dir, str(gen_raw.get("db_path", "data/ledgerdemain.db"))),
        inbox_dir=_resolve_path(config_dir, str(gen_raw.get("inbox_dir", "inbox"))),
        rules_path=_resolve_path(config_dir, str(gen_raw.get("rules_path", "rules.toml"))),
        currency=str(gen_raw.get("currency", "USD")),
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
