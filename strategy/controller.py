from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from core.models import GridOrder, GridState, OrderSide, OrderStatus, RiskAction, SymbolSession
from core.scheduler import Scheduler
from db.repository import Repository
from exchange.base import ExchangeClient
from strategy.cooldown import CooldownConfig, CooldownEvaluator
from strategy.grid_calculator import GridCalculationError, GridConfig
from strategy.grid_calculator import calculate_volatility_metric
from strategy.grid_engine import (
    ORDER_CREATE_RECOVERY_ATTEMPTS,
    ORDER_CREATE_RECOVERY_DELAY_SECONDS,
    GridEngine,
    _is_order_create_status_unknown,
    _is_post_only_rejection,
    _is_recovered_order,
    _response_order_id,
)
from strategy.observer import ObservationAborted, Observer, ObserverConfig
from strategy.risk import RiskConfig, RiskManager
from strategy.selector import SelectionConfig, Selector
from strategy.state_machine import StateMachine


@dataclass(frozen=True)
class ControllerConfig:
    capital_per_symbol: float
    leverage: int
    max_concurrent: int
    take_profit_usdt: float
    total_capital_limit: float
    max_maker_fee_rate: float = 0.0
    loop_interval_seconds: float = 10
    scheduler_check_minutes: float = 5


@dataclass(frozen=True)
class RunOnceResult:
    status: str
    selected_symbols: list[str]
    started_symbols: list[str]


@dataclass(frozen=True)
class StartupCheck:
    ok: bool
    reason: str
    balance: float


