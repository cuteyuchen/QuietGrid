from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exchange.shadow import PAPER_BASELINE, PAPER_CONSERVATIVE, ShadowBroker, ShadowExecutionProfile, ShadowExchangeClient
from quietgrid_v41.client_ids import ClientOrderIdFactory
from quietgrid_v41.events import MarketEvent
from quietgrid_v41.production_probe import probe
from quietgrid_v41.reports import write_v41_reports
from quietgrid_v41.runtime import BinancePublicTradeStream, ContinuousShadowRuntime, IterableMarketEventSource
from quietgrid_v41.sessions import capital_multiplier, session_context
from quietgrid_v41.testnet import run_testnet_order_lifecycle
from quietgrid_v40.safety import ExecutionLane, ExecutionSafetyPolicy, ProductionPrivateApiBlocked
from strategy.frozen_31111_runtime import Frozen31111Runtime


UTC = timezone.utc


def _at(seconds: float = 1.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _event(at: datetime, *, event_id: str, price: float = 99.4, quantity: float = 10.0, sequence: int = 0, receive: datetime | None = None) -> dict:
    return {
        "event_id": event_id,
        "exchange_timestamp": at,
        "receive_timestamp": receive or at,
        "sequence": sequence,
        "trade_price": price,
        "trade_qty": quantity,
        "price": price,
    }


def _market(broker: ShadowBroker, symbol: str = "MUUSDT", *, price: float = 100.0) -> None:
    broker._last_market[symbol] = {"bid": price - 1, "ask": price + 1, "last": price, "price": price}


def test_v41_public_safety_and_ids() -> None:
    policy = ExecutionSafetyPolicy(ExecutionLane.PUBLIC_DATA_ONLY)
    policy.require_public_read("https://fapi.binance.com/fapi/v1/exchangeInfo")
    with pytest.raises(ProductionPrivateApiBlocked):
        policy.require_public_read("https://fapi.binance.com/fapi/v1/account")

    factory = ClientOrderIdFactory("runtime-1")
    ids = {factory.order("PAPER_BASELINE", "MUUSDT"), factory.stop("MUUSDT"), factory.force_flat("MUUSDT", "ep-1", 1)}
    assert len(ids) == 3
    assert all(item.startswith("qg-v41-") for item in ids)
    assert ClientOrderIdFactory.force_flat("MUUSDT", "ep-1", 1) == ClientOrderIdFactory.force_flat("MUUSDT", "ep-1", 1)
    assert ClientOrderIdFactory.force_flat("MUUSDT", "ep-1", 1) != ClientOrderIdFactory.force_flat("MUUSDT", "ep-2", 1)


def test_v41_stop_market_long_short_gap_and_zero_position(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "stops.db", PAPER_BASELINE)
        _market(broker)
        await broker.place_market_order("MUUSDT", "BUY", 10, reduce_only=False, client_id="seed-long")
        stop = await broker.place_stop_market_order("MUUSDT", "SELL", 95, "qg-v41-stop-MUUSDT-1", quantity=10)
        assert stop["status"] == "STOP_PENDING"
        filled = await broker.process_market_event("MUUSDT", _event(_at(), event_id="gap-long", price=90, quantity=1))
        assert filled[0]["status"] == "FILLED"
        assert filled[0]["price"] == 90
        assert (await broker.get_position("MUUSDT"))["qty"] == 0
        assert (await broker.get_order("MUUSDT", stop["orderId"], ""))["orderType"] == "STOP_MARKET"

        await broker.place_market_order("MUUSDT", "SELL", 7, reduce_only=False, client_id="seed-short")
        short_stop = await broker.place_stop_market_order("MUUSDT", "BUY", 105, "qg-v41-stop-MUUSDT-2", quantity=7)
        short_fill = await broker.process_market_event("MUUSDT", _event(_at(), event_id="gap-short", price=110, quantity=1))
        assert short_fill[0]["order_id"] == short_stop["orderId"]
        assert (await broker.get_position("MUUSDT"))["qty"] == 0

        zero_stop = await broker.place_stop_market_order("MUUSDT", "SELL", 95, "qg-v41-stop-MUUSDT-zero")
        zero_fill = await broker.process_market_event("MUUSDT", _event(_at(), event_id="zero-stop", price=90, quantity=1))
        assert zero_fill and zero_fill[-1]["order_id"] == zero_stop["orderId"]
        assert (await broker.get_position("MUUSDT"))["qty"] == 0

    asyncio.run(scenario())


def test_v41_stop_cancel_before_trigger_and_equal_time_race(tmp_path: Path) -> None:
    async def scenario() -> None:
        profile = ShadowExecutionProfile("STOP", 0.0, 1.0, 0, 500, 90.0)
        broker = ShadowBroker(tmp_path / "stop-race.db", profile)
        _market(broker)
        pending = await broker.place_stop_market_order("MUUSDT", "SELL", 95, "stop-cancel")
        cancel = await broker.cancel_order("MUUSDT", pending["orderId"])
        assert cancel["status"] == "CANCEL_PENDING"
        before_cancel = datetime.fromisoformat(cancel["cancel_effective_at"]) - timedelta(microseconds=1)
        assert await broker.process_market_event("MUUSDT", _event(before_cancel, event_id="before-stop", price=100)) == []
        assert await broker.process_market_event("MUUSDT", _event(datetime.fromisoformat(cancel["cancel_effective_at"]), event_id="cancel-settles", price=100)) == []
        assert (await broker.get_order("MUUSDT", pending["orderId"], ""))["status"] == "CANCELED"

        await broker.place_market_order("MUUSDT", "BUY", 4, reduce_only=False, client_id="seed-race")
        race = await broker.place_stop_market_order("MUUSDT", "SELL", 95, "stop-race", quantity=4)
        canceled = await broker.cancel_order("MUUSDT", race["orderId"])
        cancel_at = datetime.fromisoformat(canceled["cancel_effective_at"])
        fills = await broker.process_market_event("MUUSDT", _event(cancel_at, event_id="equal-time", price=90))
        assert any(item["order_id"] == race["orderId"] for item in fills)
        assert (await broker.get_position("MUUSDT"))["qty"] == 0

    asyncio.run(scenario())


def test_v41_reduce_only_caps_long_short_and_zero_position(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "reduce.db", PAPER_BASELINE)
        _market(broker)
        await broker.place_market_order("MUUSDT", "BUY", 10, reduce_only=False, client_id="long")
        result = await broker.place_market_order("MUUSDT", "SELL", 15, reduce_only=True, client_id="reduce-long")
        assert result["status"] == "FILLED" and result["executedQty"] == 10
        assert (await broker.get_position("MUUSDT"))["qty"] == 0
        no_position = await broker.place_market_order("MUUSDT", "SELL", 1, reduce_only=True, client_id="reduce-zero")
        assert no_position["status"] == "REJECTED"

        await broker.place_market_order("MUUSDT", "SELL", 6, reduce_only=False, client_id="short")
        wrong_side = await broker.place_market_order("MUUSDT", "SELL", 1, reduce_only=True, client_id="wrong-side")
        assert wrong_side["status"] == "REJECTED"
        reduced = await broker.place_market_order("MUUSDT", "BUY", 9, reduce_only=True, client_id="reduce-short")
        assert reduced["executedQty"] == 6
        assert (await broker.get_position("MUUSDT"))["qty"] == 0

    asyncio.run(scenario())


def test_v41_force_flat_is_latched_episode_scoped_and_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "force-flat.db", PAPER_BASELINE)
        _market(broker)
        await broker.place_market_order("MUUSDT", "BUY", 5, reduce_only=False, client_id="seed")
        resting = await broker.place_limit_order_post_only("MUUSDT", "BUY", 98, 4, "risk-order")
        result = await broker.force_flat("MUUSDT", episode_id="episode-1")
        assert result["status"] == "WAIT_CANCEL_TERMINAL"
        assert result["cancel_effective_at"] is not None
        blocked = await broker.place_limit_order_post_only("MUUSDT", "BUY", 98, 1, "blocked")
        assert blocked["status"] == "REJECTED"
        assert blocked["rejectReason"] == "FORCE_FLAT_LATCHED"
        settle_at = datetime.fromisoformat(result["cancel_effective_at"])
        await broker.process_market_event("MUUSDT", _event(settle_at, event_id="cancel-settles", price=100, quantity=1))
        completed = await broker.force_flat("MUUSDT", episode_id="episode-1")
        assert completed["status"] == "COMPLETE"
        assert (await broker.get_position("MUUSDT"))["qty"] == 0
        assert (await broker.get_order("MUUSDT", resting["orderId"], ""))["status"] == "CANCELED"
        retry = await broker.force_flat("MUUSDT", episode_id="episode-1")
        assert retry == completed
        second = await broker.force_flat("MUUSDT", episode_id="episode-2")
        assert second["episode_id"] == "episode-2" and second["status"] == "COMPLETE"

    asyncio.run(scenario())


