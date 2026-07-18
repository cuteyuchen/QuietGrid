from __future__ import annotations

import sqlite3
from pathlib import Path


TABLE_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id      TEXT,
    window_start    DATETIME NOT NULL,
    window_end      DATETIME,
    status          TEXT NOT NULL DEFAULT 'open',
    total_pnl       REAL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id       INTEGER REFERENCES windows(id),
    symbol          TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'IDLE',
    grid_upper      REAL,
    grid_lower      REAL,
    grid_num        INTEGER,
    step_pct        REAL,
    baseline_atr    REAL,
    stop_loss_price REAL,
    volatility_method TEXT,
    volatility_value  REAL,
    volatility_window INTEGER,
    volatility_current_value  REAL,
    volatility_current_window INTEGER,
    volatility_current_at     DATETIME,
    capital         REAL DEFAULT 200,
    leverage        INTEGER DEFAULT 10,
    realized_pnl    REAL DEFAULT 0,
    open_time       DATETIME,
    close_time      DATETIME,
    close_reason    TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    qty             REAL NOT NULL,
    quote_qty       REAL NOT NULL,
    grid_index      INTEGER,
    grid_pnl        REAL,
    fee             REAL DEFAULT 0,
    funding_fee     REAL DEFAULT 0,
    trade_time      DATETIME NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    grid_index      INTEGER,
    side            TEXT NOT NULL,
    price           REAL NOT NULL,
    qty             REAL NOT NULL,
    status          TEXT NOT NULL,
    entry_price     REAL,
    created_at      DATETIME NOT NULL,
    filled_at       DATETIME,
    fill_price      REAL,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, client_id)
);

CREATE TABLE IF NOT EXISTS state_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    from_state      TEXT NOT NULL,
    to_state        TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    detail          TEXT,
    log_time        DATETIME NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    level           TEXT NOT NULL,
    module          TEXT NOT NULL,
    message         TEXT NOT NULL,
    detail          TEXT,
    log_time        DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS control_state (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS selection_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT NOT NULL DEFAULT 'default',
    environment     TEXT NOT NULL DEFAULT 'testnet',
    snapshot_at     DATETIME NOT NULL,
    rank            INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    score           REAL,
    volume_score    REAL,
    depth_score     REAL,
    volume_24h      REAL,
    depth_usdt      REAL,
    bid_price       REAL,
    ask_price       REAL,
    spread_pct      REAL,
    selected        INTEGER NOT NULL DEFAULT 0,
    disabled        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ok',
    error           TEXT,
    UNIQUE(account_id, environment, symbol)
);

CREATE TABLE IF NOT EXISTS round_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id           INTEGER NOT NULL REFERENCES windows(id),
    symbol              TEXT NOT NULL,
    liquidity_rank      INTEGER,
    score               REAL,
    volume_score        REAL,
    depth_score         REAL,
    volume_24h          REAL,
    depth_usdt          REAL,
    price               REAL,
    bid_price           REAL,
    ask_price           REAL,
    spread_pct          REAL,
    volatility_method   TEXT,
    volatility_value    REAL,
    volatility_window   INTEGER,
    range_lower         REAL,
    range_upper         REAL,
    range_width_pct     REAL,
    threshold_met       INTEGER NOT NULL DEFAULT 0,
    session_id          INTEGER REFERENCES sessions(id),
    stage               TEXT NOT NULL DEFAULT 'scanning',
    error               TEXT,
    last_kline_close_at DATETIME,
    market_updated_at   DATETIME,
    calculated_at       DATETIME,
    data_stale          INTEGER NOT NULL DEFAULT 0,
    updated_at          DATETIME NOT NULL,
    UNIQUE(window_id, symbol)
);

