from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Outbox:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def enqueue(self, tenant_id: str, topic: str, subject_id: str, payload: Mapping[str, Any], stable_key: str) -> str:
        message_id = hashlib.sha256(f"{tenant_id}|{topic}|{subject_id}|{stable_key}".encode()).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO outbox(message_id, tenant_id, topic, subject_id, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (message_id, tenant_id, topic, subject_id, json.dumps(dict(payload), ensure_ascii=False, sort_keys=True), now()),
        )
        return message_id

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY created_at, message_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def deliver(self, sender: Callable[[str, str, Mapping[str, Any]], None], limit: int = 100) -> tuple[int, int]:
        delivered = failed = 0
        for item in self.pending(limit):
            try:
                sender(item["topic"], item["subject_id"], item["payload"])
            except Exception as exc:
                failed += 1
                self.connection.execute(
                    "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE message_id=?",
                    (str(exc)[:500], item["message_id"]),
                )
            else:
                delivered += 1
                self.connection.execute(
                    "UPDATE outbox SET attempts=attempts+1, last_error=NULL, delivered_at=? WHERE message_id=?",
                    (now(), item["message_id"]),
                )
        return delivered, failed
