from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import floor, isfinite
from typing import Callable

from core.models import (
    GridDirectionMode,
    GridOrder,
    OrderIntent,
    OrderSide,
    OrderStatus,
    SymbolSession,
)
from exchange.base import ExchangeClient


@dataclass(frozen=True)
class GridEngineConfig:
    qty_precision: int = 6
    price_precision: int = 6
    seed_max_slippage_pct: float = 0.002
    reduce_target_step_fraction_by_symbol: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderSyncEvent:
    order: GridOrder
    price: float
    qty: float


@dataclass(frozen=True)
class OrderSyncResult:
    filled: list[OrderSyncEvent]
    partially_filled: list[OrderSyncEvent]


SystemLogCallback = Callable[[str, str, str, str | None, datetime], None]

ORDER_CREATE_RECOVERY_ATTEMPTS = 5
ORDER_CREATE_RECOVERY_DELAY_SECONDS = 0.25


class GridEngine:
    def __init__(
        self,
        exchange: ExchangeClient,
        config: GridEngineConfig | None = None,
        log_system: SystemLogCallback | None = None,
    ) -> None:
        self.exchange = exchange
        self.config = config or GridEngineConfig()
        self.log_system = log_system

    async def start(
        self,
        session: SymbolSession,
        current_price: float,
        *,
        place_protection: bool = True,
        client_id_tag: str = "",
        establish_seed: bool = True,
    ) -> list[GridOrder]:
        if session.params is None:
            raise ValueError("启动网格前必须先计算 GridParams。")
        if not _positive_finite(current_price):
            raise ValueError("当前价格必须为正的有限数。")
        if not _positive_finite(session.capital):
            raise ValueError("会话本金必须为正的有限数。")
        if not _positive_finite(session.leverage):
            raise ValueError("杠杆倍数必须为正的有限数。")
        if not _positive_finite(session.params.grid_num):
            raise ValueError("网格数量必须为正的有限数。")

        rules = await self.exchange.get_symbol_rules(session.symbol)
        tick_size = _symbol_rule_positive_float(rules, "tick_size")
        step_size = _symbol_rule_positive_float(rules, "step_size")
        min_qty = _symbol_rule_non_negative_float(rules, "min_qty", default=0.0)
        min_notional = _symbol_rule_non_negative_float(rules, "min_notional", default=0.0)

        order_specs: list[tuple[int, OrderSide, float]] = []
        for index, raw_price in enumerate(session.params.grid_prices):
            if raw_price == current_price:
                continue
            side = OrderSide.BUY if raw_price < current_price else OrderSide.SELL
            price = self._round_to_step(raw_price, tick_size)
            if price <= 0:
                raise ValueError("网格价格按交易所精度取整后必须大于 0。")
            order_specs.append((index, side, price))

        quantities = self._order_quantities(session, order_specs, current_price, step_size)
        sized_order_specs = [
            (index, side, price, quantities[index])
            for index, side, price in order_specs
        ]
        if any(qty <= 0 or qty < min_qty for _index, _side, _price, qty in sized_order_specs):
            raise ValueError("每格下单量小于交易所最小下单量。")
        if min_notional > 0 and any(
            price * qty < min_notional
            for _index, _side, price, qty in sized_order_specs
        ):
            raise ValueError("每格名义金额小于交易所最小名义金额。")

        stop_buffer_pct = _stop_buffer_pct(session.params.lower, session.params.stop_loss_price)
        long_stop_price = self._round_to_step(session.params.stop_loss_price, tick_size)
        short_stop_price = self._round_to_step(session.params.upper * (1 + stop_buffer_pct), tick_size)
        if long_stop_price <= 0 or short_stop_price <= 0:
            raise ValueError("止损价格按交易所精度取整后必须大于 0。")

        await self.exchange.set_margin_type(session.symbol, "ISOLATED")
        await self.exchange.set_leverage(session.symbol, session.leverage)

        created: list[GridOrder] = []
        if session.direction_mode != GridDirectionMode.NEUTRAL and establish_seed:
            seed_order = await self._establish_seed_position(
                session,
                sized_order_specs,
                current_price=current_price,
                step_size=step_size,
                min_qty=min_qty,
                min_notional=min_notional,
                client_id_tag=client_id_tag,
            )
            session.orders.append(seed_order)
        elif (
            session.direction_mode != GridDirectionMode.NEUTRAL
            and session.seed_entry_price is None
        ):
            raise ValueError("方向网格恢复时缺少原种子成交价，拒绝盲目恢复。")
        for index, side, price, qty in sized_order_specs:
            tag = f"-{client_id_tag}" if client_id_tag else ""
            client_id = f"qg-{session.session_id}{tag}-{index}-{side.value.lower()}"
            position_side, order_intent, entry_price = _initial_order_metadata(
                session.direction_mode,
                side,
                session.seed_entry_price,
            )
            try:
                response = await self._place_limit_order_post_only_reconciled(
                    session.symbol,
                    side.value,
                    price,
                    qty,
                    client_id,
                    position_side=position_side,
                )
                order_id = _response_order_id(response, client_id)
            except Exception as exc:
                if _is_post_only_rejection(exc):
                    continue
                try:
                    if place_protection:
                        await self.exchange.cancel_all_orders(session.symbol)
                    else:
                        await self._cancel_grid_orders(session.symbol, created)
                except Exception as cancel_exc:
                    self._log_force_close_warning(
                        session,
                        "Cancel all orders failed after grid order setup failure; grid orders may still be open.",
                        f"order_error={exc}, cancel_error={cancel_exc}",
                    )
                    raise cancel_exc
                for order in created:
                    order.status = OrderStatus.CANCELLED
                raise
            order = GridOrder(
                symbol=session.symbol,
                order_id=order_id,
                client_id=client_id,
                grid_index=index,
                side=side,
                price=price,
                qty=qty,
                status=OrderStatus.OPEN,
                created_at=datetime.now(timezone.utc),
                entry_price=entry_price,
                position_side=position_side,
                order_intent=order_intent,
            )
            created.append(order)

        if not created:
            raise ValueError("所有 POST_ONLY 网格挂单均被拒绝。")

        session.orders.extend(created)
        if not place_protection:
            return created
        try:
            protection_sides = {
                GridDirectionMode.LONG: ("long",),
                GridDirectionMode.SHORT: ("short",),
                GridDirectionMode.NEUTRAL: ("long", "short"),
            }[session.direction_mode]
            for protection_side in protection_sides:
                await self._place_stop_protection_side(
                    session,
                    protection_side,
                    tick_size,
                    f"qg-{session.session_id}{f'-{client_id_tag}' if client_id_tag else ''}-stop-{protection_side}",
                )
        except Exception as exc:
            if _is_stop_requires_open_position(exc):
                self._log_force_close_warning(
                    session,
                    "Exchange requires an open position before placing close-position stop orders; delayed stop protection will be armed after the first fill.",
                    f"reason={exc}",
                )
                return created
            try:
                await self.exchange.cancel_all_orders(session.symbol)
            except Exception as cancel_exc:
                self._log_force_close_warning(
                    session,
                    "Cancel all orders failed after stop order setup failure; grid orders may still be open.",
                    f"stop_error={exc}, cancel_error={cancel_exc}",
                )
                raise cancel_exc
            for order in created:
                order.status = OrderStatus.CANCELLED
            raise exc

        return created

    async def _establish_seed_position(
        self,
        session: SymbolSession,
        order_specs: list[tuple[int, OrderSide, float, float]],
        *,
        current_price: float,
        step_size: float,
        min_qty: float,
        min_notional: float,
        client_id_tag: str = "",
    ) -> GridOrder:
        if session.direction_mode == GridDirectionMode.LONG:
            seed_side = OrderSide.BUY
            position_side = "LONG"
            reduce_side = OrderSide.SELL
        elif session.direction_mode == GridDirectionMode.SHORT:
            seed_side = OrderSide.SELL
            position_side = "SHORT"
            reduce_side = OrderSide.BUY
        else:
            raise ValueError("中性网格不应建立种子仓位。")

        seed_qty = self._round_to_step(
            sum(qty for _index, side, _price, qty in order_specs if side == reduce_side),
            step_size,
        )
        if seed_qty <= 0 or seed_qty < min_qty:
            raise ValueError("方向网格种子仓位小于交易所最小下单量。")
        if min_notional > 0 and current_price * seed_qty < min_notional:
            raise ValueError("方向网格种子仓位小于交易所最小名义金额。")

        tag = f"-{client_id_tag}" if client_id_tag else ""
        client_id = f"qg-{session.session_id}{tag}-seed-{position_side.lower()}"
        response = await self._place_market_order_reconciled(
            session.symbol,
            seed_side.value,
            seed_qty,
            reduce_only=False,
            position_side=position_side,
            client_id=client_id,
        )
        order_id = _response_order_id(response, client_id)
        confirmed = response
        if (
            str(response.get("status") or "").upper() != "FILLED"
            or _exchange_executed_qty(response) + _fill_qty_tolerance(seed_qty) < seed_qty
        ):
            confirmed = await self.exchange.get_order(session.symbol, order_id, client_id)
        executed_qty = _exchange_executed_qty(confirmed)
        if executed_qty + _fill_qty_tolerance(seed_qty) < seed_qty:
            raise ValueError("方向网格种子仓位未完全成交，拒绝启动网格。")
        fill_price = _exchange_order_price(confirmed, 0.0)
        slippage_pct = abs(fill_price / current_price - 1.0)
        if slippage_pct > self.config.seed_max_slippage_pct:
            raise ValueError(
                "方向网格种子仓位滑点超过上限："
                f"{slippage_pct:.6f} > {self.config.seed_max_slippage_pct:.6f}"
            )
        session.seed_position_side = position_side
        session.seed_qty = seed_qty
        session.seed_entry_price = fill_price
        session.seed_slippage_pct = slippage_pct
        now = datetime.now(timezone.utc)
        return GridOrder(
            symbol=session.symbol,
            order_id=order_id,
            client_id=client_id,
            grid_index=-1,
            side=seed_side,
            price=fill_price,
            qty=seed_qty,
            status=OrderStatus.FILLED,
            created_at=now,
            filled_at=now,
            fill_price=fill_price,
            position_side=position_side,
            order_intent=OrderIntent.SEED,
        )

    async def pause_grid_orders(self, session: SymbolSession) -> list[GridOrder]:
        cancelled = [order for order in session.orders if order.status == OrderStatus.OPEN]
        await self._cancel_grid_orders(session.symbol, cancelled)
        return cancelled

    async def suppress_inventory_increasing_orders(
        self,
        session: SymbolSession,
        *,
        net_qty: float,
        position_side: str | None = None,
    ) -> list[GridOrder]:
        if abs(net_qty) <= 1e-12:
            return []
        increasing_side = OrderSide.BUY if net_qty > 0 else OrderSide.SELL
        cancelled = [
            order
            for order in session.orders
            if order.status == OrderStatus.OPEN
            and order.order_intent == OrderIntent.OPEN
            and order.entry_price is None
            and (
                order.side == increasing_side
                or session.direction_mode != GridDirectionMode.NEUTRAL
            )
            and (
                position_side is None
                or str(order.position_side or "").upper() == position_side.upper()
            )
        ]
        await self._cancel_grid_orders(session.symbol, cancelled)
        return cancelled

    async def enforce_unpaired_lot_cap(
        self,
        session: SymbolSession,
        *,
        long_lot_count: int,
        short_lot_count: int,
        max_lots_per_side: int,
        client_id_tag: str,
    ) -> list[GridOrder]:
        if max_lots_per_side < 0:
            raise ValueError("max_lots_per_side 不能为负。")
        if max_lots_per_side == 0:
            return []
        changed: list[GridOrder] = []
        for position_side, lot_count, net_qty in (
            ("LONG", long_lot_count, 1.0),
            ("SHORT", short_lot_count, -1.0),
        ):
            if lot_count >= max_lots_per_side:
                changed.extend(
                    await self.suppress_inventory_increasing_orders(
                        session,
                        net_qty=net_qty,
                        position_side=position_side,
                    )
                )
                continue
            changed.extend(
                await self.restore_cancelled_opening_orders(
                    session,
                    position_side=position_side,
                    client_id_tag=client_id_tag,
                )
            )
        return changed

    async def restore_cancelled_opening_orders(
        self,
        session: SymbolSession,
        *,
        position_side: str,
        client_id_tag: str,
    ) -> list[GridOrder]:
        existing = {
            (
                order.grid_index,
                order.side,
                str(order.position_side or "").upper(),
                order.order_intent,
            )
            for order in session.orders
            if order.status == OrderStatus.OPEN
        }
        candidates = [
            order
            for order in session.orders
            if order.status == OrderStatus.CANCELLED
            and order.order_intent == OrderIntent.OPEN
            and str(order.position_side or "").upper() == position_side.upper()
        ]
        restored: list[GridOrder] = []
        for order in candidates:
            key = (
                order.grid_index,
                order.side,
                str(order.position_side or "").upper(),
                order.order_intent,
            )
            if key in existing:
                continue
            client_id = f"{order.client_id}-cap-{client_id_tag}"
            response = await self._place_limit_order_post_only_reconciled(
                session.symbol,
                order.side.value,
                order.price,
                order.qty,
                client_id,
                position_side=order.position_side,
            )
            restored_order = GridOrder(
                symbol=order.symbol,
                order_id=_response_order_id(response, client_id),
                client_id=client_id,
                grid_index=order.grid_index,
                side=order.side,
                price=order.price,
                qty=order.qty,
                status=OrderStatus.OPEN,
                created_at=datetime.now(timezone.utc),
                entry_price=None,
                position_side=order.position_side,
                order_intent=OrderIntent.OPEN,
            )
            restored.append(restored_order)
            existing.add(key)
        session.orders.extend(restored)
        return restored

    async def enter_defensive(
        self,
        session: SymbolSession,
        *,
        has_inventory: bool,
    ) -> list[GridOrder]:
        cancelled = [
            order
            for order in session.orders
            if order.status == OrderStatus.OPEN
            and order.order_intent not in {OrderIntent.PROTECTION, OrderIntent.SEED}
            and (not has_inventory or order.order_intent == OrderIntent.OPEN)
        ]
        await self._cancel_grid_orders(session.symbol, cancelled)
        return cancelled

    async def restore_defensive_orders(
        self,
        session: SymbolSession,
        *,
        client_id_tag: str,
    ) -> list[GridOrder]:
        restored: list[GridOrder] = []
        for order in [
            item
            for item in session.orders
            if item.status == OrderStatus.CANCELLED
            and item.order_intent in {OrderIntent.OPEN, OrderIntent.REDUCE}
        ]:
            client_id = f"{order.client_id}-d-{client_id_tag}"
            response = await self._place_limit_order_post_only_reconciled(
                session.symbol,
                order.side.value,
                order.price,
                order.qty,
                client_id,
                position_side=order.position_side,
            )
            restored.append(
                GridOrder(
                    symbol=order.symbol,
                    order_id=_response_order_id(response, client_id),
                    client_id=client_id,
                    grid_index=order.grid_index,
                    side=order.side,
                    price=order.price,
                    qty=order.qty,
                    status=OrderStatus.OPEN,
                    created_at=datetime.now(timezone.utc),
                    entry_price=order.entry_price,
                    position_side=order.position_side,
                    order_intent=order.order_intent,
                )
            )
        session.orders.extend(restored)
        return restored

    async def _cancel_grid_orders(self, symbol: str, orders: list[GridOrder]) -> None:
        for order in orders:
            await self.exchange.cancel_order(symbol, order.order_id)
            order.status = OrderStatus.CANCELLED

    def _order_quantities(
        self,
        session: SymbolSession,
        order_specs: list[tuple[int, OrderSide, float]],
        current_price: float,
        step_size: float,
    ) -> dict[int, float]:
        params = session.params
        if params is None:
            raise ValueError("计算网格仓位前必须存在 GridParams。")
        if not params.qty_weights:
            qty = self._round_to_step(
                session.capital * session.leverage / current_price / params.grid_num,
                step_size,
            )
            return {index: qty for index, _side, _price in order_specs}
        if len(params.qty_weights) != len(params.grid_prices):
            raise ValueError("qty_weights 长度必须等于 grid_prices 长度。")
        selected_weight = sum(params.qty_weights[index] for index, _side, _price in order_specs)
        if selected_weight <= 0:
            raise ValueError("qty_weights 合计必须为正数。")
        total_notional = session.capital * session.leverage
        return {
            index: self._round_to_step(
                total_notional * (params.qty_weights[index] / selected_weight) / price,
                step_size,
            )
            for index, _side, price in order_specs
        }

    async def ensure_stop_protection_for_position(self, session: SymbolSession, position_qty: float) -> None:
        qty = _finite_float(position_qty, "position qty")
        if abs(qty) <= 1e-12:
            return
        side_name = "long" if qty > 0 else "short"
        if side_name in session.stop_protection_sides:
            return
        rules = await self.exchange.get_symbol_rules(session.symbol)
        tick_size = _symbol_rule_positive_float(rules, "tick_size")
        await self._place_stop_protection_side(
            session,
            side_name,
            tick_size,
            f"qg-{session.session_id}-stop-{side_name}-pos",
        )

    async def _place_stop_protection_side(
        self,
        session: SymbolSession,
        side_name: str,
        tick_size: float,
        client_id: str,
    ) -> None:
        if session.params is None:
            raise ValueError("挂交易所端止损前必须存在 GridParams。")
        stop_buffer_pct = _stop_buffer_pct(session.params.lower, session.params.stop_loss_price)
        if side_name == "long":
            side = OrderSide.SELL.value
            stop_price = self._round_to_step(session.params.stop_loss_price, tick_size)
        elif side_name == "short":
            side = OrderSide.BUY.value
            stop_price = self._round_to_step(session.params.upper * (1 + stop_buffer_pct), tick_size)
        else:
            raise ValueError("未知止损保护方向。")
        if stop_price <= 0:
            raise ValueError("止损价格按交易所精度取整后必须大于 0。")
        _response_order_id(
            await self._place_stop_market_order_reconciled(
                session.symbol,
                side,
                stop_price,
                client_id,
                close_position=True,
            ),
            client_id,
        )
        session.stop_protection_sides.add(side_name)

    async def stop(self, session: SymbolSession, reason: str) -> None:
        await self.exchange.cancel_all_orders(session.symbol)
        for order in session.orders:
            if order.status == OrderStatus.OPEN:
                order.status = OrderStatus.CANCELLED

    async def sync_orders(self, session: SymbolSession) -> OrderSyncResult:
        open_orders = await self.exchange.get_open_orders(session.symbol)
        open_ids = {str(order.get("orderId", order.get("client_id", order.get("clientOrderId", "")))) for order in open_orders}
        open_client_ids = {str(order.get("client_id", order.get("clientOrderId", ""))) for order in open_orders}
        open_by_id = {
            str(order.get("orderId", order.get("client_id", order.get("clientOrderId", "")))): order
            for order in open_orders
        }
        open_by_client_id = {
            str(order.get("client_id", order.get("clientOrderId", ""))): order
            for order in open_orders
        }
        inferred_fills: list[OrderSyncEvent] = []
        inferred_partial_fills: list[OrderSyncEvent] = []
        for order in session.orders:
            if order.status != OrderStatus.OPEN:
                continue
            open_order = open_by_id.get(order.order_id) or open_by_client_id.get(order.client_id)
            if open_order is not None:
                executed_qty = _exchange_executed_qty(open_order)
                if executed_qty > 1e-12:
                    fill_price = _exchange_order_price(open_order, order.price)
                    if executed_qty > order.qty + 1e-12:
                        inferred_fills.append(OrderSyncEvent(order=order, price=fill_price, qty=executed_qty))
                    elif executed_qty + 1e-12 < order.qty:
                        inferred_partial_fills.append(OrderSyncEvent(order=order, price=fill_price, qty=executed_qty))
                    else:
                        order.status = OrderStatus.FILLED
                        order.filled_at = datetime.now(timezone.utc)
                        inferred_fills.append(OrderSyncEvent(order=order, price=fill_price, qty=executed_qty))
                continue
            if order.order_id not in open_ids and order.client_id not in open_client_ids:
                try:
                    exchange_order = await self.exchange.get_order(session.symbol, order.order_id, order.client_id)
                except Exception as exc:
                    self._log_reconciliation_warning(
                        session,
                        order,
                        "Order lookup failed during reconciliation; keeping local order open.",
                        f"reason={exc}",
                    )
                    continue
                exchange_status = str(exchange_order.get("status", exchange_order.get("X", ""))).upper()
                if exchange_status == "FILLED":
                    try:
                        fill_qty = _exchange_order_qty(exchange_order, order.qty)
                        fill_price = _exchange_order_price(exchange_order, order.price)
                    except ValueError as exc:
                        self._log_reconciliation_warning(
                            session,
                            order,
                            "Invalid exchange fill details during reconciliation; keeping local order open.",
                            f"reason={exc}",
                        )
                        continue
                    if fill_qty + 1e-12 < order.qty:
                        inferred_partial_fills.append(
                            OrderSyncEvent(
                                order=order,
                                price=fill_price,
                                qty=fill_qty,
                            )
                        )
                        continue
                    if fill_qty > order.qty + 1e-12:
                        inferred_fills.append(
                            OrderSyncEvent(
                                order=order,
                                price=fill_price,
                                qty=fill_qty,
                            )
                        )
                        continue
                    order.status = OrderStatus.FILLED
                    order.filled_at = datetime.now(timezone.utc)
                    inferred_fills.append(
                        OrderSyncEvent(
                            order=order,
                            price=fill_price,
                            qty=fill_qty,
                        )
                    )
                elif exchange_status == "PARTIALLY_FILLED":
                    try:
                        fill_price = _exchange_order_price(exchange_order, order.price)
                        fill_qty = _exchange_order_qty(exchange_order, order.qty)
                    except ValueError as exc:
                        self._log_reconciliation_warning(
                            session,
                            order,
                            "Invalid exchange fill details during reconciliation; keeping local order open.",
                            f"reason={exc}",
                        )
                        continue
                    inferred_partial_fills.append(
                        OrderSyncEvent(
                            order=order,
                            price=fill_price,
                            qty=fill_qty,
                        )
                    )
                elif exchange_status in {"CANCELED", "CANCELLED", "EXPIRED"}:
                    order.status = OrderStatus.CANCELLED
                elif exchange_status == "REJECTED":
                    order.status = OrderStatus.REJECTED
                else:
                    self._log_reconciliation_warning(
                        session,
                        order,
                        "Unknown exchange order status during reconciliation; keeping local order open.",
                        f"exchange_status={exchange_status or '<missing>'}",
                    )
        return OrderSyncResult(filled=inferred_fills, partially_filled=inferred_partial_fills)

    def _log_reconciliation_warning(
        self,
        session: SymbolSession,
        order: GridOrder,
        message: str,
        detail: str,
    ) -> None:
        if self.log_system is None:
            return
        self.log_system(
            "WARN",
            "order_reconciliation",
            message,
            (
                f"session_id={session.session_id}, symbol={session.symbol}, "
                f"order_id={order.order_id}, client_id={order.client_id}, {detail}"
            ),
            datetime.now(timezone.utc),
        )

    def _log_force_close_warning(self, session: SymbolSession, message: str, detail: str) -> None:
        if self.log_system is None:
            return
        self.log_system(
            "WARN",
            "force_close",
            message,
            f"session_id={session.session_id}, symbol={session.symbol}, {detail}",
            datetime.now(timezone.utc),
        )

    async def handle_order_filled(
        self,
        session: SymbolSession,
        client_id: str,
        fill_price: float | None = None,
    ) -> GridOrder | None:
        if session.params is None:
            raise ValueError("补单前必须存在 GridParams。")

        filled_order = next((order for order in session.orders if order.client_id == client_id), None)
        if filled_order is None:
            return None
        if filled_order.status == OrderStatus.FILLED and filled_order.fill_price is not None:
            return None
        if fill_price is not None:
            fill_price = _positive_float(fill_price, "fill_price")
        filled_order.status = OrderStatus.FILLED
        filled_order.filled_at = datetime.now(timezone.utc)
        filled_order.fill_price = fill_price

        if filled_order.side == OrderSide.BUY:
            next_index = filled_order.grid_index + 1
            next_side = OrderSide.SELL
            entry_price = None if filled_order.entry_price is not None else fill_price or filled_order.price
        else:
            next_index = filled_order.grid_index - 1
            next_side = OrderSide.BUY
            entry_price = None if filled_order.entry_price is not None else fill_price or filled_order.price
        next_position_side = _refill_grid_position_side(next_side, entry_price)
        if next_index < 0 or next_index >= len(session.params.grid_prices):
            return None

        rules = await self.exchange.get_symbol_rules(session.symbol)
        tick_size = _symbol_rule_positive_float(rules, "tick_size")
        step_size = _symbol_rule_positive_float(rules, "step_size")
        min_qty = _symbol_rule_non_negative_float(rules, "min_qty", default=0.0)
        min_notional = _symbol_rule_non_negative_float(rules, "min_notional", default=0.0)
        price = session.params.grid_prices[next_index]
        if entry_price is not None:
            fraction = self.config.reduce_target_step_fraction_by_symbol.get(
                session.symbol.upper(),
                1.0,
            )
            if not 0 < fraction <= 1:
                raise ValueError("减仓目标比例必须在 (0, 1] 内。")
            price = filled_order.price + (price - filled_order.price) * fraction
            price = self._round_reduce_target(price, tick_size, next_side)
        else:
            price = self._round_to_step(price, tick_size)
        refill_qty = self._round_to_step(filled_order.qty, step_size)
        if refill_qty <= 0 or refill_qty < min_qty:
            raise ValueError("补单数量小于交易所最小下单量。")
        if min_notional > 0 and price * refill_qty < min_notional:
            raise ValueError("补单名义金额小于交易所最小名义金额。")
        new_client_id = f"{filled_order.client_id}-re-{next_side.value.lower()}"
        response = await self._place_limit_order_post_only_reconciled(
            session.symbol,
            next_side.value,
            price,
            refill_qty,
            new_client_id,
            position_side=next_position_side,
        )
        order_id = _response_order_id(response, new_client_id)
        new_order = GridOrder(
            symbol=session.symbol,
            order_id=order_id,
            client_id=new_client_id,
            grid_index=next_index,
            side=next_side,
            price=price,
            qty=refill_qty,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
            entry_price=entry_price,
            position_side=next_position_side,
            order_intent=(
                OrderIntent.REDUCE if entry_price is not None else OrderIntent.OPEN
            ),
        )
        session.orders.append(new_order)
        return new_order

    async def _place_limit_order_post_only_reconciled(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict:
        try:
            return await self.exchange.place_limit_order_post_only(
                symbol,
                side,
                price,
                qty,
                client_id,
                position_side=position_side,
            )
        except Exception as exc:
            recovered = await self._recover_order_by_client_id_after_create_exception(symbol, client_id, exc)
            if recovered is not None:
                return recovered
            raise

    async def _place_stop_market_order_reconciled(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ) -> dict:
        try:
            return await self.exchange.place_stop_market_order(
                symbol,
                side,
                stop_price,
                client_id,
                close_position=close_position,
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

    @staticmethod
    def grid_pnl_for_fill(order: GridOrder, fill_price: float) -> float | None:
        if order.entry_price is None:
            return None
        entry_price = _positive_float(order.entry_price, "entry_price")
        valid_fill_price = _positive_float(fill_price, "fill_price")
        qty = _positive_float(order.qty, "order qty")
        if order.side == OrderSide.SELL:
            pnl = (valid_fill_price - entry_price) * qty
        else:
            pnl = (entry_price - valid_fill_price) * qty
        return _finite_float(pnl, "grid pnl")

    async def force_close(self, session: SymbolSession, reason: str) -> list[dict]:
        cancel_error: Exception | None = None
        try:
            await self.stop(session, reason)
        except Exception as exc:
            cancel_error = exc
            self._log_force_close_warning(
                session,
                "Cancel all orders failed during force close; attempting position close anyway.",
                f"reason={exc}",
        )
        position = await self.exchange.get_position(session.symbol)
        close_orders: list[dict] = []
        for side, qty, position_side in _position_close_specs(position):
            kwargs = {"position_side": position_side} if position_side is not None else {}
            client_id = _force_close_client_id(session, side, position_side)
            response = await self._place_market_order_reconciled(
                    session.symbol,
                    side,
                    qty,
                    reduce_only=True,
                    client_id=client_id,
                    **kwargs,
                )
            _response_order_id(response, client_id)
            close_orders.append(response)
        if cancel_error is not None:
            raise cancel_error
        return close_orders

    async def _place_market_order_reconciled(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
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
            if client_id is None:
                raise
            recovered = await self._recover_order_by_client_id_after_create_exception(symbol, client_id, exc)
            if recovered is not None:
                return recovered
            raise

    @staticmethod
    def _round_down(value: float, precision: int) -> float:
        factor = 10**precision
        return floor(value * factor) / factor

    @staticmethod
    def _round_to_step(value: float, step: float) -> float:
        if step <= 0:
            return value
        value_decimal = Decimal(str(value))
        step_decimal = Decimal(str(step))
        units = (value_decimal / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
        return float(units * step_decimal)

    @staticmethod
    def _round_reduce_target(value: float, step: float, side: OrderSide) -> float:
        if step <= 0:
            return value
        value_decimal = Decimal(str(value))
        step_decimal = Decimal(str(step))
        rounding = ROUND_CEILING if side == OrderSide.SELL else ROUND_FLOOR
        units = (value_decimal / step_decimal).to_integral_value(rounding=rounding)
        return float(units * step_decimal)


def _is_post_only_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    post_only_markers = ("post only", "gtx", "-5022", "would immediately match")
    return any(marker in text for marker in post_only_markers)


def _is_order_create_status_unknown(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "status unknown",
        "timeout waiting for response from backend server",
        "send status unknown",
        "execution status unknown",
        "bad gateway",
        "non-json or raw transport error",
    )
    return any(marker in text for marker in markers)


def _is_recovered_order(order: dict, expected_client_id: str) -> bool:
    status = str(order.get("status", order.get("X", ""))).upper()
    if status in {"", "UNKNOWN", "NOT_FOUND", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}:
        return False
    response_client_id = _response_client_id(order)
    if response_client_id is not None and response_client_id != expected_client_id:
        return False
    return any(order.get(key) not in (None, "") for key in ("orderId", "client_id", "clientOrderId", "origClientOrderId"))


def _is_stop_requires_open_position(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = ("-4509", "open position", "open positions", "positions are available")
    return any(marker in text for marker in markers)


def _initial_grid_position_side(side: OrderSide) -> str:
    return "LONG" if side == OrderSide.BUY else "SHORT"


def _initial_order_metadata(
    mode: GridDirectionMode,
    side: OrderSide,
    seed_entry_price: float | None,
) -> tuple[str, OrderIntent, float | None]:
    if mode == GridDirectionMode.LONG:
        return (
            "LONG",
            OrderIntent.OPEN if side == OrderSide.BUY else OrderIntent.REDUCE,
            None if side == OrderSide.BUY else seed_entry_price,
        )
    if mode == GridDirectionMode.SHORT:
        return (
            "SHORT",
            OrderIntent.REDUCE if side == OrderSide.BUY else OrderIntent.OPEN,
            seed_entry_price if side == OrderSide.BUY else None,
        )
    return _initial_grid_position_side(side), OrderIntent.OPEN, None


def _refill_grid_position_side(side: OrderSide, entry_price: float | None) -> str:
    if entry_price is None:
        return _initial_grid_position_side(side)
    return "SHORT" if side == OrderSide.BUY else "LONG"


def _fill_qty_tolerance(order_qty: float) -> float:
    return max(1e-12, abs(float(order_qty)) * 1e-9)


def _force_close_client_id(session: SymbolSession, side: str, position_side: str | None) -> str:
    close_side = (position_side or side).lower()
    return f"qg-{session.session_id}-close-{close_side}"


def _response_order_id(response: dict, expected_client_id: str | None = None) -> str:
    response_client_id = _response_client_id(response)
    if (
        expected_client_id is not None
        and response_client_id is not None
        and response_client_id != expected_client_id
    ):
        raise ValueError("订单响应 client id 不匹配。")
    for key in ("orderId", "client_id", "clientOrderId", "origClientOrderId"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    raise ValueError("订单响应缺少订单标识。")


def _response_client_id(response: dict) -> str | None:
    for key in ("client_id", "clientOrderId", "origClientOrderId"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _stop_buffer_pct(lower: float, stop_loss_price: float) -> float:
    if lower <= 0:
        return 0.0
    return max(0.0, 1 - stop_loss_price / lower)


def _exchange_order_price(order: dict, fallback: float) -> float:
    invalid_error: ValueError | None = None
    saw_value = False
    for key in ("avgPrice", "ap", "price", "p"):
        value = order.get(key)
        if value not in (None, ""):
            saw_value = True
            try:
                return _positive_float(value, key)
            except ValueError as exc:
                invalid_error = exc
                continue
    if saw_value:
        raise invalid_error or ValueError("exchange order price invalid")
    return _positive_float(fallback, "fallback price")


def _exchange_order_qty(order: dict, fallback: float) -> float:
    for key in ("executedQty", "z", "cumQty"):
        value = order.get(key)
        if value not in (None, ""):
            return _positive_float(value, key)
    return _positive_float(fallback, "fallback qty")


def _exchange_executed_qty(order: dict) -> float:
    for key in ("executedQty", "z", "cumQty"):
        value = order.get(key)
        if value not in (None, ""):
            return _non_negative_float(value, key)
    return 0.0


def _position_qty(position: dict) -> float:
    for key in ("qty", "positionAmt"):
        value = position.get(key)
        if value not in (None, ""):
            return _finite_float(value, key)
    raise ValueError("持仓响应缺少数量字段。")


def _position_close_specs(position: dict) -> list[tuple[str, float, str | None]]:
    if "long_qty" in position or "short_qty" in position:
        long_qty = _non_negative_float(position.get("long_qty", 0.0), "long_qty")
        short_qty = _non_negative_float(position.get("short_qty", 0.0), "short_qty")
        specs: list[tuple[str, float, str | None]] = []
        if long_qty > 0:
            specs.append((OrderSide.SELL.value, long_qty, "LONG"))
        if short_qty > 0:
            specs.append((OrderSide.BUY.value, short_qty, "SHORT"))
        return specs

    position_qty = _position_qty(position)
    qty = abs(position_qty)
    if qty <= 0:
        return []
    side = OrderSide.SELL.value if position_qty > 0 else OrderSide.BUY.value
    return [(side, qty, None)]


def _positive_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0


def _finite_float(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效数字。") from exc
    if not isfinite(number):
        raise ValueError(f"{label} 不是有限数字。")
    return number


def _positive_float(value, label: str) -> float:
    number = _finite_float(value, label)
    if number <= 0:
        raise ValueError(f"{label} 必须大于 0。")
    return number


def _non_negative_float(value, label: str) -> float:
    number = _finite_float(value, label)
    if number < 0:
        raise ValueError(f"{label} 不得小于 0。")
    return number


def _symbol_rule_positive_float(rules: dict, key: str) -> float:
    return _positive_float(rules.get(key), f"symbol rule {key}")


def _symbol_rule_non_negative_float(rules: dict, key: str, default: float = 0.0) -> float:
    value = rules.get(key, default)
    if value in (None, ""):
        value = default
    return _non_negative_float(value, f"symbol rule {key}")
