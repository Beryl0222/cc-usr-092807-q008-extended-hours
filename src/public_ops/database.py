from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def initialize_core(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            action TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            details_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_tenant_subject
            ON audit_log(tenant_id, subject_id, id);
        CREATE TABLE IF NOT EXISTS outbox (
            message_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON outbox(delivered_at, created_at);
        CREATE TABLE IF NOT EXISTS workflow_checkpoints (
            workflow_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            workflow_type TEXT NOT NULL,
            subject_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            version INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            tenant_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            external_key TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            response_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            PRIMARY KEY (tenant_id, scope, external_key)
        );
        """
    )
