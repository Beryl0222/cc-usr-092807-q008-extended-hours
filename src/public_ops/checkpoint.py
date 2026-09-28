from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .errors import ConflictError, NotFoundError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Checkpoint:
    workflow_id: str
    tenant_id: str
    workflow_type: str
    subject_id: str
    phase: str
    version: int
    state: dict[str, Any]
    updated_at: str


class CheckpointStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def start(self, workflow_id: str, tenant_id: str, workflow_type: str, subject_id: str, phase: str, state: Mapping[str, Any]) -> Checkpoint:
        self.connection.execute(
            "INSERT INTO workflow_checkpoints(workflow_id, tenant_id, workflow_type, subject_id, phase, version, state_json, updated_at) VALUES(?, ?, ?, ?, ?, 1, ?, ?)",
            (workflow_id, tenant_id, workflow_type, subject_id, phase, json.dumps(dict(state), ensure_ascii=False, sort_keys=True), now()),
        )
        return self.get(workflow_id)

    def get(self, workflow_id: str) -> Checkpoint:
        row = self.connection.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id=?", (workflow_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"工作流不存在：{workflow_id}")
        return Checkpoint(
            workflow_id=row["workflow_id"], tenant_id=row["tenant_id"], workflow_type=row["workflow_type"],
            subject_id=row["subject_id"], phase=row["phase"], version=int(row["version"]),
            state=json.loads(row["state_json"]), updated_at=row["updated_at"],
        )

    def advance(self, workflow_id: str, expected_version: int, phase: str, state: Mapping[str, Any]) -> Checkpoint:
        result = self.connection.execute(
            "UPDATE workflow_checkpoints SET phase=?, version=version+1, state_json=?, updated_at=? WHERE workflow_id=? AND version=?",
            (phase, json.dumps(dict(state), ensure_ascii=False, sort_keys=True), now(), workflow_id, expected_version),
        )
        if result.rowcount != 1:
            raise ConflictError("工作流检查点发生并发冲突")
        return self.get(workflow_id)

    def list_incomplete(self, workflow_type: str | None = None) -> list[Checkpoint]:
        if workflow_type:
            rows = self.connection.execute(
                "SELECT workflow_id FROM workflow_checkpoints WHERE workflow_type=? AND phase NOT IN ('DONE', 'ABORTED') ORDER BY updated_at",
                (workflow_type,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT workflow_id FROM workflow_checkpoints WHERE phase NOT IN ('DONE', 'ABORTED') ORDER BY updated_at"
            ).fetchall()
        return [self.get(row["workflow_id"]) for row in rows]