class TradingController:
    def __init__(
        self,
        exchange: ExchangeClient,
        scheduler: Scheduler,
        repository: Repository,
        selector_config: SelectionConfig,
        observer_config: ObserverConfig,
        grid_config: GridConfig,
        controller_config: ControllerConfig,
        cooldown_config: CooldownConfig | None = None,
    ) -> None:
        self.exchange = exchange
        self.scheduler = scheduler
        self.repository = repository
        self.selector = Selector(exchange, selector_config)
        self.observer = Observer(exchange, observer_config, grid_config)
        self.grid_config = grid_config
        self.cooldown = CooldownEvaluator(cooldown_config or CooldownConfig())
        self.engine = GridEngine(exchange, log_system=repository.log_system)
        self.state_machine = StateMachine()
        self.config = controller_config
        self.risk = RiskManager(
            scheduler,
            RiskConfig(
                take_profit_usdt=controller_config.take_profit_usdt,
                total_capital_limit=controller_config.total_capital_limit,
                max_concurrent=controller_config.max_concurrent,
            ),
        )
        self.active_sessions: dict[str, SymbolSession] = {}
        self.current_window_id: int | None = None
        self._volatility_refreshed_at: dict[int, datetime] = {}

    async def validate_startup(self, at: datetime | None = None) -> StartupCheck:
        current_time = at or datetime.now(timezone.utc)
        required_budget = self.config.capital_per_symbol * self.config.max_concurrent
        if (
            not _positive_finite_number(required_budget)
            or not _positive_finite_number(self.config.total_capital_limit)
        ):
            result = StartupCheck(False, "启动资金配置不是有效正数。", 0.0)
            self.repository.log_system(
                "ERROR",
                "controller",
                result.reason,
                (
                    f"capital_per_symbol={self.config.capital_per_symbol}, "
                    f"max_concurrent={self.config.max_concurrent}, "
                    f"total_capital_limit={self.config.total_capital_limit}"
                ),
                current_time,
            )
            return result
        if (
            not _positive_finite_number(self.config.leverage)
            or not _positive_finite_number(self.config.take_profit_usdt)
            or not _non_negative_finite_number(self.config.max_maker_fee_rate)
        ):
            result = StartupCheck(False, "启动交易参数不是有效数字。", 0.0)
            self.repository.log_system(
                "ERROR",
                "controller",
                result.reason,
                (
                    f"leverage={self.config.leverage}, "
                    f"take_profit_usdt={self.config.take_profit_usdt}, "
                    f"max_maker_fee_rate={self.config.max_maker_fee_rate}"
                ),
                current_time,
            )
            return result
        if required_budget > self.config.total_capital_limit:
            result = StartupCheck(False, "配置的并发本金超过总资金上限。", 0.0)
            self.repository.log_system("ERROR", "controller", result.reason, None, current_time)
            return result

        balance = await self.exchange.get_account_balance()
        try:
            balance = _non_negative_float(balance, "account balance")
        except ValueError as exc:
            result = StartupCheck(False, "账户可用余额不是有效数字。", 0.0)
            self.repository.log_system(
                "ERROR",
                "controller",
                result.reason,
                str(exc),
                current_time,
            )
            return result
        if balance < required_budget:
            result = StartupCheck(False, "账户可用余额低于配置并发所需本金。", balance)
            self.repository.log_system(
                "ERROR",
                "controller",
                result.reason,
                f"balance={balance}, required_budget={required_budget}",
                current_time,
            )
            return result

        result = StartupCheck(True, "启动前检查通过。", balance)
        self.repository.log_system(
            "INFO",
            "controller",
            result.reason,
            f"balance={balance}, required_budget={required_budget}",
            current_time,
        )
        return result

    async def recover_unclosed_sessions(
        self,
        at: datetime | None = None,
        recoverable_symbols: set[str] | None = None,
    ) -> list[int]:
        current_time = at or datetime.now(timezone.utc)
        recoverable = {symbol.upper() for symbol in recoverable_symbols} if recoverable_symbols is not None else None
        recovered: list[int] = []
        for row in self.repository.unclosed_sessions():
            raw_state = str(row["state"])
            symbol = str(row["symbol"])
            session_id = int(row["id"])
            if recoverable is not None and symbol.upper() not in recoverable:
                self.repository.close_session(session_id, "startup_recovery_skipped_symbol", current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    raw_state,
                    GridState.STOPPED.value,
                    "startup_recovery_skipped_symbol",
                    "当前交易所不支持该历史会话标的，启动恢复已跳过交易所平仓。",
                    current_time,
                )
                self.repository.log_system(
                    "WARN",
                    "controller",
                    "Skipped startup recovery for unsupported symbol.",
                    f"session_id={session_id}, symbol={symbol}",
                    current_time,
                )
                recovered.append(session_id)
                continue
            state = GridState(raw_state) if raw_state in {item.value for item in GridState} else GridState.CLOSING
            session = SymbolSession(
                session_id=session_id,
                symbol=symbol,
                state=state,
                params=None,
                orders=[],
                realized_pnl=float(row.get("realized_pnl") or 0.0),
                capital=float(row.get("capital") or self.config.capital_per_symbol),
                leverage=int(row.get("leverage") or self.config.leverage),
                open_time=current_time,
                state_entered_at=current_time,
            )
            try:
                await self.engine.force_close(session, "进程启动时发现未关闭会话，执行安全恢复。")
            except Exception as exc:
                self.repository.update_session_state(session.session_id, GridState.CLOSING.value)
                self.repository.log_state(
                    session.session_id,
                    session.symbol,
                    raw_state,
                    GridState.CLOSING.value,
                    "startup_recovery_failed",
                    str(exc),
                    current_time,
                )
                self.repository.log_system(
                    "ERROR",
                    "controller",
                    "Startup recovery force close failed; session left unclosed.",
                    f"session_id={session.session_id}, symbol={session.symbol}, error={exc}",
                    current_time,
                )
                raise
            self.repository.close_session(session.session_id, "startup_recovery_force_close", current_time)
            self.repository.log_state(
                session.session_id,
                session.symbol,
                raw_state,
                GridState.STOPPED.value,
                "startup_recovery",
                "进程启动时发现未关闭会话，已撤单并尝试平仓。",
                current_time,
            )
            self.repository.log_system(
                "WARN",
                "controller",
                "Recovered unclosed session on startup.",
                f"session_id={session.session_id}, symbol={session.symbol}",
                current_time,
            )
            recovered.append(session.session_id)
        return recovered

    async def run_once(self, now: datetime | None = None) -> RunOnceResult:
        current_time = now or datetime.now(timezone.utc)
        if self.scheduler.should_force_close(current_time):
            if self.active_sessions:
                await self.close_all_active_sessions("临近盘前，触发全局强制离场。", current_time)
            else:
                await self.reconcile_positions_once(current_time, include_inactive=True)
            return RunOnceResult("force_close_window", [], [])
        if not self.scheduler.is_in_window(current_time):
            if self.active_sessions:
                await self.close_all_active_sessions("不在休市交易窗口，触发全局强制离场。", current_time)
            else:
                await self.reconcile_positions_once(current_time, include_inactive=True)
            return RunOnceResult("outside_window", [], [])
        await self.reconcile_positions_once(current_time, include_inactive=True)
        if self.repository.new_entries_paused():
            self.repository.log_system(
                "INFO",
                "controller",
                "New entries are paused by console control.",
                None,
                current_time,
            )
            return RunOnceResult("new_entries_paused", [], [])

        selected = await self.selector.select()
        selected_symbols = [item.symbol for item in selected]
        self._log_selection(selected, current_time)
        if not selected_symbols:
            return RunOnceResult("no_symbols", [], [])
        disabled_symbols = self.repository.disabled_symbols()
        enabled_symbols = [symbol for symbol in selected_symbols if symbol.upper() not in disabled_symbols]
        if disabled_symbols:
            skipped_symbols = [symbol for symbol in selected_symbols if symbol.upper() in disabled_symbols]
            if skipped_symbols:
                self.repository.log_system(
                    "INFO",
                    "controller",
                    "Disabled symbols skipped before opening new grids.",
                    json.dumps({"symbols": skipped_symbols}, ensure_ascii=False),
                    current_time,
                )
        if not enabled_symbols:
            return RunOnceResult("all_symbols_disabled", selected_symbols, [])
        fee_eligible_symbols = await self._filter_symbols_by_maker_fee(enabled_symbols, current_time)
        if not fee_eligible_symbols:
            return RunOnceResult("no_fee_eligible", selected_symbols, [])

        window_id = self.repository.create_window(current_time)
        self.current_window_id = window_id
        started: list[str] = []
        for symbol in fee_eligible_symbols[: self.config.max_concurrent]:
            allowed = self.risk.can_open_new_symbol(list(self.active_sessions.values()), self.config.capital_per_symbol)
            if allowed.action != RiskAction.NONE:
                continue
            session_id = self.repository.create_session(
                window_id=window_id,
                symbol=symbol,
                state=GridState.OBSERVING.value,
                capital=self.config.capital_per_symbol,
                leverage=self.config.leverage,
                open_time=current_time,
            )
            self.state_machine.transition(symbol, GridState.OBSERVING, "window_open", at=current_time)
            self.repository.log_state(
                session_id,
                symbol,
                GridState.IDLE.value,
                GridState.OBSERVING.value,
                "window_open",
                None,
                current_time,
            )
            current_price = await self._current_price(symbol)
            try:
                params = await self.observer.observe_then_calculate(
                    symbol,
                    current_price,
                    should_abort=lambda: self.scheduler.should_force_close(),
                )
            except ObservationAborted:
                self.repository.close_session(session_id, "observation_aborted_force_close", current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    GridState.OBSERVING.value,
                    GridState.STOPPED.value,
                    "observation_aborted",
                    "观察期内触发强制离场，未建仓。",
                    current_time,
                )
                if self.active_sessions:
                    await self.close_all_active_sessions("observation_aborted_force_close", current_time)
                else:
                    self._close_current_window(current_time, status="aborted")
                return RunOnceResult("observation_aborted", selected_symbols, started)
            except GridCalculationError as exc:
                self.repository.close_session(session_id, "grid_calculation_failed", current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    GridState.OBSERVING.value,
                    GridState.STOPPED.value,
                    "grid_calculation_failed",
                    str(exc),
                    current_time,
                )
                self.repository.log_system(
                    "WARN",
                    "controller",
                    "Grid calculation failed; symbol skipped.",
                    f"symbol={symbol}, reason={exc}",
                    current_time,
                )
                continue
            self._persist_session_grid(session_id, params)
            session = SymbolSession(
                session_id=session_id,
                symbol=symbol,
                state=GridState.RUNNING,
                params=params,
                orders=[],
                realized_pnl=0.0,
                capital=self.config.capital_per_symbol,
                leverage=self.config.leverage,
                open_time=current_time,
                state_entered_at=current_time,
            )
            try:
                await self.engine.start(session, current_price)
            except Exception as exc:
                self._persist_session_orders(session)
                if any(order.status == OrderStatus.OPEN for order in session.orders):
                    session.state = GridState.CLOSING
                    session.state_entered_at = current_time
                    self.active_sessions[symbol] = session
                    self.repository.update_session_state(session_id, GridState.CLOSING.value)
                    self.state_machine.transition(symbol, GridState.CLOSING, "grid_start_cleanup_pending", str(exc), current_time)
                    self.repository.log_state(
                        session_id,
                        symbol,
                        GridState.OBSERVING.value,
                        GridState.CLOSING.value,
                        "grid_start_cleanup_pending",
                        str(exc),
                        current_time,
                    )
                    self.repository.log_system(
                        "ERROR",
                        "controller",
                        "Grid start failed and cleanup is pending; session kept active for retry.",
                        f"symbol={symbol}, reason={exc}",
                        current_time,
                    )
                else:
                    self.repository.close_session(session_id, "grid_start_failed", current_time)
                    self.repository.log_state(
                        session_id,
                        symbol,
                        GridState.OBSERVING.value,
                        GridState.STOPPED.value,
                        "grid_start_failed",
                        str(exc),
                        current_time,
                    )
                    self.repository.log_system(
                        "ERROR",
                        "controller",
                        "Grid start failed; symbol skipped.",
                        f"symbol={symbol}, reason={exc}",
                        current_time,
                    )
                continue
            self._persist_session_orders(session)
            self.active_sessions[symbol] = session
            self.state_machine.transition(symbol, GridState.RUNNING, "grid_started", at=current_time)
            self.repository.update_session_state(session_id, GridState.RUNNING.value)
            self.repository.log_state(
                session_id,
                symbol,
                GridState.OBSERVING.value,
                GridState.RUNNING.value,
                "grid_started",
                f"grid_num={params.grid_num}, step_pct={params.step_pct}",
                current_time,
            )
            started.append(symbol)

        if not started:
            if self.active_sessions:
                return RunOnceResult("cleanup_pending", selected_symbols, started)
            self._close_current_window(current_time, status="skipped")
            return RunOnceResult("no_started", selected_symbols, started)

        return RunOnceResult("started", selected_symbols, started)

    async def _filter_symbols_by_maker_fee(self, symbols: list[str], at: datetime) -> list[str]:
        eligible: list[str] = []
        for symbol in symbols:
            try:
                commission = await self.exchange.get_commission_rate(symbol)
            except Exception as exc:
                self.repository.log_system(
                    "ERROR",
                    "commission",
                    "Maker fee check failed; symbol skipped.",
                    f"symbol={symbol}, reason={exc}",
                    at,
                )
                continue
            try:
                maker_fee = _non_negative_float(_required_float(commission, "maker"), "maker")
            except (TypeError, ValueError) as exc:
                self.repository.log_system(
                    "ERROR",
                    "commission",
                    "Maker fee missing or invalid; symbol skipped.",
                    f"symbol={symbol}, reason={exc}",
                    at,
                )
                continue
            if maker_fee > self.config.max_maker_fee_rate:
                self.repository.log_system(
                    "WARN",
                    "commission",
                    "Maker fee exceeds configured limit; symbol skipped.",
                    f"symbol={symbol}, maker={maker_fee}, max={self.config.max_maker_fee_rate}",
                    at,
                )
                continue
            eligible.append(symbol)
        return eligible

    async def handle_order_filled_event(self, event: dict[str, Any]) -> GridOrder | None:
        symbol = str(event["symbol"])
        client_id = str(event["client_id"])
        session = self.active_sessions.get(symbol)
        if session is None:
            return None
        status = str(event.get("status", "FILLED")).upper()
        if status == "PARTIALLY_FILLED":
            await self._handle_partial_fill_event(session, event)
            return None
        if status != "FILLED":
            return None

        filled_order = next((order for order in session.orders if order.client_id == client_id), None)
        if filled_order is None:
            if _is_session_stop_order_client_id(session, client_id):
                await self._handle_stop_order_filled_event(session, event)
            return None
        if filled_order.status == OrderStatus.FILLED and filled_order.fill_price is not None:
            return None

        price = _positive_price(event.get("price", filled_order.price), "fill price")
        qty = _positive_qty(event.get("qty", filled_order.qty), "fill qty")
        trade_time = event.get("trade_time")
        if not isinstance(trade_time, datetime):
            trade_time = datetime.now(timezone.utc)

        if qty > 0 and qty + 1e-12 < filled_order.qty:
            if filled_order.status == OrderStatus.FILLED and filled_order.fill_price is None:
                filled_order.status = OrderStatus.OPEN
                filled_order.filled_at = None
            partial_event = dict(event)
            partial_event["status"] = "PARTIALLY_FILLED"
            partial_event.setdefault("side", filled_order.side.value)
            await self._handle_partial_fill_event(session, partial_event)
            return None

        if qty > filled_order.qty + 1e-12:
            self.repository.log_system(
                "ERROR",
                "order_event",
                "Fill quantity exceeds local order quantity; closing session.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": symbol,
                        "client_id": client_id,
                        "order_id": event.get("order_id", filled_order.order_id),
                        "event_qty": qty,
                        "order_qty": filled_order.qty,
                    },
                    ensure_ascii=False,
                ),
                trade_time,
            )
            await self._close_session(session, "成交数量超过本地订单数量，执行安全平仓。", trade_time)
            return None

        try:
            grid_pnl = self.engine.grid_pnl_for_fill(filled_order, price)
        except ValueError as exc:
            self.repository.log_system(
                "ERROR",
                "grid_pnl",
                "Grid PnL input invalid; closing session.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": symbol,
                        "client_id": client_id,
                        "order_id": event.get("order_id", filled_order.order_id),
                        "price": price,
                        "qty": qty,
                        "entry_price": filled_order.entry_price,
                        "order_qty": filled_order.qty,
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                trade_time,
            )
            await self._close_session(session, "网格收益计算输入异常，执行安全平仓。", trade_time)
            return None
        new_order = None
        refill_error: Exception | None = None
        try:
            new_order = await self.engine.handle_order_filled(session, client_id, fill_price=price)
        except Exception as exc:
            refill_error = exc
        self.repository.upsert_order(session.session_id, filled_order)
        if new_order is not None:
            self.repository.upsert_order(session.session_id, new_order)
        self.repository.create_trade(
            session_id=session.session_id,
            symbol=symbol,
            order_id=str(event.get("order_id", filled_order.order_id)),
            side=filled_order.side.value,
            price=price,
            qty=qty,
            grid_index=filled_order.grid_index,
            grid_pnl=grid_pnl,
            trade_time=trade_time,
        )
        if grid_pnl is not None:
            session.realized_pnl += grid_pnl
            self.repository.update_session_pnl(session.session_id, session.realized_pnl)
        if refill_error is not None and _is_post_only_rejection(refill_error):
            self.repository.log_system(
                "WARN",
                "grid_engine",
                "Refill post-only order rejected after fill.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": symbol,
                        "client_id": client_id,
                        "order_id": event.get("order_id", filled_order.order_id),
                        "reason": str(refill_error),
                    },
                    ensure_ascii=False,
                ),
                trade_time,
            )
        elif refill_error is not None:
            self.repository.log_system(
                "ERROR",
                "grid_engine",
                "Refill failed after fill; closing session.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": symbol,
                        "client_id": client_id,
                        "order_id": event.get("order_id", filled_order.order_id),
                        "reason": str(refill_error),
                    },
                    ensure_ascii=False,
                ),
                trade_time,
            )
            await self._close_session(session, "成交后补单失败，执行安全平仓。", trade_time)
            return None
        try:
            await self.engine.ensure_stop_protection_for_position(session, self._expected_position_qty(session))
        except Exception as exc:
            self.repository.log_system(
                "ERROR",
                "risk",
                "Failed to arm exchange stop protection after fill; closing session.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": symbol,
                        "client_id": client_id,
                        "order_id": event.get("order_id", filled_order.order_id),
                        "expected_position_qty": self._expected_position_qty(session),
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                trade_time,
            )
            await self._close_session(session, "成交后交易所端止损保护失败，执行安全平仓。", trade_time)
            return None
        return new_order

    async def _handle_partial_fill_event(self, session: SymbolSession, event: dict[str, Any]) -> None:
        event_time = event.get("trade_time")
        if not isinstance(event_time, datetime):
            event_time = datetime.now(timezone.utc)
        partial_order = next((order for order in session.orders if order.client_id == str(event.get("client_id", ""))), None)
        price: float | None = None
        qty: float | None = None
        invalid_detail: str | None = None
        try:
            price = _positive_price(event.get("price", partial_order.price if partial_order is not None else 0.0), "partial fill price")
            qty = _positive_qty(event.get("qty", 0.0), "partial fill qty")
            if partial_order is not None and qty > partial_order.qty + 1e-12:
                invalid_detail = "partial fill qty exceeds local order qty"
        except ValueError as exc:
            invalid_detail = str(exc)
        order_id = str(event.get("order_id", partial_order.order_id if partial_order is not None else event.get("client_id", "")))
        trade_id = str(event.get("trade_id", ""))
        trade_order_id = f"{order_id}:{trade_id}" if trade_id else order_id
        if (
            invalid_detail is None
            and price is not None
            and qty is not None
            and not self.repository.trade_exists(session.session_id, trade_order_id)
        ):
            self.repository.create_trade(
                session_id=session.session_id,
                symbol=session.symbol,
                order_id=trade_order_id,
                side=partial_order.side.value if partial_order is not None else str(event.get("side", "")),
                price=price,
                qty=qty,
                grid_index=partial_order.grid_index if partial_order is not None else None,
                grid_pnl=None,
                trade_time=event_time,
            )
        if invalid_detail is not None:
            self.repository.log_system(
                "ERROR",
                "partial_fill",
                "Partial fill event has invalid details; closing session without recording trade.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "client_id": event.get("client_id"),
                        "order_id": event.get("order_id"),
                        "side": event.get("side"),
                        "price": event.get("price"),
                        "qty": event.get("qty"),
                        "reason": invalid_detail,
                    },
                    ensure_ascii=False,
                ),
                event_time,
            )
        self.repository.log_system(
            "WARN",
            "partial_fill",
            "Partial fill detected; closing session because partial grid accounting is unsupported.",
            json.dumps(
                {
                    "session_id": session.session_id,
                    "symbol": session.symbol,
                    "client_id": event.get("client_id"),
                    "order_id": event.get("order_id"),
                    "side": event.get("side"),
                    "price": event.get("price"),
                    "qty": event.get("qty"),
                },
                ensure_ascii=False,
            ),
            event_time,
        )
        await self._close_session(session, "检测到部分成交，当前版本不支持部分成交网格补单，执行安全平仓。", event_time)

    async def _handle_stop_order_filled_event(self, session: SymbolSession, event: dict[str, Any]) -> None:
        trade_time = event.get("trade_time")
        if not isinstance(trade_time, datetime):
            trade_time = datetime.now(timezone.utc)
        price: float | None = None
        qty: float | None = None
        invalid_detail: str | None = None
        try:
            price = _positive_price(event.get("price", 0.0), "stop fill price")
            qty = _positive_qty(event.get("qty", 0.0), "stop fill qty")
        except ValueError as exc:
            invalid_detail = str(exc)
        order_id = str(event.get("order_id", event.get("client_id", "")))
        if price is not None and qty is not None and not self.repository.trade_exists(session.session_id, order_id):
            self.repository.create_trade(
                session_id=session.session_id,
                symbol=session.symbol,
                order_id=order_id,
                side=str(event.get("side", "")),
                price=price,
                qty=qty,
                grid_index=None,
                grid_pnl=None,
                trade_time=trade_time,
            )
        if invalid_detail is not None:
            self.repository.log_system(
                "ERROR",
                "risk",
                "Exchange stop order fill details invalid; closing session without recording trade.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "client_id": event.get("client_id"),
                        "order_id": event.get("order_id"),
                        "side": event.get("side"),
                        "price": event.get("price"),
                        "qty": event.get("qty"),
                        "reason": invalid_detail,
                    },
                    ensure_ascii=False,
                ),
                trade_time,
            )
        self.repository.log_system(
            "WARN",
            "risk",
            "Exchange stop order filled; closing session.",
            json.dumps(
                {
                    "session_id": session.session_id,
                    "symbol": session.symbol,
                    "client_id": event.get("client_id"),
                    "order_id": event.get("order_id"),
                    "side": event.get("side"),
                    "price": price if price is not None else event.get("price"),
                    "qty": qty if qty is not None else event.get("qty"),
                },
                ensure_ascii=False,
            ),
            trade_time,
        )
        await self._close_session(session, "交易所端止损单成交，关闭会话。", trade_time)

    async def handle_price_update_event(self, event: dict[str, Any]) -> str | None:
        symbol = str(event["symbol"])
        session = self.active_sessions.get(symbol)
        if session is None:
            return None
        event_time = event.get("event_time")
        if not isinstance(event_time, datetime):
            event_time = datetime.now(timezone.utc)
        decision = self.risk.evaluate_symbol(session, _positive_price(event["price"], "price event"), event_time)
        if decision.action == RiskAction.NONE:
            return None
        await self._apply_risk_decision(session, decision.action, decision.reason, event_time)
        return decision.action.value

    async def poll_active_sessions_once(self, now: datetime | None = None) -> list[tuple[str, str]]:
        current_time = now or datetime.now(timezone.utc)
        actions: list[tuple[str, str]] = []
        processed_symbols: set[str] = set()
        stop_requests = self.repository.pending_session_stop_requests()
        for symbol, session in list(self.active_sessions.items()):
            processed_symbols.add(symbol)
            stop_request = stop_requests.get(session.session_id)
            if stop_request is not None:
                action = await self._apply_session_stop_request(session, stop_request, current_time)
                actions.append((symbol, action))
                continue
            if self.scheduler.should_force_close(current_time):
                await self._close_session(session, "临近盘前，触发全局强制离场。", current_time)
                actions.append((symbol, RiskAction.FORCE_CLOSE.value))
                continue
            if not self.scheduler.is_in_window(current_time):
                await self._close_session(session, "不在休市交易窗口，触发全局强制离场。", current_time)
                actions.append((symbol, RiskAction.FORCE_CLOSE.value))
                continue
            if session.state == GridState.CLOSING:
                if await self._close_session(session, "继续清理待关闭会话。", current_time):
                    actions.append((symbol, "close_retry"))
                else:
                    actions.append((symbol, "close_retry_failed"))
                continue
            if session.state == GridState.COOLDOWN:
                recovered = await self._try_recover_from_cooldown(session, current_time)
                if recovered:
                    actions.append((symbol, "recovered"))
                continue
            sync_result = await self.engine.sync_orders(session)
            filled_handled = False
            for event in sync_result.filled:
                order = event.order
                self.repository.log_system(
                    "WARN",
                    "order_reconciliation",
                    "Inferred filled order from exchange open-order reconciliation.",
                    json.dumps(
                        {
                            "session_id": session.session_id,
                            "symbol": session.symbol,
                            "order_id": order.order_id,
                            "client_id": order.client_id,
                            "price": event.price,
                            "qty": event.qty,
                        },
                        ensure_ascii=False,
                    ),
                    current_time,
                )
                await self.handle_order_filled_event(
                    {
                        "symbol": session.symbol,
                        "client_id": order.client_id,
                        "price": event.price,
                        "qty": event.qty,
                        "order_id": order.order_id,
                        "trade_time": current_time,
                    }
                )
                filled_handled = True
            if filled_handled:
                actions.append((symbol, "filled_reconciled"))
            partial_fill_handled = False
            for event in sync_result.partially_filled:
                order = event.order
                self.repository.log_system(
                    "WARN",
                    "order_reconciliation",
                    "Inferred partially filled order from exchange order reconciliation.",
                    json.dumps(
                        {
                            "session_id": session.session_id,
                            "symbol": session.symbol,
                            "order_id": order.order_id,
                            "client_id": order.client_id,
                            "price": event.price,
                            "qty": event.qty,
                        },
                        ensure_ascii=False,
                    ),
                    current_time,
                )
                await self.handle_order_filled_event(
                    {
                        "symbol": session.symbol,
                        "client_id": order.client_id,
                        "status": "PARTIALLY_FILLED",
                        "price": event.price,
                        "qty": event.qty,
                        "order_id": order.order_id,
                        "side": order.side.value,
                        "trade_time": current_time,
                    }
                )
                actions.append((symbol, "partial_fill_reconciled"))
                partial_fill_handled = True
                break
            if partial_fill_handled:
                continue
            if symbol not in self.active_sessions:
                continue
            self._persist_session_orders(session)
            try:
                reconcile_action = await self._reconcile_active_session_position(session, current_time)
            except Exception as exc:
                self.repository.log_system(
                    "ERROR",
                    "position_reconciliation",
                    "Position reconciliation failed; forcing close.",
                    f"session_id={session.session_id}, symbol={symbol}, error={exc}",
                    current_time,
                )
                await self._close_session(session, "持仓对账异常，强制同步平仓。", current_time)
                actions.append((symbol, "position_reconciliation_failed"))
                continue
            if reconcile_action is not None:
                actions.append((symbol, reconcile_action))
                await self._close_session(session, "持仓对账异常，强制同步平仓。", current_time)
                continue
            await self._refresh_session_current_volatility_if_due(session, current_time)
            last_price = await self._current_price(symbol)
            decision = self.risk.evaluate_symbol(session, last_price, current_time)
            if decision.action == RiskAction.NONE:
                continue
            await self._apply_risk_decision(session, decision.action, decision.reason, current_time)
            actions.append((symbol, decision.action.value))
        if not any(session.state == GridState.CLOSING for session in self.active_sessions.values()):
            actions.extend(await self._reconcile_inactive_positions_once(current_time, processed_symbols))
        return actions

    async def run_loop(
        self,
        max_iterations: int | None = None,
        sleep_fn=asyncio.sleep,
    ) -> list[str]:
        statuses: list[str] = []
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            if self.active_sessions:
                actions = await self.poll_active_sessions_once()
                statuses.append("poll:" + ",".join(f"{symbol}:{action}" for symbol, action in actions))
                await sleep_fn(self.config.loop_interval_seconds)
                continue

            result = await self.run_once()
            statuses.append(result.status)
            sleep_seconds = (
                self.config.loop_interval_seconds
                if result.status not in {"outside_window", "force_close_window"}
                else self.config.scheduler_check_minutes * 60
            )
            await sleep_fn(sleep_seconds)
        return statuses

    async def reconcile_positions_once(
        self,
        now: datetime | None = None,
        include_inactive: bool = False,
    ) -> list[tuple[str, str]]:
        current_time = now or datetime.now(timezone.utc)
        actions: list[tuple[str, str]] = []
        for symbol, session in list(self.active_sessions.items()):
            try:
                action = await self._reconcile_active_session_position(session, current_time)
            except Exception as exc:
                actions.append((symbol, "position_reconciliation_failed"))
                self.repository.log_system(
                    "ERROR",
                    "position_reconciliation",
                    "Position reconciliation failed; forcing close.",
                    f"session_id={session.session_id}, symbol={symbol}, error={exc}",
                    current_time,
                )
                await self._close_session(session, "持仓对账异常，强制同步平仓。", current_time)
                continue
            if action is not None:
                actions.append((symbol, action))
                await self._close_session(session, "持仓对账异常，强制同步平仓。", current_time)

        if include_inactive:
            actions.extend(await self._reconcile_inactive_positions_once(current_time))
        return actions

    async def _reconcile_inactive_positions_once(
        self,
        current_time: datetime,
        exclude_symbols: set[str] | None = None,
    ) -> list[tuple[str, str]]:
        actions: list[tuple[str, str]] = []
        active_symbols = set(self.active_sessions) | (exclude_symbols or set())
        for symbol in await self.selector.candidate_symbols():
            if symbol in active_symbols:
                continue
            try:
                position = await self.exchange.get_position(symbol)
                close_specs = _position_close_specs(position)
            except Exception as exc:
                self.repository.log_system(
                    "ERROR",
                    "position_reconciliation",
                    "Inactive position reconciliation failed; symbol skipped.",
                    f"symbol={symbol}, error={exc}",
                    current_time,
                )
                continue
            exposure_qty = sum(qty for _side, qty, _position_side in close_specs)
            try:
                tolerance = await self._position_tolerance(symbol)
            except Exception as exc:
                if exposure_qty <= 1e-12:
                    self.repository.log_system(
                        "ERROR",
                        "position_reconciliation",
                        "Position tolerance invalid; no untracked position closed.",
                        f"symbol={symbol}, {_position_log_detail(position)}, error={exc}",
                        current_time,
                    )
                    actions.append((symbol, "position_reconciliation_failed"))
                    continue
                await self._close_position_specs(symbol, close_specs)
                self.repository.log_system(
                    "ERROR",
                    "position_reconciliation",
                    "Position tolerance invalid; closing untracked exchange position.",
                    json.dumps(
                        {
                            "symbol": symbol,
                            **_position_log_fields(position),
                            "close_specs": _position_close_specs_log(close_specs),
                            "error": str(exc),
                        },
                        ensure_ascii=False,
                    ),
                    current_time,
                )
                actions.append((symbol, "closed_untracked_position"))
                continue
            if exposure_qty <= tolerance:
                continue
            await self._close_position_specs(symbol, close_specs)
            self.repository.log_system(
                "WARN",
                "position_reconciliation",
                "Closed untracked exchange position.",
                json.dumps(
                    {
                        "symbol": symbol,
                        **_position_log_fields(position),
                        "close_specs": _position_close_specs_log(close_specs),
                    },
                    ensure_ascii=False,
                ),
                current_time,
            )
            actions.append((symbol, "closed_untracked_position"))
        return actions

    async def _close_position_specs(self, symbol: str, specs: list[tuple[str, float, str | None]]) -> None:
        for side, qty, position_side in specs:
            client_id = _reconciliation_close_client_id(symbol, side, position_side)
            _response_order_id(
                await self._place_market_order_reconciled(
                    symbol,
                    side,
                    qty,
                    reduce_only=True,
                    position_side=position_side,
                    client_id=client_id,
                ),
                client_id,
            )

    async def _place_market_order_reconciled(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool,
        position_side: str | None,
        client_id: str,
    ) -> dict:
        try:
            return await self.exchange.place_market_order(
                symbol,
                side,
                qty,
                reduce_only=reduce_only,
                position_side=position_side,
                client_id=client_id,
            )
        except Exception as exc:
            recovered = await self._recover_order_by_client_id_after_create_exception(symbol, client_id, exc)
            if recovered is not None:
                return recovered
            raise

    async def _recover_order_by_client_id_after_create_exception(
        self,
        symbol: str,
        client_id: str,
        exc: Exception,
    ) -> dict | None:
        attempts = ORDER_CREATE_RECOVERY_ATTEMPTS if _is_order_create_status_unknown(exc) else 1
        delay_seconds = ORDER_CREATE_RECOVERY_DELAY_SECONDS if attempts > 1 else 0
        for attempt in range(max(1, attempts)):
            try:
                order = await self.exchange.get_order(symbol, "", client_id)
            except Exception:
                order = None
            if order is not None and _is_recovered_order(order, client_id):
                return order
            if attempt < attempts - 1 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
        return None

    async def close_all_active_sessions(self, reason: str, at: datetime | None = None) -> list[str]:
        current_time = at or datetime.now(timezone.utc)
        closed: list[str] = []
        for symbol, session in list(self.active_sessions.items()):
            if await self._close_session(session, reason, current_time):
                closed.append(symbol)
        return closed

    async def _apply_session_stop_request(
        self,
        session: SymbolSession,
        request: dict[str, Any],
        at: datetime,
    ) -> str:
        reason = str(request.get("reason") or "控制台手动停止网格")
        request_id = str(request.get("request_id") or "")
        detail = json.dumps(
            {
                "session_id": session.session_id,
                "symbol": session.symbol,
                "request_id": request_id,
                "reason": reason,
            },
            ensure_ascii=False,
        )
        self.repository.log_system(
            "WARN",
            "console_action",
            "Session stop request is being applied.",
            detail,
            at,
        )
        closed = await self._close_session(session, f"控制台手动停止网格：{reason}", at)
        if closed:
            self.repository.update_session_stop_request(
                session.session_id,
                "completed",
                "已撤单并尝试同步平仓。",
                at,
            )
            self.repository.log_system(
                "INFO",
                "console_action",
                "Session stop request completed.",
                detail,
                at,
            )
            return "manual_stop"
        self.repository.update_session_stop_request(
            session.session_id,
            "closing",
            "停止请求已执行但清理未完成，下一轮继续重试。",
            at,
        )
        return "manual_stop_pending"

    async def _apply_risk_decision(self, session: SymbolSession, action: RiskAction, reason: str, at: datetime) -> None:
        if action in {RiskAction.FORCE_CLOSE, RiskAction.CLOSE}:
            await self._close_session(session, reason, at)
            return
        if action == RiskAction.COOLDOWN:
            await self._enter_cooldown(session, reason, at)

    async def _reconcile_active_session_position(self, session: SymbolSession, at: datetime) -> str | None:
        position = await self.exchange.get_position(session.symbol)
        tolerance = await self._position_tolerance(session.symbol)
        if _has_hedge_position_fields(position):
            actual_long_qty, actual_short_qty = _position_hedge_qty(position)
            expected_long_qty, expected_short_qty = self._expected_position_sides(session)
            if (
                abs(actual_long_qty - expected_long_qty) <= tolerance
                and abs(actual_short_qty - expected_short_qty) <= tolerance
            ):
                return None
            self.repository.log_system(
                "WARN",
                "position_reconciliation",
                "Active session position mismatch detected.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "expected_long_qty": expected_long_qty,
                        "expected_short_qty": expected_short_qty,
                        "actual_long_qty": actual_long_qty,
                        "actual_short_qty": actual_short_qty,
                        "tolerance": tolerance,
                    },
                    ensure_ascii=False,
                ),
                at,
            )
            return "position_mismatch"

        actual_qty = _position_qty(position)
        expected_qty = self._expected_position_qty(session)
        if abs(actual_qty - expected_qty) <= tolerance:
            return None
        self.repository.log_system(
            "WARN",
            "position_reconciliation",
            "Active session position mismatch detected.",
            json.dumps(
                {
                    "session_id": session.session_id,
                    "symbol": session.symbol,
                    "expected_qty": expected_qty,
                    "actual_qty": actual_qty,
                    "tolerance": tolerance,
                },
                ensure_ascii=False,
            ),
            at,
        )
        return "position_mismatch"

    async def _position_tolerance(self, symbol: str) -> float:
        rules = await self.exchange.get_symbol_rules(symbol)
        min_qty = _non_negative_float(rules.get("min_qty", 0.0), "min_qty")
        step_size = _non_negative_float(rules.get("step_size", 0.0), "step_size")
        return max(min_qty, step_size, 1e-12)

    @staticmethod
    def _expected_position_qty(session: SymbolSession) -> float:
        qty = 0.0
        for order in session.orders:
            if order.status != OrderStatus.FILLED:
                continue
            qty += order.qty if order.side == OrderSide.BUY else -order.qty
        return qty

    @staticmethod
    def _expected_position_sides(session: SymbolSession) -> tuple[float, float]:
        long_qty = 0.0
        short_qty = 0.0
        for order in session.orders:
            if order.status != OrderStatus.FILLED:
                continue
            if order.side == OrderSide.BUY:
                if order.entry_price is None:
                    long_qty += order.qty
                else:
                    short_qty -= order.qty
            else:
                if order.entry_price is None:
                    short_qty += order.qty
                else:
                    long_qty -= order.qty
        return max(0.0, long_qty), max(0.0, short_qty)

    async def _enter_cooldown(self, session: SymbolSession, reason: str, at: datetime) -> None:
        await self.engine.force_close(session, reason)
        self._persist_session_orders(session)
        old_state = session.state
        session.state = GridState.COOLDOWN
        session.state_entered_at = at
        self.repository.update_session_state(session.session_id, GridState.COOLDOWN.value)
        self.state_machine.transition(session.symbol, GridState.COOLDOWN, "risk_cooldown", reason, at)
        self.repository.log_state(
            session.session_id,
            session.symbol,
            old_state.value,
            GridState.COOLDOWN.value,
            "risk_cooldown",
            reason,
            at,
        )

    async def _try_recover_from_cooldown(self, session: SymbolSession, at: datetime) -> bool:
        if session.params is None or session.state_entered_at is None:
            return False
        klines = await self.exchange.get_klines(
            session.symbol,
            self.observer.observer_config.kline_interval,
            self.cooldown.config.calm_window_minutes,
        )
        decision = self.cooldown.evaluate(
            klines,
            baseline_atr=session.params.baseline_atr,
            min_step_pct=self.grid_config.min_step_pct,
            cooldown_started_at=session.state_entered_at,
            now=at,
        )
        if not decision.can_reobserve:
            return False

        old_state = session.state
        session.state = GridState.OBSERVING
        session.state_entered_at = at
        self.repository.update_session_state(session.session_id, GridState.OBSERVING.value)
        self.state_machine.transition(session.symbol, GridState.OBSERVING, "cooldown_recovered", decision.reason, at)
        self.repository.log_state(
            session.session_id,
            session.symbol,
            old_state.value,
            GridState.OBSERVING.value,
            "cooldown_recovered",
            decision.reason,
            at,
        )

        current_price = await self._current_price(session.symbol)
        try:
            params = await self.observer.collect_and_calculate(session.symbol, current_price)
            session.params = params
            session.orders.clear()
            self._persist_session_grid(session.session_id, params)
            await self.engine.start(session, current_price)
        except Exception as exc:
            await self._stop_after_cooldown_recovery_failure(session, exc, at)
            return False
        self._persist_session_orders(session)
        session.state = GridState.RUNNING
        session.state_entered_at = at
        self.repository.update_session_state(session.session_id, GridState.RUNNING.value)
        self.state_machine.transition(session.symbol, GridState.RUNNING, "grid_restarted", at=at)
        self.repository.log_state(
            session.session_id,
            session.symbol,
            GridState.OBSERVING.value,
            GridState.RUNNING.value,
            "grid_restarted",
            f"grid_num={params.grid_num}, step_pct={params.step_pct}",
            at,
        )
        return True

    async def _stop_after_cooldown_recovery_failure(self, session: SymbolSession, exc: Exception, at: datetime) -> None:
        reason = "cooldown_recovery_failed"
        try:
            await self.engine.force_close(session, "冷静期恢复失败，执行安全停止。")
        except Exception as close_exc:
            self._persist_session_orders(session)
            old_state = session.state
            session.state = GridState.CLOSING
            session.state_entered_at = at
            self.repository.update_session_state(session.session_id, GridState.CLOSING.value)
            if old_state != GridState.CLOSING:
                self.state_machine.transition(
                    session.symbol,
                    GridState.CLOSING,
                    "cooldown_recovery_force_close_failed",
                    str(close_exc),
                    at,
                )
                self.repository.log_state(
                    session.session_id,
                    session.symbol,
                    old_state.value,
                    GridState.CLOSING.value,
                    "cooldown_recovery_force_close_failed",
                    str(close_exc),
                    at,
                )
            self.repository.log_system(
                "ERROR",
                "controller",
                "Force close failed after cooldown recovery failure.",
                f"symbol={session.symbol}, recovery_error={exc}, close_error={close_exc}",
                at,
            )
            return
        self._persist_session_orders(session)
        old_state = session.state
        session.state = GridState.STOPPED
        session.state_entered_at = at
        self.repository.close_session(session.session_id, reason, at)
        self.repository.log_state(
            session.session_id,
            session.symbol,
            old_state.value,
            GridState.STOPPED.value,
            reason,
            str(exc),
            at,
        )
        self.state_machine.transition(session.symbol, GridState.STOPPED, reason, str(exc), at)
        self.repository.log_system(
            "ERROR",
            "controller",
            "Cooldown recovery failed; session stopped.",
            f"session_id={session.session_id}, symbol={session.symbol}, reason={exc}",
            at,
        )
        self.active_sessions.pop(session.symbol, None)
        if not self.active_sessions:
            self._close_current_window(at)

    async def _close_session(self, session: SymbolSession, reason: str, at: datetime) -> bool:
        if self._session_is_already_stopped(session):
            self._finalize_already_stopped_session(session, at)
            return True
        try:
            await self.engine.force_close(session, reason)
        except Exception as exc:
            self._persist_session_orders(session)
            old_state = session.state
            if old_state != GridState.CLOSING:
                self.state_machine.transition(session.symbol, GridState.CLOSING, "force_close_failed", str(exc), at)
                self.repository.log_state(
                    session.session_id,
                    session.symbol,
                    old_state.value,
                    GridState.CLOSING.value,
                    "force_close_failed",
                    str(exc),
                    at,
                )
            session.state = GridState.CLOSING
            session.state_entered_at = at
            self.repository.update_session_state(session.session_id, GridState.CLOSING.value)
            self.repository.log_system(
                "ERROR",
                "controller",
                "Force close failed; session kept active for retry.",
                f"session_id={session.session_id}, symbol={session.symbol}, reason={reason}, error={exc}",
                at,
            )
            return False
        if self._session_is_already_stopped(session):
            self._finalize_already_stopped_session(session, at)
            return True
        self._persist_session_orders(session)
        old_state = session.state
        if old_state != GridState.CLOSING:
            self.state_machine.transition(session.symbol, GridState.CLOSING, "risk_close", reason, at)
            self.repository.log_state(
                session.session_id,
                session.symbol,
                old_state.value,
                GridState.CLOSING.value,
                "risk_close",
                reason,
                at,
            )
        session.state = GridState.STOPPED
        self.repository.close_session(session.session_id, reason, at)
        self.repository.log_state(
            session.session_id,
            session.symbol,
            GridState.CLOSING.value,
            GridState.STOPPED.value,
            "session_stopped",
            reason,
            at,
        )
        self.state_machine.transition(session.symbol, GridState.STOPPED, "session_stopped", reason, at)
        self.active_sessions.pop(session.symbol, None)
        if not self.active_sessions:
            self._close_current_window(at)
        return True

    def _session_is_already_stopped(self, session: SymbolSession) -> bool:
        return session.state == GridState.STOPPED or self.state_machine.get_state(session.symbol) == GridState.STOPPED

    def _finalize_already_stopped_session(self, session: SymbolSession, at: datetime) -> None:
        session.state = GridState.STOPPED
        session.state_entered_at = at
        self._persist_session_orders(session)
        self.active_sessions.pop(session.symbol, None)
        if not self.active_sessions:
            self._close_current_window(at)

    async def _current_price(self, symbol: str) -> float:
        ticker = await self.exchange.get_24h_ticker(symbol)
        return _ticker_last_price(ticker)

    def _persist_session_orders(self, session: SymbolSession) -> None:
        for order in session.orders:
            self.repository.upsert_order(session.session_id, order)

    def _persist_session_grid(self, session_id: int, params) -> None:
        self.repository.update_session_grid(
            session_id,
            params.upper,
            params.lower,
            params.grid_num,
            params.step_pct,
            params.baseline_atr,
            params.stop_loss_price,
            params.volatility_method,
            params.volatility_value,
            params.volatility_window,
        )
        self.repository.update_session_current_volatility(
            session_id,
            params.volatility_value,
            params.volatility_window,
            params.calculated_at,
        )
        self._volatility_refreshed_at[session_id] = params.calculated_at

    async def _refresh_session_current_volatility_if_due(self, session: SymbolSession, at: datetime) -> None:
        if session.params is None:
            return
        last_refreshed = self._volatility_refreshed_at.get(session.session_id)
        if last_refreshed is not None:
            elapsed = (at - last_refreshed).total_seconds()
            if elapsed < self.grid_config.volatility_refresh_seconds:
                return
        limit = max(
            int(self.observer.observer_config.observe_hours * 60),
            self.observer.observer_config.min_samples,
        )
        try:
            klines = await self.exchange.get_klines(
                session.symbol,
                self.observer.observer_config.kline_interval,
                limit,
            )
            _method, volatility_value, volatility_window = calculate_volatility_metric(
                klines,
                replace(self.grid_config, min_samples=self.observer.observer_config.min_samples),
            )
        except Exception as exc:
            self.repository.log_system(
                "WARN",
                "volatility",
                "Current volatility refresh failed.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "method": session.params.volatility_method,
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                at,
            )
            return
        self.repository.update_session_current_volatility(
            session.session_id,
            volatility_value,
            volatility_window,
            at,
        )
        self._volatility_refreshed_at[session.session_id] = at

    def _close_current_window(self, at: datetime, status: str = "closed") -> None:
        if self.current_window_id is None:
            return
        self.repository.close_window(self.current_window_id, at, status=status)
        self.current_window_id = None

    def _log_selection(self, selected, at: datetime) -> None:
        detail = [
            {
                "symbol": item.symbol,
                "score": item.score,
                "volume_score": item.volume_score,
                "depth_score": item.depth_score,
                "volume_24h": item.volume_24h,
                "depth_usdt": item.depth_usdt,
            }
            for item in selected
        ]
        self.repository.log_system(
            "INFO",
            "selector",
            "Selection completed.",
            json.dumps(detail, ensure_ascii=False),
            at,
        )


