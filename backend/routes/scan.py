"""Scan endpoints."""
from __future__ import annotations

import json
from fastapi import APIRouter

from .. import db, scanner
from ..models import ScanResult

router = APIRouter(prefix="/api", tags=["scan"])


@router.post("/scan", response_model=ScanResult)
def trigger_scan(mode: str = "incremental") -> ScanResult:
    if mode not in {"full", "incremental"}:
        mode = "incremental"
    with db.connect() as conn:
        result = scanner.scan(conn, mode=mode)
    return ScanResult(**result)


@router.get("/scan/latest")
def latest_scan() -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, started_at, completed_at, case_count, mode, root_paths FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"scan": None}
        return {
            "scan": {
                "id": row["id"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "case_count": row["case_count"],
                "mode": row["mode"],
                "root_paths": json.loads(row["root_paths"] or "[]"),
            }
        }
