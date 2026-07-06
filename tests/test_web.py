from __future__ import annotations

from pathlib import Path

import pytest

import web


class FakeStreamlit:
    def __init__(self, token: str = "", entered_token: str = "") -> None:
        self.query_params = {"token": token} if token else {}
        self.entered_token = entered_token
        self.warning_message = ""
        self.stopped = False

    def text_input(self, label: str, type: str = "default") -> str:
        return self.entered_token

    def warning(self, message: str) -> None:
        self.warning_message = message

    def stop(self) -> None:
        self.stopped = True
        raise RuntimeError("streamlit stopped")


def test_streamlit_command_uses_configured_port() -> None:
    command = web._streamlit_command(Path("web.py"), 9090)

    assert command[:3] == [web.sys.executable, "-m", "streamlit"]
    assert command[3:] == [
        "run",
        "web.py",
        "--server.port",
        "9090",
        "--server.address",
        "0.0.0.0",
        "--server.headless",
        "true",
    ]


def test_require_auth_accepts_query_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_st = FakeStreamlit(token="secret")
    monkeypatch.setattr(web, "st", fake_st)

    web._require_auth("secret")

    assert fake_st.stopped is False
    assert fake_st.warning_message == ""


def test_require_auth_stops_without_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_st = FakeStreamlit()
    monkeypatch.setattr(web, "st", fake_st)

    with pytest.raises(RuntimeError, match="streamlit stopped"):
        web._require_auth("secret")

    assert fake_st.stopped is True
    assert "令牌" in fake_st.warning_message


def test_runtime_config_summary_exposes_safe_runtime_settings() -> None:
    summary = web._runtime_config_summary(
        {
            "proxy": {"enabled": True, "https": "http://127.0.0.1:7897"},
            "selection": {"symbol_allowlist": [" btcusdt ", "ETHUSDT", "BCHUSDT", "AAPLUSDT"]},
            "web": {"auth_token": "secret"},
        },
        Path("data/trading.db"),
    )

    assert summary == {
        "proxy_enabled": True,
        "proxy": "http://127.0.0.1:7897",
        "allowlist_count": 4,
        "allowlist_preview": "BTCUSDT, ETHUSDT, BCHUSDT +1",
        "database": "data\\trading.db",
        "auth_enabled": True,
        "testnet_substitutes": "BTCUSDT, ETHUSDT, BCHUSDT",
    }


def test_runtime_config_summary_handles_disabled_proxy_and_empty_allowlist() -> None:
    summary = web._runtime_config_summary(
        {
            "proxy": {"enabled": False, "https": "http://127.0.0.1:7897"},
            "selection": {"symbol_allowlist": []},
            "web": {"auth_token": ""},
        },
        Path("data/trading.db"),
    )

    assert summary["proxy_enabled"] is False
    assert summary["proxy"] == "-"
    assert summary["allowlist_count"] == 0
    assert summary["allowlist_preview"] == "-"
    assert summary["auth_enabled"] is False
    assert summary["testnet_substitutes"] == "-"


def test_localize_rows_translates_common_dashboard_fields() -> None:
    rows = web._localize_rows(
        [
            {
                "status": "open",
                "level": "WARN",
                "module": "commission_health",
                "message": "Maker fee changed.",
                "detail": None,
            }
        ],
        table="orders",
    )

    assert rows == [
        {
            "状态": "未成交",
            "级别": "警告",
            "模块": "Maker 费率健康",
            "消息": "Maker fee changed.",
            "详情": "",
        }
    ]


def test_localize_rows_uses_table_context_for_window_status() -> None:
    rows = web._localize_rows([{"status": "open", "window_start": "2026-07-04T00:00:00+00:00"}], table="windows")

    assert rows == [{"状态": "进行中", "窗口开始": "2026-07-04T00:00:00+00:00"}]


def test_commission_symbol_rows_are_display_ready() -> None:
    rows = web._localize_rows(
        web._commission_symbol_rows(
            {
                "max_maker_fee_rate": 0.0,
                "symbols": [
                    {"symbol": "BTCUSDT", "status": "warn", "maker": 0.0002, "max_maker_fee_rate": 0.0},
                    {"symbol": "ETHUSDT", "status": "error", "error": "timeout"},
                ],
            }
        ),
        table="commission_health",
    )

    assert rows == [
        {"标的": "BTCUSDT", "状态": "警告", "Maker 费率": 0.0002, "Maker 费率上限": 0.0, "错误": ""},
        {"标的": "ETHUSDT", "状态": "异常", "Maker 费率": "-", "Maker 费率上限": 0.0, "错误": "timeout"},
    ]


def test_localize_message_translates_common_system_messages() -> None:
    assert web._localize_message("Selection completed.") == "选币完成。"
    assert (
        web._localize_message("Binance signed write health check failed before binance_once.")
        == "Binance 签名写接口预检失败。"
    )
    assert web._localize_message("Custom exchange message.") == "Custom exchange message."


def test_dashboard_css_hides_streamlit_english_toolbars() -> None:
    css = web._dashboard_css()

    assert "Show/hide columns" in css
    assert "Download as CSV" in css
    assert "Fullscreen" in css
    assert "stHeader" in css