def _ticker_last_price(ticker: dict[str, Any]) -> float:
    for key in ("lastPrice", "last_price", "price", "bidPrice"):
        value = ticker.get(key)
        if value is not None:
            return _positive_price(value, key)
    raise ValueError("ticker 缺少当前价格字段。")


def _position_qty(position: dict[str, Any]) -> float:
    for key in ("qty", "positionAmt"):
        value = position.get(key)
        if value not in (None, ""):
            return _finite_float(value, key)
    raise ValueError("持仓响应缺少数量字段。")


def _has_hedge_position_fields(position: dict[str, Any]) -> bool:
    return "long_qty" in position or "short_qty" in position


def _position_hedge_qty(position: dict[str, Any]) -> tuple[float, float]:
    long_qty = _non_negative_float(position.get("long_qty", 0.0), "long_qty")
    short_qty = _non_negative_float(position.get("short_qty", 0.0), "short_qty")
    return long_qty, short_qty


def _position_close_specs(position: dict[str, Any]) -> list[tuple[str, float, str | None]]:
    if _has_hedge_position_fields(position):
        long_qty, short_qty = _position_hedge_qty(position)
        specs: list[tuple[str, float, str | None]] = []
        if long_qty > 0:
            specs.append((OrderSide.SELL.value, long_qty, "LONG"))
        if short_qty > 0:
            specs.append((OrderSide.BUY.value, short_qty, "SHORT"))
        return specs

    actual_qty = _position_qty(position)
    qty = abs(actual_qty)
    if qty <= 0:
        return []
    side = OrderSide.SELL.value if actual_qty > 0 else OrderSide.BUY.value
    return [(side, qty, None)]