def test_v41_force_flat_cancel_fill_race_reruns_position(tmp_path: Path) -> None:
    async def scenario() -> None:
        profile = ShadowExecutionProfile("FORCE_RACE", 0.0, 1.0, 0, 500, 90.0)
        broker = ShadowBroker(tmp_path / "force-race.db", profile)
        _market(broker)
        await broker.place_market_order("MUUSDT", "BUY", 5, reduce_only=False, client_id="seed")
        resting = await broker.place_limit_order_post_only("MUUSDT", "BUY", 98, 2, "race-order")
        requested = await broker.force_flat("MUUSDT", episode_id="race-1")
        cancel_at = datetime.fromisoformat(requested["cancel_effective_at"])
        before_cancel = cancel_at - timedelta(milliseconds=250)
        await broker.process_market_event("MUUSDT", _event(before_cancel, event_id="fills-before-cancel", price=97.9, quantity=3))
        assert (await broker.get_position("MUUSDT"))["qty"] == 7
        assert (await broker.get_order("MUUSDT", resting["orderId"], ""))["status"] == "FILLED"
        await broker.process_market_event("MUUSDT", _event(cancel_at, event_id="cancel-terminal", price=100, quantity=1))
        completed = await broker.force_flat("MUUSDT", episode_id="race-1")
        assert completed["status"] == "COMPLETE"
        assert (await broker.get_position("MUUSDT"))["qty"] == 0
        assert completed["attempts"][0]["order"]["executedQty"] == 7

    asyncio.run(scenario())


