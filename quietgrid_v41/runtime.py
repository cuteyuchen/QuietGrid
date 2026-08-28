from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from db.database import init_db
from exchange.shadow import PAPER_BASELINE, ShadowBroker, ShadowExchangeClient, ShadowExecutionProfile
from quietgrid_v41.events import MarketEvent
from quietgrid_v41.sessions import session_context
from strategy.frozen_31111_compiler import (
    Frozen31111ControllerCompiler,
    assert_frozen_runtime_parity,
)
from strategy.frozen_31111_runtime import Frozen31111Runtime


CORE_RUNTIME_TABLES = (
    "windows",
    "sessions",
    "orders",
    "trades",
    "state_logs",
    "system_logs",
    "control_state",
    "selection_candidates",
    "round_candidates",
)


class RuntimeDatabaseInitializationError(RuntimeError):
    pass


def _ensure_runtime_db_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    present = {str(row[0]) for row in rows}
    missing = sorted(set(CORE_RUNTIME_TABLES) - present)
    if missing:
        raise RuntimeDatabaseInitializationError(f"runtime database missing core tables: {missing}")


def _frozen_controller_config(frozen: Frozen31111Runtime, database_path: Path, repo_root: str | Path = "."):
    """Build the controller input from the attested config, with runtime-only wiring."""
    from core.config import AppConfig

    manifest = Frozen31111ControllerCompiler(frozen, repo_root, database_path).compile()
    assert_frozen_runtime_parity(manifest)
    sections = manifest["effective_raw_config"]
    return AppConfig(
        raw=sections,
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=False,
        account_id="v41-shadow",
        account_label="QuietGrid v4.1 Shadow",
    )


class MarketEventSource(Protocol):
    def __aiter__(self) -> AsyncIterator[MarketEvent | Mapping[str, Any]]: ...


class IterableMarketEventSource:
    def __init__(self, events: Iterable[MarketEvent | Mapping[str, Any]]) -> None:
        self.events = tuple(events)

    async def __aiter__(self) -> AsyncIterator[MarketEvent | Mapping[str, Any]]:
        for event in self.events:
            yield event


