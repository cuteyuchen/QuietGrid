"""回测窗口分布/分析的无状态函数。

从 api.py 抽出，均为纯函数：仅依赖入参数据与已迁移的数值工具，
不触碰 repo/config/网络。
"""

from __future__ import annotations

from typing import Any

from api_support.metrics import _numeric_quantile


def _backtest_window_distribution(
    equity_curve: Any,
    window_rows: int,
    initial_equity: float,
) -> dict[str, Any]:
    if not isinstance(equity_curve, list) or not equity_curve:
        return {
            "status": "EMPTY",
            "source": "FIXED_ROWS",
            "window_rows": window_rows,
            "window_count": 0,
            "values": [],
        }
    values: list[float] = []
    previous_equity = float(initial_equity)
    for start in range(0, len(equity_curve), window_rows):
        window = equity_curve[start : start + window_rows]
        if not window:
            continue
        end_equity = float(window[-1].get("equity") or previous_equity)
        values.append(end_equity - previous_equity)
        previous_equity = end_equity
    return {
        "status": "COMPLETED",
        "source": "FIXED_ROWS",
        "window_rows": window_rows,
        "window_count": len(values),
        "positive_ratio": (
            sum(1 for value in values if value > 0) / len(values)
            if values
            else 0.0
        ),
        "p05": _numeric_quantile(values, 0.05),
        "p50": _numeric_quantile(values, 0.50),
        "p95": _numeric_quantile(values, 0.95),
        "worst": min(values, default=0.0),
        "best": max(values, default=0.0),
        "values": values,
    }


def _nyse_window_distribution(
    window_backtests: list[dict[str, Any]],
) -> dict[str, Any]:
    completed = [
        item
        for item in window_backtests
        if item.get("status") == "COMPLETED"
        and isinstance(item.get("summary"), dict)
    ]
    values = [
        float(item["summary"].get("total_pnl") or 0.0)
        for item in completed
    ]
    skipped_count = sum(
        1 for item in window_backtests if item.get("status") == "SKIPPED"
    )
    failed_count = sum(
        1 for item in window_backtests if item.get("status") == "FAILED"
    )
    return {
        "status": "COMPLETED" if values else "EMPTY",
        "source": "NYSE_WINDOWS",
        "window_rows": None,
        "window_count": len(values),
        "total_window_count": len(window_backtests),
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "positive_ratio": (
            sum(1 for value in values if value > 0) / len(values)
            if values
            else 0.0
        ),
        "p05": _numeric_quantile(values, 0.05),
        "p50": _numeric_quantile(values, 0.50),
        "p95": _numeric_quantile(values, 0.95),
        "worst": min(values, default=0.0),
        "best": max(values, default=0.0),
        "values": values,
    }


def _window_analysis(
    window_backtests: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if window_backtests is None:
        return {
            "status": "NOT_APPLICABLE",
            "source": "RAW_RANGE",
            "total_count": 0,
            "completed_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "reason_counts": {},
            "windows": [],
        }

    windows: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for entry in window_backtests:
        window = entry.get("window")
        window = window if isinstance(window, dict) else {}
        summary = entry.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        status = str(entry.get("status") or window.get("status") or "UNKNOWN")
        reason = str(
            window.get("skip_reason")
            or entry.get("error")
            or window.get("warning")
            or ""
        )
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        windows.append(
            {
                "window_id": window.get("window_id"),
                "market_close": window.get("market_close"),
                "force_close_at": window.get("force_close_at"),
                "row_count": int(window.get("row_count") or 0),
                "observation_rows": int(window.get("observation_rows") or 0),
                "tradable_rows": int(window.get("tradable_rows") or 0),
                "status": status,
                "skip_reason": window.get("skip_reason"),
                "reason": window.get("warning") or entry.get("error"),
                "total_pnl": summary.get("total_pnl"),
                "max_drawdown": summary.get("max_drawdown"),
                "fills": summary.get("fills"),
                "stopped_reason": summary.get("stopped_reason"),
            }
        )

    completed_count = sum(1 for item in windows if item["status"] == "COMPLETED")
    skipped_count = sum(1 for item in windows if item["status"] == "SKIPPED")
    failed_count = sum(1 for item in windows if item["status"] == "FAILED")
    status = "COMPLETED"
    if failed_count or skipped_count:
        status = "COMPLETED_WITH_EXCLUSIONS" if completed_count else "NO_VALID_WINDOWS"
    return {
        "status": status,
        "source": "NYSE_WINDOWS",
        "total_count": len(windows),
        "completed_count": completed_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "reason_counts": reason_counts,
        "windows": windows,
    }


def _backtest_row_time(row: dict[str, Any]) -> str | None:
    for key in (
        "available_time",
        "event_time",
        "timestamp",
        "open_time",
        "time",
        "close_time",
    ):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None
