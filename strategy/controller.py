from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from core.models import (
    GridDirectionMode,
    GridOrder,
    GridParams,
    GridState,
    OrderIntent,
    OrderSide,
    OrderStatus,
    RiskAction,
    SymbolSession,
)
from core.scheduler import Scheduler
from db.repository import Repository
from exchange.base import ExchangeClient
from strategy.cooldown import CooldownConfig, CooldownEvaluator
from strategy.adaptive_grid import (
    AdaptiveGridConfig,
    AdaptiveGridGenerator,
    GridEconomicsError,
)
from strategy.grid_calculator import SUPPORTED_RANGE_METHODS, GridCalculationError, GridConfig
from strategy.grid_calculator import calculate_grid_params, calculate_volatility_metric
from strategy.grid_engine import (
    ORDER_CREATE_RECOVERY_ATTEMPTS,
    ORDER_CREATE_RECOVERY_DELAY_SECONDS,
    GridEngine,
    _is_order_create_status_unknown,
    _is_post_only_rejection,
    _is_recovered_order,
    _response_order_id,
    GridEngineConfig,
)
from data_sources.runtime_market import assert_runtime_provider
from strategy.market_history import RecentMarketHistoryService
from strategy.observer import ObservationAborted, Observer, ObserverConfig
from strategy.inventory import InventoryAction, InventoryConfig, InventoryManager, InventorySnapshot
from strategy.regime import RegimeConfig, RegimeDecision, RegimeEngine
from strategy.risk import RiskConfig, RiskManager
from strategy.selector import SelectionConfig, Selector
from strategy.state_machine import StateMachine

# 无状态辅助函数拆分模块；重新导出以保持 `strategy.controller._xxx` 访问方式不变。
from strategy.controller_support import (
    _closed_klines_as_of,
    _finite_float,
    _has_hedge_position_fields,
    _is_session_stop_order_client_id,
    _kline_data_age_seconds,
    _non_negative_finite_number,
    _non_negative_float,
    _orderbook_liquidity,
    _position_close_specs,
    _position_close_specs_log,
    _position_exposure,
    _position_hedge_qty,
    _position_log_detail,
    _position_log_fields,
    _position_qty,
    _positive_finite_number,
    _positive_price,
    _positive_qty,
    _reconciliation_close_client_id,
    _required_float,
    _response_client_id_or_none,
    _summarize_exchange_trades,
    _ticker_last_price,
)


@dataclass(frozen=True)
class ControllerConfig:
    capital_per_symbol: float
    leverage: int
    max_concurrent: int
    take_profit_usdt: float
    total_capital_limit: float
    max_maker_fee_rate: float = 0.0
    maker_fee_check_interval_seconds: float = 300.0
    loop_interval_seconds: float = 10
    scheduler_check_minutes: float = 5
    effective_leverage_cap: float = float("inf")
    max_session_loss_pct: float = 0.0
    max_window_loss_pct: float = 0.0
    max_symbol_inventory_pct: float = 0.10
    max_consecutive_session_losses: int = 0
    max_window_stop_count: int = 0
    block_risk_increase_hot_reload: bool = True
    direction_mode: GridDirectionMode = GridDirectionMode.NEUTRAL
    direction_overrides: dict[str, GridDirectionMode] = field(default_factory=dict)
    grid_range_multiplier_by_symbol: dict[str, float] = field(default_factory=dict)
    grid_min_step_pct_by_symbol: dict[str, float] = field(default_factory=dict)
    max_unpaired_lots_per_side_by_symbol: dict[str, int] = field(default_factory=dict)
    reduce_target_step_fraction_by_symbol: dict[str, float] = field(default_factory=dict)
    seed_execution: str = "MARKET"
    seed_max_slippage_pct: float = 0.002


@dataclass(frozen=True)
class V2FeatureFlags:
    regime_v2: bool = False
    inventory_manager: bool = False
    adaptive_grid_v2: bool = False
    risk_manager_v2: bool = False


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


