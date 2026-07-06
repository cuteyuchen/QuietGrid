from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from core.config import load_config
from core.logging_config import setup_logging
from db.database import init_db
from db.repository import Repository


st: Any | None = None

_TABLE_LABELS = {
    "windows": "交易窗口",
    "sessions": "标的会话",
    "orders": "订单记录",
    "trades": "成交记录",
    "state_logs": "状态日志",
    "system_logs": "系统日志",
}

_COLUMN_LABELS = {
    "id": "编号",
    "window_id": "窗口编号",
    "session_id": "会话编号",
    "window_start": "窗口开始",
    "window_end": "窗口结束",
    "status": "状态",
    "state": "状态",
    "total_pnl": "总盈亏",
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "symbol": "标的",
    "grid_upper": "网格上沿",
    "grid_lower": "网格下沿",
    "grid_num": "网格数量",
    "grid_index": "网格序号",
    "step_pct": "网格间距",
    "baseline_atr": "基准 ATR",
    "stop_loss_price": "止损价",
    "capital": "本金",
    "leverage": "杠杆",
    "realized_pnl": "已实现盈亏",
    "open_time": "开仓时间",
    "close_time": "关闭时间",
    "close_reason": "关闭原因",
    "order_id": "订单编号",
    "client_id": "客户端编号",
    "side": "方向",
    "price": "价格",
    "qty": "数量",
    "quote_qty": "名义金额",
    "grid_pnl": "网格盈亏",
    "fee": "手续费",
    "funding_fee": "资金费",
    "trade_time": "成交时间",
    "entry_price": "入场价",
    "filled_at": "成交时间",
    "fill_price": "成交价",
    "from_state": "原状态",
    "to_state": "新状态",
    "trigger": "触发原因",
    "detail": "详情",
    "level": "级别",
    "module": "模块",
    "message": "消息",
    "log_time": "日志时间",
    "count": "数量",
    "notional": "名义金额",
    "proxy_enabled": "代理启用",
    "proxy": "代理地址",
    "allowlist_count": "允许标的数",
    "allowlist_preview": "允许标的预览",
    "database": "数据库",
    "auth_enabled": "Web 认证",
    "testnet_substitutes": "测试网替代标的",
    "maker": "Maker 费率",
    "max_maker_fee_rate": "Maker 费率上限",
    "commission": "费率原始值",
    "error": "错误",
}

_VALUE_LABELS = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARN": "警告",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重",
    "ok": "正常",
    "warn": "警告",
    "error": "异常",
    "open": "打开",
    "closed": "已关闭",
    "pending": "待提交",
    "filled": "已成交",
    "cancelled": "已撤销",
    "canceled": "已撤销",
    "rejected": "已拒绝",
    "IDLE": "空闲",
    "OBSERVING": "观察期",
    "RUNNING": "网格运行",
    "COOLDOWN": "冷静期",
    "CLOSING": "强制离场",
    "STOPPED": "已停止",
    "BUY": "买入",
    "SELL": "卖出",
    "LONG": "多仓",
    "SHORT": "空仓",
}

_MODULE_LABELS = {
    "controller": "控制器",
    "selector": "选币",
    "commission": "费率检查",
    "commission_health": "Maker 费率健康",
    "order_reconciliation": "订单对账",
    "position_reconciliation": "持仓对账",
    "binance_check": "Binance 检查",
    "binance_signed_write_health": "签名写接口检查",
    "binance_direct_order_diagnose": "直接 REST 诊断",
    "binance_market_roundtrip_smoke": "市价开平仓烟测",
}

_MESSAGE_LABELS = {
    "Selection completed.": "选币完成。",
    "Binance testnet check completed.": "Binance 测试网检查完成。",
    "Binance maker fee health check completed.": "Binance Maker 费率健康检查完成。",
    "Binance testnet market roundtrip smoke completed.": "Binance 测试网市价开平仓烟测完成。",
    "Binance direct REST order endpoint diagnose completed.": "Binance 直接 REST 下单端点诊断完成。",
    "Binance testnet safety sweep completed.": "Binance 测试网安全清扫完成。",
    "Binance testnet safety sweep left residual exposure.": "Binance 测试网安全清扫后仍有残留暴露。",
    "Grid calculation failed; symbol skipped.": "网格参数计算失败，已跳过该标的。",
    "Grid start failed; symbol skipped.": "网格启动失败，已跳过该标的。",
    "Grid start failed and cleanup is pending; session kept active for retry.": "网格启动失败且清理未完成，会话已保留等待重试。",
    "Startup recovery force close failed; session left unclosed.": "启动恢复时强制平仓失败，会话仍未关闭。",
    "Recovered unclosed session on startup.": "启动时已恢复未关闭会话。",
    "Partial fill event has invalid details; closing session without recording trade.": "部分成交事件明细无效，已关闭会话且未记录成交。",
    "Exchange stop order fill details invalid; closing session without recording trade.": "交易所端止损成交明细无效，已关闭会话且未记录成交。",
}