def test_v41_queue_is_cumulative_persistent_and_maker_price_is_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        profile = ShadowExecutionProfile("QUEUE", 1.0, 1.0, 0, 0, 90.0)
        db = tmp_path / "queue.db"
        broker = ShadowBroker(db, profile)
        broker._last_market["MUUSDT"] = {"bid": 99.0, "ask": 101.0, "last": 100.0, "bid_qty": 100.0}
        order = await broker.place_limit_order_post_only("MUUSDT", "BUY", 99.5, 10, "queue-order")
        first = await broker.process_market_event("MUUSDT", _event(_at(), event_id="q1", price=99.4, quantity=30))
        assert first == []
        assert (await broker.get_order("MUUSDT", order["orderId"], ""))["queueAheadRemaining"] == 70

        restarted = ShadowBroker(db, profile)
        second = await restarted.process_market_event("MUUSDT", _event(_at(2), event_id="q2", price=99.4, quantity=30))
        assert second == []
        assert (await restarted.get_order("MUUSDT", order["orderId"], ""))["queueAheadRemaining"] == 40
        third = await restarted.process_market_event("MUUSDT", _event(_at(3), event_id="q3", price=99.4, quantity=50))
        assert third and third[0]["qty"] == 10 and third[0]["price"] == 99.5
        assert (await restarted.get_position("MUUSDT"))["qty"] == 10

    asyncio.run(scenario())


