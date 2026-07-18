from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.models import GridOrder, OrderSide, OrderStatus
from db.database import connect, init_db
from db.repository import Repository


def test_database_init_and_basic_writes(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)

    window_id = repo.create_window(datetime(2026, 7, 3, tzinfo=timezone.utc))
    session_id = repo.create_session(window_id, "AAPLUSDT", "OBSERVING", 200, 10, datetime.now(timezone.utc))
    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="AAPLUSDT",
            order_id="1",
            client_id="cid-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=1,
            status=OrderStatus.OPEN,
            created_at=datetime.now(timezone.utc),
        ),
    )
    repo.update_order_status(session_id, "cid-1", OrderStatus.FILLED.value, datetime.now(timezone.utc), 100)
    repo.update_session_pnl(session_id, 1.25)
    repo.close_window(window_id, datetime.now(timezone.utc))
    repo.log_state(session_id, "AAPLUSDT", "IDLE", "OBSERVING", "window_open", None, datetime.now(timezone.utc))
    repo.log_system("INFO", "test", "system-ok", None, datetime.now(timezone.utc))

    window = repo.recent_rows("windows", limit=1)[0]
    assert window["id"] == window_id
    assert window["status"] == "closed"
    assert window["total_pnl"] == 1.25
    assert repo.recent_rows("sessions", limit=1)[0]["id"] == session_id
    assert repo.recent_rows("orders", limit=1)[0]["status"] == "filled"
    assert repo.recent_rows("state_logs", limit=1)[0]["trigger"] == "window_open"
    assert repo.recent_rows("system_logs", limit=1)[0]["message"] == "system-ok"
    summary = repo.dashboard_summary()
    assert summary["active_sessions"] == 1
    assert summary["open_orders"] == 0
    assert summary["latest_system_message"] == "system-ok"


