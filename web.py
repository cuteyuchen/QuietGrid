from __future__ import annotations

import ast
import json
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
    "volatility_method": "波动率算法",
    "volatility_value": "建仓波动率",
    "volatility_value_pct": "建仓波动率(%)",
    "volatility_window": "建仓窗口K线数",
    "volatility_current_value": "当前波动率",
    "volatility_current_value_pct": "当前波动率(%)",
    "volatility_current_window": "当前窗口K线数",
    "volatility_current_at": "最近重算时间",
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
    "stage": "阶段",
    "error_type": "错误类型",
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
    "auth_enabled": "网页认证",
    "testnet_substitutes": "测试网替代标的",
    "maker": "挂单费率",
    "max_maker_fee_rate": "挂单费率上限",
    "commission": "费率原始值",
    "taker": "吃单费率",
    "error": "错误",
    "reason": "原因",
    "balance": "账户余额",
    "required_budget": "所需预算",
    "event_qty": "事件数量",
    "order_qty": "订单数量",
    "expected_qty": "预期数量",
    "actual_qty": "实际数量",
    "expected_long_qty": "预期多仓数量",
    "expected_short_qty": "预期空仓数量",
    "actual_long_qty": "实际多仓数量",
    "actual_short_qty": "实际空仓数量",
    "min_qty": "最小数量",
    "step_size": "数量步进",
    "close_specs": "平仓参数",
    "checked_symbols": "检查标的数",
    "ok_count": "正常数量",
    "warn_count": "警告数量",
    "error_count": "异常数量",
    "symbols": "标的明细",
    "score": "综合评分",
    "volume_score": "成交量评分",
    "depth_score": "深度评分",
    "volume_24h": "24h 成交额",
    "depth_usdt": "深度金额",
    "safety_sweep_ok": "安全清扫通过",
    "ordinary_before": "清扫前普通挂单数",
    "algo_before": "清扫前条件单数",
    "ordinary_after": "清扫后普通挂单数",
    "algo_after": "清扫后条件单数",
    "position_before": "清扫前持仓",
    "position_after": "清扫后持仓",
    "closed_positions": "已关闭持仓",
    "qty": "数量",
    "long_qty": "多仓数量",
    "short_qty": "空仓数量",
    "signed_write_ok": "签名写接口正常",
    "caller": "调用入口",
    "errors": "错误列表",
    "direct_order_diagnose_ok": "直接下单诊断通过",
    "endpoint_order_ok": "下单端点通过",
    "proxy_enabled": "代理启用",
    "setup_warnings": "初始化警告",
    "last_price": "最新价格",
    "limit_price": "限价价格",
    "position_side": "持仓方向",
    "http_status": "HTTP 状态",
    "response": "响应",
    "recovered_order_status": "恢复后的订单状态",
    "recovered_order_id": "恢复后的订单编号",
    "cleanup_errors": "清理错误",
    "source_file": "报告文件",
    "report_modified_at": "报告更新时间",
    "observe_rows": "观察期K线数",
    "backtest_rows": "回测K线数",
    "fills": "成交次数",
    "total_fills": "总成交次数",
    "fills_per_bar": "每K线成交数",
    "files": "文件数",
    "succeeded": "成功数",
    "failed": "失败数",
    "avg_total_pnl": "平均总盈亏",
    "grid_trade_count": "闭合网格次数",
    "total_grid_trades": "总闭合网格次数",
    "winning_grid_trades": "盈利网格次数",
    "losing_grid_trades": "亏损网格次数",
    "break_even_grid_trades": "持平网格次数",
    "win_rate": "网格胜率",
    "avg_grid_pnl": "平均单格盈亏",
    "equity_sharpe": "简化 Sharpe",
    "avg_equity_sharpe": "平均简化 Sharpe",
    "stopped_count": "触发停止次数",
    "best_file": "最佳文件",
    "worst_file": "最差文件",
    "gross_grid_pnl": "网格毛盈亏",
    "fees_paid": "已付手续费",
    "unrealized_pnl": "未实现盈亏",
    "total_pnl": "总盈亏",
    "max_equity": "最高权益",
    "max_drawdown": "最大回撤",
    "net_position_qty": "净持仓数量",
    "open_order_count": "剩余挂单数",
    "stopped_reason": "停止原因",
    "stopped_at_index": "停止K线序号",
    "stopped_at_price": "停止价格",
    "upper": "区间上沿",
    "lower": "区间下沿",
    "center": "区间中枢",
    "grid_prices": "网格价位",
    "calculated_at": "计算时间",
    "bar_index": "K线序号",
    "timestamp": "时间",
    "equity": "权益",
    "drawdown": "回撤",
    "close": "收盘价",
    "realized_pnl_after": "成交后已实现盈亏",
}

