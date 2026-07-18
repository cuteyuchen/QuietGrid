from __future__ import annotations

from pathlib import Path

import pytest

from core.config import load_config, require_testnet, select_account, select_all_accounts


def _write_config(path) -> None:
    path.write_text("database:\n  path: test.db\n", encoding="utf-8")


def test_load_config_requires_explicit_testnet_for_binance_entrypoints(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    _write_config(config_path)
    env_path.write_text("BINANCE_API_KEY=key\nBINANCE_API_SECRET=secret\n", encoding="utf-8")

    config = load_config(config_path, env_path)

    assert config.binance_testnet is False
    assert config.binance_testnet_raw is None
    with pytest.raises(RuntimeError, match="BINANCE_TESTNET=true"):
        require_testnet(config)


def test_require_testnet_rejects_non_true_values(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    _write_config(config_path)
    env_path.write_text(
        "BINANCE_API_KEY=key\nBINANCE_API_SECRET=secret\nBINANCE_TESTNET=false\n",
        encoding="utf-8",
    )

    config = load_config(config_path, env_path)

    assert config.binance_testnet is False
    assert config.binance_testnet_raw == "false"
    with pytest.raises(RuntimeError, match="显式配置 BINANCE_TESTNET=true"):
        require_testnet(config)


def test_require_testnet_accepts_explicit_true(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    _write_config(config_path)
    env_path.write_text(
        "BINANCE_API_KEY=key\nBINANCE_API_SECRET=secret\nBINANCE_TESTNET=true\n",
        encoding="utf-8",
    )

    config = load_config(config_path, env_path)

    assert config.binance_testnet is True
    assert config.binance_testnet_raw == "true"
    require_testnet(config)


def test_load_config_supports_named_accounts(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    monkeypatch.delenv("QUIETGRID_ACCOUNT_ID", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    base_db = tmp_path / "trading.db"
    hedge_db = tmp_path / "hedge.db"
    config_path.write_text(
        f"""
database:
  path: {base_db.as_posix()}
default_account_id: main
accounts:
  - id: main
    label: 主账户
    api_key_env: QG_MAIN_API_KEY
    api_secret_env: QG_MAIN_API_SECRET
    testnet: false
  - id: hedge
    label: 对冲账户
    database_path: {hedge_db.as_posix()}
    api_key_env: QG_HEDGE_API_KEY
    api_secret_env: QG_HEDGE_API_SECRET
    testnet_env: QG_HEDGE_TESTNET
""",
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            [
                "BINANCE_TESTNET=true",
                "QG_MAIN_API_KEY=main-key",
                "QG_MAIN_API_SECRET=main-secret",
                "QG_HEDGE_API_KEY=hedge-key",
                "QG_HEDGE_API_SECRET=hedge-secret",
                "QG_HEDGE_TESTNET=true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, env_path)
    hedge_config = select_account(config, "hedge")

    assert config.account_id == "main"
    assert config.account_label == "主账户"
    assert config.binance_api_key == "main-key"
    assert config.binance_api_secret == "main-secret"
    assert config.binance_testnet is False
    assert config.binance_testnet_raw == "false"
    assert config.database_path == Path(base_db.with_name("trading-main.db"))
    assert [account.id for account in config.accounts] == ["main", "hedge"]
    assert [account.account_id for account in select_all_accounts(config)] == ["main", "hedge"]
    assert hedge_config.account_id == "hedge"
    assert hedge_config.account_label == "对冲账户"
    assert hedge_config.binance_api_key == "hedge-key"
    assert hedge_config.binance_api_secret == "hedge-secret"
    assert hedge_config.binance_testnet is True
    assert hedge_config.binance_testnet_raw == "true"
    assert hedge_config.database_path == hedge_db


def test_load_config_can_select_account_from_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    monkeypatch.setenv("QUIETGRID_ACCOUNT_ID", "hedge")
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
database:
  path: trading.db
accounts:
  main:
    api_key_env: QG_MAIN_KEY
    api_secret_env: QG_MAIN_SECRET
  hedge:
    api_key_env: QG_HEDGE_KEY
    api_secret_env: QG_HEDGE_SECRET
""",
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            [
                "BINANCE_TESTNET=true",
                "QG_MAIN_KEY=main-key",
                "QG_MAIN_SECRET=main-secret",
                "QG_HEDGE_KEY=hedge-key",
                "QG_HEDGE_SECRET=hedge-secret",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path, env_path)

    assert config.account_id == "hedge"
    assert config.binance_api_key == "hedge-key"
    assert config.database_path == Path("trading-hedge.db")


def test_select_account_rejects_unknown_account(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
database:
  path: trading.db
accounts:
  main:
    api_key_env: QG_MAIN_KEY
    api_secret_env: QG_MAIN_SECRET
""",
        encoding="utf-8",
    )
    env_path.write_text("BINANCE_TESTNET=true\nQG_MAIN_KEY=key\nQG_MAIN_SECRET=secret\n", encoding="utf-8")
    config = load_config(config_path, env_path)

    with pytest.raises(ValueError, match="missing"):
        select_account(config, "missing")


# --- P0-2/3/5 启动配置校验 -------------------------------------------------

from core.config import (  # noqa: E402
    StartupConfigError,
    validate_startup_config,
    validate_symbol_profile,
    validate_v2_feature_flags,
    validate_web_binding,
)


def test_web_binding_rejects_public_address_without_token() -> None:
    with pytest.raises(StartupConfigError, match="auth_token"):
        validate_web_binding({"web": {"address": "0.0.0.0", "auth_token": ""}})


def test_web_binding_allows_loopback_without_token() -> None:
    validate_web_binding({"web": {"address": "127.0.0.1", "auth_token": ""}})


def test_web_binding_allows_public_address_with_token() -> None:
    validate_web_binding({"web": {"address": "0.0.0.0", "auth_token": "secret"}})


def test_web_binding_reads_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("QUIETGRID_WEB_TOKEN", "from-env")
    validate_web_binding(
        {"web": {"address": "0.0.0.0", "auth_token_env": "QUIETGRID_WEB_TOKEN"}}
    )
    monkeypatch.delenv("QUIETGRID_WEB_TOKEN", raising=False)
    with pytest.raises(StartupConfigError):
        validate_web_binding(
            {"web": {"address": "0.0.0.0", "auth_token_env": "QUIETGRID_WEB_TOKEN"}}
        )


def test_symbol_profile_rejects_tradfi_in_testnet() -> None:
    raw = {
        "environment": "testnet",
        "selection": {"symbol_allowlist": ["BTCUSDT", "AAPLUSDT"]},
    }
    with pytest.raises(StartupConfigError, match="AAPLUSDT"):
        validate_symbol_profile(raw)


def test_symbol_profile_allows_crypto_only_testnet() -> None:
    validate_symbol_profile(
        {"environment": "testnet", "selection": {"symbol_allowlist": ["BTCUSDT", "ETHUSDT"]}}
    )


def test_symbol_profile_ignores_non_testnet_profiles() -> None:
    validate_symbol_profile(
        {"environment": "tradfi-live", "selection": {"symbol_allowlist": ["AAPLUSDT"]}}
    )


def test_v2_feature_flags_fail_closed_when_missing() -> None:
    with pytest.raises(StartupConfigError, match="regime_v2"):
        validate_v2_feature_flags({"environment": "v2-production", "features": {}})


def test_v2_feature_flags_fail_closed_when_disabled() -> None:
    with pytest.raises(StartupConfigError, match="被关闭"):
        validate_v2_feature_flags(
            {
                "environment": "v2-production",
                "features": {
                    "regime_v2": False,
                    "inventory_manager": True,
                    "adaptive_grid_v2": True,
                    "risk_manager_v2": True,
                },
            }
        )


def test_v2_feature_flags_pass_when_all_enabled() -> None:
    validate_v2_feature_flags(
        {
            "environment": "v2-production",
            "features": {
                "regime_v2": True,
                "inventory_manager": True,
                "adaptive_grid_v2": True,
                "risk_manager_v2": True,
            },
        }
    )


def test_validate_startup_config_runs_all_checks(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "database:\n  path: test.db\nweb:\n  address: 0.0.0.0\n  auth_token: ''\n",
        encoding="utf-8",
    )
    env_path.write_text("", encoding="utf-8")
    config = load_config(config_path, env_path)
    with pytest.raises(StartupConfigError):
        validate_startup_config(config)