class RegimeAdmissionError(GridCalculationError):
    """网格已经完成求解，但最终 Regime 准入未通过。"""

    def __init__(self, message: str, *, params: GridParams) -> None:
        super().__init__(message)
        self.params = params


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
        feature_flags: V2FeatureFlags | None = None,
        regime_config: RegimeConfig | None = None,
        adaptive_grid_config: AdaptiveGridConfig | None = None,
        inventory_config: InventoryConfig | None = None,
    ) -> None:
        self.exchange = assert_runtime_provider(exchange)
        self.market_history = RecentMarketHistoryService(
            self.exchange,
            max_data_age_seconds=float(getattr(regime_config or RegimeConfig(), "max_data_age_seconds", 90.0)),
        )
        self.scheduler = scheduler
        self.repository = repository
        self.selector = Selector(exchange, selector_config)
        self.observer = Observer(exchange, observer_config, grid_config)
        self.grid_config = grid_config
        self.cooldown = CooldownEvaluator(cooldown_config or CooldownConfig())
        self.engine = GridEngine(
            exchange,
            GridEngineConfig(
                seed_max_slippage_pct=controller_config.seed_max_slippage_pct,
                reduce_target_step_fraction_by_symbol=(
                    controller_config.reduce_target_step_fraction_by_symbol
                ),
            ),
            log_system=repository.log_system,
        )
        self.state_machine = StateMachine()
        self.config = controller_config
        self.feature_flags = feature_flags or V2FeatureFlags()
        self.regime = RegimeEngine(regime_config or RegimeConfig())
        self.adaptive_grid = AdaptiveGridGenerator(adaptive_grid_config or AdaptiveGridConfig())
        for symbol in {
            *controller_config.grid_range_multiplier_by_symbol,
            *controller_config.grid_min_step_pct_by_symbol,
        }:
            self._adaptive_grid_for_symbol(symbol)
        self.inventory = InventoryManager(inventory_config or InventoryConfig())
        self.risk = RiskManager(
            scheduler,
            RiskConfig(
                take_profit_usdt=controller_config.take_profit_usdt,
                total_capital_limit=controller_config.total_capital_limit,
                max_concurrent=controller_config.max_concurrent,
                effective_leverage_cap=(
                    controller_config.effective_leverage_cap
                    if self.feature_flags.risk_manager_v2
                    else float("inf")
                ),
                max_session_loss_pct=(
                    controller_config.max_session_loss_pct
                    if self.feature_flags.risk_manager_v2
                    else 0.0
                ),
                max_window_loss_pct=(
                    controller_config.max_window_loss_pct
                    if self.feature_flags.risk_manager_v2
                    else 0.0
                ),
                max_consecutive_session_losses=(
                    controller_config.max_consecutive_session_losses
                    if self.feature_flags.risk_manager_v2
                    else 0
                ),
                max_window_stop_count=(
                    controller_config.max_window_stop_count
                    if self.feature_flags.risk_manager_v2
                    else 0
                ),
            ),
        )
        self.active_sessions: dict[str, SymbolSession] = {}
        self.current_window_id: int | None = None
        self.round_active = False
        self.round_stopping = False
        self.round_candidate_symbols: set[str] = set()
        self.round_stopped_symbols: set[str] = set()
        self._processed_kline_close_at: dict[str, datetime] = {}
        self._last_round_scan_bar_at: datetime | None = None
        self._next_round_scan_at: datetime | None = None
        self._round_scan_lock = asyncio.Lock()
        self._volatility_refreshed_at: dict[int, datetime] = {}
        self._grid_recalculated_at: dict[int, datetime] = {}
        self._session_event_locks: dict[str, asyncio.Lock] = {}
        self._last_maker_fee_by_symbol: dict[str, float] = {}
        self._last_taker_fee_by_symbol: dict[str, float] = {}
        self._symbol_rules_by_symbol: dict[str, dict[str, Any]] = {}
        self._last_kline_quality_by_symbol: dict[str, dict[str, Any]] = {}
        self._regime_by_symbol: dict[str, RegimeDecision] = {}
        self._inventory_by_symbol: dict[str, InventorySnapshot] = {}
        self._account_equity: float | None = None
        self._maker_fee_checked_at: datetime | None = None
        self._runtime_config_signature = self._runtime_config_signature_from_config(controller_config)
        self._runtime_config_error_signature: str | None = None
        self._runtime_config_deferred_signature: str | None = None

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
            or not _non_negative_finite_number(self.config.maker_fee_check_interval_seconds)
            or not 0 <= float(self.config.seed_max_slippage_pct) < 1
            or str(self.config.seed_execution).upper() != "MARKET"
        ):
            result = StartupCheck(False, "启动交易参数不是有效数字。", 0.0)
            self.repository.log_system(
                "ERROR",
                "controller",
                result.reason,
                (
                    f"leverage={self.config.leverage}, "
                    f"take_profit_usdt={self.config.take_profit_usdt}, "
                    f"max_maker_fee_rate={self.config.max_maker_fee_rate}, "
                    f"maker_fee_check_interval_seconds={self.config.maker_fee_check_interval_seconds}, "
                    f"seed_execution={self.config.seed_execution}, "
                    f"seed_max_slippage_pct={self.config.seed_max_slippage_pct}"
                ),
                current_time,
            )
            return result
        if (
            self.feature_flags.risk_manager_v2
            and self.config.leverage > self.config.effective_leverage_cap
        ):
            result = StartupCheck(False, "配置杠杆超过 v2 有效杠杆上限。", 0.0)
            self.repository.log_system(
                "ERROR",
                "risk_v2",
                result.reason,
                (
                    f"leverage={self.config.leverage}, "
                    f"effective_leverage_cap={self.config.effective_leverage_cap}"
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

        self._account_equity = balance
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
        self._apply_runtime_config_draft(current_time)
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

        effective_observer_config, effective_grid_config, effective_max_concurrent = self._effective_next_entry_settings(current_time)
        scan_limit = self._effective_scan_candidate_count(effective_max_concurrent)
        selected = await self.selector.select(max_concurrent=max(effective_max_concurrent, scan_limit))
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
        prepared: list[tuple[int, str, float, GridParams]] = []
        effective_risk = RiskManager(
            self.scheduler,
            replace(self.risk.config, max_concurrent=effective_max_concurrent),
        )
        observation_sessions: list[tuple[int, str]] = []
        for symbol in fee_eligible_symbols:
            direction_mode = self._direction_mode_for_symbol(symbol)
            session_id = self.repository.create_session(
                window_id=window_id,
                symbol=symbol,
                state=GridState.OBSERVING.value,
                capital=self.config.capital_per_symbol,
                leverage=self.config.leverage,
                open_time=current_time,
                direction_mode=direction_mode,
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
            observation_sessions.append((session_id, symbol))

        observation_results = await asyncio.gather(
            *(
                self._observe_grid_candidate(symbol, effective_observer_config, effective_grid_config)
                for _session_id, symbol in observation_sessions
            ),
            return_exceptions=True,
        )
        for (session_id, symbol), observation_result in zip(observation_sessions, observation_results):
            if isinstance(observation_result, ObservationAborted):
                for aborted_session_id, aborted_symbol in observation_sessions:
                    self.repository.close_session(aborted_session_id, "observation_aborted_force_close", current_time)
                    if self.state_machine.get_state(aborted_symbol) == GridState.OBSERVING:
                        self.state_machine.transition(aborted_symbol, GridState.STOPPED, "observation_aborted", at=current_time)
                    self.repository.log_state(
                        aborted_session_id,
                        aborted_symbol,
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
            if isinstance(observation_result, GridCalculationError):
                self.repository.close_session(session_id, "grid_calculation_failed", current_time)
                self.state_machine.transition(symbol, GridState.STOPPED, "grid_calculation_failed", str(observation_result), current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    GridState.OBSERVING.value,
                    GridState.STOPPED.value,
                    "grid_calculation_failed",
                    str(observation_result),
                    current_time,
                )
                self.repository.log_system(
                    "WARN",
                    "controller",
                    "Grid calculation failed; symbol skipped.",
                    f"symbol={symbol}, reason={observation_result}",
                    current_time,
                )
                continue
            if isinstance(observation_result, BaseException):
                self.repository.close_session(session_id, "observation_failed", current_time)
                self.state_machine.transition(symbol, GridState.STOPPED, "observation_failed", str(observation_result), current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    GridState.OBSERVING.value,
                    GridState.STOPPED.value,
                    "observation_failed",
                    str(observation_result),
                    current_time,
                )
                self.repository.log_system(
                    "ERROR",
                    "controller",
                    "Observation failed; symbol skipped.",
                    f"symbol={symbol}, reason={observation_result}",
                    current_time,
                )
                continue
            current_price, params = observation_result
            params = replace(
                params,
                direction_mode=self._direction_mode_for_symbol(symbol),
            )
            self._persist_session_grid(session_id, params)
            prepared.append((session_id, symbol, current_price, params))

        for index, (session_id, symbol, current_price, params) in enumerate(prepared):
            if index >= effective_max_concurrent:
                self.repository.close_session(session_id, "eligible_but_concurrency_limit_reached", current_time)
                self.state_machine.transition(symbol, GridState.STOPPED, "concurrency_limit", at=current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    GridState.OBSERVING.value,
                    GridState.STOPPED.value,
                    "concurrency_limit",
                    f"max_concurrent={effective_max_concurrent}",
                    current_time,
                )
                continue
            allowed = effective_risk.can_open_new_symbol(list(self.active_sessions.values()), self.config.capital_per_symbol)
            if allowed.action != RiskAction.NONE:
                self.repository.close_session(session_id, "risk_limit_reached", current_time)
                self.state_machine.transition(symbol, GridState.STOPPED, "risk_limit", allowed.reason, current_time)
                self.repository.log_state(
                    session_id,
                    symbol,
                    GridState.OBSERVING.value,
                    GridState.STOPPED.value,
                    "risk_limit",
                    allowed.reason,
                    current_time,
                )
                continue
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
                direction_mode=params.direction_mode,
            )
            try:
                await self.engine.start(session, current_price)
                self._persist_seed_execution(session, current_time)
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
                    self.state_machine.transition(symbol, GridState.STOPPED, "grid_start_failed", str(exc), current_time)
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

    async def start_round(self, now: datetime | None = None) -> RunOnceResult:
        current_time = now or datetime.now(timezone.utc)
        self._apply_runtime_config_draft(current_time)
        if self.scheduler.should_force_close(current_time):
            return RunOnceResult("force_close_window", [], [])
        if not self.scheduler.is_in_window(current_time):
            return RunOnceResult("outside_window", [], [])
        startup = await self.validate_startup(current_time)
        if not startup.ok:
            return RunOnceResult("startup_check_failed", [], [])
        runtime = self.repository.runtime_state()
        runtime_id = str(runtime.get("runtime_id") or "")
        window_id = self.repository.claim_round_window(runtime_id, current_time)
        self.current_window_id = window_id
        self.round_active = True
        self.round_stopping = False
        self.round_candidate_symbols.clear()
        self.round_stopped_symbols.clear()
        self._processed_kline_close_at.clear()
        self._last_round_scan_bar_at = None
        window_key = ""
        window_kind = ""
        classify = getattr(self.scheduler, "classify_window", None)
        if callable(classify):
            try:
                window = classify(current_time)
                window_key = str(getattr(window, "window_key", "") or "")
                kind = getattr(window, "kind", "")
                window_kind = getattr(kind, "value", str(kind or ""))
            except Exception:
                pass
        self.repository.set_round_runtime_state(
            window_id,
            "SCANNING",
            current_time,
            last_scan_at=current_time,
            window_key=window_key,
            window_kind=window_kind,
            last_status="round_started",
        )
        return await self.scan_round_once(current_time)

    async def scan_round_once(
        self,
        now: datetime | None = None,
        *,
        trigger_close_at: datetime | None = None,
    ) -> RunOnceResult:
        current_time = now or datetime.now(timezone.utc)
        async with self._round_scan_lock:
            if (
                trigger_close_at is not None
                and self._last_round_scan_bar_at is not None
                and trigger_close_at <= self._last_round_scan_bar_at
            ):
                return RunOnceResult("scan_coalesced", [], [])
            if (
                trigger_close_at is None
                and self._last_round_scan_bar_at is not None
                and self._next_round_scan_at is not None
                and current_time >= self._last_round_scan_bar_at
                and current_time < self._next_round_scan_at
            ):
                return RunOnceResult("scan_coalesced", [], [])
            result = await self._scan_round_once_locked(current_time)
            if trigger_close_at is not None and result.status not in {
                "round_inactive",
                "round_stopped",
            }:
                self._last_round_scan_bar_at = trigger_close_at
            return result

    async def _scan_round_once_locked(self, now: datetime | None = None) -> RunOnceResult:
        current_time = now or datetime.now(timezone.utc)
        if not self.round_active or self.round_stopping or self.current_window_id is None:
            return RunOnceResult("round_inactive", [], [])
        if self.scheduler.should_force_close(current_time) or not self.scheduler.is_in_window(current_time):
            await self.stop_round("交易窗口结束", current_time)
            return RunOnceResult("round_stopped", [], [])

        self.repository.mark_round_candidates_stale(
            self.current_window_id,
            current_time - timedelta(seconds=90),
            current_time,
        )

        effective_observer_config, effective_grid_config, effective_max_concurrent = self._effective_next_entry_settings(current_time)
        scan_limit = self._effective_scan_candidate_count(effective_max_concurrent)
        selected = await self.selector.select(max_concurrent=scan_limit)
        selected_symbols = [item.symbol for item in selected]
        self.round_candidate_symbols.update(selected_symbols)
        self._log_selection(selected, current_time)
        disabled_symbols = self.repository.disabled_symbols()
        stream_health = getattr(self, "stream_health_reporter", None)
        stream_entries_blocked = bool(
            stream_health is not None
            and callable(getattr(stream_health, "allows_new_entries", None))
            and not stream_health.allows_new_entries()
        )
        entries_paused = self.repository.new_entries_paused() or stream_entries_blocked
        entry_pause_reason = (
            "行情数据流正在重连，暂停新开仓"
            if stream_entries_blocked
            else "新开仓已暂停"
        )
        analyzable = [
            item
            for item in selected
            if item.symbol.upper() not in disabled_symbols
            and item.symbol not in self.round_stopped_symbols
        ]
        fee_eligible = set(
            await self._filter_symbols_by_maker_fee([item.symbol for item in analyzable], current_time)
        )
        analyses = await asyncio.gather(
            *(
                self._analyze_round_candidate(item, effective_observer_config, effective_grid_config, current_time)
                for item in selected
            ),
            return_exceptions=True,
        )
        eligible: list[tuple[Any, float, GridParams]] = []
        for rank, (item, analysis) in enumerate(zip(selected, analyses), start=1):
            symbol = item.symbol
            base = {
                "liquidity_rank": rank,
                "score": item.score,
                "volume_score": item.volume_score,
                "depth_score": item.depth_score,
                "volume_24h": item.volume_24h,
                "depth_usdt": item.depth_usdt,
                "bid_price": item.bid_price,
                "ask_price": item.ask_price,
                "spread_pct": item.spread_pct,
                "market_updated_at": current_time.isoformat(),
                "data_stale": False,
            }
            active = self.active_sessions.get(symbol)
            if active is not None:
                active_values: dict[str, Any] = {
                    **base,
                    "price": (item.bid_price + item.ask_price) / 2,
                    "threshold_met": True,
                    "session_id": active.session_id,
                    "stage": "trading" if active.state != GridState.PAUSED else "paused",
                    "error": None,
                }
                if not isinstance(analysis, BaseException):
                    current_price, params, snapshot = analysis
                    active_regime = self._regime_by_symbol.get(symbol)
                    active_values.update(
                        {
                            "price": current_price,
                            "volatility_method": params.volatility_method,
                            "volatility_value": params.volatility_value,
                            "volatility_window": params.volatility_window,
                            "range_lower": snapshot[0],
                            "range_upper": snapshot[1],
                            "range_width_pct": snapshot[2],
                            "calculated_at": params.calculated_at.isoformat(),
                            "regime_score": active_regime.grid_score if active_regime else None,
                            "regime_allowed": bool(active_regime.allowed) if active_regime else None,
                            "market_state": active_regime.state if active_regime else None,
                            "verdict": active_regime.verdict if active_regime else None,
                            "soft_breach_count": active.soft_breach_count,
                            "grid_preview_json": {
                                "lower": params.lower,
                                "upper": params.upper,
                                "grid_count": params.grid_num,
                                "level_count": params.grid_num + 1,
                            },
                            "economics_json": params.economics,
                        }
                    )
                    self.repository.update_session_current_volatility(
                        active.session_id,
                        params.volatility_value,
                        params.volatility_window,
                        params.calculated_at,
                    )
                    self._volatility_refreshed_at[active.session_id] = params.calculated_at
                self.repository.upsert_round_candidate(
                    self.current_window_id,
                    symbol,
                    current_time,
                    **active_values,
                )
                continue
            if symbol in self.round_stopped_symbols:
                self.repository.upsert_round_candidate(
                    self.current_window_id, symbol, current_time, **base, threshold_met=False, stage="stopped"
                )
                continue
            if symbol.upper() in disabled_symbols:
                self.repository.upsert_round_candidate(
                    self.current_window_id, symbol, current_time, **base, threshold_met=False, stage="not_selected", error="标的已禁用"
                )
                continue
            if symbol not in fee_eligible:
                self.repository.upsert_round_candidate(
                    self.current_window_id, symbol, current_time, **base, threshold_met=False, stage="not_selected", error="手续费率不符合条件"
                )
                continue
            if isinstance(analysis, BaseException):
                error_text = str(analysis)
                stage = "error"
                block_code = ""
                extra_values: dict[str, Any] = {}
                if isinstance(analysis, GridEconomicsError):
                    stage = "blocked_economics"
                    block_code = "BLOCKED_ECONOMICS"
                    economics = dict(analysis.economics)
                    extra_values = {
                        "economics_json": economics,
                        "grid_preview_json": {
                            "lower": economics.get("lower"),
                            "upper": economics.get("upper"),
                            "grid_count": economics.get("grid_count"),
                            "level_count": economics.get("level_count"),
                        },
                    }
                elif isinstance(analysis, RegimeAdmissionError):
                    stage = "blocked_regime"
                    regime = self._regime_by_symbol.get(symbol)
                    block_code = regime.verdict if regime else "BLOCKED_SCORE"
                    params = analysis.params
                    extra_values = {
                        "volatility_method": params.volatility_method,
                        "volatility_value": params.volatility_value,
                        "volatility_window": params.volatility_window,
                        "range_lower": params.lower,
                        "range_upper": params.upper,
                        "range_width_pct": (params.upper - params.lower) / params.lower,
                        "grid_preview_json": {
                            "lower": params.lower,
                            "upper": params.upper,
                            "grid_count": params.grid_num,
                            "level_count": params.grid_num + 1,
                        },
                        "economics_json": params.economics,
                        "maker_fee_rate": params.economics.get("maker_fee_rate"),
                        "maker_fee_source": params.economics.get("maker_fee_source"),
                        "maker_fee_checked_at": params.economics.get("maker_fee_checked_at"),
                    }
                elif isinstance(analysis, GridCalculationError):
                    upper = error_text.upper()
                    if "DATA_" in upper or "KLINE" in upper:
                        stage = "data_invalid"
                        prefix = upper.split(":", 1)[0].strip()
                        block_code = prefix if prefix.startswith("DATA_") else "DATA_INVALID"
                    elif "REGIME" in upper:
                        stage = "blocked_regime"
                        block_code = "BLOCKED_REGIME"
                    elif "PRICE_DRIFT" in upper:
                        stage = "blocked_price_drift"
                        block_code = "PRICE_DRIFT"
                    else:
                        stage = "below_threshold"
                        block_code = "BELOW_THRESHOLD"
                quality = self._last_kline_quality_by_symbol.get(symbol, {})
                regime = self._regime_by_symbol.get(symbol)
                if block_code.startswith("DATA_") and regime is None:
                    self._persist_data_blocked_regime(symbol, error_text, current_time)
                self.repository.upsert_round_candidate(
                    self.current_window_id,
                    symbol,
                    current_time,
                    **base,
                    **quality,
                    threshold_met=False,
                    stage=stage,
                    error=error_text,
                    calculated_at=current_time.isoformat(),
                    evaluation_completed_at=current_time.isoformat(),
                    regime_score=regime.grid_score if regime else None,
                    regime_allowed=(
                        False
                        if block_code == "BLOCKED_ECONOMICS"
                        else bool(regime.allowed)
                        if regime
                        else None
                    ),
                    block_code=block_code or stage.upper(),
                    block_reasons_json=[error_text],
                    market_state=regime.state if regime else None,
                    verdict=(
                        block_code
                        if block_code == "BLOCKED_ECONOMICS"
                        else regime.verdict
                        if regime
                        else block_code or stage.upper()
                    ),
                    **extra_values,
                )
                continue
            current_price, params, snapshot = analysis
            quality = self._last_kline_quality_by_symbol.get(symbol, {})
            regime = self._regime_by_symbol.get(symbol)
            self.repository.upsert_round_candidate(
                self.current_window_id,
                symbol,
                current_time,
                **base,
                **quality,
                price=current_price,
                volatility_method=params.volatility_method,
                volatility_value=params.volatility_value,
                volatility_window=params.volatility_window,
                range_lower=snapshot[0],
                range_upper=snapshot[1],
                range_width_pct=snapshot[2],
                threshold_met=True,
                stage="eligible",
                error=entry_pause_reason if entries_paused else None,
                calculated_at=params.calculated_at.isoformat(),
                evaluation_completed_at=current_time.isoformat(),
                regime_score=regime.grid_score if regime else None,
                regime_allowed=bool(regime.allowed) if regime else True,
                market_state=regime.state if regime else None,
                verdict=regime.verdict if regime else "ALLOWED",
                grid_preview_json={
                    "lower": params.lower,
                    "upper": params.upper,
                    "grid_count": params.grid_num,
                    "level_count": params.grid_num + 1,
                },
                economics_json=params.economics,
                maker_fee_rate=params.economics.get("maker_fee_rate"),
                maker_fee_source=params.economics.get("maker_fee_source"),
                maker_fee_checked_at=params.economics.get("maker_fee_checked_at"),
                block_code=(
                    "STREAM_DEGRADED"
                    if stream_entries_blocked
                    else "NEW_ENTRIES_PAUSED"
                    if entries_paused
                    else None
                ),
                block_reasons_json=[entry_pause_reason] if entries_paused else None,
            )
            if not entries_paused:
                eligible.append((item, current_price, params))

        started: list[str] = []
        slots = max(0, effective_max_concurrent - len(self.active_sessions))
        for item, current_price, params in eligible[:slots]:
            if self.round_stopping:
                break
            if await self._start_round_session(item.symbol, current_price, params, current_time):
                started.append(item.symbol)

        next_scan = current_time + timedelta(minutes=1)
        self._next_round_scan_at = next_scan
        round_state = "RUNNING" if self.active_sessions else "SCANNING"
        last_status = "started" if started else ("no_eligible_symbol" if selected_symbols else "scanning")
        self.repository.set_round_runtime_state(
            self.current_window_id,
            round_state,
            current_time,
            last_scan_at=current_time,
            next_scan_at=next_scan,
            last_status=last_status,
        )
        return RunOnceResult("started" if started else "scanning", selected_symbols, started)

    async def _analyze_round_candidate(
        self,
        item: Any,
        observer_config: ObserverConfig,
        grid_config: GridConfig,
        calculated_at: datetime,
    ) -> tuple[float, GridParams, tuple[float, float, float]]:
        self._regime_by_symbol.pop(item.symbol, None)
        current_price = (float(item.bid_price) + float(item.ask_price)) / 2
        required_bars = max(
            int(observer_config.observe_hours * 60),
            observer_config.min_samples,
            self.regime.config.long_window + 1 if self.feature_flags.regime_v2 else 0,
        )
        try:
            batch = await self.market_history.load_closed_klines(
                item.symbol,
                interval=str(observer_config.kline_interval or "1m"),
                required_bars=required_bars,
                as_of=calculated_at,
                buffer_bars=2,
            )
        except Exception as exc:
            raise GridCalculationError(
                f"DATA_PROVIDER_ERROR: {type(exc).__name__}: {exc}"
            ) from exc
        klines = list(batch.rows)
        self._last_kline_quality_by_symbol[item.symbol] = {
            "kline_required_count": batch.quality.required_bars,
            "kline_actual_count": batch.quality.actual_bars,
            "kline_last_close_at": batch.last_close_time.isoformat(),
            "kline_age_seconds": batch.age_seconds,
            "kline_missing_count": batch.missing_count,
            "kline_quality_status": "ok" if batch.quality.valid else "invalid",
            "last_kline_close_at": batch.last_close_time.isoformat(),
        }
        try:
            funding_context = await self.exchange.get_funding_context(item.symbol)
        except Exception as exc:
            raise GridCalculationError(
                f"DATA_FUNDING_ERROR: {type(exc).__name__}: {exc}"
            ) from exc
        funding_rate = float(funding_context.get("funding_rate") or 0.0)
        projected_funding_cost = self._projected_funding_cost(
            funding_rate,
            funding_context.get("next_funding_time"),
            calculated_at,
        )
        if item.symbol not in self._last_maker_fee_by_symbol:
            try:
                commission = await self.exchange.get_commission_rate(item.symbol)
                self._last_maker_fee_by_symbol[item.symbol] = _non_negative_float(
                    commission.get("maker"),
                    "maker commission rate",
                )
                self._last_taker_fee_by_symbol[item.symbol] = _non_negative_float(
                    commission.get("taker", 0.0),
                    "taker commission rate",
                )
            except Exception as exc:
                raise GridCalculationError(
                    "DATA_COMMISSION_ERROR: 未取得交易所按标的返回的 Maker 费率。"
                ) from exc
        maker_fee_rate = self._last_maker_fee_by_symbol[item.symbol]
        if item.symbol not in self._symbol_rules_by_symbol:
            try:
                self._symbol_rules_by_symbol[item.symbol] = dict(
                    await self.exchange.get_symbol_rules(item.symbol)
                )
            except Exception as exc:
                raise GridCalculationError(
                    "DATA_SYMBOL_RULES_ERROR: 未取得交易所按标的返回的下单规则。"
                ) from exc
        symbol_rules = self._symbol_rules_by_symbol[item.symbol]
        cost_breakdown = self._grid_cost_breakdown(
            maker_fee_rate,
            projected_funding_cost,
        )
        adaptive_grid = self._adaptive_grid_for_symbol(item.symbol)
        expected_min_step_pct = (
            adaptive_grid.config.min_step_pct
            if self.feature_flags.adaptive_grid_v2
            else grid_config.min_step_pct
        )
        regime_decision: RegimeDecision | None = None
        structural_decision: RegimeDecision | None = None
        if self.feature_flags.regime_v2:
            structural_decision = self.regime.evaluate(
                item.symbol,
                klines,
                spread_pct=float(item.spread_pct),
                depth_usdt=float(item.depth_usdt),
                funding_rate=float(funding_rate),
                expected_step_pct=expected_min_step_pct,
                cost_floor_pct=0.0,
                running=item.symbol in self.active_sessions,
                include_cost=False,
                as_of=calculated_at,
            )
            self._regime_by_symbol[item.symbol] = structural_decision
            if structural_decision.hard_blocks:
                self._persist_regime_decision(item.symbol, structural_decision, calculated_at)
                raise GridCalculationError(
                    "Regime Engine 禁止启动网格: "
                    + "；".join(structural_decision.hard_blocks)
                )
        lows = [float(row["low"]) for row in klines]
        highs = [float(row["high"]) for row in klines]
        lower = min(lows)
        upper = max(highs)
        range_width_pct = (upper - lower) / lower
        active_session = self.active_sessions.get(item.symbol)
        if active_session is not None and active_session.params is not None:
            params = replace(
                active_session.params,
                calculated_at=calculated_at,
                volatility_value=(
                    max(
                        structural_decision.features.atr_pct,
                        structural_decision.features.volatility_long,
                    )
                    if structural_decision is not None
                    else active_session.params.volatility_value
                ),
                volatility_window=len(klines),
            )
        elif self.feature_flags.adaptive_grid_v2:
            try:
                params = adaptive_grid.generate(
                    item.symbol,
                    klines,
                    current_price=current_price,
                    funding_rate=float(funding_rate),
                    funding_cost_rate=projected_funding_cost,
                    maker_fee_rate=maker_fee_rate,
                    regime_score=structural_decision.grid_score if structural_decision else 100.0,
                    capital=self.config.capital_per_symbol,
                    leverage=self.config.leverage,
                    tick_size=_non_negative_float(
                        symbol_rules.get("tick_size", 0.0),
                        "tick_size",
                    ),
                    step_size=_non_negative_float(
                        symbol_rules.get("step_size", 0.0),
                        "step_size",
                    ),
                    min_qty=_non_negative_float(
                        symbol_rules.get("min_qty", 0.0),
                        "min_qty",
                    ),
                    min_notional=_non_negative_float(
                        symbol_rules.get("min_notional", 0.0),
                        "min_notional",
                    ),
                    direction_mode=self._direction_mode_for_symbol(item.symbol),
                    risk_budget=self._session_risk_budget(),
                    taker_fee_rate=self._last_taker_fee_by_symbol.get(
                        item.symbol,
                        0.0,
                    ),
                    calculated_at=calculated_at,
                )
            except GridEconomicsError as exc:
                economics = dict(exc.economics)
                gross_step = float(
                    economics.get("gross_step_pct")
                    or expected_min_step_pct
                )
                economics.update(
                    {
                        **cost_breakdown,
                        "maker_fee_rate": maker_fee_rate,
                        "maker_fee_source": "binance_commission_rate",
                        "maker_fee_checked_at": calculated_at.isoformat(),
                        "gross_step_pct": gross_step,
                        "fee_net_edge_pct": (
                            gross_step - cost_breakdown["hard_cost_pct"]
                        ),
                    }
                )
                exc.economics = economics
                if structural_decision is not None:
                    blocked_decision = self.regime.evaluate(
                        item.symbol,
                        klines,
                        spread_pct=float(item.spread_pct),
                        depth_usdt=float(item.depth_usdt),
                        funding_rate=float(funding_rate),
                        expected_step_pct=gross_step,
                        cost_floor_pct=float(cost_breakdown["hard_cost_pct"]),
                        running=False,
                        cost_breakdown=self._regime_cost_breakdown(economics),
                        as_of=calculated_at,
                    )
                    blocked_decision = replace(
                        blocked_decision,
                        allowed=False,
                        verdict="BLOCKED_ECONOMICS",
                        reasons=blocked_decision.reasons + (str(exc),),
                    )
                    self._regime_by_symbol[item.symbol] = blocked_decision
                    self._persist_regime_decision(
                        item.symbol,
                        blocked_decision,
                        calculated_at,
                    )
                raise
        else:
            params = calculate_grid_params(
                item.symbol,
                klines,
                current_price,
                projected_funding_cost,
                replace(grid_config, min_samples=observer_config.min_samples),
                calculated_at=calculated_at,
            )
            params = replace(
                params,
                cost_floor_pct=cost_breakdown["hard_cost_pct"],
            )
        economics = dict(params.economics)
        economics.update(
            {
                **cost_breakdown,
                "maker_fee_rate": maker_fee_rate,
                "maker_fee_source": "binance_commission_rate",
                "maker_fee_checked_at": calculated_at.isoformat(),
                "gross_step_pct": params.step_pct,
                "fee_net_edge_pct": params.step_pct - cost_breakdown["hard_cost_pct"],
            }
        )
        regime_cost_breakdown = self._regime_cost_breakdown(economics)
        if self.feature_flags.regime_v2:
            regime_decision = self.regime.evaluate(
                item.symbol,
                klines,
                spread_pct=float(item.spread_pct),
                depth_usdt=float(item.depth_usdt),
                funding_rate=float(funding_rate),
                expected_step_pct=float(params.step_pct),
                cost_floor_pct=float(cost_breakdown["hard_cost_pct"]),
                running=active_session is not None,
                cost_breakdown=regime_cost_breakdown,
                as_of=calculated_at,
            )
            params = replace(
                params,
                regime_score=regime_decision.grid_score,
                cost_floor_pct=cost_breakdown["hard_cost_pct"],
                economics=economics,
            )
            self._regime_by_symbol[item.symbol] = regime_decision
            self._persist_regime_decision(item.symbol, regime_decision, calculated_at)
            if not regime_decision.allowed:
                detail = regime_decision.hard_blocks or regime_decision.reasons
                raise RegimeAdmissionError(
                    "Regime Engine 禁止启动网格: " + "；".join(detail),
                    params=params,
                )
        return current_price, params, (lower, upper, range_width_pct)

    def _persist_data_blocked_regime(
        self,
        symbol: str,
        error: str,
        at: datetime,
    ) -> None:
        self.repository.create_regime_decision(
            session_id=None,
            symbol=symbol,
            as_of_time=at,
            state="UNKNOWN_DATA",
            verdict="BLOCKED_DATA",
            grid_score=0.0,
            threshold_used=self.regime.config.enter_threshold,
            allowed=False,
            reasons=[error],
            hard_blocks=[error],
            component_scores={
                "volatility": None,
                "trend": None,
                "liquidity": None,
                "mean_reversion": None,
                "cost": None,
                "event": None,
            },
            cost_breakdown={},
            effective_weights={},
            score_contributions={},
            event_source_available=self.regime.config.event_source_available,
            model_version="regime-data-block-v2.1.1",
            feature_snapshot_id=None,
        )

    def _grid_cost_breakdown(
        self,
        maker_fee_rate: float,
        projected_funding_cost: float,
    ) -> dict[str, float]:
        config = self.adaptive_grid.config
        result = {
            "maker_round_trip_pct": max(0.0, float(maker_fee_rate)) * 2,
            "adverse_selection_pct": max(0.0, float(config.adverse_selection_buffer_pct)),
            "slippage_pct": max(0.0, float(config.slippage_buffer_pct)),
            "safety_margin_pct": max(0.0, float(config.safety_margin_pct)),
            "projected_funding_pct": max(0.0, float(projected_funding_cost)),
        }
        result["hard_cost_pct"] = (
            result["maker_round_trip_pct"] + result["projected_funding_pct"]
        )
        result["risk_discount_pct"] = (
            result["adverse_selection_pct"]
            + result["slippage_pct"]
            + result["safety_margin_pct"]
        )
        result["total_cost_pct"] = (
            result["hard_cost_pct"] + result["risk_discount_pct"]
        )
        return result

    def _adaptive_grid_for_symbol(self, symbol: str) -> AdaptiveGridGenerator:
        normalized = str(symbol).strip().upper()
        base = self.adaptive_grid.config
        range_multiplier = self.config.grid_range_multiplier_by_symbol.get(
            normalized,
            1.0,
        )
        min_step_pct = self.config.grid_min_step_pct_by_symbol.get(
            normalized,
            base.min_step_pct,
        )
        if not _positive_finite_number(range_multiplier):
            raise ValueError(f"{normalized} 网格区间倍率必须为正的有限数。")
        if not _positive_finite_number(min_step_pct):
            raise ValueError(f"{normalized} 最小网格格距必须为正的有限数。")
        effective_config = replace(
            base,
            k_atr_range=base.k_atr_range * float(range_multiplier),
            k_sigma_range=base.k_sigma_range * float(range_multiplier),
            min_step_pct=float(min_step_pct),
        )
        if effective_config == base:
            return self.adaptive_grid
        return AdaptiveGridGenerator(effective_config)

    @staticmethod
    def _regime_cost_breakdown(economics: dict[str, Any]) -> dict[str, float]:
        supported = {
            "maker_round_trip_pct",
            "projected_funding_pct",
            "hard_cost_pct",
            "adverse_selection_pct",
            "slippage_pct",
            "safety_margin_pct",
            "risk_discount_pct",
            "gross_step_pct",
            "fee_net_edge_pct",
            "inventory_risk_discount_pct",
            "execution_risk_discount_pct",
            "estimated_crossings_per_hour",
        }
        return {
            key: float(value)
            for key, value in economics.items()
            if key in supported and isinstance(value, (int, float))
        }

    def _projected_funding_cost(
        self,
        funding_rate: float,
        next_funding_time: Any,
        at: datetime,
    ) -> float:
        if next_funding_time in (None, ""):
            return 0.0
        try:
            if isinstance(next_funding_time, datetime):
                settlement = next_funding_time
            else:
                settlement = datetime.fromtimestamp(
                    float(next_funding_time) / 1000.0,
                    tz=timezone.utc,
                )
            if settlement.tzinfo is None:
                settlement = settlement.replace(tzinfo=timezone.utc)
            settlement = settlement.astimezone(timezone.utc)
            window = self.scheduler.classify_window(at)
            force_close_at = getattr(window, "force_close_at", None)
            if force_close_at is None:
                return 0.0
            force_close_utc = force_close_at.astimezone(timezone.utc)
            at_utc = at.astimezone(timezone.utc)
            if at_utc < settlement <= force_close_utc:
                return abs(float(funding_rate))
        except (AttributeError, TypeError, ValueError, OSError):
            return 0.0
        return 0.0

    def _persist_regime_decision(
        self,
        symbol: str,
        decision: RegimeDecision,
        source_time: datetime,
        *,
        session_id: int | None = None,
    ) -> None:
        active = self.active_sessions.get(symbol)
        resolved_session_id = session_id if session_id is not None else (
            active.session_id if active else None
        )
        feature_snapshot_id = self.repository.create_feature_snapshot(
            session_id=resolved_session_id,
            symbol=symbol,
            as_of_time=decision.as_of,
            source_time=source_time,
            features=asdict(decision.features),
            feature_version=decision.feature_version,
        )
        self.repository.create_regime_decision(
            session_id=resolved_session_id,
            symbol=symbol,
            as_of_time=decision.as_of,
            state=decision.state,
            verdict=decision.verdict,
            grid_score=decision.grid_score,
            threshold_used=decision.threshold_used,
            allowed=decision.allowed,
            reasons=decision.reasons,
            hard_blocks=decision.hard_blocks,
            component_scores=decision.component_scores,
            cost_breakdown=decision.cost_breakdown,
            effective_weights=decision.effective_weights,
            score_contributions=decision.score_contributions,
            event_source_available=decision.event_source_available,
            model_version=decision.model_version,
            feature_snapshot_id=feature_snapshot_id,
        )
        self.repository.append_event(
            "REGIME_CHANGED",
            decision.as_of,
            {
                "state": decision.state,
                "verdict": decision.verdict,
                "grid_score": decision.grid_score,
                "threshold_used": decision.threshold_used,
                "allowed": decision.allowed,
                "reasons": decision.reasons,
                "hard_blocks": decision.hard_blocks,
                "cost_breakdown": decision.cost_breakdown,
            },
            session_id=resolved_session_id,
            symbol=symbol,
        )

    def _entry_max_price_drift_pct(self) -> float:
        raw = getattr(self, "_entry_config", None)
        if isinstance(raw, dict):
            try:
                return max(0.0, float(raw.get("max_price_drift_pct", 0.002)))
            except (TypeError, ValueError):
                return 0.002
        return 0.002

    def _entry_revalidate_before_place(self) -> bool:
        raw = getattr(self, "_entry_config", None)
        if isinstance(raw, dict):
            return bool(raw.get("revalidate_before_place", True))
        return True

    async def _revalidate_entry_price(self, symbol: str, planned_price: float, at: datetime) -> float:
        if not self._entry_revalidate_before_place():
            return planned_price
        if not self.scheduler.is_in_window(at) or self.scheduler.should_force_close(at):
            raise GridCalculationError("WINDOW_NOT_ALLOWED: 下单前窗口已失效")
        ticker = await self.exchange.get_24h_ticker(symbol)
        live_price = _ticker_last_price(ticker)
        if planned_price <= 0:
            raise GridCalculationError("planned price invalid")
        drift = abs(live_price - planned_price) / planned_price
        max_drift = self._entry_max_price_drift_pct()
        if drift > max_drift:
            raise GridCalculationError(
                f"PRICE_DRIFT: 当前价相对规划价漂移 {drift:.4%} 超过 {max_drift:.4%}"
            )
        return live_price

    async def _start_round_session(
        self,
        symbol: str,
        current_price: float,
        params: GridParams,
        at: datetime,
    ) -> bool:
        if self.current_window_id is None or symbol in self.round_stopped_symbols:
            return False
        direction_mode = self._direction_mode_for_symbol(symbol)
        params = replace(params, direction_mode=direction_mode)
        entry_risk = self.risk.can_open_new_symbol(
            list(self.active_sessions.values()),
            self.config.capital_per_symbol,
            regime_allowed=(
                not self.feature_flags.regime_v2
                or bool(self._regime_by_symbol.get(symbol) and self._regime_by_symbol[symbol].allowed)
            ),
            account_equity=self._account_equity or self.config.total_capital_limit,
            window_pnl=self.repository.window_realized_pnl(self.current_window_id),
            window_stop_count=self.repository.window_stop_count(self.current_window_id),
        )
        if entry_risk.action != RiskAction.NONE:
            self.repository.create_risk_snapshot(
                as_of_time=at,
                risk_level="HALT" if entry_risk.action == RiskAction.HALT_WINDOW else "BLOCK",
                action=entry_risk.action.value,
                reason=entry_risk.reason,
                window_id=self.current_window_id,
                symbol=symbol,
                window_pnl=self.repository.window_realized_pnl(self.current_window_id),
                limits={
                    "max_window_loss_pct": self.config.max_window_loss_pct,
                    "max_window_stop_count": self.config.max_window_stop_count,
                },
            )
            if entry_risk.action == RiskAction.HALT_WINDOW:
                self.round_stopping = True
            self.repository.upsert_round_candidate(
                self.current_window_id,
                symbol,
                at,
                stage="blocked_risk",
                threshold_met=False,
                error=entry_risk.reason,
            )
            return False
        try:
            live_price = await self._revalidate_entry_price(symbol, current_price, at)
        except Exception as exc:
            self.repository.upsert_round_candidate(
                self.current_window_id,
                symbol,
                at,
                stage="blocked_price_drift" if "PRICE_DRIFT" in str(exc) else "error",
                threshold_met=False,
                error=str(exc),
            )
            self.repository.log_system(
                "WARN",
                "startup_entry",
                "Entry revalidation blocked grid start.",
                f"symbol={symbol}, reason={exc}",
                at,
            )
            return False
        self.repository.mark_round_candidate_stage(self.current_window_id, symbol, "planning", at)
        session_id = self.repository.create_session(
            self.current_window_id,
            symbol,
            GridState.OBSERVING.value,
            self.config.capital_per_symbol,
            self.config.leverage,
            at,
            direction_mode,
        )
        self.state_machine.transition(symbol, GridState.OBSERVING, "round_candidate_eligible", at=at)
        self.repository.log_state(
            session_id, symbol, GridState.IDLE.value, GridState.OBSERVING.value, "round_candidate_eligible", None, at
        )
        self._persist_session_grid(session_id, params)
        session = SymbolSession(
            session_id=session_id,
            symbol=symbol,
            state=GridState.OBSERVING,
            params=params,
            orders=[],
            realized_pnl=0.0,
            capital=self.config.capital_per_symbol,
            leverage=self.config.leverage,
            open_time=at,
            state_entered_at=at,
            direction_mode=direction_mode,
        )
        self.repository.mark_round_candidate_stage(self.current_window_id, symbol, "placing_orders", at, session_id)
        try:
            await self.engine.start(session, live_price)
            self._persist_seed_execution(session, at)
        except Exception as exc:
            self._persist_session_orders(session)
            self.active_sessions[symbol] = session
            await self._record_grid_start_failure_fills(session, at)
            closed = await self._close_session(session, "grid_start_failed", at)
            if closed:
                self.repository.log_system(
                    "ERROR",
                    "controller",
                    "Grid start failed; fills reconciled and symbol closed for this round.",
                    f"symbol={symbol}, reason={exc}",
                    at,
                )
            else:
                self.repository.log_system(
                    "ERROR",
                    "controller",
                    "Grid start failed and cleanup is pending; session kept active for retry.",
                    f"symbol={symbol}, reason={exc}",
                    at,
                )
            return False
        session.state = GridState.RUNNING
        self._persist_session_orders(session)
        self.active_sessions[symbol] = session
        self.state_machine.transition(symbol, GridState.RUNNING, "grid_started", at=at)
        self.repository.update_session_state(session_id, GridState.RUNNING.value)
        self.repository.log_state(
            session_id,
            symbol,
            GridState.OBSERVING.value,
            GridState.RUNNING.value,
            "grid_started",
            f"grid_num={params.grid_num}, step_pct={params.step_pct}",
            at,
        )
        self.repository.mark_round_candidate_stage(self.current_window_id, symbol, "trading", at, session_id)
        return True

    async def stop_round(self, reason: str, now: datetime | None = None) -> list[str]:
        current_time = now or datetime.now(timezone.utc)
        if not self.round_active or self.current_window_id is None:
            return []
        window_id = self.current_window_id
        self.round_stopping = True
        self.repository.set_round_runtime_state(window_id, "STOPPING", current_time)
        closed = await self.close_all_active_sessions(reason, current_time)
        if self.active_sessions:
            return closed
        self.repository.close_window(window_id, current_time, status="STOPPED")
        self.repository.set_round_runtime_state(window_id, "STOPPED", current_time)
        self.repository.update_round_start_request("completed", {"status": "STOPPED", "reason": reason}, current_time)
        self.repository.update_round_stop_request("completed", {"closed_symbols": closed}, current_time)
        self.current_window_id = None
        self.round_active = False
        self.round_stopping = False
        self._last_round_scan_bar_at = None
        self._next_round_scan_at = None
        return closed

    def _effective_next_entry_settings(self, at: datetime) -> tuple[ObserverConfig, GridConfig, int]:
        draft = self.repository.strategy_config_draft()
        if not draft:
            return self.observer.observer_config, self.grid_config, self.config.max_concurrent

        try:
            volatility_method = str(draft.get("volatility_method", self.grid_config.range_method)).strip().lower()
            if volatility_method not in SUPPORTED_RANGE_METHODS:
                raise ValueError(f"不支持的波动率算法: {volatility_method}")
            max_concurrent = int(draft.get("max_concurrent", self.config.max_concurrent))
            observe_hours = float(draft.get("observe_hours", self.observer.observer_config.observe_hours))
            kline_interval = str(draft.get("observe_kline_interval", self.observer.observer_config.kline_interval)).strip()
            min_step_pct = float(draft.get("min_step_pct", self.grid_config.min_step_pct))
            min_tradable_range_pct = float(
                draft.get("min_tradable_range_pct", self.grid_config.min_tradable_range_pct)
            )
            max_grid_num = int(draft.get("max_grid_num", self.grid_config.max_grid_num))
            stop_buffer_pct = float(draft.get("stop_buffer_pct", self.grid_config.stop_buffer_pct))
            safety_multiplier = float(draft.get("safety_multiplier", self.grid_config.safety_multiplier))
            if max_concurrent < 1:
                raise ValueError("max_concurrent 必须大于等于 1")
            if observe_hours <= 0:
                raise ValueError("observe_hours 必须大于 0")
            if not kline_interval:
                raise ValueError("observe_kline_interval 不能为空")
            if min_step_pct <= 0:
                raise ValueError("min_step_pct 必须大于 0")
            if min_tradable_range_pct <= 0:
                raise ValueError("min_tradable_range_pct 必须大于 0")
            if max_grid_num < 1:
                raise ValueError("max_grid_num 必须大于等于 1")
            if stop_buffer_pct < 0 or stop_buffer_pct >= 1:
                raise ValueError("stop_buffer_pct 必须在 [0, 1) 内")
            if safety_multiplier < 0:
                raise ValueError("safety_multiplier 必须为非负数")
        except (TypeError, ValueError) as exc:
            self.repository.log_system(
                "WARN",
                "controller",
                "Strategy config draft is invalid; using file config.",
                json.dumps({"draft": draft, "error": str(exc)}, ensure_ascii=False),
                at,
            )
            return self.observer.observer_config, self.grid_config, self.config.max_concurrent
        if self.config.block_risk_increase_hot_reload:
            max_concurrent = min(max_concurrent, self.config.max_concurrent)
            min_step_pct = max(min_step_pct, self.grid_config.min_step_pct)
            min_tradable_range_pct = max(
                min_tradable_range_pct,
                self.grid_config.min_tradable_range_pct,
            )
            max_grid_num = min(max_grid_num, self.grid_config.max_grid_num)
            stop_buffer_pct = min(
                stop_buffer_pct,
                self.grid_config.stop_buffer_pct,
            )
            safety_multiplier = min(
                safety_multiplier,
                self.grid_config.safety_multiplier,
            )
        return (
            replace(self.observer.observer_config, observe_hours=observe_hours, kline_interval=kline_interval),
            replace(
                self.grid_config,
                range_method=volatility_method,
                min_step_pct=min_step_pct,
                min_tradable_range_pct=min_tradable_range_pct,
                max_grid_num=max_grid_num,
                stop_buffer_pct=stop_buffer_pct,
                safety_multiplier=safety_multiplier,
            ),
            max_concurrent,
        )

    def _direction_mode_for_symbol(self, symbol: str) -> GridDirectionMode:
        normalized = str(symbol).strip().upper()
        draft = self.repository.strategy_config_draft() or {}
        raw_mode = draft.get("direction_mode", self.config.direction_mode.value)
        raw_overrides = draft.get("direction_overrides", self.config.direction_overrides)
        overrides = raw_overrides if isinstance(raw_overrides, dict) else {}
        selected = overrides.get(normalized, raw_mode)
        if isinstance(selected, GridDirectionMode):
            return selected
        return GridDirectionMode(str(selected).strip().upper())

    def _session_risk_budget(self) -> float | None:
        if self.config.max_session_loss_pct <= 0:
            return None
        equity = self._account_equity or self.config.total_capital_limit
        return float(equity) * self.config.max_session_loss_pct

    def _effective_scan_candidate_count(self, max_concurrent: int) -> int:
        draft = self.repository.strategy_config_draft() or {}
        raw_value = draft.get("scan_candidate_count", self.selector.config.scan_candidate_count)
        try:
            configured = int(raw_value)
        except (TypeError, ValueError):
            configured = 0
        return max(max_concurrent, configured if configured > 0 else max_concurrent)

    def _apply_runtime_config_draft(self, at: datetime) -> None:
        draft = self.repository.strategy_config_draft()
        if not draft:
            return
        try:
            max_concurrent = int(draft.get("max_concurrent", self.config.max_concurrent))
            capital_per_symbol = float(draft.get("capital_per_symbol", self.config.capital_per_symbol))
            leverage = int(draft.get("leverage", self.config.leverage))
            take_profit_usdt = float(draft.get("take_profit_usdt", self.config.take_profit_usdt))
            total_capital_limit = float(draft.get("total_capital_limit", self.config.total_capital_limit))
            max_maker_fee_rate = float(draft.get("max_maker_fee_rate", self.config.max_maker_fee_rate))
            if max_concurrent < 1:
                raise ValueError("max_concurrent 必须大于等于 1")
            if capital_per_symbol <= 0:
                raise ValueError("capital_per_symbol 必须大于 0")
            if leverage < 1:
                raise ValueError("leverage 必须大于等于 1")
            if take_profit_usdt <= 0:
                raise ValueError("take_profit_usdt 必须大于 0")
            if total_capital_limit <= 0:
                raise ValueError("total_capital_limit 必须大于 0")
            if max_maker_fee_rate < 0:
                raise ValueError("max_maker_fee_rate 必须为非负数")
            if not all(
                isfinite(value)
                for value in (
                    float(max_concurrent),
                    capital_per_symbol,
                    float(leverage),
                    take_profit_usdt,
                    total_capital_limit,
                    max_maker_fee_rate,
                )
            ):
                raise ValueError("运行中风控参数必须是有限数字")
        except (TypeError, ValueError) as exc:
            error_signature = json.dumps({"draft": draft, "error": str(exc)}, ensure_ascii=False, sort_keys=True)
            if error_signature != self._runtime_config_error_signature:
                self._runtime_config_error_signature = error_signature
                self.repository.log_system(
                    "WARN",
                    "controller",
                    "Runtime strategy config draft is invalid; using current runtime config.",
                    error_signature,
                    at,
                )
            return

        requested = {
            "capital_per_symbol": capital_per_symbol,
            "leverage": leverage,
            "max_concurrent": max_concurrent,
            "take_profit_usdt": take_profit_usdt,
            "total_capital_limit": total_capital_limit,
            "max_maker_fee_rate": max_maker_fee_rate,
        }
        deferred: dict[str, dict[str, float | int]] = {}
        if self.config.block_risk_increase_hot_reload:
            safe_values = {
                "capital_per_symbol": min(
                    capital_per_symbol,
                    self.config.capital_per_symbol,
                ),
                "leverage": min(leverage, self.config.leverage),
                "max_concurrent": min(
                    max_concurrent,
                    self.config.max_concurrent,
                ),
                "take_profit_usdt": min(
                    take_profit_usdt,
                    self.config.take_profit_usdt,
                ),
                "total_capital_limit": min(
                    total_capital_limit,
                    self.config.total_capital_limit,
                ),
                "max_maker_fee_rate": min(
                    max_maker_fee_rate,
                    self.config.max_maker_fee_rate,
                ),
            }
            for key, requested_value in requested.items():
                safe_value = safe_values[key]
                if requested_value > safe_value:
                    deferred[key] = {
                        "requested": requested_value,
                        "active": safe_value,
                    }
            capital_per_symbol = float(safe_values["capital_per_symbol"])
            leverage = int(safe_values["leverage"])
            max_concurrent = int(safe_values["max_concurrent"])
            take_profit_usdt = float(safe_values["take_profit_usdt"])
            total_capital_limit = float(safe_values["total_capital_limit"])
            max_maker_fee_rate = float(safe_values["max_maker_fee_rate"])
        if deferred:
            deferred_signature = json.dumps(
                deferred,
                ensure_ascii=False,
                sort_keys=True,
            )
            if deferred_signature != self._runtime_config_deferred_signature:
                self._runtime_config_deferred_signature = deferred_signature
                self.repository.log_system(
                    "WARN",
                    "controller",
                    "Risk-increasing runtime config changes were deferred.",
                    deferred_signature,
                    at,
                )
        else:
            self._runtime_config_deferred_signature = None

        next_config = replace(
            self.config,
            max_concurrent=max_concurrent,
            capital_per_symbol=capital_per_symbol,
            leverage=leverage,
            take_profit_usdt=take_profit_usdt,
            total_capital_limit=total_capital_limit,
            max_maker_fee_rate=max_maker_fee_rate,
        )
        next_signature = self._runtime_config_signature_from_config(next_config)
        if next_signature == self._runtime_config_signature:
            return
        previous = {
            "capital_per_symbol": self.config.capital_per_symbol,
            "leverage": self.config.leverage,
            "max_concurrent": self.config.max_concurrent,
            "take_profit_usdt": self.config.take_profit_usdt,
            "total_capital_limit": self.config.total_capital_limit,
            "max_maker_fee_rate": self.config.max_maker_fee_rate,
        }
        current = {
            "capital_per_symbol": capital_per_symbol,
            "leverage": leverage,
            "max_concurrent": max_concurrent,
            "take_profit_usdt": take_profit_usdt,
            "total_capital_limit": total_capital_limit,
            "max_maker_fee_rate": max_maker_fee_rate,
        }
        self.config = next_config
        self.risk.config = replace(
            self.risk.config,
            max_concurrent=max_concurrent,
            take_profit_usdt=take_profit_usdt,
            total_capital_limit=total_capital_limit,
        )
        self._runtime_config_signature = next_signature
        self._runtime_config_error_signature = None
        self.repository.log_system(
            "INFO",
            "controller",
            "Runtime strategy config draft applied.",
            json.dumps({"previous": previous, "current": current}, ensure_ascii=False),
            at,
        )

    @staticmethod
    def _runtime_config_signature_from_config(config: ControllerConfig) -> tuple[float, int, int, float, float, float]:
        return (
            float(config.capital_per_symbol),
            int(config.leverage),
            int(config.max_concurrent),
            float(config.take_profit_usdt),
            float(config.total_capital_limit),
            float(config.max_maker_fee_rate),
        )

    async def _filter_symbols_by_maker_fee(self, symbols: list[str], at: datetime) -> list[str]:
        eligible: list[str] = []
        checked = False
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
            checked = True
            self._last_maker_fee_by_symbol[symbol] = maker_fee
            try:
                self._last_taker_fee_by_symbol[symbol] = _non_negative_float(
                    commission.get("taker", 0.0),
                    "taker",
                )
            except ValueError:
                self._last_taker_fee_by_symbol[symbol] = 0.0
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
        if checked:
            self._maker_fee_checked_at = at
        return eligible

    async def handle_order_filled_event(self, event: dict[str, Any]) -> GridOrder | None:
        symbol = str(event["symbol"])
        async with self._session_event_locks.setdefault(symbol, asyncio.Lock()):
            return await self._handle_order_filled_event_locked(event)

    async def _handle_order_filled_event_locked(self, event: dict[str, Any]) -> GridOrder | None:
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
        fee = _non_negative_float(event.get("fee", 0.0), "fill fee")
        exchange_realized_pnl = _finite_float(event.get("realized_pnl", 0.0), "fill realized pnl")
        trade_time = event.get("trade_time")
        if not isinstance(trade_time, datetime):
            trade_time = datetime.now(timezone.utc)
        trade_summary = await self._load_order_trade_summary(symbol, str(event.get("order_id", filled_order.order_id)))
        if trade_summary is not None and trade_summary["qty"] + 1e-12 >= qty:
            price = trade_summary["price"]
            qty = trade_summary["qty"]
            fee = trade_summary["fee"]
            exchange_realized_pnl = trade_summary["realized_pnl"]
            trade_time = trade_summary["trade_time"]

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
        cap_changes: list[GridOrder] = []
        refill_error: Exception | None = None
        try:
            new_order = await self.engine.handle_order_filled(session, client_id, fill_price=price)
            cap_changes = await self._enforce_symbol_unpaired_lot_cap(
                session,
                mark_price=price,
                at=trade_time,
            )
        except Exception as exc:
            refill_error = exc
        changed_orders = [filled_order]
        if new_order is not None:
            changed_orders.append(new_order)
        for changed_order in cap_changes:
            if changed_order not in changed_orders:
                changed_orders.append(changed_order)
        self.repository.upsert_orders(session.session_id, changed_orders)
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
            fee=fee,
        )
        pnl_delta = (grid_pnl if grid_pnl is not None else exchange_realized_pnl) - fee
        if abs(pnl_delta) > 1e-12:
            session.realized_pnl += pnl_delta
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

    async def _enforce_symbol_unpaired_lot_cap(
        self,
        session: SymbolSession,
        *,
        mark_price: float,
        at: datetime,
    ) -> list[GridOrder]:
        limit = int(
            self.config.max_unpaired_lots_per_side_by_symbol.get(
                session.symbol.upper(),
                0,
            )
        )
        if limit == 0:
            return []
        if limit < 0:
            raise ValueError("单侧未配对库存层数上限不能为负。")
        snapshot = self.inventory.snapshot(
            session.orders,
            mark_price=mark_price,
            max_inventory_notional=session.capital * session.leverage,
        )
        return await self.engine.enforce_unpaired_lot_cap(
            session,
            long_lot_count=sum(
                1 for lot in snapshot.unpaired_lots if lot.side == "LONG"
            ),
            short_lot_count=sum(
                1 for lot in snapshot.unpaired_lots if lot.side == "SHORT"
            ),
            max_lots_per_side=limit,
            client_id_tag=str(int(at.timestamp())),
        )

    async def _handle_partial_fill_event(self, session: SymbolSession, event: dict[str, Any]) -> None:
        event_time = event.get("trade_time")
        if not isinstance(event_time, datetime):
            event_time = datetime.now(timezone.utc)
        partial_order = next((order for order in session.orders if order.client_id == str(event.get("client_id", ""))), None)
        price: float | None = None
        qty: float | None = None
        fee = 0.0
        exchange_realized_pnl = 0.0
        invalid_detail: str | None = None
        try:
            price = _positive_price(event.get("price", partial_order.price if partial_order is not None else 0.0), "partial fill price")
            qty = _positive_qty(event.get("qty", 0.0), "partial fill qty")
            fee = _non_negative_float(event.get("fee", 0.0), "partial fill fee")
            exchange_realized_pnl = _finite_float(event.get("realized_pnl", 0.0), "partial fill realized pnl")
            if partial_order is not None and qty > partial_order.qty + 1e-12:
                invalid_detail = "partial fill qty exceeds local order qty"
        except ValueError as exc:
            invalid_detail = str(exc)
        order_id = str(event.get("order_id", partial_order.order_id if partial_order is not None else event.get("client_id", "")))
        confirmed_summary: dict[str, Any] | None = None
        if invalid_detail is None and price is not None and qty is not None:
            confirmed_summary = {
                "price": price,
                "qty": qty,
                "fee": fee,
                "realized_pnl": exchange_realized_pnl,
                "trade_time": event_time,
            }
        trade_summary = await self._load_order_trade_summary(session.symbol, order_id)
        if trade_summary is not None:
            confirmed_summary = trade_summary
            price = trade_summary["price"]
            qty = trade_summary["qty"]
            fee = trade_summary["fee"]
            exchange_realized_pnl = trade_summary["realized_pnl"]
            event_time = trade_summary["trade_time"]
            invalid_detail = None
            if partial_order is not None and qty > partial_order.qty + 1e-12:
                invalid_detail = "partial fill qty exceeds local order qty"
        if invalid_detail is not None:
            self.repository.log_system(
                "ERROR",
                "partial_fill",
                "Partial fill event has invalid details; recording confirmed fill before closing session.",
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
            if partial_order is not None and confirmed_summary is not None:
                self._record_confirmed_partial_fill(session, partial_order, order_id, confirmed_summary)
            await self._close_session(session, "部分成交数据异常，执行安全平仓。", event_time)
            return
        if partial_order is None:
            await self._close_session(session, "部分成交订单无法匹配本地网格，执行安全平仓。", event_time)
            return
        local_order_qty = partial_order.qty
        if (
            confirmed_summary is not None
            and confirmed_summary["qty"] + _fill_qty_tolerance(local_order_qty) >= local_order_qty
        ):
            await self._continue_after_reconciled_partial_fill(
                session,
                partial_order,
                event,
                order_id,
                confirmed_summary,
                event_time,
                reconciliation="成交明细已确认完全成交，跳过撤单。",
            )
            return
        try:
            cancel_response = await self.exchange.cancel_order(session.symbol, partial_order.order_id)
            cancel_executed_qty = _non_negative_float(
                cancel_response.get("executedQty"),
                "cancel executed qty",
            )
            target_qty = max(qty or 0.0, cancel_executed_qty)
            if target_qty > partial_order.qty + 1e-12:
                raise ValueError("撤单响应累计成交量超过本地订单数量")
            final_summary = await self._load_order_trade_summary(
                session.symbol,
                order_id,
                attempts=5,
                min_qty=target_qty,
            )
            if final_summary is None or final_summary["qty"] > partial_order.qty + 1e-12:
                raise ValueError("撤单后无法取得有效累计成交明细")
            await self._continue_after_reconciled_partial_fill(
                session,
                partial_order,
                event,
                order_id,
                final_summary,
                event_time,
                reconciliation="剩余委托已撤销，按最终累计成交量继续网格。",
            )
        except Exception as exc:
            if _is_unknown_order_cancel_error(exc):
                reconciled = await self._reconcile_unknown_partial_cancel(
                    session,
                    partial_order,
                    order_id,
                )
                if reconciled is not None:
                    await self._continue_after_reconciled_partial_fill(
                        session,
                        partial_order,
                        event,
                        order_id,
                        reconciled["summary"],
                        event_time,
                        reconciliation=(
                            "撤单返回 -2011；已通过订单与成交明细完成对账，"
                            f"最终状态 {reconciled['status']}。"
                        ),
                    )
                    return
            latest_summary = await self._load_order_trade_summary(session.symbol, order_id, attempts=3)
            if latest_summary is not None and (
                confirmed_summary is None or latest_summary["qty"] > confirmed_summary["qty"]
            ):
                confirmed_summary = latest_summary
            if confirmed_summary is not None:
                self._record_confirmed_partial_fill(session, partial_order, order_id, confirmed_summary)
            self.repository.log_system(
                "ERROR",
                "partial_fill",
                "Failed to finalize partial fill; closing session.",
                f"session_id={session.session_id}, symbol={session.symbol}, order_id={order_id}, reason={exc}",
                event_time,
            )
            await self._close_session(session, "部分成交撤单或累计成交对账失败，执行安全平仓。", event_time)

    async def _continue_after_reconciled_partial_fill(
        self,
        session: SymbolSession,
        order: GridOrder,
        event: dict[str, Any],
        order_id: str,
        summary: dict[str, Any],
        event_time: datetime,
        *,
        reconciliation: str,
    ) -> None:
        order.qty = float(summary["qty"])
        final_event = {
            **event,
            "status": "FILLED",
            "price": summary["price"],
            "qty": summary["qty"],
            "fee": summary["fee"],
            "realized_pnl": summary["realized_pnl"],
            "trade_time": summary["trade_time"],
        }
        self.repository.log_system(
            "INFO",
            "partial_fill",
            "Partial fill reconciled; continuing grid with confirmed quantity.",
            json.dumps(
                {
                    "session_id": session.session_id,
                    "symbol": session.symbol,
                    "order_id": order_id,
                    "final_qty": summary["qty"],
                    "final_price": summary["price"],
                    "reconciliation": reconciliation,
                    "recoverable": True,
                },
                ensure_ascii=False,
            ),
            event_time,
        )
        await self._handle_order_filled_event_locked(final_event)

    async def _reconcile_unknown_partial_cancel(
        self,
        session: SymbolSession,
        order: GridOrder,
        order_id: str,
    ) -> dict[str, Any] | None:
        order_snapshot: dict[str, Any] = {}
        try:
            order_snapshot = await self.exchange.get_order(
                session.symbol,
                order_id,
                order.client_id,
            )
        except Exception:
            order_snapshot = {}
        summary = await self._load_order_trade_summary(
            session.symbol,
            order_id,
            attempts=5,
        )
        if summary is None:
            return None
        tolerance = _fill_qty_tolerance(order.qty)
        if summary["qty"] > order.qty + tolerance:
            return None
        status = str(order_snapshot.get("status") or "UNKNOWN").upper()
        fully_filled = summary["qty"] + tolerance >= order.qty
        terminal_partial = status in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
        if not fully_filled and not terminal_partial:
            return None
        return {
            "status": "FILLED" if fully_filled else status,
            "summary": summary,
        }

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
        event_time = event.get("event_time")
        if not isinstance(event_time, datetime):
            event_time = datetime.now(timezone.utc)
        price = _positive_price(event["price"], "price event")
        if self.round_active and self.current_window_id is not None and symbol in self.round_candidate_symbols:
            self.repository.update_round_candidate_market(
                self.current_window_id,
                symbol,
                price,
                event_time,
                bid_price=event.get("bid_price"),
                ask_price=event.get("ask_price"),
            )
        async with self._session_event_locks.setdefault(symbol, asyncio.Lock()):
            session = self.active_sessions.get(symbol)
            if session is None or session.state not in {
                GridState.RUNNING,
                GridState.DEFENSIVE,
                GridState.PAUSED,
                GridState.COOLDOWN,
            }:
                return None
            decision = self.risk.evaluate_symbol(session, price, event_time)
            if decision.action == RiskAction.NONE:
                return None
            await self._apply_risk_decision(session, decision.action, decision.reason, event_time)
            return decision.action.value

    async def handle_kline_closed_event(self, event: dict[str, Any]) -> str | None:
        if not bool(event.get("closed")) or not self.round_active:
            return None
        symbol = str(event.get("symbol") or "").strip().upper()
        if not symbol or symbol not in self.round_candidate_symbols:
            return None
        close_time = event.get("close_time")
        if not isinstance(close_time, datetime):
            return None
        previous = self._processed_kline_close_at.get(symbol)
        if previous is not None and close_time <= previous:
            return None
        self._processed_kline_close_at[symbol] = close_time
        if self.current_window_id is not None:
            self.repository.upsert_round_candidate(
                self.current_window_id,
                symbol,
                close_time,
                last_kline_close_at=close_time.isoformat(),
                data_stale=False,
            )
        result = await self.scan_round_once(close_time, trigger_close_at=close_time)
        return "coalesced" if result.status == "scan_coalesced" else "scanned"

    def market_stream_symbols(self) -> list[str]:
        return sorted(self.round_candidate_symbols | set(self.active_sessions))

    async def poll_active_sessions_once(self, now: datetime | None = None) -> list[tuple[str, str]]:
        current_time = now or datetime.now(timezone.utc)
        self._apply_runtime_config_draft(current_time)
        actions: list[tuple[str, str]] = []
        actions.extend(
            (f"command:{command_id}", status)
            for command_id, status in await self.process_control_commands_once(current_time)
        )
        commission_action = await self._monitor_active_maker_fee_if_due(current_time)
        if commission_action is not None:
            actions.append(("*", commission_action))
        processed_symbols: set[str] = set()
        stop_requests = self.repository.pending_session_stop_requests()
        control_requests = self.repository.pending_session_control_requests()
        for symbol, session in list(self.active_sessions.items()):
            processed_symbols.add(symbol)
            stop_request = stop_requests.get(session.session_id)
            if stop_request is not None:
                action = await self._apply_session_stop_request(session, stop_request, current_time)
                actions.append((symbol, action))
                continue
            control_request = control_requests.get(session.session_id)
            if control_request is not None:
                action = await self._apply_session_control_request(session, control_request, current_time)
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
            if session.state == GridState.PAUSED:
                try:
                    reconcile_action = await self._reconcile_active_session_position(session, current_time)
                except Exception as exc:
                    self.repository.log_system(
                        "ERROR",
                        "position_reconciliation",
                        "Position reconciliation failed while session paused; forcing close.",
                        f"session_id={session.session_id}, symbol={symbol}, error={exc}",
                        current_time,
                    )
                    await self._close_session(session, "暂停期间持仓对账异常，执行安全平仓。", current_time)
                    actions.append((symbol, "position_reconciliation_failed"))
                    continue
                if reconcile_action is not None:
                    await self._close_session(session, "暂停期间持仓不一致，执行安全平仓。", current_time)
                    actions.append((symbol, reconcile_action))
                    continue
                await self._refresh_session_current_volatility_if_due(session, current_time)
                decision = self.risk.evaluate_symbol(session, await self._current_price(symbol), current_time)
                if decision.action != RiskAction.NONE:
                    await self._apply_risk_decision(session, decision.action, decision.reason, current_time)
                    actions.append((symbol, decision.action.value))
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
            if session.state != GridState.DEFENSIVE:
                regrid_action = await self._recalculate_session_grid_if_due(session, current_time)
                if regrid_action is not None:
                    actions.append((symbol, regrid_action))
                    if regrid_action != "rolling_regrid_skipped":
                        continue
            await self._refresh_session_current_volatility_if_due(session, current_time)
            last_price = await self._current_price(symbol)
            regime_decision = self._regime_by_symbol.get(symbol)
            if self.feature_flags.regime_v2 and regime_decision is not None:
                should_cooldown, retention_reason = self._update_regime_retention(
                    session,
                    regime_decision,
                )
                if should_cooldown:
                    if regime_decision.verdict == "BLOCKED_SCORE":
                        await self._enter_defensive(session, retention_reason, current_time)
                        actions.append((symbol, RiskAction.DEFEND.value))
                    else:
                        await self._close_session(
                            session,
                            f"Regime 硬性阻断: {retention_reason}",
                            current_time,
                        )
                        actions.append((symbol, RiskAction.CLOSE.value))
                    continue
                if session.state == GridState.DEFENSIVE and regime_decision.allowed:
                    if await self._recover_from_defensive(session, current_time):
                        actions.append((symbol, "defensive_recovered"))

            inventory_snapshot: InventorySnapshot | None = None
            if self.feature_flags.inventory_manager:
                max_inventory_notional = (
                    (self._account_equity or self.config.total_capital_limit)
                    * self.config.effective_leverage_cap
                    * self.config.max_symbol_inventory_pct
                )
                inventory_decision = self.inventory.evaluate(
                    session.orders,
                    mark_price=last_price,
                    max_inventory_notional=max_inventory_notional,
                    trend_direction=(
                        regime_decision.features.trend_direction
                        if regime_decision is not None
                        else 0
                    ),
                )
                inventory_snapshot = inventory_decision.snapshot
                self._inventory_by_symbol[symbol] = inventory_snapshot
                self.repository.replace_inventory_lots(
                    session.session_id,
                    symbol,
                    inventory_snapshot.unpaired_lots,
                    current_time,
                )
                self.repository.create_inventory_snapshot(
                    session.session_id,
                    symbol,
                    inventory_snapshot,
                    current_time,
                )
                if inventory_decision.action == InventoryAction.CLOSE:
                    await self._close_session(session, inventory_decision.reason, current_time)
                    actions.append((symbol, RiskAction.CLOSE.value))
                    continue
                if inventory_decision.action != InventoryAction.ALLOW:
                    cancelled = await self.engine.suppress_inventory_increasing_orders(
                        session,
                        net_qty=inventory_snapshot.net_qty,
                    )
                    self._persist_session_orders(session)
                    self.repository.append_event(
                        "RISK_LIMIT_BREACHED",
                        current_time,
                        {
                            "source": "inventory",
                            "action": inventory_decision.action.value,
                            "reason": inventory_decision.reason,
                            "utilization": inventory_snapshot.utilization,
                            "cancelled_orders": len(cancelled),
                        },
                        session_id=session.session_id,
                        symbol=symbol,
                    )
                    actions.append((symbol, inventory_decision.action.value.lower()))

            window_pnl = self.repository.window_realized_pnl(self.current_window_id)
            decision = self.risk.evaluate_symbol(
                session,
                last_price,
                current_time,
                inventory=inventory_snapshot,
                account_equity=self._account_equity or self.config.total_capital_limit,
                window_pnl=window_pnl,
                window_stop_count=self.repository.window_stop_count(self.current_window_id),
            )
            if decision.action == RiskAction.NONE:
                continue
            self.repository.create_risk_snapshot(
                as_of_time=current_time,
                risk_level=(
                    "CRITICAL"
                    if decision.action in {RiskAction.CLOSE, RiskAction.FORCE_CLOSE, RiskAction.HALT_WINDOW}
                    else "HIGH"
                ),
                action=decision.action.value,
                reason=decision.reason,
                session_id=session.session_id,
                window_id=self.current_window_id,
                symbol=symbol,
                session_pnl=session.realized_pnl + (inventory_snapshot.unrealized_pnl if inventory_snapshot else 0.0),
                window_pnl=window_pnl,
                inventory_utilization=inventory_snapshot.utilization if inventory_snapshot else None,
                limits={
                    "max_session_loss_pct": self.config.max_session_loss_pct,
                    "max_window_loss_pct": self.config.max_window_loss_pct,
                    "max_symbol_inventory_pct": self.config.max_symbol_inventory_pct,
                },
            )
            await self._apply_risk_decision(session, decision.action, decision.reason, current_time)
            actions.append((symbol, decision.action.value))
        if not any(session.state == GridState.CLOSING for session in self.active_sessions.values()):
            actions.extend(await self._reconcile_inactive_positions_once(current_time, processed_symbols))
        return actions

    async def _monitor_active_maker_fee_if_due(self, at: datetime) -> str | None:
        if not self.active_sessions:
            return None
        interval = float(self.config.maker_fee_check_interval_seconds)
        if interval <= 0:
            return None
        if self._maker_fee_checked_at is not None:
            elapsed = (at - self._maker_fee_checked_at).total_seconds()
            if elapsed < interval:
                return None
        result = await self._active_maker_fee_health(sorted(self.active_sessions))
        self._maker_fee_checked_at = at
        status = str(result["status"])
        changed_count = int(result["changed_count"])
        message = "Maker fee changed." if changed_count or status == "warn" else "Binance maker fee health check completed."
        level = "ERROR" if status == "error" else "WARN" if status == "warn" or changed_count else "INFO"
        self.repository.log_system(
            level,
            "commission_health",
            message,
            json.dumps(result, ensure_ascii=False),
            at,
        )
        if status == "error":
            return "commission_error"
        if status == "warn":
            return "commission_warn"
        if changed_count:
            return "commission_changed"
        return None

    async def _active_maker_fee_health(self, symbols: list[str]) -> dict[str, Any]:
        details: list[dict[str, Any]] = []
        for symbol in symbols:
            previous = self._last_maker_fee_by_symbol.get(symbol)
            try:
                commission = await self.exchange.get_commission_rate(symbol)
                maker_fee = _non_negative_float(_required_float(commission, "maker"), "maker")
            except Exception as exc:
                details.append(
                    {
                        "symbol": symbol,
                        "status": "error",
                        "previous_maker": previous,
                        "commission": {},
                        "error": str(exc),
                    }
                )
                continue
            changed = previous is not None and abs(maker_fee - previous) > 1e-12
            status = "warn" if maker_fee > self.config.max_maker_fee_rate else "ok"
            details.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "maker": maker_fee,
                    "previous_maker": previous,
                    "changed": changed,
                    "max_maker_fee_rate": self.config.max_maker_fee_rate,
                    "commission": commission,
                }
            )
            self._last_maker_fee_by_symbol[symbol] = maker_fee
            try:
                self._last_taker_fee_by_symbol[symbol] = _non_negative_float(
                    commission.get("taker", 0.0),
                    "taker",
                )
            except ValueError:
                self._last_taker_fee_by_symbol[symbol] = 0.0

        error_count = sum(1 for item in details if item["status"] == "error")
        warn_count = sum(1 for item in details if item["status"] == "warn")
        changed_count = sum(1 for item in details if item.get("changed"))
        status = "error" if error_count else "warn" if warn_count else "ok"
        return {
            "status": status,
            "max_maker_fee_rate": self.config.max_maker_fee_rate,
            "checked_symbols": len(details),
            "ok_count": sum(1 for item in details if item["status"] == "ok"),
            "warn_count": warn_count,
            "error_count": error_count,
            "changed_count": changed_count,
            "symbols": details,
        }

    async def run_loop(
        self,
        max_iterations: int | None = None,
        sleep_fn=asyncio.sleep,
    ) -> list[str]:
        statuses: list[str] = []
        iteration = 0
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            current_time = datetime.now(timezone.utc)
            command_actions = await self.process_control_commands_once(current_time)
            if command_actions:
                statuses.extend(f"command:{command_id}:{status}" for command_id, status in command_actions)
            if self.round_active:
                stop_request = self.repository.round_stop_request()
                if stop_request is not None:
                    closed = await self.stop_round(str(stop_request.get("reason") or "控制台停止整轮"), current_time)
                    statuses.append("round_stopped:" + ",".join(closed))
                elif self.scheduler.should_force_close(current_time) or not self.scheduler.is_in_window(current_time):
                    closed = await self.stop_round("交易窗口结束", current_time)
                    statuses.append("round_stopped:" + ",".join(closed))
                else:
                    actions = await self.poll_active_sessions_once(current_time) if self.active_sessions else []
                    if self._next_round_scan_at is None or current_time >= self._next_round_scan_at:
                        result = await self.scan_round_once(current_time)
                        statuses.append(result.status)
                    else:
                        statuses.append("poll:" + ",".join(f"{symbol}:{action}" for symbol, action in actions))
                await sleep_fn(self.config.loop_interval_seconds)
                continue

            auto_actions = self.bootstrap_auto_entry(current_time)
            if auto_actions:
                statuses.extend(auto_actions)
            request = self.repository.round_start_request()
            if request is None:
                statuses.append("idle_waiting_start")
                await sleep_fn(self.config.loop_interval_seconds)
                continue
            try:
                result = await self.start_round(current_time)
            except Exception as exc:
                self.repository.update_round_start_request("failed", str(exc), datetime.now(timezone.utc))
                raise
            if result.status in {"outside_window", "force_close_window", "startup_check_failed"}:
                self.repository.update_round_start_request("failed", {"status": result.status}, datetime.now(timezone.utc))
            statuses.append(result.status)
            await sleep_fn(self.config.loop_interval_seconds)
        return statuses

    def bootstrap_auto_entry(self, now: datetime | None = None) -> list[str]:
        current_time = now or datetime.now(timezone.utc)
        control = self.repository.auto_trading_control()
        if not isinstance(control, dict) or not bool(control.get("enabled")):
            return []
        classify = getattr(self.scheduler, "classify_window", None)
        if callable(classify):
            window = classify(current_time)
            allowed = bool(getattr(window, "allowed", False))
            window_key = str(getattr(window, "window_key", "") or "")
            kind = getattr(getattr(window, "kind", None), "value", getattr(window, "kind", ""))
        else:
            allowed = bool(self.scheduler.is_in_window(current_time))
            window_key = f"LEGACY:{current_time.date().isoformat()}"
            kind = "LEGACY"
        if not allowed:
            return [f"auto_waiting_window:{kind}"]
        runtime = self.repository.runtime_state()
        runtime_id = str(runtime.get("runtime_id") or "")
        if not runtime_id:
            return ["auto_missing_runtime"]
        request_id = f"auto-round:{self.repository.account_id}:{window_key or kind}"
        try:
            _request, created = self.repository.ensure_round_start_request(
                runtime_id=runtime_id,
                reason="auto_trading_window",
                request_id=request_id,
                window_key=window_key or str(kind),
                requested_at=current_time,
            )
        except Exception as exc:
            return [f"auto_request_failed:{exc}"]
        return [f"auto_round_requested:{window_key}" if created else f"auto_round_exists:{window_key}"]

    async def process_control_commands_once(
        self,
        now: datetime | None = None,
    ) -> list[tuple[str, str]]:
        current_time = now or datetime.now(timezone.utc)
        results: list[tuple[str, str]] = []
        for command in self.repository.pending_control_commands():
            command_id = str(command.get("command_id") or "")
            command_type = str(command.get("command_type") or "").upper()
            target_id = command.get("target_id")
            try:
                self.repository.update_control_command(
                    command_id,
                    "ACCEPTED",
                    {"accepted_at": current_time.isoformat()},
                    current_time,
                )
                result = await self._execute_control_command(
                    command_type,
                    target_id,
                    command.get("payload") if isinstance(command.get("payload"), dict) else {},
                    str(command.get("reason") or "控制台命令"),
                    current_time,
                )
            except ValueError as exc:
                self.repository.update_control_command(
                    command_id,
                    "REJECTED",
                    {"error": str(exc)},
                    current_time,
                )
                results.append((command_id, "rejected"))
                continue
            except Exception as exc:
                self.repository.update_control_command(
                    command_id,
                    "FAILED",
                    {"error": str(exc)},
                    current_time,
                )
                self.repository.log_system(
                    "ERROR",
                    "control_command",
                    "v2 control command failed.",
                    f"command_id={command_id}, command_type={command_type}, error={exc}",
                    current_time,
                )
                results.append((command_id, "failed"))
                continue
            self.repository.update_control_command(
                command_id,
                "EXECUTED",
                result,
                current_time,
            )
            self.repository.append_event(
                "CONTROL_COMMAND",
                current_time,
                {
                    "command_id": command_id,
                    "command_type": command_type,
                    "result": result,
                },
                session_id=int(target_id) if command_type == "CLOSE_SESSION" and str(target_id).isdigit() else None,
            )
            results.append((command_id, "executed"))
        return results

    async def _execute_control_command(
        self,
        command_type: str,
        target_id: Any,
        payload: dict[str, Any],
        reason: str,
        at: datetime,
    ) -> dict[str, Any]:
        if command_type == "PAUSE_NEW_ENTRIES":
            self.repository.set_control_state("new_entries_paused", True, at)
            return {"new_entries_paused": True}
        if command_type == "RESUME_NEW_ENTRIES":
            risk = self.risk.can_open_new_symbol(
                [],
                self.config.capital_per_symbol,
                account_equity=self._account_equity or self.config.total_capital_limit,
                window_pnl=self.repository.window_realized_pnl(self.current_window_id),
                window_stop_count=self.repository.window_stop_count(self.current_window_id),
            )
            if risk.action in {RiskAction.HALT_WINDOW, RiskAction.BLOCK}:
                raise ValueError(risk.reason)
            self.repository.set_control_state("new_entries_paused", False, at)
            return {"new_entries_paused": False}
        if command_type == "CLOSE_SESSION":
            try:
                session_id = int(target_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("close-session 缺少有效 session_id。") from exc
            session = next(
                (item for item in self.active_sessions.values() if item.session_id == session_id),
                None,
            )
            if session is None:
                raise ValueError("目标会话不存在或已经关闭。")
            closed = await self._close_session(session, reason, at)
            if not closed:
                raise RuntimeError("会话清理未完成，将保持 CLOSING。")
            return {"session_id": session_id, "closed": True}
        if command_type == "STOP_ALL":
            closed = (
                await self.stop_round(reason, at)
                if self.round_active
                else await self.close_all_active_sessions(reason, at)
            )
            return {"closed_symbols": closed}
        if command_type == "SAFETY_SWEEP":
            closed = await self.close_all_active_sessions(reason, at)
            reconciled = await self.reconcile_positions_once(at, include_inactive=True)
            return {"closed_symbols": closed, "reconciled": reconciled}
        raise ValueError(f"不支持的控制命令: {command_type}")

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
        request_type = str(request.get("request_type") or "stop")
        is_manual_close = request_type == "manual_close"
        default_reason = "控制台手动平仓" if is_manual_close else "控制台手动停止网格"
        action_label = "手动平仓" if is_manual_close else "手动停止网格"
        reason = str(request.get("reason") or default_reason)
        request_id = str(request.get("request_id") or "")
        detail = json.dumps(
            {
                "session_id": session.session_id,
                "symbol": session.symbol,
                "request_id": request_id,
                "request_type": request_type,
                "reason": reason,
            },
            ensure_ascii=False,
        )
        self.repository.log_system(
            "WARN",
            "console_action",
            "Session manual close request is being applied." if is_manual_close else "Session stop request is being applied.",
            detail,
            at,
        )
        closed = await self._close_session(session, f"控制台{action_label}：{reason}", at)
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
                "Session manual close request completed." if is_manual_close else "Session stop request completed.",
                detail,
                at,
            )
            return "manual_close" if is_manual_close else "manual_stop"
        self.repository.update_session_stop_request(
            session.session_id,
            "closing",
            f"{action_label}请求已执行但清理未完成，下一轮继续重试。",
            at,
        )
        return "manual_close_pending" if is_manual_close else "manual_stop_pending"

    async def _apply_session_control_request(
        self,
        session: SymbolSession,
        request: dict[str, Any],
        at: datetime,
    ) -> str:
        action = str(request.get("action") or "").strip().lower()
        if action == "pause":
            if session.state == GridState.PAUSED:
                self.repository.update_session_control_request(session.session_id, "completed", "会话已处于暂停状态。", at)
                return "already_paused"
            try:
                await self.engine.pause_grid_orders(session)
            except Exception as exc:
                self.repository.update_session_control_request(session.session_id, "failed", str(exc), at)
                return "pause_failed"
            previous = session.state
            session.state = GridState.PAUSED
            session.state_entered_at = at
            self._persist_session_orders(session)
            self.repository.update_session_state(session.session_id, GridState.PAUSED.value)
            if self.round_active and self.current_window_id is not None:
                self.repository.mark_round_candidate_stage(
                    self.current_window_id,
                    session.symbol,
                    "paused",
                    at,
                    session.session_id,
                )
            self.state_machine.transition(session.symbol, GridState.PAUSED, "console_pause", at=at)
            self.repository.log_state(
                session.session_id,
                session.symbol,
                previous.value,
                GridState.PAUSED.value,
                "console_pause",
                str(request.get("reason") or "控制台暂停"),
                at,
            )
            self.repository.update_session_control_request(session.session_id, "completed", "已撤销网格挂单，持仓和保护性止损保留。", at)
            return "paused"
        if action == "resume":
            if session.state != GridState.PAUSED:
                self.repository.update_session_control_request(session.session_id, "completed", "会话当前不是暂停状态。", at)
                return "not_paused"
            effective_observer_config, effective_grid_config, _ = self._effective_next_entry_settings(at)
            current_price = await self._current_price(session.symbol)
            try:
                position = await self.exchange.get_position(session.symbol)
                tolerance = await self._position_tolerance(session.symbol)
                establish_seed = _position_exposure(position) <= tolerance
                params = await self.observer.observe_then_calculate(
                    session.symbol,
                    current_price,
                    should_abort=lambda: self.scheduler.should_force_close(),
                    observer_config=effective_observer_config,
                    grid_config=effective_grid_config,
                )
                params = replace(params, direction_mode=session.direction_mode)
                session.params = params
                self._persist_session_grid(session.session_id, params)
                await self.engine.start(
                    session,
                    current_price,
                    place_protection=False,
                    client_id_tag=f"r{int(at.timestamp())}",
                    establish_seed=establish_seed,
                )
                if establish_seed:
                    self._persist_seed_execution(session, at)
            except Exception as exc:
                self._persist_session_orders(session)
                self.repository.update_session_control_request(session.session_id, "failed", str(exc), at)
                return "resume_failed"
            session.state = GridState.RUNNING
            session.state_entered_at = at
            self._persist_session_orders(session)
            self.repository.update_session_state(session.session_id, GridState.RUNNING.value)
            self.repository.update_session_current_volatility(
                session.session_id,
                params.volatility_value,
                params.volatility_window,
                params.calculated_at,
            )
            self._volatility_refreshed_at[session.session_id] = params.calculated_at
            if self.round_active and self.current_window_id is not None:
                self.repository.upsert_round_candidate(
                    self.current_window_id,
                    session.symbol,
                    at,
                    price=current_price,
                    volatility_method=params.volatility_method,
                    volatility_value=params.volatility_value,
                    volatility_window=params.volatility_window,
                    threshold_met=True,
                    session_id=session.session_id,
                    stage="trading",
                    error=None,
                    calculated_at=params.calculated_at.isoformat(),
                    market_updated_at=at.isoformat(),
                    data_stale=False,
                )
            self.state_machine.transition(session.symbol, GridState.RUNNING, "console_resume", at=at)
            self.repository.log_state(
                session.session_id,
                session.symbol,
                GridState.PAUSED.value,
                GridState.RUNNING.value,
                "console_resume",
                str(request.get("reason") or "控制台恢复"),
                at,
            )
            self.repository.update_session_control_request(session.session_id, "completed", "已重新计算区间并恢复网格挂单。", at)
            return "resumed"
        self.repository.update_session_control_request(session.session_id, "failed", "不支持的控制动作。", at)
        return "control_failed"

    async def _apply_risk_decision(self, session: SymbolSession, action: RiskAction, reason: str, at: datetime) -> None:
        if session.state in {GridState.CLOSING, GridState.STOPPED}:
            return
        if action == RiskAction.HALT_WINDOW:
            if self.round_active:
                await self.stop_round(reason, at)
            else:
                await self.close_all_active_sessions(reason, at)
            return
        if action in {RiskAction.FORCE_CLOSE, RiskAction.CLOSE}:
            await self._close_session(session, reason, at)
            return
        if action == RiskAction.REDUCE:
            snapshot = self._inventory_by_symbol.get(session.symbol)
            if snapshot is not None:
                await self.engine.suppress_inventory_increasing_orders(
                    session,
                    net_qty=snapshot.net_qty,
                )
                self._persist_session_orders(session)
            return
        if action == RiskAction.DEFEND:
            await self._enter_defensive(session, reason, at)
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
            intent = order.order_intent
            if intent == OrderIntent.OPEN and order.entry_price is not None:
                intent = OrderIntent.REDUCE
            position_side = str(order.position_side or "").upper()
            if not position_side:
                if intent == OrderIntent.REDUCE:
                    position_side = "SHORT" if order.side == OrderSide.BUY else "LONG"
                else:
                    position_side = "LONG" if order.side == OrderSide.BUY else "SHORT"
            direction = 1.0 if position_side == "LONG" else -1.0
            if intent == OrderIntent.REDUCE:
                direction *= -1.0
            qty += direction * order.qty
        return qty

    @staticmethod
    def _expected_position_sides(session: SymbolSession) -> tuple[float, float]:
        long_qty = 0.0
        short_qty = 0.0
        for order in session.orders:
            if order.status != OrderStatus.FILLED:
                continue
            intent = order.order_intent
            if intent == OrderIntent.OPEN and order.entry_price is not None:
                intent = OrderIntent.REDUCE
            position_side = str(order.position_side or "").upper()
            if not position_side:
                if intent == OrderIntent.REDUCE:
                    position_side = "SHORT" if order.side == OrderSide.BUY else "LONG"
                else:
                    position_side = "LONG" if order.side == OrderSide.BUY else "SHORT"
            delta = -order.qty if intent == OrderIntent.REDUCE else order.qty
            if position_side == "LONG":
                long_qty += delta
            else:
                short_qty += delta
        return max(0.0, long_qty), max(0.0, short_qty)

    def _update_regime_retention(
        self,
        session: SymbolSession,
        decision: RegimeDecision,
    ) -> tuple[bool, str]:
        if (
            session.last_retention_decision_at is not None
            and decision.as_of <= session.last_retention_decision_at
        ):
            return False, "同一 Regime 快照已经消费。"
        session.last_retention_decision_at = decision.as_of
        if decision.allowed:
            session.soft_breach_count = 0
            self.repository.update_session_retention(
                session.session_id,
                0,
                decision.as_of,
            )
            return False, ""
        if decision.verdict == "BLOCKED_SCORE":
            session.soft_breach_count += 1
            self.repository.update_session_retention(
                session.session_id,
                session.soft_breach_count,
                decision.as_of,
            )
            limit = self.regime.config.soft_breach_limit
            return (
                session.soft_breach_count >= limit,
                "Regime 连续软性不达标 "
                f"{session.soft_breach_count}/{limit}: {decision.state}",
            )
        self.repository.update_session_retention(
            session.session_id,
            session.soft_breach_count,
            decision.as_of,
        )
        return (
            True,
            f"Regime 硬性阻断 {decision.verdict}: {decision.state}",
        )

    async def _enter_defensive(
        self,
        session: SymbolSession,
        reason: str,
        at: datetime,
    ) -> None:
        if session.state == GridState.DEFENSIVE:
            return
        position = await self.exchange.get_position(session.symbol)
        tolerance = await self._position_tolerance(session.symbol)
        cancelled = await self.engine.enter_defensive(
            session,
            has_inventory=_position_exposure(position) > tolerance,
        )
        self._persist_session_orders(session)
        old_state = session.state
        session.state = GridState.DEFENSIVE
        session.state_entered_at = at
        self.repository.update_session_state(session.session_id, GridState.DEFENSIVE.value)
        if self.round_active and self.current_window_id is not None:
            self.repository.mark_round_candidate_stage(
                self.current_window_id,
                session.symbol,
                "defensive",
                at,
                session.session_id,
            )
        self.state_machine.transition(
            session.symbol,
            GridState.DEFENSIVE,
            "regime_soft_defensive",
            reason,
            at,
        )
        self.repository.log_state(
            session.session_id,
            session.symbol,
            old_state.value,
            GridState.DEFENSIVE.value,
            "regime_soft_defensive",
            f"{reason}; cancelled_opening_orders={len(cancelled)}",
            at,
        )

    async def _recover_from_defensive(
        self,
        session: SymbolSession,
        at: datetime,
    ) -> bool:
        if session.state != GridState.DEFENSIVE:
            return False
        mismatch = await self._reconcile_active_session_position(session, at)
        if mismatch is not None:
            await self._close_session(
                session,
                "防御状态恢复前订单或持仓无法对账，执行安全退出。",
                at,
            )
            return False
        try:
            restored = await self.engine.restore_defensive_orders(
                session,
                client_id_tag=str(int(at.timestamp())),
            )
        except Exception as exc:
            self.repository.log_system(
                "WARN",
                "defensive",
                "Defensive grid restore failed; session remains defensive.",
                f"session_id={session.session_id}, symbol={session.symbol}, error={exc}",
                at,
            )
            return False
        self._persist_session_orders(session)
        old_state = session.state
        session.state = GridState.RUNNING
        session.state_entered_at = at
        self.repository.update_session_state(session.session_id, GridState.RUNNING.value)
        if self.round_active and self.current_window_id is not None:
            self.repository.mark_round_candidate_stage(
                self.current_window_id,
                session.symbol,
                "trading",
                at,
                session.session_id,
            )
        self.state_machine.transition(
            session.symbol,
            GridState.RUNNING,
            "regime_defensive_recovered",
            f"restored_orders={len(restored)}",
            at,
        )
        self.repository.log_state(
            session.session_id,
            session.symbol,
            old_state.value,
            GridState.RUNNING.value,
            "regime_defensive_recovered",
            f"restored_orders={len(restored)}",
            at,
        )
        return True

    async def _enter_cooldown(self, session: SymbolSession, reason: str, at: datetime) -> None:
        close_orders = await self.engine.force_close(session, reason)
        await self._record_force_close_trades(session, close_orders or [], at)
        if self._session_is_already_stopped(session):
            self._finalize_already_stopped_session(session, at)
            return
        self._persist_session_orders(session)
        old_state = session.state
        session.state = GridState.COOLDOWN
        session.state_entered_at = at
        self.repository.update_session_state(session.session_id, GridState.COOLDOWN.value)
        if self.round_active and self.current_window_id is not None:
            self.repository.mark_round_candidate_stage(
                self.current_window_id, session.symbol, "cooldown", at, session.session_id
            )
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
        recovery_kline_limit = max(
            self.cooldown.config.calm_window_minutes,
            self.regime.config.long_window + 2
            if (self.feature_flags.regime_v2 or self.feature_flags.adaptive_grid_v2)
            else 0,
        )
        klines = await self.exchange.get_klines(
            session.symbol,
            self.observer.observer_config.kline_interval,
            recovery_kline_limit,
        )
        klines = _closed_klines_as_of(klines, at)
        decision = self.cooldown.evaluate(
            klines,
            baseline_atr=session.params.baseline_atr,
            min_step_pct=session.params.step_pct,
            cooldown_started_at=session.state_entered_at,
            now=at,
        )
        if not decision.can_reobserve:
            return False

        try:
            position, open_orders = await asyncio.gather(
                self.exchange.get_position(session.symbol),
                self.exchange.get_open_orders(session.symbol),
            )
            tolerance = await self._position_tolerance(session.symbol)
            if _position_exposure(position) > tolerance:
                self.repository.log_system(
                    "WARN",
                    "cooldown",
                    "Cooldown recovery blocked because exchange position is not flat.",
                    json.dumps(
                        {
                            "session_id": session.session_id,
                            "symbol": session.symbol,
                            "position": _position_log_fields(position),
                            "tolerance": tolerance,
                        },
                        ensure_ascii=False,
                    ),
                    at,
                )
                return False
            if open_orders:
                self.repository.log_system(
                    "WARN",
                    "cooldown",
                    "Cooldown recovery blocked because exchange orders remain open.",
                    json.dumps(
                        {
                            "session_id": session.session_id,
                            "symbol": session.symbol,
                            "open_order_count": len(open_orders),
                        },
                        ensure_ascii=False,
                    ),
                    at,
                )
                return False
        except Exception as exc:
            self.repository.log_system(
                "WARN",
                "cooldown",
                "Cooldown recovery preflight failed; recovery remains blocked.",
                f"session_id={session.session_id}, symbol={session.symbol}, error={exc}",
                at,
            )
            return False

        regime_decision: RegimeDecision | None = None
        funding_rate = 0.0
        projected_funding_cost = 0.0
        prepared_params: GridParams | None = None
        if self.feature_flags.regime_v2 or self.feature_flags.adaptive_grid_v2:
            try:
                orderbook, funding_context, commission, symbol_rules = await asyncio.gather(
                    self.exchange.get_orderbook_depth(
                        session.symbol,
                        self.selector.config.depth_levels,
                    ),
                    self.exchange.get_funding_context(session.symbol),
                    self.exchange.get_commission_rate(session.symbol),
                    self.exchange.get_symbol_rules(session.symbol),
                )
                funding_rate = float(funding_context.get("funding_rate") or 0.0)
                projected_funding_cost = self._projected_funding_cost(
                    funding_rate,
                    funding_context.get("next_funding_time"),
                    at,
                )
                spread_pct, depth_usdt = _orderbook_liquidity(
                    orderbook,
                    self.selector.config.depth_levels,
                )
                maker_fee_rate = _non_negative_float(
                    commission.get("maker"),
                    "maker commission rate",
                )
                taker_fee_rate = _non_negative_float(
                    commission.get("taker", 0.0),
                    "taker commission rate",
                )
                self._last_maker_fee_by_symbol[session.symbol] = maker_fee_rate
                self._last_taker_fee_by_symbol[session.symbol] = taker_fee_rate
                self._symbol_rules_by_symbol[session.symbol] = dict(symbol_rules)
                structural_decision: RegimeDecision | None = None
                if self.feature_flags.regime_v2:
                    adaptive_grid = self._adaptive_grid_for_symbol(session.symbol)
                    structural_decision = self.regime.evaluate(
                        session.symbol,
                        klines,
                        spread_pct=spread_pct,
                        depth_usdt=depth_usdt,
                        funding_rate=float(funding_rate),
                        data_age_seconds=_kline_data_age_seconds(klines, at),
                        expected_step_pct=(
                            adaptive_grid.config.min_step_pct
                            if self.feature_flags.adaptive_grid_v2
                            else self.grid_config.min_step_pct
                        ),
                        cost_floor_pct=0.0,
                        running=False,
                        include_cost=False,
                        as_of=at,
                    )
                    if structural_decision.hard_blocks:
                        self._regime_by_symbol[session.symbol] = structural_decision
                        self._persist_regime_decision(
                            session.symbol,
                            structural_decision,
                            at,
                            session_id=session.session_id,
                        )
                        return False
                if self.feature_flags.adaptive_grid_v2:
                    current_price = await self._current_price(session.symbol)
                    prepared_params = self._adaptive_grid_for_symbol(session.symbol).generate(
                        session.symbol,
                        klines,
                        current_price=current_price,
                        funding_rate=funding_rate,
                        funding_cost_rate=projected_funding_cost,
                        maker_fee_rate=maker_fee_rate,
                        regime_score=structural_decision.grid_score if structural_decision else 100.0,
                        capital=session.capital,
                        leverage=session.leverage,
                        tick_size=_non_negative_float(symbol_rules.get("tick_size", 0.0), "tick_size"),
                        step_size=_non_negative_float(symbol_rules.get("step_size", 0.0), "step_size"),
                        min_qty=_non_negative_float(symbol_rules.get("min_qty", 0.0), "min_qty"),
                        min_notional=_non_negative_float(symbol_rules.get("min_notional", 0.0), "min_notional"),
                        direction_mode=session.direction_mode,
                        risk_budget=self._session_risk_budget(),
                        taker_fee_rate=taker_fee_rate,
                        calculated_at=at,
                    )
                if self.feature_flags.regime_v2:
                    if prepared_params is None:
                        current_price = await self._current_price(session.symbol)
                        prepared_params = calculate_grid_params(
                            session.symbol,
                            klines,
                            current_price,
                            projected_funding_cost,
                            self.grid_config,
                            calculated_at=at,
                        )
                    cost_breakdown = self._grid_cost_breakdown(
                        maker_fee_rate,
                        projected_funding_cost,
                    )
                    regime_decision = self.regime.evaluate(
                        symbol=session.symbol,
                        klines=klines,
                        spread_pct=spread_pct,
                        depth_usdt=depth_usdt,
                        funding_rate=funding_rate,
                        data_age_seconds=_kline_data_age_seconds(klines, at),
                        expected_step_pct=prepared_params.step_pct,
                        cost_floor_pct=cost_breakdown["total_cost_pct"],
                        running=False,
                        cost_breakdown=cost_breakdown,
                        as_of=at,
                    )
                    prepared_params = replace(
                        prepared_params,
                        regime_score=regime_decision.grid_score,
                        cost_floor_pct=cost_breakdown["total_cost_pct"],
                    )
                    self._regime_by_symbol[session.symbol] = regime_decision
                    self._persist_regime_decision(
                        session.symbol,
                        regime_decision,
                        at,
                        session_id=session.session_id,
                    )
            except Exception as exc:
                self.repository.log_system(
                    "WARN",
                    "cooldown",
                    "Cooldown recovery market preflight failed; recovery remains blocked.",
                    f"session_id={session.session_id}, symbol={session.symbol}, error={exc}",
                    at,
                )
                return False

        window_pnl = self.repository.window_realized_pnl(self.current_window_id)
        window_stop_count = self.repository.window_stop_count(self.current_window_id)
        other_sessions = [
            item
            for item in self.active_sessions.values()
            if item.session_id != session.session_id
        ]
        entry_risk = self.risk.can_open_new_symbol(
            other_sessions,
            session.capital,
            regime_allowed=regime_decision.allowed if regime_decision is not None else True,
            account_equity=self._account_equity or self.config.total_capital_limit,
            window_pnl=window_pnl,
            window_stop_count=window_stop_count,
        )
        if entry_risk.action != RiskAction.NONE:
            self.repository.create_risk_snapshot(
                as_of_time=at,
                scope="SESSION",
                scope_id=str(session.session_id),
                action=entry_risk.action.value,
                reason=entry_risk.reason,
                priority=entry_risk.priority,
                metrics={
                    "phase": "cooldown_recovery",
                    "window_pnl": window_pnl,
                    "window_stop_count": window_stop_count,
                },
                session_id=session.session_id,
                symbol=session.symbol,
            )
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
            if prepared_params is not None:
                params = prepared_params
            elif self.feature_flags.adaptive_grid_v2:
                if session.symbol not in self._last_maker_fee_by_symbol:
                    raise GridCalculationError(
                        "DATA_COMMISSION_ERROR: 冷却恢复前未取得交易所 Maker 费率。"
                    )
                params = self._adaptive_grid_for_symbol(session.symbol).generate(
                    session.symbol,
                    klines,
                    current_price=current_price,
                    funding_rate=float(funding_rate),
                    funding_cost_rate=projected_funding_cost,
                    maker_fee_rate=self._last_maker_fee_by_symbol[session.symbol],
                    regime_score=regime_decision.grid_score if regime_decision else 100.0,
                    capital=session.capital,
                    leverage=session.leverage,
                    tick_size=_non_negative_float(
                        self._symbol_rules_by_symbol.get(session.symbol, {}).get("tick_size", 0.0),
                        "tick_size",
                    ),
                    step_size=_non_negative_float(
                        self._symbol_rules_by_symbol.get(session.symbol, {}).get("step_size", 0.0),
                        "step_size",
                    ),
                    min_qty=_non_negative_float(
                        self._symbol_rules_by_symbol.get(session.symbol, {}).get("min_qty", 0.0),
                        "min_qty",
                    ),
                    min_notional=_non_negative_float(
                        self._symbol_rules_by_symbol.get(session.symbol, {}).get("min_notional", 0.0),
                        "min_notional",
                    ),
                    direction_mode=session.direction_mode,
                    risk_budget=self._session_risk_budget(),
                    taker_fee_rate=self._last_taker_fee_by_symbol.get(session.symbol, 0.0),
                    calculated_at=at,
                )
            else:
                params = await self.observer.collect_and_calculate(
                    session.symbol,
                    current_price,
                )
            params = replace(params, direction_mode=session.direction_mode)
            session.params = params
            session.orders.clear()
            self._persist_session_grid(session.session_id, params)
            await self.engine.start(
                session,
                current_price,
                client_id_tag=f"r{int(at.timestamp())}",
            )
            self._persist_seed_execution(session, at)
        except Exception as exc:
            await self._stop_after_cooldown_recovery_failure(session, exc, at)
            return False
        self._persist_session_orders(session)
        session.state = GridState.RUNNING
        session.state_entered_at = at
        session.soft_breach_count = 0
        self.repository.update_session_state(session.session_id, GridState.RUNNING.value)
        self.repository.update_session_soft_breach_count(session.session_id, 0)
        if self.round_active and self.current_window_id is not None:
            self.repository.mark_round_candidate_stage(
                self.current_window_id, session.symbol, "trading", at, session.session_id
            )
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
        if self.round_active and self.current_window_id is not None:
            self.round_stopped_symbols.add(session.symbol)
            self.repository.mark_round_candidate_stage(
                self.current_window_id, session.symbol, "stopped", at, session.session_id
            )
        if not self.active_sessions and not self.round_active:
            self._close_current_window(at)

    async def _close_session(self, session: SymbolSession, reason: str, at: datetime) -> bool:
        if self._session_is_already_stopped(session):
            self._finalize_already_stopped_session(session, at)
            return True
        old_state = session.state
        if old_state != GridState.CLOSING:
            self.state_machine.transition(session.symbol, GridState.CLOSING, "risk_close", reason, at)
            session.state = GridState.CLOSING
            session.state_entered_at = at
            self.repository.update_session_state(session.session_id, GridState.CLOSING.value)
            self.repository.log_state(
                session.session_id,
                session.symbol,
                old_state.value,
                GridState.CLOSING.value,
                "risk_close",
                reason,
                at,
            )
        try:
            close_orders = await self.engine.force_close(session, reason)
        except Exception as exc:
            self._persist_session_orders(session)
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
        await self._record_force_close_trades(session, close_orders or [], at)
        if self._session_is_already_stopped(session):
            self._finalize_already_stopped_session(session, at)
            return True
        self._persist_session_orders(session)
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
        if self.round_active and self.current_window_id is not None:
            self.round_stopped_symbols.add(session.symbol)
            self.repository.mark_round_candidate_stage(
                self.current_window_id, session.symbol, "stopped", at, session.session_id
            )
        if not self.active_sessions and not self.round_active:
            self._close_current_window(at)
        return True

    async def _load_order_trade_summary(
        self,
        symbol: str,
        order_id: str,
        attempts: int = 1,
        min_qty: float | None = None,
    ) -> dict[str, Any] | None:
        if not order_id:
            return None
        retry_count = max(1, attempts)
        for attempt in range(retry_count):
            try:
                trades = await self.exchange.get_order_trades(symbol, order_id)
            except Exception:
                trades = []
            try:
                summary = _summarize_exchange_trades(trades)
            except (TypeError, ValueError):
                summary = None
            if summary is not None and (min_qty is None or summary["qty"] + 1e-12 >= min_qty):
                return summary
            if attempt < retry_count - 1:
                await asyncio.sleep(0.2)
        return None

    async def _record_force_close_trades(
        self,
        session: SymbolSession,
        close_orders: list[dict[str, Any]],
        at: datetime,
    ) -> None:
        for response in close_orders:
            order_id = _response_order_id(response, _response_client_id_or_none(response))
            if self.repository.trade_exists(session.session_id, order_id):
                continue
            summary = await self._load_order_trade_summary(session.symbol, order_id, attempts=5)
            if summary is None:
                if order_id.isdigit():
                    self.repository.log_system(
                        "ERROR",
                        "trade_accounting",
                        "Force-close order filled but Binance trade details were unavailable.",
                        f"session_id={session.session_id}, symbol={session.symbol}, order_id={order_id}",
                        at,
                    )
                continue
            self.repository.create_trade(
                session_id=session.session_id,
                symbol=session.symbol,
                order_id=order_id,
                side=summary["side"],
                price=summary["price"],
                qty=summary["qty"],
                grid_index=None,
                grid_pnl=summary["realized_pnl"],
                trade_time=summary["trade_time"],
                fee=summary["fee"],
            )
            session.realized_pnl += summary["realized_pnl"] - summary["fee"]
            self.repository.update_session_pnl(session.session_id, session.realized_pnl)

    async def _record_grid_start_failure_fills(self, session: SymbolSession, at: datetime) -> None:
        for order in session.orders:
            if self.repository.trade_exists(session.session_id, order.order_id):
                continue
            summary = await self._load_order_trade_summary(session.symbol, order.order_id, attempts=5)
            if summary is None:
                continue
            try:
                self._record_confirmed_partial_fill(session, order, order.order_id, summary)
            except (TypeError, ValueError) as exc:
                self.repository.log_system(
                    "ERROR",
                    "trade_accounting",
                    "Grid-start fill details were invalid during cleanup.",
                    f"session_id={session.session_id}, symbol={session.symbol}, order_id={order.order_id}, error={exc}",
                    at,
                )

    def _record_confirmed_partial_fill(
        self,
        session: SymbolSession,
        order: GridOrder,
        order_id: str,
        summary: dict[str, Any],
    ) -> None:
        if self.repository.trade_exists(session.session_id, order_id):
            return
        price = _positive_price(summary.get("price"), "confirmed partial fill price")
        qty = _positive_qty(summary.get("qty"), "confirmed partial fill qty")
        fee = _non_negative_float(summary.get("fee", 0.0), "confirmed partial fill fee")
        exchange_realized_pnl = _finite_float(
            summary.get("realized_pnl", 0.0),
            "confirmed partial fill realized pnl",
        )
        trade_time = summary.get("trade_time")
        if not isinstance(trade_time, datetime):
            trade_time = datetime.now(timezone.utc)
        grid_pnl = self.engine.grid_pnl_for_fill(order, price)
        if grid_pnl is not None and abs(qty - order.qty) > 1e-12:
            grid_pnl *= qty / order.qty
        self.repository.create_trade(
            session_id=session.session_id,
            symbol=session.symbol,
            order_id=order_id,
            side=order.side.value,
            price=price,
            qty=qty,
            grid_index=order.grid_index,
            grid_pnl=grid_pnl,
            trade_time=trade_time,
            fee=fee,
        )
        pnl_delta = (grid_pnl if grid_pnl is not None else exchange_realized_pnl) - fee
        if abs(pnl_delta) > 1e-12:
            session.realized_pnl += pnl_delta
            self.repository.update_session_pnl(session.session_id, session.realized_pnl)
        order.fill_price = price
        order.filled_at = trade_time

    def _session_is_already_stopped(self, session: SymbolSession) -> bool:
        return session.state == GridState.STOPPED or self.state_machine.get_state(session.symbol) == GridState.STOPPED

    def _finalize_already_stopped_session(self, session: SymbolSession, at: datetime) -> None:
        session.state = GridState.STOPPED
        session.state_entered_at = at
        self._persist_session_orders(session)
        self.active_sessions.pop(session.symbol, None)
        if self.round_active and self.current_window_id is not None:
            self.round_stopped_symbols.add(session.symbol)
            self.repository.mark_round_candidate_stage(
                self.current_window_id, session.symbol, "stopped", at, session.session_id
            )
        if not self.active_sessions and not self.round_active:
            self._close_current_window(at)

    async def _current_price(self, symbol: str) -> float:
        ticker = await self.exchange.get_24h_ticker(symbol)
        return _ticker_last_price(ticker)

    async def _observe_grid_candidate(
        self,
        symbol: str,
        observer_config: ObserverConfig,
        grid_config: GridConfig,
    ) -> tuple[float, GridParams]:
        current_price = await self._current_price(symbol)
        params = await self.observer.observe_then_calculate(
            symbol,
            current_price,
            should_abort=lambda: self.scheduler.should_force_close(),
            observer_config=observer_config,
            grid_config=grid_config,
        )
        return current_price, params

    def _persist_session_orders(self, session: SymbolSession) -> None:
        self.repository.upsert_orders(session.session_id, session.orders)

    def _persist_seed_execution(self, session: SymbolSession, at: datetime) -> None:
        if session.seed_qty <= 0 or session.seed_entry_price is None:
            return
        session.seed_fee = (
            session.seed_qty
            * session.seed_entry_price
            * self._last_taker_fee_by_symbol.get(session.symbol, 0.0)
        )
        self.repository.update_session_seed(
            session.session_id,
            position_side=session.seed_position_side,
            qty=session.seed_qty,
            entry_price=session.seed_entry_price,
            slippage_pct=session.seed_slippage_pct,
            fee=session.seed_fee,
        )
        seed_order = next(
            (
                order
                for order in session.orders
                if order.order_intent.value == "SEED"
            ),
            None,
        )
        if seed_order is None or self.repository.trade_exists(
            session.session_id,
            seed_order.order_id,
        ):
            return
        self.repository.create_trade(
            session.session_id,
            session.symbol,
            seed_order.order_id,
            seed_order.side.value,
            session.seed_entry_price,
            session.seed_qty,
            seed_order.grid_index,
            None,
            seed_order.filled_at or at,
            fee=session.seed_fee,
        )

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
            params.regime_score,
            params.grid_mode,
            params.cost_floor_pct,
            params.parameter_version,
        )
        self.repository.create_grid_plan(session_id, params)
        self.repository.update_session_current_volatility(
            session_id,
            params.volatility_value,
            params.volatility_window,
            params.calculated_at,
        )
        self._volatility_refreshed_at[session_id] = params.calculated_at
        self._grid_recalculated_at[session_id] = params.calculated_at

    async def _recalculate_session_grid_if_due(self, session: SymbolSession, at: datetime) -> str | None:
        if (
            not self.grid_config.rolling_regrid_enabled
            or session.state != GridState.RUNNING
            or session.params is None
        ):
            return None
        last_recalculated = self._grid_recalculated_at.get(session.session_id)
        if last_recalculated is not None:
            elapsed = (at - last_recalculated).total_seconds()
            if elapsed < self.grid_config.rolling_regrid_seconds:
                return None

        self._grid_recalculated_at[session.session_id] = at
        if any(order.status == OrderStatus.FILLED for order in session.orders):
            self._log_rolling_regrid_skip(session, "session_has_filled_orders", at)
            return "rolling_regrid_skipped"

        position = await self.exchange.get_position(session.symbol)
        tolerance = await self._position_tolerance(session.symbol)
        exposure_qty = sum(qty for _side, qty, _position_side in _position_close_specs(position))
        if exposure_qty > tolerance:
            self._log_rolling_regrid_skip(
                session,
                "session_has_exchange_exposure",
                at,
                {**_position_log_fields(position), "tolerance": tolerance},
            )
            return "rolling_regrid_skipped"

        old_params = session.params
        current_price = await self._current_price(session.symbol)
        effective_observer_config, effective_grid_config, _max_concurrent = self._effective_next_entry_settings(at)
        try:
            params = await self.observer.calculate_from_recent_klines(
                session.symbol,
                current_price,
                observer_config=effective_observer_config,
                grid_config=effective_grid_config,
            )
        except Exception as exc:
            self.repository.log_system(
                "WARN",
                "rolling_regrid",
                "Rolling grid recalculation failed before touching orders.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                at,
            )
            return "rolling_regrid_failed"

        try:
            await self.exchange.cancel_all_orders(session.symbol)
        except Exception as exc:
            self.repository.log_system(
                "ERROR",
                "rolling_regrid",
                "Rolling grid recalculation cancel-all failed; keeping current grid.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                at,
            )
            return "rolling_regrid_failed"

        for order in session.orders:
            if order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELLED
        self._persist_session_orders(session)
        session.orders.clear()
        session.stop_protection_sides.clear()
        params = replace(params, direction_mode=session.direction_mode)
        session.params = params
        self._persist_session_grid(session.session_id, params)
        try:
            await self.engine.start(session, current_price)
        except Exception as exc:
            self._persist_session_orders(session)
            self.repository.log_system(
                "ERROR",
                "rolling_regrid",
                "Rolling grid restart failed; closing session for safety.",
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "symbol": session.symbol,
                        "reason": str(exc),
                    },
                    ensure_ascii=False,
                ),
                at,
            )
            await self._close_session(session, "滚动重算区间失败，执行安全平仓。", at)
            return "rolling_regrid_failed"

        self._persist_session_orders(session)
        self.repository.log_state(
            session.session_id,
            session.symbol,
            GridState.RUNNING.value,
            GridState.RUNNING.value,
            "rolling_regrid",
            (
                f"old_lower={old_params.lower}, old_upper={old_params.upper}, "
                f"new_lower={params.lower}, new_upper={params.upper}, "
                f"grid_num={params.grid_num}, step_pct={params.step_pct}"
            ),
            at,
        )
        self.repository.log_system(
            "INFO",
            "rolling_regrid",
            "Rolling grid recalculated.",
            json.dumps(
                {
                    "session_id": session.session_id,
                    "symbol": session.symbol,
                    "old_lower": old_params.lower,
                    "old_upper": old_params.upper,
                    "new_lower": params.lower,
                    "new_upper": params.upper,
                    "grid_num": params.grid_num,
                    "step_pct": params.step_pct,
                },
                ensure_ascii=False,
            ),
            at,
        )
        return "rolling_regrid"

    def _log_rolling_regrid_skip(
        self,
        session: SymbolSession,
        reason: str,
        at: datetime,
        extra: dict[str, Any] | None = None,
    ) -> None:
        detail = {
            "session_id": session.session_id,
            "symbol": session.symbol,
            "reason": reason,
        }
        if extra:
            detail.update(extra)
        self.repository.log_system(
            "INFO",
            "rolling_regrid",
            "Rolling grid recalculation skipped.",
            json.dumps(detail, ensure_ascii=False),
            at,
        )

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


def _fill_qty_tolerance(order_qty: float) -> float:
    return max(1e-12, abs(float(order_qty)) * 1e-9)


def _is_unknown_order_cancel_error(exc: BaseException) -> bool:
    detail = str(exc).lower()
    return "-2011" in detail or "unknown order" in detail

