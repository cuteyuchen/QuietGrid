from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class AccountConfig:
    id: str
    label: str
    binance_api_key: str
    binance_api_secret: str
    database_path: Path
    binance_testnet: bool = False
    binance_testnet_raw: str | None = None


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    binance_api_key: str
    binance_api_secret: str
    binance_testnet: bool
    binance_testnet_raw: str | None = None
    account_id: str = "default"
    account_label: str = "默认账户"
    accounts: tuple[AccountConfig, ...] = ()

    @property
    def database_path(self) -> Path:
        for account in self.accounts:
            if account.id == self.account_id:
                return account.database_path
        return Path(self.raw["database"]["path"])


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config(config_path: str | Path = "config/config.yaml", env_path: str | Path = ".env") -> AppConfig:
    load_dotenv(env_path)
    with Path(config_path).open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    raw_testnet = os.getenv("BINANCE_TESTNET")
    global_testnet = _as_bool(raw_testnet, False)
    accounts = _load_accounts(raw, global_testnet, raw_testnet)
    selected_account_id = _selected_account_id(raw, accounts)
    base_config = AppConfig(
        raw=raw,
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=global_testnet,
        binance_testnet_raw=raw_testnet,
        accounts=accounts,
    )
    return select_account(base_config, selected_account_id)


def select_account(config: AppConfig, account_id: str | None) -> AppConfig:
    normalized_id = str(account_id or config.account_id or "").strip()
    if not normalized_id:
        normalized_id = "default"
    if not config.accounts:
        if normalized_id not in {"default", config.account_id}:
            raise ValueError(f"未找到账户配置: {normalized_id}")
        return AppConfig(
            raw=config.raw,
            binance_api_key=config.binance_api_key,
            binance_api_secret=config.binance_api_secret,
            binance_testnet=config.binance_testnet,
            binance_testnet_raw=config.binance_testnet_raw,
            account_id=config.account_id,
            account_label=config.account_label,
            accounts=config.accounts,
        )
    for account in config.accounts:
        if account.id == normalized_id:
            return AppConfig(
                raw=config.raw,
                binance_api_key=account.binance_api_key,
                binance_api_secret=account.binance_api_secret,
                binance_testnet=account.binance_testnet,
                binance_testnet_raw=account.binance_testnet_raw,
                account_id=account.id,
                account_label=account.label,
                accounts=config.accounts,
            )
    raise ValueError(f"未找到账户配置: {normalized_id}")


def select_all_accounts(config: AppConfig) -> tuple[AppConfig, ...]:
    return tuple(select_account(config, account.id) for account in config.accounts)


def require_testnet(config: AppConfig) -> None:
    raw_value = getattr(config, "binance_testnet_raw", None)
    if raw_value is not None and raw_value.strip().lower() != "true":
        raise RuntimeError("当前实现阶段要求显式配置 BINANCE_TESTNET=true，避免误连真实交易环境。")
    if raw_value is None and not config.binance_testnet:
        raise RuntimeError("当前实现阶段要求 BINANCE_TESTNET=true，避免误连真实交易环境。")


def _load_accounts(
    raw: dict[str, Any],
    default_testnet: bool,
    default_testnet_raw: str | None,
) -> tuple[AccountConfig, ...]:
    database_path = Path(raw.get("database", {}).get("path", "data/trading.db"))
    raw_accounts = raw.get("accounts")
    if not raw_accounts:
        return (
            AccountConfig(
                id="default",
                label="默认账户",
                binance_api_key=os.getenv("BINANCE_API_KEY", ""),
                binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
                binance_testnet=default_testnet,
                binance_testnet_raw=default_testnet_raw,
                database_path=database_path,
            ),
        )

    normalized_specs = _normalize_account_specs(raw_accounts)
    accounts: list[AccountConfig] = []
    seen: set[str] = set()
    for raw_id, spec in normalized_specs:
        account_id = _normalize_account_id(spec.get("id") or raw_id)
        if not account_id:
            raise ValueError("账户 id 不能为空。")
        if account_id in seen:
            raise ValueError(f"账户 id 重复: {account_id}")
        seen.add(account_id)
        env_suffix = _env_suffix(account_id)
        api_key_env = str(spec.get("api_key_env") or f"BINANCE_{env_suffix}_API_KEY")
        api_secret_env = str(spec.get("api_secret_env") or f"BINANCE_{env_suffix}_API_SECRET")
        account_testnet, account_testnet_raw = _account_testnet(
            spec,
            env_suffix,
            default_testnet,
            default_testnet_raw,
        )
        accounts.append(
            AccountConfig(
                id=account_id,
                label=str(spec.get("label") or account_id),
                binance_api_key=str(spec.get("api_key") or os.getenv(api_key_env, "")),
                binance_api_secret=str(spec.get("api_secret") or os.getenv(api_secret_env, "")),
                binance_testnet=account_testnet,
                binance_testnet_raw=account_testnet_raw,
                database_path=_account_database_path(database_path, account_id, spec.get("database_path")),
            )
        )
    return tuple(accounts)


def _normalize_account_specs(raw_accounts: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(raw_accounts, dict):
        return [(str(account_id), dict(spec or {})) for account_id, spec in raw_accounts.items()]
    if isinstance(raw_accounts, list):
        return [(str(item.get("id", "")), dict(item)) for item in raw_accounts if isinstance(item, dict)]
    raise ValueError("accounts 必须是列表或映射。")


def _selected_account_id(raw: dict[str, Any], accounts: tuple[AccountConfig, ...]) -> str:
    env_selected = os.getenv("QUIETGRID_ACCOUNT_ID")
    if env_selected:
        return _normalize_account_id(env_selected)
    raw_selected = raw.get("account_id") or raw.get("default_account_id")
    if raw_selected:
        return _normalize_account_id(raw_selected)
    for account in accounts:
        return account.id
    return "default"


def _normalize_account_id(value: Any) -> str:
    return str(value or "").strip()


def _env_suffix(account_id: str) -> str:
    chars = [char if char.isalnum() else "_" for char in account_id.upper()]
    return "".join(chars).strip("_") or "DEFAULT"


def _account_testnet(
    spec: dict[str, Any],
    env_suffix: str,
    default_testnet: bool,
    default_testnet_raw: str | None,
) -> tuple[bool, str | None]:
    env_name = str(spec.get("testnet_env") or spec.get("binance_testnet_env") or f"BINANCE_{env_suffix}_TESTNET")
    env_value = os.getenv(env_name)
    if env_value is not None:
        return _as_bool(env_value, default_testnet), env_value

    if "testnet" in spec:
        raw_value = _raw_bool_text(spec.get("testnet"))
        return _as_bool(raw_value, default_testnet), raw_value

    if "binance_testnet" in spec:
        raw_value = _raw_bool_text(spec.get("binance_testnet"))
        return _as_bool(raw_value, default_testnet), raw_value

    return default_testnet, default_testnet_raw


def _raw_bool_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _account_database_path(base_path: Path, account_id: str, explicit_path: Any) -> Path:
    if explicit_path:
        return Path(str(explicit_path))
    if account_id == "default":
        return base_path
    return base_path.with_name(f"{base_path.stem}-{account_id}{base_path.suffix}")
