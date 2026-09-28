from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError


TABLE = 'notifications'
STATUSES = ('QUEUED', 'SENT', 'FAILED', 'CANCELLED')
TRANSITIONS = {'QUEUED': ['SENT', 'FAILED', 'CANCELLED'], 'SENT': [], 'FAILED': ['QUEUED', 'CANCELLED'], 'CANCELLED': []}
REQUIRED_FIELDS = ('recipient_ref', 'channel', 'template', 'subject_id')
PROTECTED_FIELDS = ('recipient_ref', 'rendered_body')
DATE_FIELDS = ()
NUMERIC_FIELDS = ()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field_name, "时间不能为空")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(field_name, "时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(field_name, "时间必须带时区")
    return parsed


def normalize_text(value: Any, field_name: str, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise ValidationError(field_name, "必须是字符串")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValidationError(field_name, "不能为空")
    if len(normalized) > maximum:
        raise ValidationError(field_name, f"不得超过{maximum}个字符")
    return normalized


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValidationError("payload", "必须是对象")
    missing = [name for name in REQUIRED_FIELDS if name not in payload]
    if missing:
        raise ValidationError("payload", "缺少字段：" + "、".join(missing))
    normalized: dict[str, Any] = {}
    for name, value in payload.items():
        if name in DATE_FIELDS:
            normalized[name] = parse_time(str(value), name).isoformat()
        elif name in NUMERIC_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(name, "必须是数值")
            if value < 0:
                raise ValidationError(name, "不能为负数")
            normalized[name] = value
        elif isinstance(value, str):
            normalized[name] = normalize_text(value, name)
        elif isinstance(value, (int, float, bool)) or value is None:
            normalized[name] = value
        elif isinstance(value, (list, dict)):
            normalized[name] = value
        else:
            raise ValidationError(name, "包含不支持的值")
    return normalized


@dataclass(frozen=True)
class NotificationRecord:
    record_id: str
    tenant_id: str
    status: str
    version: int
    payload: dict[str, Any]
    created_at: str
    updated_at: str
    archived_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "NotificationRecord":
        return cls(
            record_id=row["record_id"],
            tenant_id=row["tenant_id"],
            status=row["status"],
            version=int(row["version"]),
            payload=json.loads(row["payload_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
        )

    def public_view(self, role: str) -> dict[str, Any]:
        data = asdict(self)
        if role not in {"auditor", "administrator"}:
            for key in PROTECTED_FIELDS:
                data["payload"].pop(key, None)
        return data


@dataclass(frozen=True)
class NotificationChange:
    record_id: str
    tenant_id: str
    action: str
    actor_id: str
    previous_version: int
    current_version: int
    occurred_at: str
    details: dict[str, Any] = field(default_factory=dict)


class NotificationRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create_schema(self) -> None:
        self.connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                record_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT,
                PRIMARY KEY (tenant_id, record_id)
            );
            CREATE TABLE IF NOT EXISTS {TABLE}_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                previous_version INTEGER NOT NULL,
                current_version INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_{TABLE}_status
                ON {TABLE}(tenant_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_{TABLE}_changes_record
                ON {TABLE}_changes(tenant_id, record_id, id);
            """
        )

    def create(
        self,
        tenant_id: str,
        record_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
        initial_status: str | None = None,
    ) -> NotificationRecord:
        tenant_id = normalize_text(tenant_id, "tenant_id", 100)
        record_id = normalize_text(record_id, "record_id", 160)
        actor_id = normalize_text(actor_id, "actor_id", 120)
        normalized = normalize_payload(payload)
        status = initial_status or STATUSES[0]
        if status not in STATUSES:
            raise ValidationError("status", "初始状态无效")
        timestamp = utc_now()
        try:
            self.connection.execute(
                f"INSERT INTO {TABLE}(record_id, tenant_id, status, version, payload_json, created_at, updated_at) "
                "VALUES(?, ?, ?, 1, ?, ?, ?)",
                (record_id, tenant_id, status, json.dumps(normalized, ensure_ascii=False, sort_keys=True), timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"记录已存在：{tenant_id}/{record_id}") from exc
        self._append_change(record_id, tenant_id, "CREATE", actor_id, 0, 1, {"status": status})
        return self.get(tenant_id, record_id)

    def get(self, tenant_id: str, record_id: str, include_archived: bool = False) -> NotificationRecord:
        sql = f"SELECT * FROM {TABLE} WHERE tenant_id=? AND record_id=?"
        params: list[Any] = [tenant_id, record_id]
        if not include_archived:
            sql += " AND archived_at IS NULL"
        row = self.connection.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError(f"记录不存在：{tenant_id}/{record_id}")
        return NotificationRecord.from_row(row)

    def list_records(
        self,
        tenant_id: str,
        statuses: Iterable[str] | None = None,
        limit: int = 100,
        after_updated_at: str | None = None,
    ) -> list[NotificationRecord]:
        if limit < 1 or limit > 500:
            raise ValidationError("limit", "范围必须为1到500")
        clauses = ["tenant_id=?", "archived_at IS NULL"]
        params: list[Any] = [tenant_id]
        chosen = tuple(statuses or ())
        if chosen:
            invalid = [value for value in chosen if value not in STATUSES]
            if invalid:
                raise ValidationError("statuses", "包含无效状态")
            clauses.append("status IN (" + ",".join("?" for _ in chosen) + ")")
            params.extend(chosen)
        if after_updated_at:
            parse_time(after_updated_at, "after_updated_at")
            clauses.append("updated_at>?")
            params.append(after_updated_at)
        params.append(limit)
        rows = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE " + " AND ".join(clauses) + " ORDER BY updated_at, record_id LIMIT ?",
            params,
        ).fetchall()
        return [NotificationRecord.from_row(row) for row in rows]

    def update_payload(
        self,
        tenant_id: str,
        record_id: str,
        patch: Mapping[str, Any],
        expected_version: int,
        actor_id: str,
    ) -> NotificationRecord:
        current = self.get(tenant_id, record_id)
        if current.version != expected_version:
            raise ConflictError(f"版本冲突：当前为{current.version}，提交为{expected_version}")
        merged = dict(current.payload)
        merged.update(patch)
        normalized = normalize_payload(merged)
        next_version = current.version + 1
        timestamp = utc_now()
        result = self.connection.execute(
            f"UPDATE {TABLE} SET payload_json=?, version=?, updated_at=? "
            "WHERE tenant_id=? AND record_id=? AND version=? AND archived_at IS NULL",
            (json.dumps(normalized, ensure_ascii=False, sort_keys=True), next_version, timestamp, tenant_id, record_id, current.version),
        )
        if result.rowcount != 1:
            raise ConflictError("并发更新失败")
        self._append_change(record_id, tenant_id, "UPDATE", actor_id, current.version, next_version, {"fields": sorted(patch)})
        return self.get(tenant_id, record_id)

    def transition(
        self,
        tenant_id: str,
        record_id: str,
        target_status: str,
        expected_version: int,
        actor_id: str,
        reason: str,
    ) -> NotificationRecord:
        current = self.get(tenant_id, record_id)
        if current.version != expected_version:
            raise ConflictError("状态变更使用了旧版本")
        allowed = TRANSITIONS.get(current.status, ())
        if target_status not in allowed:
            raise ValidationError("target_status", f"不允许从{current.status}变为{target_status}")
        next_version = current.version + 1
        timestamp = utc_now()
        result = self.connection.execute(
            f"UPDATE {TABLE} SET status=?, version=?, updated_at=? "
            "WHERE tenant_id=? AND record_id=? AND version=? AND archived_at IS NULL",
            (target_status, next_version, timestamp, tenant_id, record_id, current.version),
        )
        if result.rowcount != 1:
            raise ConflictError("状态变更发生并发冲突")
        self._append_change(record_id, tenant_id, "TRANSITION", actor_id, current.version, next_version, {"from": current.status, "to": target_status, "reason": normalize_text(reason, "reason")})
        return self.get(tenant_id, record_id)

    def archive(self, tenant_id: str, record_id: str, expected_version: int, actor_id: str, reason: str) -> NotificationRecord:
        current = self.get(tenant_id, record_id)
        if current.version != expected_version:
            raise ConflictError("归档使用了旧版本")
        timestamp = utc_now()
        next_version = current.version + 1
        self.connection.execute(
            f"UPDATE {TABLE} SET archived_at=?, version=?, updated_at=? WHERE tenant_id=? AND record_id=? AND version=?",
            (timestamp, next_version, timestamp, tenant_id, record_id, current.version),
        )
        self._append_change(record_id, tenant_id, "ARCHIVE", actor_id, current.version, next_version, {"reason": normalize_text(reason, "reason")})
        return self.get(tenant_id, record_id, include_archived=True)

    def restore(self, tenant_id: str, record_id: str, expected_version: int, actor_id: str) -> NotificationRecord:
        current = self.get(tenant_id, record_id, include_archived=True)
        if current.archived_at is None:
            return current
        if current.version != expected_version:
            raise ConflictError("恢复使用了旧版本")
        timestamp = utc_now()
        next_version = current.version + 1
        self.connection.execute(
            f"UPDATE {TABLE} SET archived_at=NULL, version=?, updated_at=? WHERE tenant_id=? AND record_id=? AND version=?",
            (next_version, timestamp, tenant_id, record_id, current.version),
        )
        self._append_change(record_id, tenant_id, "RESTORE", actor_id, current.version, next_version, {})
        return self.get(tenant_id, record_id)

    def changes(self, tenant_id: str, record_id: str) -> list[NotificationChange]:
        rows = self.connection.execute(
            f"SELECT * FROM {TABLE}_changes WHERE tenant_id=? AND record_id=? ORDER BY id",
            (tenant_id, record_id),
        ).fetchall()
        return [
            NotificationChange(
                record_id=row["record_id"], tenant_id=row["tenant_id"], action=row["action"],
                actor_id=row["actor_id"], previous_version=int(row["previous_version"]),
                current_version=int(row["current_version"]), occurred_at=row["occurred_at"],
                details=json.loads(row["details_json"]),
            )
            for row in rows
        ]

    def _append_change(self, record_id: str, tenant_id: str, action: str, actor_id: str, previous_version: int, current_version: int, details: Mapping[str, Any]) -> None:
        self.connection.execute(
            f"INSERT INTO {TABLE}_changes(record_id, tenant_id, action, actor_id, previous_version, current_version, occurred_at, details_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, tenant_id, action, actor_id, previous_version, current_version, utc_now(), json.dumps(dict(details), ensure_ascii=False, sort_keys=True)),
        )


class NotificationService:
    def __init__(self, repository: NotificationRepository, permissions: Any, audit: Any):
        self.repository = repository
        self.permissions = permissions
        self.audit = audit

    def register(self, tenant_id: str, record_id: str, payload: Mapping[str, Any], actor_id: str, role: str) -> NotificationRecord:
        self.permissions.require(role, "notifications.create")
        record = self.repository.create(tenant_id, record_id, payload, actor_id)
        self.audit.write(tenant_id, actor_id, "notifications.create", record_id, {"version": record.version})
        return record

    def revise(self, tenant_id: str, record_id: str, patch: Mapping[str, Any], expected_version: int, actor_id: str, role: str) -> NotificationRecord:
        self.permissions.require(role, "notifications.update")
        record = self.repository.update_payload(tenant_id, record_id, patch, expected_version, actor_id)
        self.audit.write(tenant_id, actor_id, "notifications.update", record_id, {"version": record.version, "fields": sorted(patch)})
        return record

    def move(self, tenant_id: str, record_id: str, target_status: str, expected_version: int, actor_id: str, role: str, reason: str) -> NotificationRecord:
        self.permissions.require(role, "notifications.transition")
        record = self.repository.transition(tenant_id, record_id, target_status, expected_version, actor_id, reason)
        self.audit.write(tenant_id, actor_id, "notifications.transition", record_id, {"version": record.version, "status": record.status})
        return record

    def view(self, tenant_id: str, record_id: str, actor_id: str, role: str) -> dict[str, Any]:
        self.permissions.require(role, "notifications.read")
        record = self.repository.get(tenant_id, record_id)
        self.audit.write(tenant_id, actor_id, "notifications.read", record_id, {"version": record.version})
        return record.public_view(role)
