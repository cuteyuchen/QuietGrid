"""数据库行 -> 前端响应的无状态 payload 构造函数。

从 api.py 抽出，均为纯函数：仅依赖入参 row/trades、web.py 的本地化辅助
与标准库，不触碰 repo/config/网络。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import web as legacy_web

from api_support.metrics import _optional_float


def _exchange_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": str(
            row.get("orderId")
            or row.get("order_id")
            or ""
        ),
        "client_id": str(
            row.get("clientOrderId")
            or row.get("origClientOrderId")
            or row.get("client_id")
            or ""
        ),
        "symbol": str(row.get("symbol") or ""),
        "side": str(row.get("side") or "").upper(),
        "price": _optional_float(row.get("price")),
        "qty": _optional_float(
            row.get("origQty")
            if row.get("origQty") not in (None, "")
            else row.get("qty")
        ),
        "executed_qty": _optional_float(row.get("executedQty")),
        "status": str(row.get("status") or "OPEN").upper(),
        "type": str(row.get("type") or ""),
        "reduce_only": bool(row.get("reduceOnly", False)),
        "update_time": row.get("updateTime") or row.get("time"),
    }


def _grid_round_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_id": int(row.get("window_id") or 0),
        "window_start": row.get("window_start"),
        "window_end": row.get("window_end"),
        "status": row.get("status"),
        "status_label": legacy_web._localize_scalar_text(str(row.get("status") or "")),
        "total_pnl": row.get("total_pnl"),
        "session_count": int(row.get("session_count") or 0),
        "active_session_count": int(row.get("active_session_count") or 0),
    }


def _round_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_id": int(row.get("window_id") or 0),
        "symbol": str(row.get("symbol") or ""),
        "rank": row.get("liquidity_rank"),
        "score": row.get("score"),
        "volume_score": row.get("volume_score"),
        "depth_score": row.get("depth_score"),
        "volume_24h": row.get("volume_24h"),
        "depth_usdt": row.get("depth_usdt"),
        "price": row.get("price"),
        "bid_price": row.get("bid_price"),
        "ask_price": row.get("ask_price"),
        "spread_pct": row.get("spread_pct"),
        "volatility_method": row.get("volatility_method"),
        "volatility_method_label": legacy_web._localize_scalar_text(str(row.get("volatility_method") or "")),
        "volatility_value": row.get("volatility_value"),
        "volatility_window": row.get("volatility_window"),
        "range_lower": row.get("range_lower"),
        "range_upper": row.get("range_upper"),
        "range_width_pct": row.get("range_width_pct"),
        "threshold_met": bool(row.get("threshold_met")),
        "selected": bool(row.get("session_id")),
        "disabled": False,
        "status": "stale" if row.get("data_stale") else "ok",
        "current_volatility": row.get("volatility_value"),
        "current_volatility_window": row.get("volatility_window"),
        "snapshot_at": row.get("calculated_at") or row.get("updated_at"),
        "session_id": row.get("session_id"),
        "stage": row.get("stage"),
        "error": row.get("error") or "",
        "market_state": row.get("market_state") or "",
        "verdict": row.get("verdict") or row.get("block_code") or "",
        "soft_breach_count": int(row.get("soft_breach_count") or 0),
        "grid_preview": row.get("grid_preview") or {},
        "economics": row.get("economics") or {},
        "maker_fee_rate": row.get("maker_fee_rate"),
        "maker_fee_source": row.get("maker_fee_source") or "",
        "maker_fee_checked_at": row.get("maker_fee_checked_at") or "",
        "regime_score": row.get("regime_score"),
        "regime_allowed": (
            None if row.get("regime_allowed") is None else bool(row.get("regime_allowed"))
        ),
        "block_code": row.get("block_code") or "",
        "block_reasons": row.get("block_reasons") or [],
        "kline_required_count": row.get("kline_required_count"),
        "kline_actual_count": row.get("kline_actual_count"),
        "kline_age_seconds": row.get("kline_age_seconds"),
        "kline_missing_count": row.get("kline_missing_count"),
        "kline_quality_status": row.get("kline_quality_status") or "",
        "last_kline_close_at": row.get("last_kline_close_at"),
        "market_updated_at": row.get("market_updated_at"),
        "calculated_at": row.get("calculated_at"),
        "data_stale": bool(row.get("data_stale")),
        "updated_at": row.get("updated_at"),
    }


def _order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "symbol": row.get("symbol"),
        "order_id": row.get("order_id"),
        "client_id": row.get("client_id"),
        "grid_index": row.get("grid_index"),
        "side": row.get("side"),
        "side_label": legacy_web._localize_scalar_text(str(row.get("side") or "")),
        "price": row.get("price"),
        "qty": row.get("qty"),
        "status": row.get("status"),
        "status_label": legacy_web._order_status_label(str(row.get("status") or "")),
        "entry_price": row.get("entry_price"),
        "created_at": row.get("created_at"),
        "filled_at": row.get("filled_at"),
        "fill_price": row.get("fill_price"),
        "position_side": row.get("position_side"),
        "order_intent": row.get("order_intent") or "OPEN",
        "updated_at": row.get("updated_at"),
    }


def _trade_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "symbol": row.get("symbol"),
        "order_id": row.get("order_id"),
        "side": row.get("side"),
        "side_label": legacy_web._localize_scalar_text(str(row.get("side") or "")),
        "price": row.get("price"),
        "qty": row.get("qty"),
        "quote_qty": row.get("quote_qty"),
        "grid_index": row.get("grid_index"),
        "grid_pnl": row.get("grid_pnl"),
        "fee": row.get("fee"),
        "funding_fee": row.get("funding_fee"),
        "trade_time": row.get("trade_time"),
        "created_at": row.get("created_at"),
    }


def _session_performance_payload(row: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: (str(item.get("trade_time") or ""), int(item.get("id") or 0)))
    gross_grid_pnl = sum(float(item.get("grid_pnl") or 0.0) for item in ordered if item.get("grid_pnl") is not None)
    trading_fees = sum(float(item.get("fee") or 0.0) for item in ordered)
    funding_fee = sum(float(item.get("funding_fee") or 0.0) for item in ordered)
    realized_pnl = float(row.get("realized_pnl") or 0.0)
    unpaired_pnl = realized_pnl - gross_grid_pnl + trading_fees - funding_fee
    capital = float(row.get("capital") or 0.0)
    roi = realized_pnl / capital if capital > 0 else None
    initial_margin = capital
    current_margin = max(0.0, capital + realized_pnl) if capital > 0 else None
    margin_change = (current_margin - initial_margin) if current_margin is not None else None
    duration_hours = _session_duration_hours(row)
    annualized_roi = roi * (24 * 365 / duration_hours) if roi is not None and duration_hours and duration_hours > 0 else None

    cumulative = 0.0
    curve = []
    for item in ordered:
        cumulative += float(item.get("grid_pnl") or 0.0)
        cumulative -= float(item.get("fee") or 0.0)
        cumulative += float(item.get("funding_fee") or 0.0)
        curve.append(
            {
                "time": item.get("trade_time"),
                "value": cumulative,
            }
        )

    return {
        "gross_grid_pnl": gross_grid_pnl,
        "trading_fees": trading_fees,
        "funding_fee": funding_fee,
        "realized_pnl": realized_pnl,
        "unpaired_pnl": unpaired_pnl,
        "initial_margin": initial_margin,
        "current_margin": current_margin,
        "margin_change": margin_change,
        "roi": roi,
        "annualized_roi": annualized_roi,
        "duration_hours": duration_hours,
        "trade_count": len(ordered),
        "unpaired_trade_count": sum(1 for item in ordered if item.get("grid_pnl") is None),
        "pnl_curve": curve[-80:],
    }


def _session_duration_hours(row: dict[str, Any]) -> float | None:
    try:
        opened_at = datetime.fromisoformat(str(row.get("open_time")))
    except (TypeError, ValueError):
        return None
    close_value = row.get("close_time")
    if close_value:
        try:
            closed_at = datetime.fromisoformat(str(close_value))
        except (TypeError, ValueError):
            closed_at = datetime.now(timezone.utc)
    else:
        closed_at = datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    return max(0.0, (closed_at.astimezone(timezone.utc) - opened_at.astimezone(timezone.utc)).total_seconds() / 3600)


def _system_log_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "time": row.get("log_time"),
        "level": row.get("level"),
        "level_label": legacy_web._localize_scalar_text(str(row.get("level") or "")),
        "module": row.get("module"),
        "module_label": legacy_web._localize_value("module", row.get("module")),
        "message": legacy_web._localize_message(str(row.get("message") or "")),
        "detail": legacy_web._localize_detail(str(row.get("detail") or "")) if row.get("detail") else "",
    }


def _verification_payload(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("verification_status") or "unknown")
    module = str(row.get("module") or "")
    return {
        "module": module,
        "name": row.get("verification_item") or legacy_web._ENVIRONMENT_VERIFICATION_LABELS.get(module, module),
        "status": status,
        "status_label": legacy_web._VERIFICATION_STATUS_LABELS.get(status, status),
        "last_checked": row.get("last_checked") or "",
        "latest_message": legacy_web._localize_message(str(row.get("latest_message") or "")),
        "detail": row.get("detail_summary") or "",
    }
