from __future__ import annotations

import pytest

from core.config import load_config, require_testnet


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