_COLUMN_LABELS.update(
    {
        "startup_ok": "启动检查通过",
        "tradable_symbols": "可交易标的数",
        "eligible_symbols": "候选标的数",
        "eligible": "候选标的数",
        "sample": "样本标的",
        "sample_symbol": "样本标的",
        "sample_rules": "样本交易规则",
        "sample_commission": "样本费率",
        "commission_health": "费率健康",
        "residuals": "残留暴露",
        "cancel_all_error": "全撤错误",
        "fallback_errors": "逐单撤单错误",
        "position_after_close": "平仓后持仓",
        "position_rows": "持仓行数",
        "ordinary_open": "普通挂单数",
        "algo_open": "条件单数",
        "position_smoke_ok": "持仓烟测通过",
        "dual_side_position": "双向持仓模式",
        "test_order_ok": "测试下单通过",
        "smoke_ok": "订单烟测通过",
        "stream_ok": "价格流通过",
        "listen_key_ok": "用户流密钥通过",
        "listen_key_length": "用户流密钥长度",
        "market_roundtrip_ok": "市价开平仓通过",
        "leverage_ok": "杠杆设置通过",
        "margin_type_ok": "保证金模式通过",
        "stop_price": "止损触发价",
        "trigger_price": "触发价",
        "open_client_id": "开仓客户端编号",
        "close_client_id": "平仓客户端编号",
        "limit_order_id": "限价单编号",
        "stop_order_id": "止损单编号",
        "open_order_id": "开仓订单编号",
        "close_order_id": "平仓订单编号",
        "limit_status": "限价单状态",
        "stop_status": "止损单状态",
        "limit_response": "限价响应",
        "market_response": "市价响应",
        "stop_response": "止损响应",
        "stop_supported": "支持止损单",
        "stop_error": "止损错误",
        "status_code": "HTTP 状态码",
        "ok": "请求成功",
        "code": "错误码",
        "msg": "错误消息",
        "json": "JSON 响应",
        "text": "文本响应",
        "path": "请求路径",
        "method": "请求方法",
        "attempted_symbols": "已尝试标的",
        "attempt_failures": "尝试失败",
        "event": "事件",
        "algo_id": "条件单编号",
        "algo_stop_ok": "条件止损单通过",
        "open_seen": "已看到挂单",
        "cancel_response": "撤单响应",
        "remaining_open": "剩余挂单数",
        "verification_item": "验证项",
        "verification_status": "验证状态",
        "last_checked": "最后验证时间",
        "latest_message": "最近消息",
        "detail_summary": "验证摘要",
        "orderId": "订单编号",
        "clientOrderId": "客户端订单编号",
        "newClientOrderId": "新客户端订单编号",
        "type": "订单类型",
        "side": "方向",
        "time": "交易所时间",
        "workingTime": "生效时间",
        "orderListId": "订单列表编号",
        "transactTime": "成交回报时间",
        "symbolStatus": "标的状态",
        "origQty": "原始数量",
        "executedQty": "已成交数量",
        "cumQty": "累计成交数量",
        "cumQuote": "累计成交金额",
        "avgPrice": "平均成交价",
        "timeInForce": "有效方式",
        "positionSide": "持仓方向",
        "reduceOnly": "只减仓",
        "closePosition": "平仓单",
        "workingType": "触发价格类型",
        "priceProtect": "价格保护",
        "origType": "原始订单类型",
        "updateTime": "更新时间",
        "priceRate": "回调比例",
        "activatePrice": "激活价",
        "priceMatch": "价格匹配",
        "selfTradePreventionMode": "自成交保护模式",
        "goodTillDate": "有效截止时间",
    }
)

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
    "std": "标准差",
    "quantile": "分位数",
    "parkinson": "Parkinson",
    "garman_klass": "Garman-Klass",
    "rogers_satchell": "Rogers-Satchell",
    "yang_zhang": "Yang-Zhang",
    "LONG": "多仓",
    "SHORT": "空仓",
    "stop_loss": "止损触发",
    "range_break": "区间击穿",
    "startup_recovery_force_close": "启动恢复强制离场",
    "startup_recovery_skipped_symbol": "启动恢复跳过不支持标的",
}

_VALUE_LABELS.update(
    {
        "NEW": "新建",
        "PARTIALLY_FILLED": "部分成交",
        "FILLED": "已成交",
        "CANCELED": "已撤销",
        "CANCELLED": "已撤销",
        "EXPIRED": "已过期",
        "REJECTED": "已拒绝",
        "TRADING": "可交易",
        "LIMIT": "限价单",
        "MARKET": "市价单",
        "STOP_MARKET": "止损市价单",
        "GTX": "只挂单",
        "GTC": "一直有效",
        "IOC": "立即成交否则取消",
        "FOK": "全部成交否则取消",
        "ISOLATED": "逐仓",
        "CROSSED": "全仓",
        "BOTH": "单向持仓",
        "CONTRACT_PRICE": "合约价格",
        "MARK_PRICE": "标记价格",
        "ACK": "确认响应",
        "RESULT": "完整响应",
        "true": "是",
        "false": "否",
        "none": "-",
        "null": "-",
        "success": "成功",
        "normal": "正常",
        "client_create": "创建 Binance 客户端",
        "binance_once": "Binance 单轮编排",
        "binance_loop": "Binance 长循环",
        "binance_signed_write_health": "签名写接口预检",
        "controller loop": "控制器循环",
        "user stream": "用户数据流",
        "price stream": "价格流",
        "<missing>": "缺失",
        "passed": "通过",
        "warning": "警告",
        "failed": "失败",
        "unknown": "状态未知",
        "not_run": "未运行",
    }
)

_BACKTEST_SUMMARY_KEYS = [
    "symbol",
    "observe_rows",
    "backtest_rows",
    "fills",
    "total_fills",
    "fills_per_bar",
    "files",
    "succeeded",
    "failed",
    "grid_trade_count",
    "total_grid_trades",
    "winning_grid_trades",
    "losing_grid_trades",
    "break_even_grid_trades",
    "win_rate",
    "avg_grid_pnl",
    "gross_grid_pnl",
    "fees_paid",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "max_equity",
    "max_drawdown",
    "equity_sharpe",
    "avg_equity_sharpe",
    "net_position_qty",
    "open_order_count",
    "stopped_reason",
    "stopped_count",
    "stopped_at_index",
    "stopped_at_price",
    "last_price",
    "avg_total_pnl",
    "best_file",
    "worst_file",
]

_BACKTEST_GRID_KEYS = [
    "symbol",
    "upper",
    "lower",
    "center",
    "grid_num",
    "step_pct",
    "baseline_atr",
    "stop_loss_price",
    "volatility_method",
    "volatility_value",
    "volatility_window",
    "calculated_at",
]

_TESTNET_VERIFICATION_MODULES = [
    "binance_check",
    "commission_health",
    "binance_price_stream_smoke",
    "binance_listen_key_smoke",
    "binance_position_smoke",
    "binance_signed_write_health",
    "binance_test_order_smoke",
    "binance_direct_order_diagnose",
    "binance_algo_stop_smoke",
    "binance_market_roundtrip_smoke",
    "binance_safety_sweep",
]
_ENVIRONMENT_VERIFICATION_MODULES = _TESTNET_VERIFICATION_MODULES

_TESTNET_VERIFICATION_LABELS = {
    "binance_check": "连接与账户检查",
    "commission_health": "挂单费率检查",
    "binance_price_stream_smoke": "WebSocket 价格流",
    "binance_listen_key_smoke": "用户流密钥",
    "binance_position_smoke": "持仓与挂单只读检查",
    "binance_signed_write_health": "签名写接口预检",
    "binance_test_order_smoke": "测试下单参数校验",
    "binance_direct_order_diagnose": "真实下单端点诊断",
    "binance_algo_stop_smoke": "条件止损单创建撤销",
    "binance_market_roundtrip_smoke": "市价开平仓烟测",
    "binance_safety_sweep": "安全清扫",
}
_ENVIRONMENT_VERIFICATION_LABELS = _TESTNET_VERIFICATION_LABELS

