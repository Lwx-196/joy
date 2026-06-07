"""Scan endpoints."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import db, scanner
from ..models import ScanResult

router = APIRouter(prefix="/api", tags=["scan"])


class ScanRequest(BaseModel):
    mode: str = "incremental"
    roots: list[str] | None = Field(default=None, description="Extra root paths to scan (appended to DEFAULT_ROOTS)")


@router.post("/scan", response_model=ScanResult)
def trigger_scan(body: ScanRequest | None = None) -> ScanResult:
    mode = body.mode if body else "incremental"
    if mode not in {"full", "incremental"}:
        mode = "incremental"
    roots = list(scanner.DEFAULT_ROOTS)
    if body and body.roots:
        roots.extend(Path(r) for r in body.roots)
    with db.connect() as conn:
        result = scanner.scan(conn, roots=roots, mode=mode)
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
