from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .checkpoint import CheckpointStore
from .database import transaction
from .errors import ConflictError, NotFoundError, ValidationError
from .outbox import Outbox
from .program import ProgramRepository
from .reservation import ReservationRepository
from .settlement import SettlementRepository


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CancellationResult:
    program_id: str
    cancelled_reservations: tuple[str, ...]
    refund_settlements: tuple[str, ...]
    notification_ids: tuple[str, ...]


class ProgramCancellationWorkflow:
    """协调节目取消、预订作废、退款和通知。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.programs = ProgramRepository(connection)
        self.reservations = ReservationRepository(connection)
        self.settlements = SettlementRepository(connection)
        self.checkpoints = CheckpointStore(connection)
        self.outbox = Outbox(connection)

    def cancel(
        self,
        tenant_id: str,
        program_id: str,
        actor_id: str,
        reason: str,
        after_program_cancelled: Callable[[], None] | None = None,
    ) -> CancellationResult:
        program = self.programs.get(tenant_id, program_id)
        if program.status == "CANCELLED":
            return CancellationResult(program_id, (), (), ())
        workflow_id = f"program-cancel:{tenant_id}:{program_id}:{program.version}"
        with transaction(self.connection, immediate=True):
            checkpoint = self.checkpoints.start(
                workflow_id, tenant_id, "PROGRAM_CANCELLATION", program_id, "PROGRAM_MARKED",
                {"reason": reason, "program_version": program.version},
            )
            cancelled = self.programs.transition(
                tenant_id, program_id, "CANCELLED", program.version, actor_id, reason
            )
        if after_program_cancelled:
            after_program_cancelled()
        reservation_rows = self.connection.execute(
            "SELECT record_id FROM reservations WHERE tenant_id=? AND archived_at IS NULL "
            "AND json_extract(payload_json, '$.program_id')=? AND status IN ('HELD','CONFIRMED') ORDER BY record_id",
            (tenant_id, program_id),
        ).fetchall()
        cancelled_reservations: list[str] = []
        refund_settlements: list[str] = []
        notification_ids: list[str] = []
        with transaction(self.connection, immediate=True):
            checkpoint = self.checkpoints.get(workflow_id)
            for row in reservation_rows:
                reservation = self.reservations.get(tenant_id, row["record_id"])
                changed = self.reservations.transition(
                    tenant_id, reservation.record_id, "CANCELLED", reservation.version, actor_id, reason
                )
                cancelled_reservations.append(changed.record_id)
                amount = float(changed.payload.get("paid_amount", 0))
                if amount > 0:
                    settlement_id = f"refund:{changed.record_id}:{changed.version}"
                    self.settlements.create(
                        tenant_id, settlement_id,
                        {"reservation_id": changed.record_id, "amount": amount, "currency": changed.payload.get("currency", "CNY"), "reason": reason},
                        actor_id, "PENDING",
                    )
                    refund_settlements.append(settlement_id)
                notification_ids.append(
                    self.outbox.enqueue(
                        tenant_id, "PROGRAM_CANCELLED", changed.record_id,
                        {"program_id": program_id, "reservation_id": changed.record_id, "reason": reason},
                        stable_key=f"{cancelled.version}:{changed.version}",
                    )
                )
            self.checkpoints.advance(
                workflow_id, checkpoint.version, "DONE",
                {"cancelled_reservations": cancelled_reservations, "refund_settlements": refund_settlements},
            )
        return CancellationResult(program_id, tuple(cancelled_reservations), tuple(refund_settlements), tuple(notification_ids))

    def recover(self, tenant_id: str, actor_id: str) -> list[CancellationResult]:
        results: list[CancellationResult] = []
        for checkpoint in self.checkpoints.list_incomplete("PROGRAM_CANCELLATION"):
            if checkpoint.tenant_id != tenant_id:
                continue
            results.append(self.cancel(tenant_id, checkpoint.subject_id, actor_id, checkpoint.state.get("reason", "恢复取消流程")))
        return results


class CapacityPlanner:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def available(self, tenant_id: str, program_id: str) -> int:
        program = self.connection.execute(
            "SELECT payload_json FROM programs WHERE tenant_id=? AND record_id=? AND archived_at IS NULL",
            (tenant_id, program_id),
        ).fetchone()
        if program is None:
            raise NotFoundError("节目不存在")
        import json
        capacity = int(json.loads(program["payload_json"]).get("capacity", 0))
        rows = self.connection.execute(
            "SELECT payload_json FROM reservations WHERE tenant_id=? AND archived_at IS NULL "
            "AND json_extract(payload_json, '$.program_id')=? AND status IN ('HELD','CONFIRMED')",
            (tenant_id, program_id),
        ).fetchall()
        used = sum(int(json.loads(row["payload_json"]).get("quantity", 1)) for row in rows)
        return max(0, capacity - used)

    def assert_capacity(self, tenant_id: str, program_id: str, requested: int) -> None:
        if requested < 1:
            raise ValidationError("requested", "数量必须为正数")
        remaining = self.available(tenant_id, program_id)
        if requested > remaining:
            raise ConflictError(f"余量不足：剩余{remaining}")


class CouponSettlementPolicy:
    def apply(self, face_value: float, order_amount: float, stacking: bool, already_discounted: float) -> float:
        if face_value < 0 or order_amount < 0 or already_discounted < 0:
            raise ValidationError("amount", "金额不能为负")
        if not stacking and already_discounted > 0:
            return 0.0
        available = max(0.0, order_amount - already_discounted)
        return round(min(face_value, available), 2)

    def reverse(self, applied_amount: float, consumed_amount: float) -> float:
        if consumed_amount > applied_amount:
            raise ValidationError("consumed_amount", "核销金额不能超过优惠金额")
        return round(applied_amount - consumed_amount, 2)