def test_v41_latency_touch_stale_duplicate_and_ordering(tmp_path: Path) -> None:
    async def scenario() -> None:
        profile = ShadowExecutionProfile("LATENCY", 0.0, 1.0, 1000, 0, 90.0)
        broker = ShadowBroker(tmp_path / "events.db", profile)
        _market(broker)
        order = await broker.place_limit_order_post_only("MUUSDT", "BUY", 99.5, 1, "latency")
        effective = datetime.fromisoformat(order["effective_at"])
        assert await broker.process_market_event("MUUSDT", _event(effective - timedelta(microseconds=1), event_id="early", price=99.4)) == []
        assert await broker.process_market_event("MUUSDT", _event(effective + timedelta(seconds=1), event_id="late", price=99.4))

        stale_at = _at(120)
        stale = _event(stale_at, event_id="stale", price=100, receive=_at(240))
        await broker.process_market_event("MUUSDT", stale)
        with pytest.raises(RuntimeError, match="stale"):
            await broker.place_market_order("MUUSDT", "BUY", 1, reduce_only=False, client_id="stale-market")

        fresh = _event(_at(1), event_id="seq-10", price=100, sequence=10)
        assert await broker.process_market_event("MUUSDT", fresh) == []

        other_symbol = _event(_at(3), event_id="seq-10", price=100, sequence=10)
        assert await broker.process_market_event("SNDKUSDT", other_symbol) == []
        lower = _event(_at(2), event_id="seq-9", price=100, sequence=9)
        assert await broker.process_market_event("MUUSDT", lower) == []
        assert (await broker.reconcile())["result"] == "RECONCILED"
        assert await broker.process_market_event("MUUSDT", fresh) == []

    asyncio.run(scenario())


def test_v41_stream_has_reconnect_and_rest_recovery_hooks() -> None:
    recovery_calls: list[tuple[str, ...]] = []
    stream = BinancePublicTradeStream(("MUUSDT",), rest_recovery=lambda symbols: recovery_calls.append(tuple(symbols)))
    assert stream.reconnect_count == 0
    assert stream.rest_recovery is not None


def test_v41_funding_is_settled_once_across_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = tmp_path / "funding.db"
        broker = ShadowBroker(db, PAPER_BASELINE)
        _market(broker)
        await broker.place_market_order("MUUSDT", "BUY", 2, reduce_only=False, client_id="funding-seed")
        settlement = _at(1)
        first = await broker.apply_funding_settlement("MUUSDT", settlement, funding_rate=0.01, mark_price=100)
        second = await broker.apply_funding_settlement("MUUSDT", settlement, funding_rate=0.01, mark_price=100)
        assert first["applied"] is True and second["applied"] is False
        restarted = ShadowBroker(db, PAPER_BASELINE)
        third = await restarted.apply_funding_settlement("MUUSDT", settlement, funding_rate=0.01, mark_price=100)
        assert third["applied"] is False
        assert (await restarted.get_account_summary())["funding_pnl"] == pytest.approx(-2.0)

    asyncio.run(scenario())


def test_v41_sessions_and_profiles_are_separate(tmp_path: Path) -> None:
    assert session_context("SOXLUSDT").calendar == "NYSE"
    assert session_context("SKHYNIXUSDT").calendar == "XKRX"
    assert capital_multiplier("SOXLUSDT") == 0.5
    assert PAPER_BASELINE.name != PAPER_CONSERVATIVE.name

    async def scenario() -> None:
        baseline = ShadowBroker(tmp_path / "baseline.db", PAPER_BASELINE)
        conservative = ShadowBroker(tmp_path / "conservative.db", PAPER_CONSERVATIVE)
        _market(baseline)
        _market(conservative)
        await baseline.place_market_order("MUUSDT", "BUY", 1, reduce_only=False, client_id="baseline")
        assert (await conservative.get_position("MUUSDT"))["qty"] == 0

    asyncio.run(scenario())