def main() -> None:
    config = load_config()
    if not _running_under_streamlit():
        web_config = config.raw["web"]
        raise SystemExit(
            _launch_streamlit(
                int(web_config["port"]),
                str(web_config.get("address", "0.0.0.0")),
            )
        )

    render_dashboard(config.raw)


def render_dashboard(raw_config: dict) -> None:
    setup_logging(raw_config)
    database_path = Path(raw_config["database"]["path"])
    init_db(database_path)
    repo = Repository(database_path)
    streamlit = _streamlit()

    streamlit.set_page_config(page_title="QuietGrid", layout="wide")
    _inject_dashboard_css(streamlit)
    auth_token = str(raw_config.get("web", {}).get("auth_token") or "")
    _require_auth(auth_token)

    streamlit.title("QuietGrid 监控")
    streamlit.caption("只读监控界面。当前阶段展示数据库最近记录。")

    summary = repo.dashboard_summary()
    col1, col2, col3, col4 = streamlit.columns(4)
    col1.metric("活跃会话", summary["active_sessions"])
    col2.metric("未成交挂单", summary["open_orders"])
    col3.metric("已实现盈亏", f'{summary["realized_pnl"]:.4f}')
    col4.metric("最近系统消息", _localize_message(summary["latest_system_message"]) or "-")

    _render_runtime_status_panel(streamlit, raw_config, database_path)
    _render_order_status_panel(streamlit, repo)
    _render_commission_health_panel(streamlit, repo)
    _render_alert_panel(streamlit, repo)

    for table in ["windows", "sessions", "orders", "trades", "state_logs", "system_logs"]:
        streamlit.subheader(_TABLE_LABELS[table])
        streamlit.dataframe(_localize_rows(repo.recent_rows(table), table=table), use_container_width=True)


def _render_runtime_status_panel(streamlit: Any, raw_config: dict, database_path: Path) -> None:
    streamlit.subheader("运行配置")
    summary = _runtime_config_summary(raw_config, database_path)
    col1, col2, col3, col4 = streamlit.columns(4)
    col1.metric("代理", "启用" if summary["proxy_enabled"] else "关闭", summary["proxy"])
    col2.metric("允许标的", summary["allowlist_count"], summary["allowlist_preview"])
    col3.metric("数据库", summary["database"])
    col4.metric("Web认证", "启用" if summary["auth_enabled"] else "关闭")
    streamlit.dataframe(_localize_rows([summary]), use_container_width=True)


def _inject_dashboard_css(streamlit: Any) -> None:
    streamlit.markdown(_dashboard_css(), unsafe_allow_html=True)


def _dashboard_css() -> str:
    return """
<style>
[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stElementToolbar"] {
    display: none !important;
}
button[aria-label="Deploy"],
button[aria-label="Show/hide columns"],
button[aria-label="Download as CSV"],
button[aria-label="Search"],
button[aria-label="Fullscreen"],
button[title="Deploy"],
button[title="Show/hide columns"],
button[title="Download as CSV"],
button[title="Search"],
button[title="Fullscreen"] {
    display: none !important;
}
</style>
"""


def _runtime_config_summary(raw_config: dict, database_path: Path) -> dict[str, Any]:
    selection = raw_config.get("selection", {})
    allowlist = [str(symbol).strip().upper() for symbol in selection.get("symbol_allowlist", []) if str(symbol).strip()]
    proxy_config = raw_config.get("proxy", {})
    proxy_enabled = bool(proxy_config.get("enabled"))
    proxy = "-"
    if proxy_enabled:
        proxy = str(proxy_config.get("https") or proxy_config.get("http") or "-")
    preview = ", ".join(allowlist[:3])
    if len(allowlist) > 3:
        preview = f"{preview} +{len(allowlist) - 3}"
    return {
        "proxy_enabled": proxy_enabled,
        "proxy": proxy,
        "allowlist_count": len(allowlist),
        "allowlist_preview": preview or "-",
        "database": str(database_path),
        "auth_enabled": bool(str(raw_config.get("web", {}).get("auth_token") or "")),
        "testnet_substitutes": ", ".join(symbol for symbol in allowlist if symbol in {"BTCUSDT", "ETHUSDT", "BCHUSDT"}) or "-",
    }


def _render_order_status_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("订单健康")
    counts = repo.order_status_counts()
    if not counts:
        streamlit.caption("暂无订单记录。")
        return

    columns = streamlit.columns(len(counts))
    for column, row in zip(columns, counts):
        label = _order_status_label(str(row["status"]))
        column.metric(label, row["count"], f'{row["notional"]:.4f} USDT')
    streamlit.dataframe(_localize_rows(counts, table="orders"), use_container_width=True)


