from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .database import connect, initialize_core
from .venue import VenueRepository
from .program import ProgramRepository
from .partner import PartnerRepository
from .asset import AssetRepository
from .reservation import ReservationRepository
from .coupon import CouponRepository
from .settlement import SettlementRepository
from .incident import IncidentRepository
from .amendment import AmendmentRepository
from .notification import NotificationRepository
from .audit_case import AuditCaseRepository
from .campaign import CampaignRepository
from .culture_workflows import CapacityPlanner, CouponSettlementPolicy


def initialize(connection: Any) -> None:
    initialize_core(connection)
    VenueRepository(connection).create_schema()
    ProgramRepository(connection).create_schema()
    PartnerRepository(connection).create_schema()
    AssetRepository(connection).create_schema()
    ReservationRepository(connection).create_schema()
    CouponRepository(connection).create_schema()
    SettlementRepository(connection).create_schema()
    IncidentRepository(connection).create_schema()
    AmendmentRepository(connection).create_schema()
    NotificationRepository(connection).create_schema()
    AuditCaseRepository(connection).create_schema()
    CampaignRepository(connection).create_schema()


def command_init(args: argparse.Namespace) -> int:
    connection = connect(args.database)
    initialize(connection)
    print(json.dumps({"database": str(args.database), "initialized": True}, ensure_ascii=False))
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    connection = connect(args.database)
    initialize(connection)
    if 'culture' == "culture":
        result = CouponSettlementPolicy().apply(30.0, 120.0, False, 0.0)
        payload = {"project": "public_ops", "coupon_applied": result, "tables": len(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())}
    else:
        risk, reasons = ScreeningRiskPolicy().classify(56, {"bleeding"}, True, False)
        vaccine = VaccineSeasonPolicy().recommendation(68, {"cardiovascular"}, None, "2026-09-01T00:00:00+08:00")
        payload = {"project": "health_ops", "risk": risk, "reasons": reasons, "vaccine": vaccine, "tables": len(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="项目维护命令")
    parser.add_argument("--database", default=":memory:")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init")
    init_parser.set_defaults(handler=command_init)
    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.set_defaults(handler=command_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