CREATE TABLE IF NOT EXISTS event_store (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    account_id      TEXT NOT NULL DEFAULT 'default',
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT,
    event_type      TEXT NOT NULL,
    event_time      DATETIME NOT NULL,
    available_time  DATETIME NOT NULL,
    payload_json    TEXT NOT NULL,
    code_commit     TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    symbol          TEXT NOT NULL,
    as_of_time      DATETIME NOT NULL,
    source_time     DATETIME NOT NULL,
    features_json   TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS regime_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id),
    symbol              TEXT NOT NULL,
    as_of_time          DATETIME NOT NULL,
    state               TEXT NOT NULL,
    grid_score          REAL NOT NULL,
    allowed             INTEGER NOT NULL,
    reasons_json        TEXT NOT NULL,
    hard_blocks_json    TEXT NOT NULL,
    component_scores_json TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    feature_snapshot_id INTEGER REFERENCES feature_snapshots(id),
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS grid_plans (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id),
    symbol              TEXT NOT NULL,
    as_of_time          DATETIME NOT NULL,
    center              REAL NOT NULL,
    lower_price         REAL NOT NULL,
    upper_price         REAL NOT NULL,
    step_pct            REAL NOT NULL,
    grid_num            INTEGER NOT NULL,
    prices_json         TEXT NOT NULL,
    qty_weights_json    TEXT NOT NULL,
    cost_floor_pct      REAL NOT NULL DEFAULT 0,
    regime_score        REAL,
    parameter_version   TEXT NOT NULL,
    expires_at          DATETIME,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inventory_lots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES sessions(id),
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL,
    entry_price         REAL NOT NULL,
    qty                 REAL NOT NULL,
    entry_grid_index    INTEGER,
    target_exit_price   REAL,
    opened_at           DATETIME,
    status              TEXT NOT NULL DEFAULT 'OPEN',
    updated_at          DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES sessions(id),
    symbol              TEXT NOT NULL,
    as_of_time          DATETIME NOT NULL,
    net_qty             REAL NOT NULL,
    net_notional        REAL NOT NULL,
    gross_notional      REAL NOT NULL,
    avg_entry_price     REAL,
    unrealized_pnl      REAL NOT NULL,
    utilization         REAL NOT NULL,
    risk_score          REAL NOT NULL,
    risk_level          TEXT NOT NULL,
    unpaired_lots       INTEGER NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id),
    window_id           INTEGER REFERENCES windows(id),
    symbol              TEXT,
    as_of_time          DATETIME NOT NULL,
    risk_level          TEXT NOT NULL,
    action              TEXT NOT NULL,
    reason              TEXT NOT NULL,
    session_pnl         REAL,
    window_pnl          REAL,
    inventory_utilization REAL,
    limits_json         TEXT NOT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parameter_versions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    version             TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL,
    config_json         TEXT NOT NULL,
    code_commit         TEXT,
    validation_report   TEXT,
    activated_at        DATETIME,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL UNIQUE,
    symbol              TEXT NOT NULL,
    started_at          DATETIME NOT NULL,
    completed_at        DATETIME,
    data_start          DATETIME,
    data_end            DATETIME,
    fill_model          TEXT NOT NULL,
    parameter_version   TEXT,
    code_commit         TEXT,
    status              TEXT NOT NULL,
    report_path         TEXT,
    config_json         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_run_id     INTEGER NOT NULL REFERENCES backtest_runs(id),
    metric_name         TEXT NOT NULL,
    metric_value        REAL,
    metric_json         TEXT,
    UNIQUE(backtest_run_id, metric_name)
);

