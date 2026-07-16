from __future__ import annotations

import json
import os
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
                "trigger": "grid_started",
                "detail": None,
            }
        ],
        table="orders",
    )

    assert rows == [
        {
            "状态": "未成交",
            "级别": "警告",
            "模块": "挂单费率健康",
            "消息": "挂单费率已变化。",
            "触发原因": "网格启动",
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
        {"标的": "BTCUSDT", "状态": "警告", "挂单费率": 0.0002, "挂单费率上限": 0.0, "错误": ""},
        {"标的": "ETHUSDT", "状态": "异常", "挂单费率": "-", "挂单费率上限": 0.0, "错误": "请求超时"},
    ]


def test_active_volatility_rows_are_display_ready() -> None:
    rows = web._localize_rows(
        web._active_volatility_rows(
            [
                {
                    "session_id": 7,
                    "symbol": "AAPLUSDT",
                    "state": "RUNNING",
                    "volatility_method": "garman_klass",
                    "volatility_value": 0.0125,
                    "volatility_window": 60,
                    "volatility_current_value": 0.0105,
                    "volatility_current_window": 30,
                    "volatility_current_at": "2026-07-08T12:00:00+00:00",
                    "grid_lower": 99.0,
                    "grid_upper": 101.0,
                    "grid_num": 4,
                    "step_pct": 0.005,
                    "baseline_atr": 0.2,
                }
            ]
        ),
        table="active_volatility",
    )

    assert rows == [
        {
            "会话编号": 7,
            "标的": "AAPLUSDT",
            "状态": "网格运行",
            "波动率算法": "Garman-Klass",
            "建仓波动率(%)": 1.25,
            "建仓窗口K线数": 60,
            "当前波动率(%)": 1.05,
            "当前窗口K线数": 30,
            "最近重算时间": "2026-07-08T12:00:00+00:00",
            "网格下沿": 99.0,
            "网格上沿": 101.0,
            "网格数量": 4,
            "网格间距": 0.005,
            "基准 ATR": 0.2,
        }
    ]


def test_localize_message_translates_common_system_messages() -> None:
    assert web._localize_message("Selection completed.") == "选币完成。"
    assert (
        web._localize_message("Binance signed write health check failed before binance_once.")
        == "Binance 签名写接口预检失败。"
    )
    assert (
        web._localize_message("Timeout waiting for response from backend server. Send status unknown; execution status unknown.")
        == "等待后端服务器响应超时；发送状态未知，执行状态未知。"
    )
    assert (
        web._localize_message("APIError(code=-1007): Timeout waiting for response from backend server. Send status unknown; execution status unknown.")
        == "Binance API 错误（代码 -1007）：等待后端服务器响应超时；发送状态未知，执行状态未知。"
    )
    assert (
        web._localize_message("APIError(code=-4120): Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.")
        == "Binance API 错误（代码 -4120）：当前端点不支持该订单类型，请改用 Algo Order API 端点。"
    )
    assert (
        web._localize_message("Cannot connect to host testnet.binance.vision:443 ssl:default [None]")
        == "无法连接到主机 testnet.binance.vision:443（ssl:default [None]）。"
    )
    assert (
        web._localize_message(
            "Service unavailable from a restricted location according to 'b. Eligibility' in https://www.binance.com/en/terms."
        )
        == "Binance 服务拒绝当前代理出口地区；请切换到 Binance 测试网允许的代理节点后重试。"
    )
    assert web._localize_message("Custom exchange message.") == "Custom exchange message."


def test_compact_latest_message_uses_short_chinese_status() -> None:
    assert web._compact_latest_message("Binance testnet position smoke completed.") == "持仓只读通过"
    assert web._compact_latest_message("Binance current-environment position check completed.") == "持仓只读通过"
    assert web._compact_latest_message("Binance maker fee health check completed.") == "费率检查完成"
    assert web._compact_latest_message("Binance testnet bounded run completed.") == "有界运行完成"
    assert web._compact_latest_message("Binance current-environment bounded run completed.") == "有界运行完成"


def test_localize_detail_translates_json_keys_and_common_values() -> None:
    detail = (
        '{"signed_write_ok": false, "caller": "binance_once", "proxy_enabled": true, '
        '"errors": ["Binance current-environment safety sweep completed."], '
        '"symbols": [{"symbol": "BTCUSDT", "status": "warn", "long_qty": 0.0}]}'
    )

    localized = web._localize_detail(detail)

    assert "签名写接口正常" in localized
    assert "调用入口" in localized
    assert "代理启用" in localized
    assert "错误列表" in localized
    assert "Binance 当前环境安全清扫完成。" in localized
    assert "标的明细" in localized
    assert "多仓数量" in localized


def test_localize_detail_translates_key_value_pairs() -> None:
    assert web._localize_detail("grid_num=1, step_pct=0.0015") == "网格数量=1，网格间距=0.0015"


def test_localize_detail_translates_nested_key_value_pairs() -> None:
    detail = "symbols=599, eligible=3, sample=BTCUSDT, commission={'maker': 0.0002, 'taker': 0.0004}, commission_health=warn"

    localized = web._localize_detail(detail)

    assert localized == (
        '标的总数=599，候选标的数=3，样本标的=BTCUSDT，'
        '费率原始值={"挂单费率": 0.0002, "吃单费率": 0.0004}，费率健康=警告'
    )


def test_localize_detail_translates_binance_response_fields() -> None:
    detail = '{"status": "NEW", "positionSide": "LONG", "msg": "Timeout waiting for response from backend server. Send status unknown; execution status unknown."}'

    localized = web._localize_detail(detail)

    assert "新建" in localized
    assert "持仓方向" in localized
    assert "多仓" in localized
    assert "发送状态未知" in localized


def test_testnet_verification_rows_summarize_latest_logs() -> None:
    rows = web._testnet_verification_rows(
        [
            {
                "module": "binance_check",
                "level": "INFO",
                "message": "Binance testnet check completed.",
                "detail": "symbols=599, eligible=3, sample=BTCUSDT, commission_health=warn",
                "log_time": "2026-07-07T12:00:00+00:00",
            },
            {
                "module": "commission_health",
                "level": "WARN",
                "message": "Binance maker fee health check completed.",
                "detail": json.dumps({"status": "warn", "checked_symbols": 3, "ok_count": 0, "warn_count": 3, "error_count": 0}),
                "log_time": "2026-07-07T12:01:00+00:00",
            },
            {
                "module": "binance_direct_order_diagnose",
                "level": "ERROR",
                "message": "Binance direct REST order endpoint diagnose completed.",
                "detail": json.dumps(
                    {
                        "direct_order_diagnose_ok": True,
                        "endpoint_order_ok": False,
                        "http_status": 503,
                        "response": {"json": {"code": -1007}},
                        "cleanup_errors": [],
                    }
                ),
                "log_time": "2026-07-07T12:02:00+00:00",
            },
            {
                "module": "binance_safety_sweep",
                "level": "INFO",
                "message": "Binance current-environment safety sweep completed.",
                "detail": json.dumps(
                    {
                        "safety_sweep_ok": True,
                        "symbols": [
                            {
                                "symbol": "BTCUSDT",
                                "ordinary_after": 0,
                                "algo_after": 0,
                                "position_after": {"qty": 0.0, "long_qty": 0.0, "short_qty": 0.0},
                            }
                        ],
                        "closed_sessions": [{"session_id": 12, "symbol": "BTCUSDT", "from_state": "RUNNING"}],
                    }
                ),
                "log_time": "2026-07-07T12:03:00+00:00",
            },
            {
                "module": "binance_safety_sweep",
                "level": "ERROR",
                "message": "Older sweep should not override latest.",
                "detail": json.dumps({"safety_sweep_ok": False, "symbols": []}),
                "log_time": "2026-07-07T11:03:00+00:00",
            },
        ]
    )

    by_module = {row["module"]: row for row in rows}

    assert by_module["binance_check"]["verification_status"] == "passed"
    assert by_module["commission_health"]["verification_status"] == "warning"
    assert by_module["binance_direct_order_diagnose"]["verification_status"] == "unknown"
    assert by_module["binance_safety_sweep"]["verification_status"] == "passed"
    assert by_module["binance_safety_sweep"]["last_checked"] == "2026-07-07T12:03:00+00:00"
    assert by_module["binance_price_stream_smoke"]["verification_status"] == "not_run"
    assert "清扫标的: 1" in by_module["binance_safety_sweep"]["detail_summary"]
    assert "同步关闭会话: 1" in by_module["binance_safety_sweep"]["detail_summary"]


def test_testnet_verification_rows_localize_status_and_detect_position_residual() -> None:
    rows = web._testnet_verification_rows(
        [
            {
                "module": "binance_position_smoke",
                "level": "INFO",
                "message": "Binance testnet position smoke completed.",
                "detail": json.dumps(
                    {
                        "position_smoke_ok": True,
                        "dual_side_position": True,
                        "symbols": [{"symbol": "BTCUSDT", "qty": 0.1, "long_qty": 0.1, "short_qty": 0.0, "ordinary_open": 1, "algo_open": 0}],
                    }
                ),
                "log_time": "2026-07-07T12:00:00+00:00",
            }
        ]
    )

    localized = web._localize_rows(rows, table="testnet_verification")
    position_row = next(row for row in localized if row["模块"] == "持仓只读烟测")

    assert position_row["验证状态"] == "警告"
    assert position_row["最近消息"] == "Binance 测试网持仓只读烟测完成。"
    assert "存在暴露: 1" in position_row["验证摘要"]


def test_testnet_verification_labels_use_chinese_product_terms() -> None:
    rows = web._localize_rows(web._testnet_verification_rows([]), table="testnet_verification")

    labels = {row["验证项"] for row in rows}

    assert "用户流密钥" in labels
    assert "测试下单参数校验" in labels
    assert all("smoke/check" not in row["验证摘要"] for row in rows)


def test_testnet_verification_localizes_client_create_failure() -> None:
    rows = web._testnet_verification_rows(
        [
            {
                "module": "binance_test_order_smoke",
                "level": "ERROR",
                "message": "Binance testnet client creation failed.",
                "detail": json.dumps(
                    {
                        "ok": False,
                        "stage": "client_create",
                        "error": "Cannot connect to host testnet.binance.vision:443 ssl:default [None]",
                    }
                ),
                "log_time": "2026-07-07T12:00:00+00:00",
            }
        ]
    )

    localized = web._localize_rows(rows, table="testnet_verification")
    test_order_row = next(row for row in localized if row["模块"] == "测试下单参数烟测")

    assert test_order_row["验证状态"] == "失败"
    assert test_order_row["最近消息"] == "Binance 测试网客户端创建失败。"
    assert "阶段: 创建 Binance 客户端" in test_order_row["验证摘要"]
    assert "无法连接到主机 testnet.binance.vision:443" in test_order_row["验证摘要"]


def test_load_recent_backtest_reports_reads_latest_valid_json(tmp_path) -> None:
    old_report = tmp_path / "old.json"
    latest_report = tmp_path / "latest.json"
    invalid_report = tmp_path / "invalid.json"
    old_report.write_text(json.dumps(_sample_backtest_report("BTCUSDT", 1.0)), encoding="utf-8")
    latest_report.write_text(json.dumps(_sample_backtest_report("ETHUSDT", 2.0)), encoding="utf-8")
    invalid_report.write_text("{bad json", encoding="utf-8")
    os.utime(old_report, (1000, 1000))
    os.utime(latest_report, (2000, 2000))
    os.utime(invalid_report, (3000, 3000))

    reports = web._load_recent_backtest_reports(tmp_path, limit=1)

    assert len(reports) == 1
    assert reports[0]["source_file"] == "latest.json"
    assert reports[0]["report"]["summary"]["symbol"] == "ETHUSDT"


def test_backtest_report_rows_are_display_ready() -> None:
    report = _sample_backtest_report("BTCUSDT", 1.25)
    loaded = {"source_file": "backtest.json", "report_modified_at": "2026-07-06T12:00:00", "report": report}

    summary = web._localize_rows([web._backtest_summary_row(loaded)])[0]
    grid = web._localize_rows([web._backtest_grid_row(report)])[0]
    fills = web._localize_rows(web._backtest_section_rows(report, "fills", limit=1))[0]
    equity = web._localize_rows(web._backtest_section_rows(report, "equity_curve", limit=1))[0]

    assert summary["报告文件"] == "backtest.json"
    assert summary["标的"] == "BTCUSDT"
    assert summary["总盈亏"] == 1.25
    assert summary["网格胜率"] == 1.0
    assert summary["平均单格盈亏"] == 1.25
    assert summary["简化 Sharpe"] == 0.5
    assert summary["停止原因"] == "区间击穿"
    assert grid["区间上沿"] == 101.0
    assert fills["方向"] == "买入"
    assert equity["权益"] == 1.25


def test_backtest_batch_report_rows_are_display_ready() -> None:
    report = {
        "summary": {
            "files": 2,
            "succeeded": 1,
            "failed": 1,
            "symbol": "BTCUSDT",
            "total_pnl": 1.25,
            "avg_total_pnl": 1.25,
            "max_drawdown": 0.1,
            "total_fills": 4,
            "total_grid_trades": 2,
            "winning_grid_trades": 2,
            "losing_grid_trades": 0,
            "break_even_grid_trades": 0,
            "win_rate": 1.0,
            "avg_grid_pnl": 0.625,
            "fills_per_bar": 0.5,
            "avg_equity_sharpe": 0.25,
            "stopped_count": 1,
            "best_file": "window-a.csv",
            "worst_file": "window-a.csv",
        },
        "reports": [
            {
                "source_file": "window-a.csv",
                "symbol": "BTCUSDT",
                "total_pnl": 1.25,
                "total_fills": 4,
                "grid_trade_count": 2,
                "win_rate": 1.0,
            }
        ],
        "errors": [{"source_file": "bad.csv", "error": "缺少必要列"}],
    }
    loaded = {"source_file": "batch.json", "report_modified_at": "2026-07-06T12:00:00", "report": report}

    summary = web._localize_rows([web._backtest_summary_row(loaded)])[0]
    batch_rows = web._localize_rows(web._backtest_section_rows(report, "reports", limit=10))
    error_rows = web._localize_rows(web._backtest_section_rows(report, "errors", limit=10))

    assert summary["文件数"] == 2
    assert summary["成功数"] == 1
    assert summary["失败数"] == 1
    assert summary["总成交次数"] == 4
    assert summary["总闭合网格次数"] == 2
    assert summary["网格胜率"] == 1.0
    assert summary["平均单格盈亏"] == 0.625
    assert summary["平均简化 Sharpe"] == 0.25
    assert summary["平均总盈亏"] == 1.25
    assert summary["最佳文件"] == "window-a.csv"
    assert batch_rows[0]["报告文件"] == "window-a.csv"
    assert batch_rows[0]["闭合网格次数"] == 2
    assert error_rows[0]["错误"] == "缺少必要列"


def test_dashboard_css_hides_streamlit_english_toolbars() -> None:
    css = web._dashboard_css()

    assert "Show/hide columns" in css
    assert "Download as CSV" in css
    assert "Fullscreen" in css
    assert "stHeader" in css
    assert "[data-testid=\"stMetric\"]" in css
    assert "[data-testid=\"stTabs\"]" in css
    assert "@media (max-width: 480px)" in css
    assert "word-break: keep-all" in css


def _sample_backtest_report(symbol: str, total_pnl: float) -> dict:
    return {
        "summary": {
            "symbol": symbol,
            "observe_rows": 60,
            "backtest_rows": 3,
            "fills": 1,
            "fills_per_bar": 0.5,
            "grid_trade_count": 1,
            "winning_grid_trades": 1,
            "losing_grid_trades": 0,
            "break_even_grid_trades": 0,
            "win_rate": 1.0,
            "avg_grid_pnl": 1.25,
            "gross_grid_pnl": 1.5,
            "fees_paid": 0.25,
            "realized_pnl": 1.25,
            "unrealized_pnl": 0.0,
            "total_pnl": total_pnl,
            "max_equity": total_pnl,
            "max_drawdown": 0.1,
            "equity_sharpe": 0.5,
            "net_position_qty": 0.0,
            "open_order_count": 2,
            "stopped_reason": "range_break",
            "stopped_at_index": 2,
            "stopped_at_price": 98.5,
            "last_price": 100.5,
        },
        "grid_params": {
            "symbol": symbol,
            "upper": 101.0,
            "lower": 99.0,
            "center": 100.0,
            "grid_num": 2,
            "step_pct": 0.01,
            "baseline_atr": 0.2,
            "stop_loss_price": 98.0,
            "calculated_at": "2026-07-06T12:00:00+00:00",
        },
        "fills": [
            {
                "symbol": symbol,
                "side": "BUY",
                "grid_index": 1,
                "price": 99.5,
                "qty": 1.0,
                "fee": 0.0,
                "grid_pnl": None,
                "realized_pnl_after": 0.0,
                "bar_index": 1,
                "timestamp": "2026-07-06T12:01:00+00:00",
            }
        ],
        "equity_curve": [
            {
                "bar_index": 2,
                "equity": total_pnl,
                "realized_pnl": 1.25,
                "unrealized_pnl": 0.0,
                "drawdown": 0.1,
                "close": 100.5,
                "timestamp": "2026-07-06T12:02:00+00:00",
            }
        ],
    }