class _RuntimeController:
    def __init__(self) -> None:
        self.events: list[MarketEvent] = []
        self.frozen_runtime = None

    def bind_frozen_runtime(self, frozen: Frozen31111Runtime) -> None:
        self.frozen_runtime = frozen

    async def run_once(self, now=None):
        self.events.append(now)


def test_v41_runtime_drives_frozen_controller_through_shadow_client(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = _RuntimeController()
        runtime = ContinuousShadowRuntime.create(
            repo_root=".",
            market_data=object(),
            profile=PAPER_BASELINE,
            db_path=tmp_path / "runtime.db",
            journal_path=tmp_path / "events.jsonl",
            controller=controller,
            report_output_dir=tmp_path / "reports",
        )
        event = MarketEvent.from_mapping({"symbol": "MUUSDT", "event_type": "TRADE", "exchange_timestamp": _at(), "event_id": "runtime-1", "price": 100, "quantity": 1})
        result = await runtime.run(IterableMarketEventSource([event]), max_events=1)
        assert isinstance(runtime.exchange, ShadowExchangeClient)
        assert controller.frozen_runtime.candidate_id == "31111-NEUTRAL"
        assert controller.frozen_runtime.candidate_sha == "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774"
        assert result["events_processed"] == 1 and result["controller_ticks"] == 1
        assert (tmp_path / "events.jsonl").exists()

    asyncio.run(scenario())


def test_v41_default_controller_path_uses_frozen_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class Controller:
        pass

    def fake_build(exchange, config, live_observation=None):
        captured["exchange"] = exchange
        captured["config"] = config
        captured["live_observation"] = live_observation
        return Controller()

    monkeypatch.setattr("trader._build_controller", fake_build)
    ContinuousShadowRuntime.create(
        repo_root=".",
        market_data=object(),
        db_path=tmp_path / "default-controller.db",
        controller=None,
        report_output_dir=tmp_path / "reports",
    )
    config = captured["config"]
    assert config.raw["selection"]["symbol_allowlist"] == ["SNDKUSDT", "MUUSDT", "SOXLUSDT"]
    assert config.raw["trading"]["leverage"] == 1
    assert config.raw["features"]["regime_v2"] is True
    assert captured["live_observation"] is False


def test_v41_frozen_compiler_maps_real_controller_and_sha(tmp_path: Path) -> None:
    runtime = ContinuousShadowRuntime.create(
        repo_root=".",
        market_data=object(),
        db_path=tmp_path / "frozen-controller.db",
        report_output_dir=tmp_path / "reports",
    )
    manifest = runtime.strategy_manifest
    semantics = manifest["strategy_semantics"]
    config = runtime.controller.config
    assert manifest["effective_strategy_sha"] == "776d027897d3c32fdcae3bf69641e0a8460ea006115134600a77e4982d92faa3"
    assert manifest["candidate_sha"] == "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774"
    assert semantics["direction"] == "NEUTRAL"
    assert semantics["symbol_profiles"]["SKHYNIXUSDT"]["runtime_role"] == "RESEARCH_ONLY"
    assert semantics["symbol_profiles"]["SNDKUSDT"]["runtime_role"] == "SHADOW_ELIGIBLE"
    assert config.take_profit_usdt == 0.0
    assert runtime.controller.risk.config.profit_protection_enabled is False
    assert config.leverage == 1
    assert config.grid_range_multiplier_by_symbol["MUUSDT"] == 2.0
    assert config.grid_min_step_pct_by_symbol["MUUSDT"] == pytest.approx(
        semantics["grid"]["min_step_pct_by_symbol"]["MUUSDT"]
    )
    generator = runtime.controller._adaptive_grid_for_symbol("MUUSDT")
    assert generator.config.min_grid_num == 5
    assert generator.config.max_grid_num == 10
    assert generator.config.k_atr_range == pytest.approx(semantics["range"]["effective_k_atr_range"])
    assert generator.config.k_sigma_range == pytest.approx(semantics["range"]["effective_k_sigma_range"])
    assert generator.config.max_range_pct == pytest.approx(semantics["range"]["effective_max_range_pct"])
    assert runtime.controller.inventory.config.caution_utilization == pytest.approx(0.4)
    assert runtime.controller.inventory.config.critical_utilization == pytest.approx(0.8)
    assert runtime.controller._capital_for_symbol("SOXLUSDT") == pytest.approx(config.capital_per_symbol * 0.5)


def test_v41_market_event_identity_and_native_trade_id(tmp_path: Path) -> None:
    event = MarketEvent.from_mapping(
        {"symbol": "MUUSDT", "event_type": "TRADE", "t": "12345", "exchange_timestamp": _at(), "price": 100, "quantity": 1}
    )
    assert event.event_id == "12345"
    assert event.raw_event_id == "12345"
    fallback = MarketEvent.from_mapping({"symbol": "MUUSDT", "exchange_timestamp": _at(), "price": 100, "quantity": 1})
    assert fallback.event_id.startswith("EVENT_ID_FALLBACK_HASH:")
    assert fallback.payload.get("event_id_mode") == "EVENT_ID_FALLBACK_HASH"

    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "identity.db", PAPER_BASELINE)
        broker._last_market["MUUSDT"] = {"bid": 99, "ask": 101, "last": 100}
        broker._last_market["SNDKUSDT"] = {"bid": 99, "ask": 101, "last": 100}
        at = _at()
        trade = {
            "source": "PUBLIC_WEBSOCKET",
            "symbol": "MUUSDT",
            "event_type": "TRADE",
            "exchange_timestamp": at,
            "receive_timestamp": at,
            "event_id": "same-id",
            "trade_price": 100,
            "trade_qty": 1,
        }
        assert await broker.process_market_event("MUUSDT", trade) == []
        assert await broker.process_market_event("MUUSDT", trade) == []
        assert await broker.process_market_event("SNDKUSDT", {**trade, "symbol": "SNDKUSDT"}) == []
        assert await broker.process_market_event("MUUSDT", {**trade, "event_type": "BOOK_TICKER", "bid": 99, "ask": 101}) == []
        second = {
            **trade,
            "event_id": "same-id-2",
            "exchange_timestamp": at + timedelta(microseconds=1),
        }
        assert await broker.process_market_event("MUUSDT", second) == []

    asyncio.run(scenario())