_VERIFICATION_STATUS_LABELS = {
    "passed": "通过",
    "warning": "警告",
    "failed": "失败",
    "unknown": "状态未知",
    "not_run": "未运行",
}

_MODULE_LABELS = {
    "controller": "控制器",
    "selector": "选币",
    "commission": "费率检查",
    "commission_health": "挂单费率健康",
    "order_reconciliation": "订单对账",
    "position_reconciliation": "持仓对账",
    "volatility": "波动率",
    "binance_check": "Binance 检查",
    "binance_signed_write_health": "签名写接口检查",
    "binance_test_order_smoke": "测试下单参数烟测",
    "binance_price_stream_smoke": "价格流烟测",
    "binance_listen_key_smoke": "用户流密钥烟测",
    "binance_algo_stop_smoke": "条件止损单烟测",
    "binance_position_smoke": "持仓只读烟测",
    "binance_direct_order_diagnose": "直接 REST 诊断",
    "binance_market_roundtrip_smoke": "市价开平仓烟测",
    "binance_safety_sweep": "当前环境安全清扫",
    "binance_loop": "Binance 循环",
    "grid_engine": "网格引擎",
    "force_close": "强制离场",
    "order_event": "订单事件",
    "partial_fill": "部分成交",
    "console_action": "控制台动作",
    "risk": "风控",
    "test": "测试",
}

_TRIGGER_LABELS = {
    "window_open": "交易窗口打开",
    "grid_started": "网格启动",
    "grid_restarted": "网格重启",
    "grid_start_failed": "网格启动失败",
    "grid_start_cleanup_pending": "网格启动失败，等待清理",
    "grid_calculation_failed": "网格计算失败",
    "observation_aborted": "观察期中止",
    "observation_aborted_force_close": "观察期中止并强制离场",
    "risk_close": "风控离场",
    "risk_cooldown": "进入冷静期",
    "session_stopped": "会话停止",
    "force_close_failed": "强制离场失败",
    "force_close_window": "交易窗口强制离场",
    "outside_window": "不在交易窗口",
    "startup_recovery": "启动恢复",
    "startup_recovery_failed": "启动恢复失败",
    "startup_recovery_force_close": "启动恢复强制离场",
    "startup_recovery_skipped_symbol": "启动恢复跳过不支持标的",
    "cooldown_recovered": "冷静期恢复",
    "cooldown_recovery_failed": "冷静期恢复失败",
    "cooldown_recovery_force_close_failed": "冷静期恢复后强制离场失败",
}

_MESSAGE_LABELS = {
    "Selection completed.": "选币完成。",
    "Binance testnet check completed.": "Binance 测试网检查完成。",
    "Binance maker fee health check completed.": "Binance 挂单费率健康检查完成。",
    "Binance testnet order/test smoke completed.": "Binance 测试网测试下单参数烟测完成。",
    "Binance testnet price stream smoke completed.": "Binance 测试网价格流烟测完成。",
    "Binance testnet listenKey smoke completed.": "Binance 测试网用户流密钥烟测完成。",
    "Binance testnet algo stop smoke completed.": "Binance 测试网条件止损单烟测完成。",
    "Binance testnet position smoke completed.": "Binance 测试网持仓只读烟测完成。",
    "Binance current-environment position check completed.": "Binance 当前环境持仓只读检查完成。",
    "Binance testnet market roundtrip smoke completed.": "Binance 测试网市价开平仓烟测完成。",
    "Binance direct REST order endpoint diagnose completed.": "Binance 直接 REST 下单端点诊断完成。",
    "Binance testnet safety sweep completed.": "Binance 测试网安全清扫完成。",
    "Binance testnet safety sweep left residual exposure.": "Binance 测试网安全清扫后仍有残留暴露。",
    "Binance testnet bounded run completed.": "Binance 测试网有界运行完成。",
    "Binance testnet bounded run failed after cleanup.": "Binance 测试网有界运行失败，已执行清理。",
    "Binance current-environment safety sweep completed.": "Binance 当前环境安全清扫完成。",
    "Binance current-environment safety sweep left residual exposure.": "Binance 当前环境安全清扫后仍有残留暴露。",
    "Binance current-environment bounded run completed.": "Binance 当前环境有界运行完成。",
    "Binance current-environment bounded run failed after cleanup.": "Binance 当前环境有界运行失败，已执行清理。",
    "Binance loop bounded runtime reached; shutting down.": "Binance 循环达到有界运行时长，正在关闭。",
    "Console action requested.": "控制台动作已请求。",
    "Console action completed.": "控制台动作已完成。",
    "Console action failed.": "控制台动作执行失败。",
    "New entries are paused by console control.": "控制台已暂停新开仓。",
    "Binance testnet client creation failed.": "Binance 测试网客户端创建失败。",
    "Grid calculation failed; symbol skipped.": "网格参数计算失败，已跳过该标的。",
    "Grid start failed; symbol skipped.": "网格启动失败，已跳过该标的。",
    "Grid start failed and cleanup is pending; session kept active for retry.": "网格启动失败且清理未完成，会话已保留等待重试。",
    "Startup recovery force close failed; session left unclosed.": "启动恢复时强制平仓失败，会话仍未关闭。",
    "Recovered unclosed session on startup.": "启动时已恢复未关闭会话。",
    "Skipped startup recovery for unsupported symbol.": "启动恢复已跳过当前交易所不支持的历史标的。",
    "Partial fill event has invalid details; closing session without recording trade.": "部分成交事件明细无效，已关闭会话且未记录成交。",
    "Exchange stop order fill details invalid; closing session without recording trade.": "交易所端止损成交明细无效，已关闭会话且未记录成交。",
    "Maker fee check failed; symbol skipped.": "挂单费率检查失败，已跳过该标的。",
    "Maker fee missing or invalid; symbol skipped.": "挂单费率缺失或无效，已跳过该标的。",
    "Maker fee exceeds configured limit; symbol skipped.": "挂单费率超过配置上限，已跳过该标的。",
    "Inferred filled order from exchange open-order reconciliation.": "通过交易所挂单对账推断订单已成交。",
    "Inferred partially filled order from exchange order reconciliation.": "通过交易所订单对账推断订单已部分成交。",
    "Position reconciliation failed; forcing close.": "持仓对账失败，正在强制平仓。",
    "Inactive position reconciliation failed; symbol skipped.": "非活跃持仓对账失败，已跳过该标的。",
    "Active session position mismatch detected.": "检测到活跃会话持仓不一致。",
    "Closed untracked exchange position.": "已关闭未跟踪的交易所持仓。",
    "Position tolerance invalid; closing untracked exchange position.": "持仓容差无效，正在关闭未跟踪持仓。",
    "Position tolerance invalid; no untracked position closed.": "持仓容差无效，未关闭未跟踪持仓。",
    "Force close failed; session kept active for retry.": "强制离场失败，会话保留以便重试。",
    "Force close failed after cooldown recovery failure.": "冷静期恢复失败后强制离场失败。",
    "Cooldown recovery failed; session stopped.": "冷静期恢复失败，会话已停止。",
    "Fill quantity exceeds local order quantity; closing session.": "成交数量超过本地订单数量，正在关闭会话。",
        "Refill post-only order rejected after fill.": "成交后的只挂单补单被拒绝。",
    "Refill failed after fill; closing session.": "成交后补单失败，正在关闭会话。",
    "Grid PnL input invalid; closing session.": "网格盈亏输入无效，正在关闭会话。",
    "Current volatility refresh failed.": "当前波动率刷新失败。",
}