def test_database_init_persists_wal_for_new_connections(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)

    with connect(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert journal_mode == "wal"


def test_database_connections_enable_runtime_pragmas_and_performance_indexes(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)

    with connect(db_path) as conn:
        session_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(sessions)").fetchall()}
        order_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(orders)").fetchall()}
        log_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(system_logs)").fetchall()}

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert "idx_sessions_active" in session_indexes
        assert "idx_orders_session_status" in order_indexes
        assert "idx_orders_status_session" in order_indexes
        assert "idx_system_logs_level_id" in log_indexes
        assert "idx_system_logs_module_id" in log_indexes


def test_database_init_migrates_existing_sessions_with_volatility_columns(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'IDLE',
                close_time DATETIME
            )
            """
        )
        conn.commit()

    init_db(db_path)

    with connect(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}

    assert {
        "volatility_method",
        "volatility_value",
        "volatility_window",
        "volatility_current_value",
        "volatility_current_window",
        "volatility_current_at",
    }.issubset(columns)


def test_dashboard_summary_counts_open_orders(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "AAPLUSDT", "RUNNING", 200, 10, now)
    closed_session_id = repo.create_session(window_id, "MSFTUSDT", "RUNNING", 200, 10, now)

    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="AAPLUSDT",
            order_id="open-1",
            client_id="cid-open-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=1,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="AAPLUSDT",
            order_id="filled-1",
            client_id="cid-filled-1",
            grid_index=2,
            side=OrderSide.SELL,
            price=101,
            qty=1,
            status=OrderStatus.FILLED,
            created_at=now,
            filled_at=now,
            fill_price=101,
        ),
    )
    repo.upsert_order(
        closed_session_id,
        GridOrder(
            symbol="MSFTUSDT",
            order_id="historical-open-1",
            client_id="cid-historical-open-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=1,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    repo.close_session(closed_session_id, "startup_recovery_skipped_symbol", now)

    summary = repo.dashboard_summary()

    assert summary["active_sessions"] == 1
    assert summary["open_orders"] == 1


def test_repository_bulk_upserts_orders_in_one_call(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "AAPLUSDT", "RUNNING", 200, 10, now)

    repo.upsert_orders(
        session_id,
        [
            GridOrder(
                symbol="AAPLUSDT",
                order_id="order-1",
                client_id="cid-1",
                grid_index=1,
                side=OrderSide.BUY,
                price=100,
                qty=0.5,
                status=OrderStatus.OPEN,
                created_at=now,
            ),
            GridOrder(
                symbol="AAPLUSDT",
                order_id="order-2",
                client_id="cid-2",
                grid_index=2,
                side=OrderSide.SELL,
                price=101,
                qty=0.5,
                status=OrderStatus.OPEN,
                created_at=now,
            ),
        ],
    )
    repo.upsert_orders(
        session_id,
        [
            GridOrder(
                symbol="AAPLUSDT",
                order_id="order-2-new",
                client_id="cid-2",
                grid_index=2,
                side=OrderSide.SELL,
                price=102,
                qty=0.5,
                status=OrderStatus.FILLED,
                created_at=now,
                filled_at=now,
                fill_price=102,
            )
        ],
    )

    rows = sorted(repo.console_orders(session_id), key=lambda row: row["client_id"])

    assert len(rows) == 2
    assert rows[0]["order_id"] == "order-1"
    assert rows[0]["status"] == "open"
    assert rows[1]["order_id"] == "order-2-new"
    assert rows[1]["status"] == "filled"
    assert rows[1]["fill_price"] == 102


def test_repository_persists_session_volatility_snapshot_and_current_value(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "AAPLUSDT", "RUNNING", 200, 10, now)

    repo.update_session_grid(
        session_id,
        grid_upper=101.0,
        grid_lower=99.0,
        grid_num=4,
        step_pct=0.005,
        baseline_atr=0.2,
        stop_loss_price=98.0,
        volatility_method="garman_klass",
        volatility_value=0.0125,
        volatility_window=60,
    )
    repo.update_session_current_volatility(session_id, 0.0105, 30, now)

    row = repo.active_session_volatility_rows()[0]

    assert row["session_id"] == session_id
    assert row["volatility_method"] == "garman_klass"
    assert row["volatility_value"] == 0.0125
    assert row["volatility_window"] == 60
    assert row["volatility_current_value"] == 0.0105
    assert row["volatility_current_window"] == 30
    assert row["volatility_current_at"] == now.isoformat()


def test_dashboard_order_status_counts_and_recent_alerts(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "AAPLUSDT", "RUNNING", 200, 10, now)
    closed_session_id = repo.create_session(window_id, "MSFTUSDT", "RUNNING", 200, 10, now)

    for status in (OrderStatus.OPEN, OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.CANCELLED):
        suffix = status.value
        repo.upsert_order(
            session_id,
            GridOrder(
                symbol="AAPLUSDT",
                order_id=f"order-{suffix}-{len(repo.recent_rows('orders'))}",
                client_id=f"cid-{suffix}-{len(repo.recent_rows('orders'))}",
                grid_index=1,
                side=OrderSide.BUY,
                price=100,
                qty=0.5,
                status=status,
                created_at=now,
            ),
        )
    repo.upsert_order(
        closed_session_id,
        GridOrder(
            symbol="MSFTUSDT",
            order_id="historical-open-1",
            client_id="cid-historical-open-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=10,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    repo.close_session(closed_session_id, "startup_recovery_skipped_symbol", now)
    repo.log_system("INFO", "controller", "normal loop", None, now)
    repo.log_system("WARN", "order_reconciliation", "Recovered filled order.", "client_id=cid-open", now)
    repo.log_system("ERROR", "position_reconciliation", "Position mismatch.", "symbol=AAPLUSDT", now)

    counts = repo.order_status_counts()
    alerts = repo.recent_alert_events(limit=5)

    assert counts == [
        {"status": "open", "count": 2, "qty": 1.0, "notional": 100.0},
        {"status": "filled", "count": 1, "qty": 0.5, "notional": 50.0},
        {"status": "cancelled", "count": 1, "qty": 0.5, "notional": 50.0},
    ]
    assert [alert["level"] for alert in alerts] == ["ERROR", "WARN"]
    assert alerts[0]["module"] == "position_reconciliation"
    assert alerts[1]["module"] == "order_reconciliation"


def test_latest_system_logs_by_modules_returns_latest_per_module(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)

    repo.log_system("INFO", "binance_check", "old", None, now)
    repo.log_system("WARN", "commission_health", "fee warning", None, now)
    repo.log_system("ERROR", "binance_check", "latest", "detail", now)

    rows = repo.latest_system_logs_by_modules(["binance_check", "commission_health", "missing"])

    assert [row["module"] for row in rows] == ["binance_check", "commission_health"]
    assert rows[0]["message"] == "latest"
    assert rows[0]["detail"] == "detail"
    assert rows[1]["message"] == "fee warning"


def test_log_system_calls_notifier_after_persisting(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    calls = []
    repo = Repository(db_path, notifier=lambda *args: calls.append(args))
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)

    repo.log_system("WARN", "order_reconciliation", "Recovered filled order.", "client_id=cid-open", now)

    assert repo.recent_rows("system_logs", limit=1)[0]["message"] == "Recovered filled order."
    assert calls == [("WARN", "order_reconciliation", "Recovered filled order.", "client_id=cid-open", now)]


def test_log_system_persists_when_notifier_fails(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)

    def failing_notifier(*args) -> None:
        raise RuntimeError("webhook down")

    repo = Repository(db_path, notifier=failing_notifier)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)

    repo.log_system("ERROR", "risk", "Position mismatch.", "symbol=AAPLUSDT", now)

    row = repo.recent_rows("system_logs", limit=1)[0]
    assert row["level"] == "ERROR"
    assert row["module"] == "risk"
    assert row["message"] == "Position mismatch."


def test_repository_persists_control_state(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)

    repo.set_control_state("new_entries_paused", True, now)

    state = repo.get_control_state()
    assert state["new_entries_paused"]["value"] is True
    assert state["new_entries_paused"]["updated_at"] == now.isoformat()
    assert repo.new_entries_paused() is True

    repo.set_control_state("new_entries_paused", False, now)

    assert repo.new_entries_paused() is False


def test_repository_persists_disabled_symbols_and_stop_requests(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)

    assert repo.set_symbol_disabled("btcusdt", True, now) == ["BTCUSDT"]
    assert repo.disabled_symbols() == {"BTCUSDT"}
    assert repo.set_symbol_disabled("ETHUSDT", True, now) == ["BTCUSDT", "ETHUSDT"]
    assert repo.set_symbol_disabled("BTCUSDT", False, now) == ["ETHUSDT"]
    assert repo.disabled_symbols() == {"ETHUSDT"}

    request = repo.request_session_stop(12, "ethusdt", "手动停止", "req-1", now)

    assert request["symbol"] == "ETHUSDT"
    assert request["request_type"] == "stop"
    assert repo.pending_session_stop_requests()[12]["status"] == "requested"

    repo.update_session_stop_request(12, "completed", "已处理", now)

    assert repo.pending_session_stop_requests() == {}
    stored = repo.session_stop_requests(include_terminal=True)["12"]
    assert stored["status"] == "completed"
    assert stored["detail"] == "已处理"

    close_request = repo.request_session_stop(13, "btcusdt", "手动平仓", "close-1", now, request_type="manual_close")
    assert close_request["request_type"] == "manual_close"
    assert repo.pending_session_stop_requests()[13]["request_type"] == "manual_close"


def test_repository_persists_strategy_config_draft(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    draft = {
        "volatility_method": "yang_zhang",
        "max_concurrent": 2,
        "observe_hours": 1.5,
        "min_step_pct": 0.002,
        "max_grid_num": 12,
    }

    assert repo.strategy_config_draft() is None
    repo.set_strategy_config_draft(draft, now)

    assert repo.strategy_config_draft() == draft
    state = repo.get_control_state()["strategy_config_draft"]
    assert state["updated_at"] == now.isoformat()


def test_latest_commission_health_parses_latest_detail(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    old_detail = {
        "status": "ok",
        "checked_symbols": 1,
        "ok_count": 1,
        "warn_count": 0,
        "error_count": 0,
        "symbols": [{"symbol": "AAPLUSDT", "status": "ok", "maker": 0.0}],
    }
    latest_detail = {
        "status": "warn",
        "checked_symbols": 2,
        "ok_count": 1,
        "warn_count": 1,
        "error_count": 0,
        "symbols": [
            {"symbol": "AAPLUSDT", "status": "ok", "maker": 0.0, "max_maker_fee_rate": 0.0},
            {"symbol": "BTCUSDT", "status": "warn", "maker": 0.0002, "max_maker_fee_rate": 0.0},
        ],
    }

    repo.log_system("INFO", "commission_health", "old", json.dumps(old_detail), now)
    repo.log_system("WARN", "commission_health", "latest", json.dumps(latest_detail), now)

    health = repo.latest_commission_health()

    assert health is not None
    assert health["level"] == "WARN"
    assert health["status"] == "warn"
    assert health["checked_symbols"] == 2
    assert health["symbols"][1]["symbol"] == "BTCUSDT"


def test_wal_allows_writer_commit_while_reader_transaction_is_open(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)

    reader = connect(db_path)
    writer = connect(db_path)
    try:
        reader.execute("PRAGMA busy_timeout = 100")
        writer.execute("PRAGMA busy_timeout = 100")
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 0

        writer.execute(
            "INSERT INTO windows (window_start) VALUES (?)",
            (datetime(2026, 7, 4, tzinfo=timezone.utc).isoformat(),),
        )
        writer.commit()

        assert reader.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 0
        reader.commit()
        assert reader.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
    finally:
        reader.close()
        writer.close()


def test_trade_create_is_idempotent_by_session_and_order_id(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "AAPLUSDT", "RUNNING", 200, 10, now)

    first_id = repo.create_trade(session_id, "AAPLUSDT", "order-1", "BUY", 100.0, 0.5, 1, None, now)
    duplicate_id = repo.create_trade(session_id, "AAPLUSDT", "order-1", "BUY", 100.0, 0.5, 1, None, now)

    assert duplicate_id == first_id
    trades = repo.recent_rows("trades")
    assert len(trades) == 1
    assert trades[0]["order_id"] == "order-1"


def test_round_candidate_is_marked_stale_after_ninety_seconds_without_market_update(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    repo.upsert_round_candidate(
        window_id,
        "BTCUSDT",
        now,
        price=62000.0,
        market_updated_at=now.isoformat(),
        stage="scanning",
        data_stale=False,
    )

    changed = repo.mark_round_candidates_stale(
        window_id,
        now + timedelta(seconds=90),
        now + timedelta(seconds=91),
    )

    assert changed == 1
    assert repo.round_candidates(window_id)[0]["data_stale"] == 1


def test_v2_event_and_regime_snapshots_round_trip(tmp_path) -> None:
    db_path = tmp_path / "v2.db"
    init_db(db_path)
    repo = Repository(db_path, account_id="paper")
    now = datetime.now(timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 100, 1, now)

    event_id = repo.append_event(
        "BAR_CLOSED",
        now,
        {"close": 100.0},
        session_id=session_id,
        symbol="BTCUSDT",
    )
    feature_id = repo.create_feature_snapshot(
        session_id=session_id,
        symbol="BTCUSDT",
        as_of_time=now,
        source_time=now,
        features={"volatility_expansion": 0.8},
        feature_version="features-v2",
    )
    repo.create_regime_decision(
        session_id=session_id,
        symbol="BTCUSDT",
        as_of_time=now,
        state="QUIET_RANGE",
        verdict="ALLOWED",
        grid_score=88.0,
        threshold_used=75.0,
        allowed=True,
        reasons=["低波动"],
        hard_blocks=[],
        component_scores={"trend": 90.0, "event": None},
        cost_breakdown={"planned_step_pct": 0.003, "total_cost_pct": 0.001},
        effective_weights={"trend": 0.2, "event": 0.0},
        score_contributions={"trend": 18.0, "event": 0.0},
        event_source_available=False,
        model_version="regime-v2",
        feature_snapshot_id=feature_id,
    )

    assert event_id
    assert repo.session_events(session_id)[0]["payload"] == {"close": 100.0}
    regime = repo.latest_regime_decision("BTCUSDT")
    assert regime is not None
    assert regime["allowed"] == 1
    assert regime["verdict"] == "ALLOWED"
    assert regime["threshold_used"] == 75.0
    assert regime["reasons"] == ["低波动"]
    assert regime["component_scores"]["event"] is None
    assert regime["cost_breakdown"]["planned_step_pct"] == 0.003
    assert regime["effective_weights"]["event"] == 0.0
    assert regime["score_contributions"]["trend"] == 18.0
    assert repo.regime_decision_history("BTCUSDT")[0]["grid_score"] == 88.0


def test_v2_schema_is_idempotent_and_session_grid_metadata_is_persisted(tmp_path) -> None:
    db_path = tmp_path / "v2-grid.db"
    init_db(db_path)
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "OBSERVING", 100, 1, now)

    repo.update_session_grid(
        session_id,
        102,
        98,
        10,
        0.002,
        0.5,
        97,
        "adaptive_v2",
        0.01,
        60,
        regime_score=86,
        grid_mode="adaptive_v2",
        cost_floor_pct=0.001,
        parameter_version="adaptive-grid-v2",
    )

    row = repo.get_session(session_id)
    assert row is not None
    assert row["regime_score"] == 86
    assert row["grid_mode"] == "adaptive_v2"
    assert row["parameter_version"] == "adaptive-grid-v2"


def test_repository_persists_frozen_dataset_windows(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 17, tzinfo=timezone.utc).isoformat()
    repo.save_backtest_dataset(
        {
            "dataset_id": "dataset-window-test",
            "provider": "csv",
            "market": "LOCAL",
            "symbol": "BTCUSDT",
            "interval": "1m",
            "requested_start": now,
            "requested_end": now,
            "actual_start": now,
            "actual_end": now,
            "row_count": 100,
            "file_path": "dataset-window-test.csv",
            "checksum": "abc123",
            "schema_version": 2,
            "quality_status": "READY",
            "quality_report": {},
            "window_mode": "NYSE_CLOSED_ONLY",
            "window_count": 0,
            "status": "READY",
        }
    )

    repo.replace_backtest_dataset_windows(
        "dataset-window-test",
        [
            {
                "window_id": "nyse_20260717T200000Z",
                "market_close": "2026-07-17T20:00:00+00:00",
                "force_close_at": "2026-07-20T06:00:00+00:00",
                "row_start_index": 10,
                "row_end_index": 80,
                "row_count": 70,
                "observation_rows": 30,
                "tradable_rows": 40,
                "status": "READY",
                "warning": None,
                "skip_reason": None,
            }
        ],
    )

    windows = repo.backtest_dataset_windows("dataset-window-test")
    dataset = repo.get_backtest_dataset("dataset-window-test")
    assert len(windows) == 1
    assert windows[0]["row_start_index"] == 10
    assert windows[0]["tradable_rows"] == 40
    assert dataset is not None
    assert dataset["window_count"] == 1
