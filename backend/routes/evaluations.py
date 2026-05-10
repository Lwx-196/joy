"""Evaluation endpoints — Phase 2 评估台 (阶段 3).

Endpoints:
    POST   /api/evaluations                       create new evaluation
    GET    /api/evaluations                       list by subject (history)
    GET    /api/evaluations/pending               list subjects without active evaluation
    GET    /api/evaluations/recent                list active evaluations (newest first)
    POST   /api/evaluations/{id}/undo             soft-delete an evaluation

Subject polymorphism:
- subject_kind = 'case' → subject_id is cases.id
- subject_kind = 'render' → subject_id is render_jobs.id

For case evaluations we ALSO write a case_revisions row (op='evaluate' /
'undo_evaluate') so the case-level "近期变更" drawer surfaces them. For render
evaluations we don't, because case_revisions is keyed by case_id and a render
evaluation's subject is a job, not a case.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit, db

router = APIRouter(tags=["evaluations"])

ALLOWED_VERDICTS: set[str] = {"approved", "needs_recheck", "rejected"}
ALLOWED_KINDS: set[str] = {"case", "render"}


# ----------------------------------------------------------------------
# Pydantic
# ----------------------------------------------------------------------


class EvaluationCreate(BaseModel):
    subject_kind: Literal["case", "render"]
    subject_id: int
    verdict: Literal["approved", "needs_recheck", "rejected"]
    reviewer: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=2000)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"],
        "verdict": row["verdict"],
        "reviewer": row["reviewer"],
        "note": row["note"],
        "source_route": row["source_route"],
        "created_at": row["created_at"],
        "undone_at": row["undone_at"],
    }


def _validate_subject_exists(
    conn: sqlite3.Connection, subject_kind: str, subject_id: int
) -> None:
    if subject_kind == "case":
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ?", (subject_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"case {subject_id} not found")
    elif subject_kind == "render":
        row = conn.execute(
            "SELECT id, status FROM render_jobs WHERE id = ?", (subject_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"render job {subject_id} not found")
        if row["status"] not in {"done", "done_with_issues"}:
            raise HTTPException(
                400, f"can only evaluate completed render jobs (got {row['status']})"
            )


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------


@router.post("/api/evaluations")
def create_evaluation(payload: EvaluationCreate) -> dict[str, Any]:
    reviewer = payload.reviewer.strip()
    if not reviewer:
        raise HTTPException(400, "reviewer cannot be blank")
    note = payload.note.strip() if payload.note else None

    with db.connect() as conn:
        _validate_subject_exists(conn, payload.subject_kind, payload.subject_id)

        # Re-evaluation: any existing active evaluation for this subject is
        # auto-undone so a subject has at most one active row at any time.
        # Without this, "已评" listings would show multiple rows per subject and
        # the pending NOT-EXISTS query would diverge from "recent" semantics.
        conn.execute(
            """
            UPDATE evaluations
            SET undone_at = ?
            WHERE subject_kind = ?
              AND subject_id = ?
              AND undone_at IS NULL
            """,
            (_now_iso(), payload.subject_kind, payload.subject_id),
        )

        cur = conn.execute(
            """
            INSERT INTO evaluations
                (subject_kind, subject_id, verdict, reviewer, note, source_route, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.subject_kind,
                payload.subject_id,
                payload.verdict,
                reviewer,
                note,
                "/api/evaluations",
                _now_iso(),
            ),
        )
        eval_id = cur.lastrowid or 0

        # Case evaluations also surface in the per-case "近期变更" drawer via
        # case_revisions. Render evaluations don't (subject != case).
        if payload.subject_kind == "case":
            audit.record_revision(
                conn,
                payload.subject_id,
                op="evaluate",
                before={"evaluation_active": None},
                after={
                    "evaluation_active": {
                        "id": eval_id,
                        "verdict": payload.verdict,
                        "reviewer": reviewer,
                        "note": note,
                    }
                },
                source_route="/api/evaluations",
                actor="user",
            )

        row = conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eval_id,)
        ).fetchone()
    return _row_to_dict(row)


