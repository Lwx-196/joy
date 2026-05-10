"""Stress-test status and preflight endpoints."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from .. import db, stress

router = APIRouter(tags=["stress"])


def _dir_size(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _counts(conn, table: str, status_column: str = "status") -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {status_column} AS status, COUNT(*) AS n FROM {table} GROUP BY {status_column}"
    ).fetchall()
    return {str(row["status"]): int(row["n"]) for row in rows}


@router.get("/api/stress/status")
def stress_status() -> dict[str, Any]:
    with db.connect() as conn:
        case_row = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN trashed_at IS NULL THEN 1 ELSE 0 END) AS active
            FROM cases
            """
        ).fetchone()
        image_total = 0
        for row in conn.execute("SELECT meta_json FROM cases WHERE trashed_at IS NULL").fetchall():
            try:
                import json

                meta = json.loads(row["meta_json"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            files = meta.get("image_files") if isinstance(meta, dict) else None
            if isinstance(files, list):
                image_total += len(files)
        render_counts = _counts(conn, "render_jobs")
        simulation_counts = _counts(conn, "simulation_jobs")
        quality_counts = _counts(conn, "render_quality", "quality_status")

    root = stress.output_root()
    return stress.status_payload(
        db_path=db.DB_PATH,
        extra={
            "cases": {
                "total": int(case_row["total"] or 0),
                "active": int(case_row["active"] or 0),
                "image_files": image_total,
            },
            "render_jobs": render_counts,
            "simulation_jobs": simulation_counts,
            "render_quality": quality_counts,
            "output_root_size_bytes": _dir_size(root),
        },
    )
