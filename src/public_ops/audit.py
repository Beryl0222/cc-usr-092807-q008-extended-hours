from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def write(self, tenant_id: str, actor_id: str, action: str, subject_id: str, details: Mapping[str, Any]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO audit_log(tenant_id, actor_id, action, subject_id, details_json, occurred_at) VALUES(?, ?, ?, ?, ?, ?)",
            (tenant_id, actor_id, action, subject_id, json.dumps(dict(details), ensure_ascii=False, sort_keys=True), now()),
        )
        return int(cursor.lastrowid)

    def history(self, tenant_id: str, subject_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM audit_log WHERE tenant_id=? AND subject_id=? ORDER BY id",
            (tenant_id, subject_id),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "actor_id": row["actor_id"],
                "action": row["action"],
                "details": json.loads(row["details_json"]),
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        ]