def _reconciliation_close_client_id(symbol: str, side: str, position_side: str | None) -> str:
    symbol_part = "".join(char.lower() for char in str(symbol) if char.isalnum())[:20] or "symbol"
    close_side = str(position_side or side).lower()
    return f"qgr-{symbol_part}-{close_side}"


def _position_log_fields(position: dict[str, Any]) -> dict[str, Any]:
    fields = {"actual_qty": _position_qty(position)}
    if _has_hedge_position_fields(position):
        long_qty, short_qty = _position_hedge_qty(position)
        fields["actual_long_qty"] = long_qty
        fields["actual_short_qty"] = short_qty
    return fields


def _position_log_detail(position: dict[str, Any]) -> str:
    fields = _position_log_fields(position)
    return ", ".join(f"{key}={value}" for key, value in fields.items())


def _position_close_specs_log(specs: list[tuple[str, float, str | None]]) -> list[dict[str, Any]]:
    return [
        {"side": side, "qty": qty, "position_side": position_side}
        for side, qty, position_side in specs
    ]


def _required_float(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if value in (None, ""):
        raise ValueError(f"缺少 {key} 字段")
    return _finite_float(value, key)


def _positive_price(value: Any, label: str) -> float:
    price = _finite_float(value, label)
    if price <= 0:
        raise ValueError(f"{label} 必须大于 0。")
    return price


def _positive_qty(value: Any, label: str) -> float:
    qty = _finite_float(value, label)
    if qty <= 0:
        raise ValueError(f"{label} 必须大于 0。")
    return qty


def _non_negative_float(value: Any, label: str) -> float:
    number = _finite_float(value, label)
    if number < 0:
        raise ValueError(f"{label} 必须为非负数。")
    return number


def _positive_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0


def _non_negative_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number >= 0


def _finite_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效数字。") from exc
    if not isfinite(number):
        raise ValueError(f"{label} 不是有限数字。")
    return number


def _is_session_stop_order_client_id(session: SymbolSession, client_id: str) -> bool:
    prefixes = (
        f"qg-{session.session_id}-stop-long",
        f"qg-{session.session_id}-stop-short",
    )
    return any(client_id == prefix or client_id.startswith(f"{prefix}-") for prefix in prefixes)
