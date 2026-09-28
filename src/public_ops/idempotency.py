from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import ConflictError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class IdempotencyStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def begin(self, tenant_id: str, scope: str, external_key: str, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        digest = request_hash(payload)
        row = self.connection.execute(
            "SELECT * FROM idempotency_keys WHERE tenant_id=? AND scope=? AND external_key=?",
            (tenant_id, scope, external_key),
        ).fetchone()
        if row is not None:
            if row["request_hash"] != digest:
                raise ConflictError("同一幂等键对应了不同内容")
            return json.loads(row["response_json"]) if row["response_json"] else None
        self.connection.execute(
            "INSERT INTO idempotency_keys(tenant_id, scope, external_key, request_hash, created_at) VALUES(?, ?, ?, ?, ?)",
            (tenant_id, scope, external_key, digest, now()),
        )
        return None

    def finish(self, tenant_id: str, scope: str, external_key: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "UPDATE idempotency_keys SET response_json=?, completed_at=? WHERE tenant_id=? AND scope=? AND external_key=?",
            (json.dumps(dict(response), ensure_ascii=False, sort_keys=True), now(), tenant_id, scope, external_key),
        )