CREATE TABLE IF NOT EXISTS backtest_datasets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id          TEXT NOT NULL UNIQUE,
    account_id          TEXT NOT NULL DEFAULT 'default',
    provider            TEXT NOT NULL,
    market              TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    interval            TEXT NOT NULL,
    price_type          TEXT NOT NULL DEFAULT 'CONTRACT',
    requested_start     DATETIME NOT NULL,
    requested_end       DATETIME NOT NULL,
    actual_start        DATETIME,
    actual_end          DATETIME,
    row_count           INTEGER NOT NULL DEFAULT 0,
    file_format         TEXT NOT NULL DEFAULT 'csv',
    file_path           TEXT NOT NULL,
    checksum            TEXT NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    quality_status      TEXT NOT NULL,
    quality_report_json TEXT NOT NULL,
    window_mode         TEXT NOT NULL DEFAULT 'NYSE_CLOSED_ONLY',
    window_count        INTEGER,
    raw_window_count    INTEGER,
    eligible_window_count INTEGER,
    skipped_window_count  INTEGER,
    source_segments_json  TEXT,
    has_funding         INTEGER NOT NULL DEFAULT 0,
    funding_event_count INTEGER,
    funding_file_path   TEXT,
    status              TEXT NOT NULL,
    error               TEXT,
    deleted_at          DATETIME,
    created_at          DATETIME NOT NULL,
    updated_at          DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_dataset_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT NOT NULL UNIQUE,
    account_id          TEXT NOT NULL DEFAULT 'default',
    dataset_id          TEXT,
    provider            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    interval            TEXT NOT NULL,
    requested_start     DATETIME NOT NULL,
    requested_end       DATETIME NOT NULL,
    window_mode         TEXT NOT NULL DEFAULT 'NYSE_CLOSED_ONLY',
    status              TEXT NOT NULL,
    stage               TEXT NOT NULL,
    progress            REAL NOT NULL DEFAULT 0,
    current_page        INTEGER NOT NULL DEFAULT 0,
    total_pages         INTEGER NOT NULL DEFAULT 0,
    downloaded_rows     INTEGER NOT NULL DEFAULT 0,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    created_at          DATETIME NOT NULL,
    started_at          DATETIME,
    completed_at        DATETIME,
    updated_at          DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_dataset_windows (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id          TEXT NOT NULL REFERENCES backtest_datasets(dataset_id),
    window_id           TEXT NOT NULL,
    market_close        DATETIME NOT NULL,
    force_close_at      DATETIME NOT NULL,
    row_start_index     INTEGER,
    row_end_index       INTEGER,
    row_count           INTEGER NOT NULL,
    observation_rows    INTEGER NOT NULL,
    tradable_rows       INTEGER NOT NULL,
    status              TEXT NOT NULL,
    warning             TEXT,
    skip_reason         TEXT,
    created_at          DATETIME NOT NULL,
    UNIQUE(dataset_id, window_id)
);

CREATE TABLE IF NOT EXISTS control_commands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id          TEXT NOT NULL UNIQUE,
    account_id          TEXT NOT NULL DEFAULT 'default',
    command_type        TEXT NOT NULL,
    target_type         TEXT NOT NULL,
    target_id           TEXT,
    payload_json        TEXT NOT NULL,
    reason              TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL UNIQUE,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    result_json         TEXT,
    requested_by        TEXT,
    requested_at        DATETIME NOT NULL,
    updated_at          DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    actor               TEXT NOT NULL,
    action              TEXT NOT NULL,
    resource_type       TEXT NOT NULL,
    resource_id         TEXT,
    detail_json         TEXT NOT NULL,
    source_ip           TEXT,
    user_agent          TEXT,
    created_at          DATETIME NOT NULL
);
"""

INDEX_SCHEMA_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_window ON sessions(window_id);
CREATE INDEX IF NOT EXISTS idx_sessions_symbol ON sessions(symbol);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(close_time, state, id);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_session_status ON orders(session_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status_session ON orders(status, session_id);
CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(trade_time);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_session_order ON trades(session_id, order_id);
CREATE INDEX IF NOT EXISTS idx_state_logs_session ON state_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_system_logs_level_id ON system_logs(level, id);
CREATE INDEX IF NOT EXISTS idx_system_logs_module_id ON system_logs(module, id);
CREATE INDEX IF NOT EXISTS idx_selection_candidates_scope_rank ON selection_candidates(account_id, environment, rank);
DROP INDEX IF EXISTS uq_windows_runtime;
CREATE INDEX IF NOT EXISTS idx_windows_runtime ON windows(runtime_id) WHERE runtime_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_round_candidates_window_rank ON round_candidates(window_id, liquidity_rank);
CREATE INDEX IF NOT EXISTS idx_round_candidates_session ON round_candidates(session_id);
CREATE INDEX IF NOT EXISTS idx_event_store_session_time ON event_store(session_id, event_time);
CREATE INDEX IF NOT EXISTS idx_event_store_symbol_time ON event_store(symbol, event_time);
CREATE INDEX IF NOT EXISTS idx_feature_snapshots_symbol_time ON feature_snapshots(symbol, as_of_time);
CREATE INDEX IF NOT EXISTS idx_regime_decisions_symbol_time ON regime_decisions(symbol, as_of_time);
CREATE INDEX IF NOT EXISTS idx_grid_plans_session_time ON grid_plans(session_id, as_of_time);
CREATE INDEX IF NOT EXISTS idx_inventory_lots_session_status ON inventory_lots(session_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_snapshots_session_time ON inventory_snapshots(session_id, as_of_time);
CREATE INDEX IF NOT EXISTS idx_risk_snapshots_window_time ON risk_snapshots(window_id, as_of_time);
CREATE INDEX IF NOT EXISTS idx_control_commands_status_time ON control_commands(status, requested_at);
CREATE INDEX IF NOT EXISTS idx_backtest_datasets_identity ON backtest_datasets(account_id, provider, symbol, interval, requested_start, requested_end);
CREATE INDEX IF NOT EXISTS idx_backtest_datasets_status ON backtest_datasets(status, deleted_at);
CREATE INDEX IF NOT EXISTS idx_backtest_dataset_jobs_account_status ON backtest_dataset_jobs(account_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_backtest_dataset_windows_dataset ON backtest_dataset_windows(dataset_id, market_close);
"""