# ----------------------------------------------------------------------
# Single subject history
# ----------------------------------------------------------------------


@router.get("/api/evaluations")
def list_by_subject(
    subject_kind: str = Query(...),
    subject_id: int = Query(...),
    limit: int = Query(50, le=200),
) -> list[dict[str, Any]]:
    if subject_kind not in ALLOWED_KINDS:
        raise HTTPException(400, f"subject_kind must be one of {sorted(ALLOWED_KINDS)}")
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM evaluations
            WHERE subject_kind = ? AND subject_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (subject_kind, subject_id, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ----------------------------------------------------------------------
# Pending list (subjects WITHOUT an active evaluation)
# ----------------------------------------------------------------------


@router.get("/api/evaluations/pending")
def list_pending(
    subject_kind: str = Query(...),
    brand: str | None = Query(default=None),
    limit: int = Query(50, le=200),
) -> dict[str, Any]:
    if subject_kind not in ALLOWED_KINDS:
        raise HTTPException(400, f"subject_kind must be one of {sorted(ALLOWED_KINDS)}")

    with db.connect() as conn:
        if subject_kind == "case":
            rows = conn.execute(
                """
                SELECT c.id, c.abs_path, c.customer_raw, c.category, c.template_tier,
                       c.blocking_issues_json, c.review_status, c.indexed_at,
                       cust.canonical_name AS customer_name
                FROM cases c
                LEFT JOIN customers cust ON cust.id = c.customer_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM evaluations e
                    WHERE e.subject_kind = 'case'
                      AND e.subject_id = c.id
                      AND e.undone_at IS NULL
                )
                ORDER BY c.indexed_at DESC, c.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = [
                {
                    "subject_kind": "case",
                    "subject_id": r["id"],
                    "case_id": r["id"],
                    "abs_path": r["abs_path"],
                    "customer_raw": r["customer_raw"],
                    "customer_name": r["customer_name"],
                    "category": r["category"],
                    "template_tier": r["template_tier"],
                    "blocking_issues_json": r["blocking_issues_json"],
                    "review_status": r["review_status"],
                    "indexed_at": r["indexed_at"],
                }
                for r in rows
            ]
        else:  # render
            params: list[Any] = []
            brand_filter = ""
            if brand:
                brand_filter = " AND j.brand = ? "
                params.append(brand)
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT j.id, j.case_id, j.brand, j.template, j.output_path,
                       j.manifest_path, j.finished_at, j.meta_json,
                       c.abs_path, c.customer_raw, cust.canonical_name AS customer_name
                FROM render_jobs j
                JOIN cases c ON c.id = j.case_id
                LEFT JOIN customers cust ON cust.id = c.customer_id
                WHERE j.status IN ('done', 'done_with_issues')
                  {brand_filter}
                  AND NOT EXISTS (
                      SELECT 1 FROM evaluations e
                      WHERE e.subject_kind = 'render'
                        AND e.subject_id = j.id
                        AND e.undone_at IS NULL
                  )
                ORDER BY j.finished_at DESC, j.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            items = [
                {
                    "subject_kind": "render",
                    "subject_id": r["id"],
                    "case_id": r["case_id"],
                    "brand": r["brand"],
                    "template": r["template"],
                    "output_path": r["output_path"],
                    "manifest_path": r["manifest_path"],
                    "finished_at": r["finished_at"],
                    "meta_json": r["meta_json"],
                    "abs_path": r["abs_path"],
                    "customer_raw": r["customer_raw"],
                    "customer_name": r["customer_name"],
                }
                for r in rows
            ]

        # Total pending count (independent of limit) so UI can show "N 待评".
        if subject_kind == "case":
            count_row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM cases c
                WHERE NOT EXISTS (
                    SELECT 1 FROM evaluations e
                    WHERE e.subject_kind = 'case'
                      AND e.subject_id = c.id
                      AND e.undone_at IS NULL
                )
                """
            ).fetchone()
        else:
            count_params: list[Any] = []
            count_brand = ""
            if brand:
                count_brand = " AND j.brand = ? "
                count_params.append(brand)
            count_row = conn.execute(
                f"""
                SELECT COUNT(*) AS n FROM render_jobs j
                WHERE j.status IN ('done', 'done_with_issues')
                  {count_brand}
                  AND NOT EXISTS (
                      SELECT 1 FROM evaluations e
                      WHERE e.subject_kind = 'render'
                        AND e.subject_id = j.id
                        AND e.undone_at IS NULL
                  )
                """,
                tuple(count_params),
            ).fetchone()
        total = int(count_row["n"]) if count_row else 0

    return {"subject_kind": subject_kind, "total": total, "items": items}


# ----------------------------------------------------------------------
# Recent active evaluations (newest first)
# ----------------------------------------------------------------------


@router.get("/api/evaluations/recent")
def list_recent(
    subject_kind: str = Query(...),
    brand: str | None = Query(default=None),
    limit: int = Query(20, le=200),
) -> dict[str, Any]:
    if subject_kind not in ALLOWED_KINDS:
        raise HTTPException(400, f"subject_kind must be one of {sorted(ALLOWED_KINDS)}")

    with db.connect() as conn:
        if subject_kind == "case":
            rows = conn.execute(
                """
                SELECT e.*,
                       c.abs_path, c.customer_raw, c.category, c.template_tier,
                       cust.canonical_name AS customer_name
                FROM evaluations e
                JOIN cases c ON c.id = e.subject_id
                LEFT JOIN customers cust ON cust.id = c.customer_id
                WHERE e.subject_kind = 'case'
                  AND e.undone_at IS NULL
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            items = [
                {
                    **_row_to_dict(r),
                    "case_id": r["subject_id"],
                    "abs_path": r["abs_path"],
                    "customer_raw": r["customer_raw"],
                    "customer_name": r["customer_name"],
                    "category": r["category"],
                    "template_tier": r["template_tier"],
                }
                for r in rows
            ]
        else:
            params: list[Any] = []
            brand_filter = ""
            if brand:
                brand_filter = " AND j.brand = ? "
                params.append(brand)
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT e.*,
                       j.case_id, j.brand, j.template, j.output_path, j.finished_at,
                       c.abs_path, c.customer_raw, cust.canonical_name AS customer_name
                FROM evaluations e
                JOIN render_jobs j ON j.id = e.subject_id
                JOIN cases c ON c.id = j.case_id
                LEFT JOIN customers cust ON cust.id = c.customer_id
                WHERE e.subject_kind = 'render'
                  AND e.undone_at IS NULL
                  {brand_filter}
                ORDER BY e.created_at DESC, e.id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            items = [
                {
                    **_row_to_dict(r),
                    "case_id": r["case_id"],
                    "brand": r["brand"],
                    "template": r["template"],
                    "output_path": r["output_path"],
                    "finished_at": r["finished_at"],
                    "abs_path": r["abs_path"],
                    "customer_raw": r["customer_raw"],
                    "customer_name": r["customer_name"],
                }
                for r in rows
            ]
    return {"subject_kind": subject_kind, "items": items}


# ----------------------------------------------------------------------
# Undo
# ----------------------------------------------------------------------


@router.post("/api/evaluations/{eval_id}/undo")
def undo_evaluation(eval_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eval_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "evaluation not found")
        if row["undone_at"] is not None:
            raise HTTPException(409, "evaluation already undone")

        conn.execute(
            "UPDATE evaluations SET undone_at = ? WHERE id = ?",
            (_now_iso(), eval_id),
        )

        if row["subject_kind"] == "case":
            audit.record_revision(
                conn,
                row["subject_id"],
                op="undo_evaluate",
                before={
                    "evaluation_active": {
                        "id": row["id"],
                        "verdict": row["verdict"],
                        "reviewer": row["reviewer"],
                        "note": row["note"],
                    }
                },
                after={"evaluation_active": None},
                source_route=f"/api/evaluations/{eval_id}/undo",
                actor="user",
            )

        new_row = conn.execute(
            "SELECT * FROM evaluations WHERE id = ?", (eval_id,)
        ).fetchone()
    return _row_to_dict(new_row)