class BinancePublicTradeStream:
    """Production public websocket source; it never accepts credentials."""

    def __init__(self, symbols: Iterable[str], *, ws_url: str = "wss://fstream.binance.com/stream", reconnect_delay: float = 1.0, rest_recovery: Callable[[Iterable[str]], Any] | None = None) -> None:
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.ws_url = ws_url.rstrip("/")
        self.reconnect_delay = reconnect_delay
        self.reconnect_count = 0
        self.last_error: str | None = None
        self.rest_recovery = rest_recovery

    async def __aiter__(self) -> AsyncIterator[MarketEvent]:
        import websockets

        streams = "/".join(
            f"{symbol.lower()}@trade/{symbol.lower()}@bookTicker"
            for symbol in self.symbols
        )
        if not streams:
            return
        url = f"{self.ws_url}?streams={streams}"
        while True:
            try:
                async with websockets.connect(url, open_timeout=10, close_timeout=5) as websocket:
                    async for raw in websocket:
                        payload = json.loads(raw)
                        data = payload.get("data", payload)
                        received = datetime.now(timezone.utc)
                        if str(data.get("e", "")).lower() == "trade":
                            trade_id = data.get("t")
                            if trade_id in (None, ""):
                                raise ValueError("Binance @trade event missing trade id t")
                            yield MarketEvent.from_mapping(
                                {
                                    "source": "PUBLIC_WEBSOCKET",
                                    "symbol": data.get("s"),
                                    "event_type": "TRADE",
                                    "exchange_timestamp": data.get("T", data.get("E")),
                                    "receive_timestamp": received,
                                    "event_id": trade_id,
                                    "raw_event_id": trade_id,
                                    "price": data.get("p"),
                                    "quantity": data.get("q"),
                                    "payload": data,
                                }
                            )
                        elif str(data.get("e", "")).lower() == "bookTicker":
                            book_id = data.get("u")
                            yield MarketEvent.from_mapping(
                                {
                                    "source": "PUBLIC_WEBSOCKET",
                                    "symbol": data.get("s"),
                                    "event_type": "BOOK_TICKER",
                                    "exchange_timestamp": data.get("T", data.get("E")),
                                    "receive_timestamp": received,
                                    "event_id": book_id if book_id not in (None, "") else "",
                                    "raw_event_id": book_id if book_id not in (None, "") else "",
                                    "bid": data.get("b"),
                                    "bid_qty": data.get("B"),
                                    "ask": data.get("a"),
                                    "ask_qty": data.get("A"),
                                    "payload": data,
                                }
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconnect_count += 1
                self.last_error = str(exc)
                if self.rest_recovery is not None:
                    try:
                        recovered = self.rest_recovery(self.symbols)
                        if hasattr(recovered, "__await__"):
                            await recovered
                    except asyncio.CancelledError:
                        raise
                    except Exception as recovery_exc:
                        self.last_error = f"{exc}; REST recovery failed: {recovery_exc}"
                await asyncio.sleep(self.reconnect_delay)


class ContinuousShadowRuntime:
    """Event loop that feeds one frozen strategy graph and one isolated paper lane."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        frozen: Frozen31111Runtime,
        broker: ShadowBroker,
        exchange: ShadowExchangeClient,
        controller: Any,
        market_data: Any | None = None,
        journal_path: str | Path | None = None,
        report_output_dir: str | Path | None = None,
    ) -> None:
        if frozen.candidate_id != "31111-NEUTRAL":
            raise ValueError("v4.1 runtime requires frozen candidate 31111-NEUTRAL")
        if frozen.candidate_sha != "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774":
            raise ValueError("v4.1 runtime candidate SHA does not match the attested artifact")
        if frozen.economic_leverage != 1:
            raise ValueError("v4.1 runtime economic leverage is frozen at 1x")
        for symbol in frozen.symbols:
            session_context(symbol)
        self.repo_root = Path(repo_root).resolve()
        self.frozen = frozen
        self.broker = broker
        self.exchange = exchange
        self.controller = controller
        self.market_data = market_data
        compiler = Frozen31111ControllerCompiler(
            frozen,
            self.repo_root,
            broker.db_path,
        )
        self.strategy_manifest = compiler.compile()
        self.journal_path = Path(journal_path) if journal_path else self.repo_root / "data" / "runtime" / "v41" / f"market-events-{broker.profile.name.lower()}.jsonl"
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self.controller_ticks = 0
        self._next_controller_tick_at: datetime | None = None
        self.report_output_dir = (
            Path(report_output_dir).resolve()
            if report_output_dir is not None
            else self.repo_root / "reports" / "testnet-shadow-v4.1"
        )
        self._bind_controller()
        assert_frozen_runtime_parity(self.strategy_manifest, self.controller)
        self._write_strategy_manifest()

    @classmethod
    def create(
        cls,
        *,
        repo_root: str | Path = ".",
        market_data: Any,
        profile: ShadowExecutionProfile = PAPER_BASELINE,
        db_path: str | Path | None = None,
        controller: Any | None = None,
        controller_factory: Any | None = None,
        journal_path: str | Path | None = None,
        initial_cash: float = 10_000.0,
        report_output_dir: str | Path | None = None,
    ) -> "ContinuousShadowRuntime":
        root = Path(repo_root).resolve()
        frozen = Frozen31111Runtime.load(root)
        runtime_db = Path(db_path or root / "data" / "runtime" / "v41" / f"shadow-{profile.name.lower()}.sqlite")
        init_db(runtime_db)
        _ensure_runtime_db_schema(runtime_db)
        broker = ShadowBroker(runtime_db, profile, initial_cash=initial_cash)
        exchange = ShadowExchangeClient(market_data, broker)
        if controller is None:
            if controller_factory is not None:
                controller = controller_factory(exchange, frozen)
            else:
                from trader import _build_controller

                controller = _build_controller(
                    exchange,
                    _frozen_controller_config(frozen, runtime_db, root),
                    live_observation=False,
                )
        return cls(repo_root=root, frozen=frozen, broker=broker, exchange=exchange, controller=controller, market_data=market_data, journal_path=journal_path, report_output_dir=report_output_dir)

    def _bind_controller(self) -> None:
        binder = getattr(self.controller, "bind_frozen_runtime", None)
        if callable(binder):
            binder(self.frozen)
        else:
            setattr(self.controller, "frozen_runtime", self.frozen)
        setattr(self.controller, "frozen_candidate_id", self.frozen.candidate_id)
        setattr(self.controller, "frozen_candidate_sha", self.frozen.candidate_sha)
        setattr(self.controller, "effective_strategy_manifest", self.strategy_manifest)

    async def recover_from_rest(self, symbols: Iterable[str] | None = None) -> list[MarketEvent]:
        if self.market_data is None:
            return []
        recovered: list[MarketEvent] = []
        for symbol in tuple(symbols or self.frozen.symbols):
            ticker = await self.market_data.get_24h_ticker(symbol)
            depth = await self.market_data.get_orderbook_depth(symbol, 5)
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            event = MarketEvent.from_mapping(
                {
                    "source": "PUBLIC_REST_RECOVERY",
                    "symbol": symbol,
                    "event_type": "REST_RECOVERY",
                    "exchange_timestamp": ticker.get("closeTime") or ticker.get("openTime") or datetime.now(timezone.utc),
                    "receive_timestamp": datetime.now(timezone.utc),
                    "event_id": f"rest-recovery:{symbol}:{ticker.get('closeTime', '')}",
                    "price": ticker.get("lastPrice"),
                    "bid": bids[0][0] if bids else None,
                    "ask": asks[0][0] if asks else None,
                    "mark_price": ticker.get("lastPrice"),
                    "payload": {"ticker": ticker, "depth": depth},
                }
            )
            await self._consume(event, controller_tick=False)
            recovered.append(event)
        return recovered

    def _controller_tick_is_due(self, event: MarketEvent) -> bool:
        if self._next_controller_tick_at is None:
            return True
        return event.receive_timestamp >= self._next_controller_tick_at

    async def _controller_tick(self, event: MarketEvent) -> Any:
        if not self._controller_tick_is_due(event):
            return None
        interval = float(getattr(getattr(self.controller, "config", None), "loop_interval_seconds", 0.0) or 0.0)
        self._next_controller_tick_at = event.receive_timestamp + timedelta(seconds=interval if interval > 0 else 0.0)
        handler = getattr(self.controller, "on_market_event", None)
        if callable(handler):
            result = handler(event)
        else:
            handler = getattr(self.controller, "run_once", None)
            if not callable(handler):
                return None
            try:
                result = handler(now=event.exchange_timestamp)
            except TypeError:
                result = handler()
        if hasattr(result, "__await__"):
            result = await result
        self.controller_ticks += 1
        return result

    def _append_journal(self, event: MarketEvent) -> None:
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.as_record(), ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":")) + "\n")

    async def _consume(self, event: MarketEvent, *, controller_tick: bool = True) -> list[dict[str, Any]]:
        self._append_journal(event)
        if event.event_type in {"FUNDING_SETTLEMENT", "FUNDING"} and event.funding_rate is not None:
            await self.broker.apply_funding_settlement(
                event.symbol,
                event.payload.get("settlement_timestamp", event.exchange_timestamp),
                funding_rate=event.funding_rate,
                mark_price=event.mark_price or event.price,
            )
        fills = await self.broker.process_market_event(event.symbol, event.broker_payload())
        if controller_tick:
            await self._controller_tick(event)
        return fills

    async def run(
        self,
        source: MarketEventSource,
        *,
        max_events: int | None = None,
        max_seconds: float | None = None,
        bootstrap_rest: bool = False,
        reconcile: bool = True,
    ) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        start_clock = time.monotonic()
        processed = 0
        fill_count = 0
        stop_reason = "SOURCE_EXHAUSTED"
        if bootstrap_rest:
            await self.recover_from_rest()
        iterator = source.__aiter__()
        while max_events is None or processed < max_events:
            if max_seconds is not None:
                remaining = float(max_seconds) - (time.monotonic() - start_clock)
                if remaining <= 0:
                    stop_reason = "BOUNDED_TIMEOUT"
                    break
            else:
                remaining = None
            try:
                item = await asyncio.wait_for(anext(iterator), timeout=remaining) if remaining is not None else await anext(iterator)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                stop_reason = "BOUNDED_TIMEOUT"
                break
            event = item if isinstance(item, MarketEvent) else MarketEvent.from_mapping(item)
            if not event.symbol:
                raise ValueError("market event requires symbol")
            fills = await self._consume(event)
            processed += 1
            fill_count += len(fills)
        ended = datetime.now(timezone.utc)
        reconcile_result = await self.broker.reconcile() if reconcile else {"result": "SKIPPED"}
        result = {
            "status": "COMPLETED",
            "stop_reason": stop_reason,
            "events_processed": processed,
            "fills_processed": fill_count,
            "controller_ticks": self.controller_ticks,
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat(),
            "duration_seconds": (ended - started).total_seconds(),
            "candidate_id": self.frozen.candidate_id,
            "candidate_sha": self.frozen.candidate_sha,
            "economic_leverage": self.frozen.economic_leverage,
            "execution_profile": self.broker.profile.name,
            "production_private_api": "DISABLED",
            "reconcile": reconcile_result,
        }
        self._write_manifest(result)
        return result

    def _write_manifest(self, result: dict[str, Any]) -> None:
        path = self.report_output_dir / f"run-manifest-{self.broker.profile.name.lower()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _write_strategy_manifest(self) -> None:
        path = self.report_output_dir / "effective-strategy-manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.strategy_manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
