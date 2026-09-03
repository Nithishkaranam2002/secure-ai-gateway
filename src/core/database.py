import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator

from src.core.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL,
    plan        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id   TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    amount      REAL NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers (customer_id)
);

CREATE TABLE IF NOT EXISTS tenants (
    api_key                TEXT PRIMARY KEY,
    tenant_name            TEXT NOT NULL,
    token_limit_per_minute INTEGER NOT NULL,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key     TEXT NOT NULL,
    tokens      INTEGER NOT NULL,
    recorded_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_usage_key_time
    ON token_usage (api_key, recorded_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    component   TEXT NOT NULL,
    action      TEXT NOT NULL,
    decision    TEXT NOT NULL,
    actor       TEXT,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (occurred_at);
"""


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        settings.database_path,
        check_same_thread=False,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialise_database() -> None:
    with get_connection() as connection:
        connection.executescript(SCHEMA)