SCHEMA_SQL = TABLE_SCHEMA_SQL + "\n" + INDEX_SCHEMA_SQL


SESSION_COLUMN_MIGRATIONS = {
    "window_id": "INTEGER REFERENCES windows(id)",
    "volatility_method": "TEXT",
    "volatility_value": "REAL",
    "volatility_window": "INTEGER",
    "volatility_current_value": "REAL",
    "volatility_current_window": "INTEGER",
    "volatility_current_at": "DATETIME",
    "regime_score": "REAL",
    "grid_mode": "TEXT",
    "cost_floor_pct": "REAL",
    "parameter_version": "TEXT",
}

WINDOW_COLUMN_MIGRATIONS = {
    "runtime_id": "TEXT",
}

BACKTEST_RUN_COLUMN_MIGRATIONS = {
    "dataset_id": "TEXT",
    "dataset_checksum": "TEXT",
    "data_provider": "TEXT",
    "window_mode": "TEXT",
    "dataset_schema_version": "INTEGER",
    "window_count": "INTEGER",
}

BACKTEST_DATASET_WINDOW_COLUMN_MIGRATIONS = {
    "row_start_index": "INTEGER",
    "row_end_index": "INTEGER",
    "skip_reason": "TEXT",
}

BACKTEST_DATASET_COLUMN_MIGRATIONS = {
    "raw_window_count": "INTEGER",
    "eligible_window_count": "INTEGER",
    "skipped_window_count": "INTEGER",
    "source_segments_json": "TEXT",
    "has_funding": "INTEGER NOT NULL DEFAULT 0",
    "funding_event_count": "INTEGER",
    "funding_file_path": "TEXT",
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(TABLE_SCHEMA_SQL)
        _ensure_columns(conn, "windows", WINDOW_COLUMN_MIGRATIONS)
        _ensure_session_columns(conn)
        _ensure_columns(conn, "backtest_runs", BACKTEST_RUN_COLUMN_MIGRATIONS)
        _ensure_columns(
            conn,
            "backtest_dataset_windows",
            BACKTEST_DATASET_WINDOW_COLUMN_MIGRATIONS,
        )
        _ensure_columns(
            conn,
            "backtest_datasets",
            BACKTEST_DATASET_COLUMN_MIGRATIONS,
        )
        conn.executescript(INDEX_SCHEMA_SQL)
        conn.commit()


def _ensure_session_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "sessions", SESSION_COLUMN_MIGRATIONS)


def _ensure_columns(conn: sqlite3.Connection, table: str, migrations: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, column_type in migrations.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")