def test_v41_book_freshness_separates_trade_and_book_and_blocks_maker(tmp_path: Path) -> None:
    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "freshness.db", PAPER_BASELINE)
        assert broker._book_is_fresh("MUUSDT") is False
        broker._last_market["MUUSDT"] = {"bid": 99, "ask": 101}
        assert broker._book_is_fresh("MUUSDT") is True
        at = _at()
        await broker.place_market_order("MUUSDT", "BUY", 1, reduce_only=False, client_id="freshness-seed")
        await broker.process_market_event(
            "MUUSDT",
            {
                "source": "PUBLIC_WEBSOCKET",
                "symbol": "MUUSDT",
                "event_type": "BOOK_TICKER",
                "exchange_timestamp": at,
                "receive_timestamp": at,
                "event_id": "book-1",
                "bid": 99,
                "bid_qty": 50,
                "ask": 101,
                "ask_qty": 40,
            },
        )
        assert broker._book_is_fresh("MUUSDT") is True
        await broker.process_market_event(
            "MUUSDT",
            {
                "source": "PUBLIC_WEBSOCKET",
                "symbol": "MUUSDT",
                "event_type": "TRADE",
                "exchange_timestamp": at + timedelta(seconds=1),
                "receive_timestamp": at + timedelta(seconds=1),
                "event_id": "trade-1",
                "trade_price": 100,
                "trade_qty": 1,
            },
        )
        assert broker._book_is_fresh("MUUSDT") is True
        broker._last_market["MUUSDT"]["_book_received_at"] = datetime.now(UTC) - timedelta(seconds=broker.profile.stale_timeout_seconds + 1)
        stale_rejected = await broker.place_limit_order_post_only("MUUSDT", "BUY", 99, 1, "stale-blocked")
        assert stale_rejected["status"] == "REJECTED"
        assert stale_rejected["rejectReason"] == "BOOK_DATA_STALE"
        reduced = await broker.place_limit_order_post_only("MUUSDT", "SELL", 101, 1, "stale-reduce", position_side="LONG")
        assert reduced["status"] == "NEW"

    asyncio.run(scenario())