_MESSAGE_LABELS.update(
    {
        "QuietGrid initialized in testnet-safe mode. Use --mock-once, --binance-check, --binance-order-smoke, --binance-test-order-smoke, --binance-market-roundtrip-smoke, --binance-direct-order-diagnose, --binance-price-stream-smoke, --binance-signed-write-health, --binance-listen-key-smoke, --binance-algo-stop-smoke, --binance-position-smoke, --binance-safety-sweep, --backtest-csv, --backtest-dir, --binance-once, --mock-loop or --binance-loop.": "QuietGrid 已按测试网安全模式初始化，可使用 CLI 参数运行检查、烟测、回测或循环任务。",
        "Failed to arm exchange stop protection after fill; closing session.": "成交后设置交易所止损保护失败，正在关闭会话。",
        "Partial fill detected; closing session because partial grid accounting is unsupported.": "检测到部分成交；当前不支持部分网格记账，正在关闭会话。",
        "Exchange stop order filled; closing session.": "交易所止损单已成交，正在关闭会话。",
        "Cancel all orders failed after grid order setup failure; grid orders may still be open.": "网格挂单初始化失败后全撤失败，可能仍有网格挂单。",
        "Exchange requires an open position before placing close-position stop orders; delayed stop protection will be armed after the first fill.": "交易所要求先有持仓才能挂平仓止损单，将在首次成交后补设止损保护。",
        "Cancel all orders failed after stop order setup failure; grid orders may still be open.": "止损单初始化失败后全撤失败，可能仍有网格挂单。",
        "Order lookup failed during reconciliation; keeping local order open.": "订单对账查询失败，本地订单保持未成交。",
        "Invalid exchange fill details during reconciliation; keeping local order open.": "订单对账中的交易所成交明细无效，本地订单保持未成交。",
        "Unknown exchange order status during reconciliation; keeping local order open.": "订单对账遇到未知交易所状态，本地订单保持未成交。",
        "Cancel all orders failed during force close; attempting position close anyway.": "强制离场时全撤失败，仍将尝试平仓。",
        "Binance price stream smoke timed out without receiving an event.": "Binance 价格流烟测超时，未收到事件。",
        "Binance price stream stopped unexpectedly.": "Binance 价格流异常停止。",
        "Maker fee changed.": "挂单费率已变化。",
        "Recovered filled order.": "已恢复成交订单。",
        "Position mismatch.": "持仓不一致。",
        "normal loop": "常规循环。",
        "system-ok": "系统正常。",
        "timeout": "请求超时",
        "user stream failed": "用户数据流失败",
        "price stream failed": "价格流失败",
        "loop failed": "循环失败",
        "signed write timeout": "签名写接口超时",
        "Timeout waiting for response from backend server. Send status unknown; execution status unknown.": "等待后端服务器响应超时；发送状态未知，执行状态未知。",
    }
)


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

    streamlit.title("QuietGrid 当前环境运维看板")
    streamlit.caption("只读页面，仅展示运行配置、当前环境验证、回测结果和最近数据库记录。")

    summary = repo.dashboard_summary()
    col1, col2, col3, col4 = streamlit.columns(4)
    col1.metric("活跃会话", summary["active_sessions"])
    col2.metric("未成交挂单", summary["open_orders"])
    col3.metric("已实现盈亏", f'{summary["realized_pnl"]:.4f}')
    col4.metric("最近系统消息", _compact_latest_message(summary["latest_system_message"]))

    _render_runtime_status_panel(streamlit, raw_config, database_path)
    _render_order_status_panel(streamlit, repo)
    _render_commission_health_panel(streamlit, repo)
    _render_active_volatility_panel(streamlit, repo)
    _render_environment_verification_panel(streamlit, repo)
    _render_backtest_report_panel(streamlit, Path("reports"))
    _render_alert_panel(streamlit, repo)
    _render_recent_data_panel(streamlit, repo)


