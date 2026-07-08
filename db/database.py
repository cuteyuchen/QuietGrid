from __future__ import annotations

import sqlite3
from pathlib import Path


TABLE_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS windows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
"""

INDEX_SCHEMA_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_window ON sessions(window_id);
CREATE INDEX IF NOT EXISTS idx_sessions_symbol ON sessions(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(trade_time);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trades_session_order ON trades(session_id, order_id);
CREATE INDEX IF NOT EXISTS idx_state_logs_session ON state_logs(session_id);
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
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(TABLE_SCHEMA_SQL)
        _ensure_session_columns(conn)
        conn.executescript(INDEX_SCHEMA_SQL)
        conn.commit()


def _ensure_session_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for name, column_type in SESSION_COLUMN_MIGRATIONS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {column_type}")