def test_v41_reports_do_not_overwrite_and_can_target_temp_dir(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    write_v41_reports(tmp_path, output_dir=reports, probe={"classification": "PRODUCTION_PUBLIC_PROBE_INCOMPLETE", "symbols": []})
    probe_json = reports / "production-public-capability.json"
    assert probe_json.exists()
    write_v41_reports(tmp_path, output_dir=reports, runtime={"status": "SKIPPED"})
    assert json.loads(probe_json.read_text(encoding="utf-8"))["classification"] == "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"
    baseline_manifest = reports / "run-manifest-paper_baseline.json"
    conservative_manifest = reports / "run-manifest-paper_conservative.json"
    assert baseline_manifest != conservative_manifest
    assert baseline_manifest.exists() is False
    assert conservative_manifest.exists() is False
    assert (reports / "run-manifest.json").exists()


def test_v41_probe_is_public_only_and_partial_is_a_string(monkeypatch: pytest.MonkeyPatch) -> None:
    class PublicMarket:
        async def get_symbols(self):
            return [{"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL"} for symbol in ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")]

        async def get_server_time(self):
            return 1

        async def get_symbol_rules(self, symbol):
            return {"tick_size": 0.01, "step_size": 1, "min_qty": 1, "min_notional": 5}

        async def get_24h_ticker(self, symbol):
            return {"symbol": symbol, "lastPrice": "100"}

        async def get_orderbook_depth(self, symbol, limit):
            return {"bids": [["99", "10"]], "asks": [["101", "10"]]}

        async def get_klines(self, symbol, interval, limit):
            return [{"open": 100}]

        async def get_funding_context(self, symbol):
            return {"markPrice": "100"}

        async def get_funding_rate(self, symbol):
            return 0.0001

        async def close(self):
            return None

    async def fake_ws(symbols, timeout):
        return {"status": "SUPPORTED", "message_received": True}

    monkeypatch.setattr("quietgrid_v41.production_probe._public_websocket_probe", fake_ws)
    result = asyncio.run(probe(network=True, websocket=True, market_data=PublicMarket()))
    assert result["classification"] == "PRODUCTION_PUBLIC_TRADFI_SUPPORTED"
    assert all(item["final_capability"] == "SUPPORTED" for item in result["symbols"])


def test_v41_probe_incomplete_when_network_is_unavailable_or_before_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    skipped = asyncio.run(probe(network=False))
    assert skipped["classification"] == "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"

    class BrokenMarket:
        async def get_symbols(self):
            raise OSError("network unavailable")

        async def close(self):
            return None

    failed = asyncio.run(probe(network=True, websocket=False, market_data=BrokenMarket()))
    assert failed["classification"] == "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"


def test_v41_testnet_lifecycle_requires_explicit_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUIETGRID_V41_ALLOW_TESTNET_ORDER_LIFECYCLE", raising=False)
    result = asyncio.run(run_testnet_order_lifecycle(object()))
    assert result["status"] == "SKIPPED_TESTNET_ORDER_LIFECYCLE_NOT_AUTHORIZED"