def _render_runtime_status_panel(streamlit: Any, raw_config: dict, database_path: Path) -> None:
    streamlit.subheader("运行配置")
    summary = _runtime_config_summary(raw_config, database_path)
    col1, col2, col3, col4 = streamlit.columns(4)
    col1.metric("代理", "启用" if summary["proxy_enabled"] else "关闭", summary["proxy"])
    col2.metric("允许标的", summary["allowlist_count"], summary["allowlist_preview"])
    col3.metric("数据库", summary["database"])
    col4.metric("网页认证", "启用" if summary["auth_enabled"] else "关闭")
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
.block-container {
    max-width: 1440px;
    padding-top: 1.5rem;
    padding-bottom: 3rem;
}
h1 {
    font-size: 2rem !important;
    line-height: 1.25 !important;
    margin-bottom: 0.25rem !important;
    word-break: keep-all;
}
h3 {
    margin-top: 1.5rem !important;
}
[data-testid="stMetric"] {
    min-height: 92px;
    padding: 0.9rem 1rem;
    border: 1px solid rgba(15, 23, 42, 0.10);
    border-radius: 8px;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.98), rgba(248, 250, 252, 0.98));
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}
[data-testid="stMetricLabel"] p {
    color: #475569;
    font-size: 0.86rem;
    line-height: 1.35;
}
[data-testid="stMetricValue"] {
    color: #0f172a;
    font-size: 1.55rem;
    line-height: 1.25;
}
[data-testid="stDataFrame"] {
    border: 1px solid rgba(15, 23, 42, 0.10);
    border-radius: 8px;
    overflow: hidden;
}
[data-testid="stTabs"] button {
    min-height: 44px;
}
@media (max-width: 480px) {
    h1 {
        font-size: 1.5rem !important;
        line-height: 1.3 !important;
    }
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
    streamlit.subheader("挂单费率健康")
    health = repo.latest_commission_health()
    if not health:
        streamlit.caption("暂无挂单费率检查记录。")
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


def _render_active_volatility_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("活跃标的波动率")
    rows = _active_volatility_rows(repo.active_session_volatility_rows())
    if not rows:
        streamlit.caption("暂无活跃标的波动率。")
        return
    streamlit.dataframe(_localize_rows(rows, table="active_volatility"), use_container_width=True)


def _render_testnet_verification_panel(streamlit: Any, repo: Repository) -> None:
    _render_environment_verification_panel(streamlit, repo)


def _render_environment_verification_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("当前环境验证状态")
    streamlit.caption("基于系统日志的只读汇总；刷新页面不会触发任何 Binance API 调用。")
    latest_logs = repo.latest_system_logs_by_modules(_ENVIRONMENT_VERIFICATION_MODULES)
    rows = _environment_verification_rows(latest_logs)
    counts = {status: sum(1 for row in rows if row["verification_status"] == status) for status in _VERIFICATION_STATUS_LABELS}
    col1, col2, col3, col4 = streamlit.columns(4)
    col1.metric("通过", counts["passed"])
    col2.metric("警告/未知", counts["warning"] + counts["unknown"])
    col3.metric("失败", counts["failed"])
    col4.metric("未运行", counts["not_run"])
    streamlit.dataframe(_localize_rows(rows, table="testnet_verification"), use_container_width=True)


def _render_alert_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("近期风险/恢复事件")
    alerts = repo.recent_alert_events()
    if not alerts:
        streamlit.caption("暂无警告或错误事件。")
        return
    streamlit.dataframe(_localize_rows(alerts, table="system_logs"), use_container_width=True)


def _render_recent_data_panel(streamlit: Any, repo: Repository) -> None:
    streamlit.subheader("最近数据")
    tables = ["windows", "sessions", "orders", "trades", "state_logs", "system_logs"]
    tabs = streamlit.tabs([_TABLE_LABELS[table] for table in tables])
    for tab, table in zip(tabs, tables):
        with tab:
            rows = repo.recent_rows(table)
            if rows:
                streamlit.dataframe(_localize_rows(rows, table=table), use_container_width=True)
            else:
                streamlit.caption(f"暂无{_TABLE_LABELS[table]}。")


def _render_backtest_report_panel(streamlit: Any, report_dir: Path) -> None:
    streamlit.subheader("离线回测报告")
    reports = _load_recent_backtest_reports(report_dir)
    if not reports:
        streamlit.caption("暂无回测报告。运行回测时使用 --backtest-output reports/backtest.json 后会显示在这里。")
        return

    latest = reports[0]
    report = latest["report"]
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    streamlit.caption(f"最新报告：{latest['source_file']}，更新时间：{latest['report_modified_at']}")
    col1, col2, col3, col4, col5 = streamlit.columns(5)
    col1.metric("标的", summary.get("symbol", "-"))
    col2.metric("总盈亏", _format_metric(summary.get("total_pnl")))
    col3.metric("最大回撤", _format_metric(summary.get("max_drawdown")))
    col4.metric("成交次数", summary.get("fills", summary.get("total_fills", 0)))
    col5.metric("停止原因", _backtest_stop_reason_label(summary.get("stopped_reason")))

    streamlit.dataframe(_localize_rows([_backtest_summary_row(latest)]), use_container_width=True)

    grid_row = _backtest_grid_row(report)
    if grid_row:
        streamlit.markdown("**网格参数**")
        streamlit.dataframe(_localize_rows([grid_row]), use_container_width=True)

    fill_rows = _backtest_section_rows(report, "fills", limit=10)
    if fill_rows:
        streamlit.markdown("**最近成交**")
        streamlit.dataframe(_localize_rows(fill_rows), use_container_width=True)

    equity_rows = _backtest_section_rows(report, "equity_curve", limit=20)
    if equity_rows:
        streamlit.markdown("**权益曲线尾部**")
        streamlit.dataframe(_localize_rows(equity_rows), use_container_width=True)

    batch_rows = _backtest_section_rows(report, "reports", limit=10)
    if batch_rows:
        streamlit.markdown("**批量样本**")
        streamlit.dataframe(_localize_rows(batch_rows), use_container_width=True)

    error_rows = _backtest_section_rows(report, "errors", limit=10)
    if error_rows:
        streamlit.markdown("**失败样本**")
        streamlit.dataframe(_localize_rows(error_rows), use_container_width=True)


def _testnet_verification_rows(log_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _environment_verification_rows(log_rows)


def _environment_verification_rows(log_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_by_module: dict[str, dict[str, Any]] = {}
    for row in log_rows:
        module = str(row.get("module"))
        latest_by_module.setdefault(module, row)
    rows = []
    for module in _ENVIRONMENT_VERIFICATION_MODULES:
        log_row = latest_by_module.get(module)
        if log_row is None:
            rows.append(
                {
                    "verification_item": _ENVIRONMENT_VERIFICATION_LABELS[module],
                    "verification_status": "not_run",
                    "last_checked": "-",
                    "module": module,
                    "latest_message": "尚未运行。",
                    "detail_summary": "运行对应检查或烟测命令后会在这里显示最近结果。",
                }
            )
            continue
        detail = _parse_verification_detail(log_row.get("detail"))
        rows.append(
            {
                "verification_item": _ENVIRONMENT_VERIFICATION_LABELS[module],
                "verification_status": _testnet_verification_status(module, log_row, detail),
                "last_checked": log_row.get("log_time", "-") or "-",
                "module": module,
                "latest_message": log_row.get("message", "") or "-",
                "detail_summary": _testnet_verification_summary(module, detail),
            }
        )
    return rows


def _parse_verification_detail(detail: Any) -> dict[str, Any]:
    if detail is None or detail == "":
        return {}
    text = str(detail).strip()
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    key_values: dict[str, Any] = {}
    for part in _split_top_level_parts(text):
        key, separator, raw_value = part.partition("=")
        if not separator:
            continue
        value, parsed_ok = _parse_detail_literal(raw_value.strip())
        key_values[key.strip()] = value if parsed_ok else raw_value.strip()
    return key_values


def _testnet_verification_status(module: str, log_row: dict[str, Any], detail: dict[str, Any]) -> str:
    if _contains_unknown_execution_status(log_row) or _contains_unknown_execution_status(detail):
        return "unknown"
    if module == "commission_health":
        return {"ok": "passed", "warn": "warning", "error": "failed"}.get(str(detail.get("status", "")).lower(), _level_status(log_row))
    if module == "binance_signed_write_health":
        return "passed" if detail.get("signed_write_ok") is True else "failed"
    if module == "binance_test_order_smoke":
        if detail.get("test_order_ok") is True:
            return "passed" if detail.get("stop_supported", True) else "warning"
        return "failed"
    if module == "binance_price_stream_smoke":
        return "passed" if detail.get("stream_ok") is True else "failed"
    if module == "binance_listen_key_smoke":
        return "passed" if detail.get("listen_key_ok") is True else "failed"
    if module == "binance_position_smoke":
        if detail.get("position_smoke_ok") is not True:
            return "failed"
        return "warning" if _position_smoke_has_residual(detail) else "passed"
    if module == "binance_direct_order_diagnose":
        if detail.get("endpoint_order_ok") is True and detail.get("direct_order_diagnose_ok") is True:
            return "passed"
        if detail.get("direct_order_diagnose_ok") is True:
            return "unknown"
        return "failed"
    if module == "binance_algo_stop_smoke":
        if detail.get("algo_stop_ok") is not True:
            return "failed"
        return "passed" if _int_value(detail.get("remaining_open")) == 0 else "warning"
    if module == "binance_market_roundtrip_smoke":
        return "passed" if detail.get("market_roundtrip_ok") is True else "failed"
    if module == "binance_safety_sweep":
        if detail.get("safety_sweep_ok") is not True:
            return "failed"
        return "warning" if _safety_sweep_has_residual(detail) else "passed"
    return _level_status(log_row)


def _level_status(log_row: dict[str, Any]) -> str:
    level = str(log_row.get("level", "")).upper()
    if level == "ERROR":
        return "failed"
    if level in {"WARN", "WARNING"}:
        return "warning"
    return "passed"


def _contains_unknown_execution_status(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_unknown_execution_status(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unknown_execution_status(item) for item in value)
    if isinstance(value, (int, float)) and value == -1007:
        return True
    text = str(value).lower()
    return any(
        marker in text
        for marker in [
            "-1007",
            "send status unknown",
            "execution status unknown",
            "status unknown",
            "http 408",
            "timeout waiting for response from backend server",
        ]
    )


def _testnet_verification_summary(module: str, detail: dict[str, Any]) -> str:
    if not detail:
        return "-"
    if detail.get("error"):
        return _join_summary_parts(
            [
                _summary_part("阶段", _localize_scalar_text(str(detail.get("stage", ""))) if detail.get("stage") else None),
                _summary_part("错误", _localize_message(str(detail.get("error")))),
            ]
        )
    if module == "binance_check":
        return _join_summary_parts(
            [
                _summary_part("可交易标的", detail.get("symbols") or detail.get("tradable_symbols")),
                _summary_part("候选标的", detail.get("eligible") or detail.get("eligible_symbols")),
                _summary_part("样本", detail.get("sample") or detail.get("sample_symbol")),
                _summary_part("费率健康", _localize_scalar_text(str(detail.get("commission_health", ""))) if detail.get("commission_health") else None),
            ]
        )
    if module == "commission_health":
        return _join_summary_parts(
            [
                _summary_part("检查标的", detail.get("checked_symbols")),
                _summary_part("正常", detail.get("ok_count")),
                _summary_part("警告", detail.get("warn_count")),
                _summary_part("异常", detail.get("error_count")),
            ]
        )
    if module == "binance_signed_write_health":
        errors = detail.get("errors") if isinstance(detail.get("errors"), list) else []
        return _join_summary_parts(
            [
                _summary_part("调用入口", _localize_scalar_text(str(detail.get("caller"))) if detail.get("caller") else None),
                _summary_part("标的", detail.get("symbol")),
                _summary_part("错误数", len(errors) if errors else 0),
                _summary_part("首个错误", _localize_message(str(errors[0])) if errors else None),
            ]
        )
    if module == "binance_test_order_smoke":
        return _join_summary_parts(
            [
                _summary_part("标的", detail.get("symbol")),
                _summary_part("止损支持", _yes_no(detail.get("stop_supported")) if "stop_supported" in detail else None),
                _summary_part("止损错误", _localize_message(str(detail.get("stop_error"))) if detail.get("stop_error") else None),
            ]
        )
    if module == "binance_price_stream_smoke":
        event = detail.get("event") if isinstance(detail.get("event"), dict) else {}
        return _join_summary_parts(
            [
                _summary_part("标的", detail.get("symbol") or event.get("symbol")),
                _summary_part("价格", event.get("price") or event.get("p")),
                _summary_part("事件时间", event.get("event_time") or event.get("E")),
            ]
        )
    if module == "binance_listen_key_smoke":
        return _summary_part("用户流密钥长度", detail.get("listen_key_length")) or "-"
    if module == "binance_position_smoke":
        symbols = _verification_symbols(detail)
        residual_count = sum(1 for item in symbols if _position_symbol_has_residual(item))
        return _join_summary_parts(
            [
                _summary_part("检查标的", len(symbols)),
                _summary_part("双向持仓", _yes_no(detail.get("dual_side_position")) if "dual_side_position" in detail else None),
                _summary_part("存在暴露", residual_count),
            ]
        )
    if module == "binance_direct_order_diagnose":
        cleanup_errors = detail.get("cleanup_errors") if isinstance(detail.get("cleanup_errors"), list) else []
        return _join_summary_parts(
            [
                _summary_part("HTTP", detail.get("http_status")),
                _summary_part("端点下单", _yes_no(detail.get("endpoint_order_ok")) if "endpoint_order_ok" in detail else None),
                _summary_part("恢复订单", detail.get("recovered_order_id")),
                _summary_part("清理错误", len(cleanup_errors)),
            ]
        )
    if module == "binance_algo_stop_smoke":
        return _join_summary_parts(
            [
                _summary_part("标的", detail.get("symbol")),
                _summary_part("看到挂单", _yes_no(detail.get("open_seen")) if "open_seen" in detail else None),
                _summary_part("剩余挂单", detail.get("remaining_open")),
            ]
        )
    if module == "binance_market_roundtrip_smoke":
        return _join_summary_parts(
            [
                _summary_part("标的", detail.get("symbol")),
                _summary_part("开仓单", detail.get("open_order_id")),
                _summary_part("平仓单", detail.get("close_order_id")),
                _summary_part("平仓后暴露", _position_exposure(detail.get("position_after_close"))),
            ]
        )
    if module == "binance_safety_sweep":
        symbols = _verification_symbols(detail)
        residual_count = sum(1 for item in symbols if _safety_symbol_has_residual(item))
        closed_sessions = detail.get("closed_sessions")
        return _join_summary_parts(
            [
                _summary_part("清扫标的", len(symbols)),
                _summary_part("残留标的", residual_count),
                _summary_part("同步关闭会话", len(closed_sessions) if isinstance(closed_sessions, list) else None),
                _summary_part("残留说明", "; ".join(str(item) for item in detail.get("residuals", [])[:2]) if isinstance(detail.get("residuals"), list) else None),
            ]
        )
    return _generic_verification_summary(detail)


def _generic_verification_summary(detail: dict[str, Any]) -> str:
    parts = []
    for key, value in detail.items():
        if isinstance(value, (dict, list)):
            continue
        parts.append(_summary_part(_COLUMN_LABELS.get(str(key), str(key)), _localize_detail_value(value)))
        if len(parts) >= 4:
            break
    return _join_summary_parts(parts)


def _verification_symbols(detail: dict[str, Any]) -> list[dict[str, Any]]:
    symbols = detail.get("symbols")
    if not isinstance(symbols, list):
        return []
    return [item for item in symbols if isinstance(item, dict)]


def _position_smoke_has_residual(detail: dict[str, Any]) -> bool:
    return any(_position_symbol_has_residual(item) for item in _verification_symbols(detail))


def _position_symbol_has_residual(item: dict[str, Any]) -> bool:
    return (
        _float_abs(item.get("qty")) > 1e-12
        or _float_abs(item.get("long_qty")) > 1e-12
        or _float_abs(item.get("short_qty")) > 1e-12
        or _int_value(item.get("ordinary_open")) > 0
        or _int_value(item.get("algo_open")) > 0
    )


def _safety_sweep_has_residual(detail: dict[str, Any]) -> bool:
    residuals = detail.get("residuals")
    return bool(residuals) or any(_safety_symbol_has_residual(item) for item in _verification_symbols(detail))


def _safety_symbol_has_residual(item: dict[str, Any]) -> bool:
    return (
        _int_value(item.get("ordinary_after")) > 0
        or _int_value(item.get("algo_after")) > 0
        or _position_exposure(item.get("position_after")) > 1e-12
    )


def _position_exposure(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    return max(_float_abs(value.get("qty")), _float_abs(value.get("long_qty")), _float_abs(value.get("short_qty")))


def _float_abs(value: Any) -> float:
    try:
        return abs(float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _summary_part(label: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    return f"{label}: {value}"


def _join_summary_parts(parts: list[str | None]) -> str:
    compact = [part for part in parts if part]
    return "，".join(compact) if compact else "-"


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


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


def _active_volatility_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        result.append(
            {
                "session_id": row.get("session_id"),
                "symbol": row.get("symbol"),
                "state": row.get("state"),
                "volatility_method": row.get("volatility_method"),
                "volatility_value_pct": _percent_value(row.get("volatility_value")),
                "volatility_window": row.get("volatility_window"),
                "volatility_current_value_pct": _percent_value(row.get("volatility_current_value")),
                "volatility_current_window": row.get("volatility_current_window"),
                "volatility_current_at": row.get("volatility_current_at"),
                "grid_lower": row.get("grid_lower"),
                "grid_upper": row.get("grid_upper"),
                "grid_num": row.get("grid_num"),
                "step_pct": row.get("step_pct"),
                "baseline_atr": row.get("baseline_atr"),
            }
        )
    return result


def _percent_value(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value) * 100
    except (TypeError, ValueError):
        return None


def _load_recent_backtest_reports(report_dir: Path, limit: int = 3) -> list[dict[str, Any]]:
    if limit <= 0 or not report_dir.exists() or not report_dir.is_dir():
        return []
    loaded = []
    for path in sorted(report_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict) or not isinstance(report.get("summary"), dict):
            continue
        loaded.append(
            {
                "source_file": path.name,
                "report_modified_at": _format_file_mtime(path),
                "report": report,
            }
        )
        if len(loaded) >= limit:
            break
    return loaded


def _format_file_mtime(path: Path) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _backtest_summary_row(loaded_report: dict[str, Any]) -> dict[str, Any]:
    report = loaded_report.get("report", {})
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    row = {
        "source_file": loaded_report.get("source_file", "-"),
        "report_modified_at": loaded_report.get("report_modified_at", "-"),
    }
    if isinstance(summary, dict):
        for key in _BACKTEST_SUMMARY_KEYS:
            if key in summary:
                row[key] = summary[key]
    return row


def _backtest_grid_row(report: dict[str, Any]) -> dict[str, Any]:
    grid_params = report.get("grid_params")
    if not isinstance(grid_params, dict):
        return {}
    return {key: grid_params[key] for key in _BACKTEST_GRID_KEYS if key in grid_params}


def _backtest_section_rows(report: dict[str, Any], section: str, limit: int) -> list[dict[str, Any]]:
    rows = report.get(section)
    if limit <= 0 or not isinstance(rows, list):
        return []
    return [row for row in rows[-limit:] if isinstance(row, dict)]


def _format_metric(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _compact_latest_message(message: Any) -> str:
    text = _localize_message(str(message or "")) or "-"
    for prefix in ("Binance 测试网", "Binance 当前环境", "Binance "):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
    replacements = {
        "持仓只读烟测完成。": "持仓只读通过",
        "持仓只读检查完成。": "持仓只读通过",
        "价格流烟测完成。": "价格流通过",
        "用户流密钥烟测完成。": "用户流密钥通过",
        "条件止损单烟测完成。": "条件止损通过",
        "市价开平仓烟测完成。": "市价开平仓通过",
        "测试下单参数烟测完成。": "测试下单参数通过",
        "安全清扫完成。": "安全清扫完成",
        "有界运行完成。": "有界运行完成",
        "有界运行失败，已执行清理。": "有界运行失败",
        "检查完成。": "连接检查完成",
        "挂单费率健康检查完成。": "费率检查完成",
    }
    text = replacements.get(text, text.rstrip("。"))
    return text if len(text) <= 16 else f"{text[:15]}…"


def _backtest_stop_reason_label(value: Any) -> str:
    if value is None or value == "":
        return "-"
    text = str(value)
    return _VALUE_LABELS.get(text, text)


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
    if key in {"message", "latest_message"}:
        return _localize_message(text)
    if key == "trigger":
        return _TRIGGER_LABELS.get(text, text)
    if key == "detail":
        return _localize_detail(text)
    if key == "status" and text.lower() == "open":
        if table == "orders":
            return "未成交"
        if table == "windows":
            return "进行中"
        return "打开"
    if key in {
        "level",
        "status",
        "state",
        "from_state",
        "to_state",
        "side",
        "position_side",
        "stopped_reason",
        "recovered_order_status",
        "limit_status",
        "stop_status",
        "verification_status",
        "close_reason",
        "volatility_method",
    }:
        return _localize_scalar_text(text)
    if key in {"error", "reason", "stop_error"}:
        return _localize_message(text)
    return value


def _localize_detail(detail: str) -> str:
    if not detail:
        return ""
    stripped = detail.strip()
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError):
        return _localize_key_value_detail(stripped)
    return json.dumps(_localize_detail_value(parsed), ensure_ascii=False)


def _localize_detail_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {_COLUMN_LABELS.get(str(key), str(key)): _localize_detail_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_localize_detail_value(item) for item in value]
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return ""
    if isinstance(value, str):
        return _localize_scalar_text(value)
    return value


def _localize_key_value_detail(detail: str) -> str:
    parts = []
    changed = False
    for part in _split_top_level_parts(detail):
        key, separator, value = part.partition("=")
        if not separator:
            localized = _localize_message(part)
            changed = changed or localized != part
            parts.append(localized)
            continue
        key = key.strip()
        raw_value = value.strip()
        parsed, parsed_ok = _parse_detail_literal(raw_value)
        label = _detail_label(key, parsed if parsed_ok else raw_value)
        localized_value = _format_detail_value(parsed) if parsed_ok else _localize_scalar_text(raw_value)
        changed = changed or label != key or localized_value != raw_value
        parts.append(f"{label}={localized_value}")
    return "，".join(parts) if changed else _localize_message(detail)


def _split_top_level_parts(detail: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = set(pairs.values())
    for char in detail:
        current.append(char)
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in pairs:
            depth += 1
        elif char in closing and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            current.pop()
            parts.append("".join(current).strip())
            current = []
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _parse_detail_literal(value: str) -> tuple[Any, bool]:
    for parser in (json.loads, ast.literal_eval):
        try:
            return parser(value), True
        except (TypeError, ValueError, SyntaxError):
            continue
    return value, False


def _detail_label(key: str, value: Any) -> str:
    if key == "symbols" and not isinstance(value, (dict, list)):
        return "标的总数"
    return _COLUMN_LABELS.get(key, key)


def _format_detail_value(value: Any) -> str:
    localized = _localize_detail_value(value)
    if isinstance(localized, (dict, list)):
        return json.dumps(localized, ensure_ascii=False)
    return str(localized)


def _localize_scalar_text(text: str) -> str:
    return _VALUE_LABELS.get(text, _VALUE_LABELS.get(text.upper(), _VALUE_LABELS.get(text.lower(), _localize_message(text))))


def _localize_message(message: str) -> str:
    if not message:
        return ""
    if "�" in message:
        return "日志文本编码异常，无法显示原文。"
    if message in _MESSAGE_LABELS:
        return _MESSAGE_LABELS[message]
    if "Service unavailable from a restricted location according to 'b. Eligibility'" in message:
        return "Binance 服务拒绝当前代理出口地区；请切换到 Binance 测试网允许的代理节点后重试。"
    if (
        not message.startswith("APIError(code=")
        and "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead." in message
    ):
        return message.replace(
            "Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.",
            "当前端点不支持该订单类型，请改用 Algo Order API 端点。",
        )
    if message.startswith("APIError(code="):
        code_part, _, suffix = message.partition("):")
        code = code_part.removeprefix("APIError(code=").strip()
        detail = _localize_message(suffix.strip()) if suffix.strip() else "未提供错误详情。"
        return f"Binance API 错误（代码 {code}）：{detail}"
    if "Cannot connect to host " in message:
        _, _, rest = message.partition("Cannot connect to host ")
        host, separator, suffix = rest.partition(" ssl:")
        if separator:
            return f"无法连接到主机 {host}（ssl:{suffix}）。"
        return f"无法连接到主机 {rest}。"
    if "Server disconnected" in message:
        return "服务器已断开连接。"
    for prefix, label in {
        "set margin type failed:": "设置保证金模式失败：",
        "set leverage failed:": "设置杠杆失败：",
        "invalid maker commission:": "无效挂单费率：",
        "commission response missing maker": "费率响应缺少挂单费率",
        "request failed ": "请求失败：",
        "Market roundtrip smoke close left residual position:": "市价开平仓烟测平仓后仍有残留持仓：",
    }.items():
        if message.startswith(prefix):
            suffix = message[len(prefix) :].strip()
            return f"{label}{_localize_message(suffix) if suffix else ''}"
    signed_prefix = "Binance signed write health check "
    if message.startswith(signed_prefix):
        if " passed " in message:
            return "Binance 签名写接口预检通过。"
        if " failed " in message:
            return "Binance 签名写接口预检失败。"
    if message.startswith("Binance ") and message.endswith(" stopped unexpectedly."):
        task_name = message.removeprefix("Binance ").removesuffix(" stopped unexpectedly.")
        return f"Binance {_localize_scalar_text(task_name)}异常停止。"
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