def _render_commission_health_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("Maker 费率健康")
    health = repo.latest_commission_health()
    if not health:
        streamlit.caption("暂无 Maker 费率检查记录。")
        return

    status = str(health.get("status", "") or health.get("level", ""))
    col1, col2, col3, col4 = streamlit.columns(4)
    col1.metric("整体状态", _commission_status_label(status))
    col2.metric("检查标的", health.get("checked_symbols", 0))
    col3.metric("警告数量", health.get("warn_count", 0))
    col4.metric("异常数量", health.get("error_count", 0))
    symbol_rows = _commission_symbol_rows(health)
    if symbol_rows:
        streamlit.dataframe(_localize_rows(symbol_rows, table="commission_health"), use_container_width=True)
    else:
        streamlit.dataframe(_localize_rows([health]), use_container_width=True)


def _render_alert_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("近期风险/恢复事件")
    alerts = repo.recent_alert_events()
    if not alerts:
        streamlit.caption("暂无 WARN/ERROR 事件。")
        return
    streamlit.dataframe(_localize_rows(alerts, table="system_logs"), use_container_width=True)


def _order_status_label(status: str) -> str:
    return {
        "pending": "待提交",
        "open": "未成交",
        "filled": "已成交",
        "cancelled": "已撤销",
        "rejected": "已拒绝",
    }.get(status, status)


def _commission_status_label(status: str) -> str:
    return _VALUE_LABELS.get(status.strip().lower(), _VALUE_LABELS.get(status.strip().upper(), status or "-"))


def _commission_symbol_rows(health: dict[str, Any]) -> list[dict[str, Any]]:
    symbols = health.get("symbols")
    if not isinstance(symbols, list):
        return []
    rows = []
    for item in symbols:
        if isinstance(item, dict):
            rows.append(
                {
                    "symbol": item.get("symbol", "-"),
                    "status": item.get("status", "-"),
                    "maker": item.get("maker", "-"),
                    "max_maker_fee_rate": item.get("max_maker_fee_rate", health.get("max_maker_fee_rate", "-")),
                    "error": item.get("error", ""),
                }
            )
    return rows


def _localize_rows(rows: list[dict[str, Any]], table: str | None = None) -> list[dict[str, Any]]:
    return [_localize_row(row, table=table) for row in rows]


def _localize_row(row: dict[str, Any], table: str | None = None) -> dict[str, Any]:
    return {_COLUMN_LABELS.get(key, key): _localize_value(key, value, table=table) for key, value in row.items()}


def _localize_value(key: str, value: Any, table: str | None = None) -> Any:
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return ""
    text = str(value)
    if key == "module":
        return _MODULE_LABELS.get(text, text)
    if key == "message":
        return _localize_message(text)
    if key == "status" and text.lower() == "open":
        if table == "orders":
            return "未成交"
        if table == "windows":
            return "进行中"
        return "打开"
    if key in {"level", "status", "state", "from_state", "to_state", "side"}:
        return _VALUE_LABELS.get(text, _VALUE_LABELS.get(text.upper(), _VALUE_LABELS.get(text.lower(), value)))
    return value


def _localize_message(message: str) -> str:
    if not message:
        return ""
    if message in _MESSAGE_LABELS:
        return _MESSAGE_LABELS[message]
    signed_prefix = "Binance signed write health check "
    if message.startswith(signed_prefix):
        if " passed " in message:
            return "Binance 签名写接口预检通过。"
        if " failed " in message:
            return "Binance 签名写接口预检失败。"
    if message.startswith("Binance ") and message.endswith(" stopped unexpectedly."):
        return "Binance 后台任务异常停止。"
    return message


def _require_auth(auth_token: str) -> None:
    if not auth_token:
        return
    streamlit = _streamlit()
    provided = _query_token()
    if provided == auth_token:
        return
    entered = streamlit.text_input("访问令牌", type="password")
    if entered == auth_token:
        return
    streamlit.warning("需要有效令牌才能查看监控。")
    streamlit.stop()


def _query_token() -> str:
    raw = _streamlit().query_params.get("token", "")
    if isinstance(raw, list):
        return str(raw[0]) if raw else ""
    return str(raw)


def _running_under_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except Exception:
        return False
    return get_script_run_ctx() is not None


def _streamlit() -> Any:
    global st
    if st is None:
        try:
            import streamlit as streamlit_module
        except ImportError as exc:
            raise RuntimeError("缺少 streamlit 依赖，请先安装 requirements.txt。") from exc
        st = streamlit_module
    return st


def _streamlit_command(script_path: Path, port: int, address: str = "0.0.0.0") -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(script_path),
        "--server.port",
        str(port),
        "--server.address",
        address,
        "--server.headless",
        "true",
    ]


def _launch_streamlit(port: int, address: str = "0.0.0.0") -> int:
    script_path = Path(__file__).resolve()
    return subprocess.run(_streamlit_command(script_path, port, address), check=False).returncode


if __name__ == "__main__":
    main()
