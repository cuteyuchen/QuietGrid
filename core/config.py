from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    binance_api_key: str
    binance_api_secret: str
    binance_testnet: bool
    binance_testnet_raw: str | None = None

    @property
    def database_path(self) -> Path:
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
    return AppConfig(
        raw=raw,
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        binance_testnet=_as_bool(raw_testnet, False),
        binance_testnet_raw=raw_testnet,
    )


def require_testnet(config: AppConfig) -> None:
    raw_value = getattr(config, "binance_testnet_raw", None)
    if raw_value is not None and raw_value.strip().lower() != "true":
        raise RuntimeError("当前实现阶段要求显式配置 BINANCE_TESTNET=true，避免误连真实交易环境。")
    if raw_value is None and not config.binance_testnet:
        raise RuntimeError("当前实现阶段要求 BINANCE_TESTNET=true，避免误连真实交易环境。")
