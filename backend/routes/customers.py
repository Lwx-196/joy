"""Customer endpoints: list, detail, create, update, merge."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import audit, customer_resolver, db
from ..models import (
    CaseSummary,
    CustomerCreate,
    CustomerDetail,
    CustomerMerge,
    CustomerSummary,
    CustomerUpdate,
)
from .cases import _row_to_summary

router = APIRouter(prefix="/api/customers", tags=["customers"])


def _summary_from_row(row, case_count: int) -> CustomerSummary:
    return CustomerSummary(
        id=row["id"],
        canonical_name=row["canonical_name"],
        aliases=json.loads(row["aliases_json"] or "[]"),
        notes=row["notes"],
        case_count=case_count,
    )


@router.get("", response_model=list[CustomerSummary])
def list_customers(q: str | None = None) -> list[CustomerSummary]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM customers ORDER BY canonical_name").fetchall()
        counts = {r["customer_id"]: r["n"] for r in conn.execute(
            "SELECT customer_id, COUNT(*) AS n FROM cases "
            "WHERE customer_id IS NOT NULL AND trashed_at IS NULL GROUP BY customer_id"
        ).fetchall()}
    out: list[CustomerSummary] = []
    for r in rows:
        if q and q not in r["canonical_name"]:
            aliases = json.loads(r["aliases_json"] or "[]")
            if not any(q in a for a in aliases):
                continue
        out.append(_summary_from_row(r, counts.get(r["id"], 0)))
    return out


@router.get("/candidates")
def candidates(raw: str = Query(..., min_length=1)) -> dict:
    """Resolve a raw customer name to candidates (no auto-merge)."""
    with db.connect() as conn:
        result = customer_resolver.resolve(raw, conn)
    return {
        "raw": result.raw,
        "normalized": result.normalized,
        "decision": result.decision,
        "suggestion": result.suggestion,
        "candidates": result.candidates,
    }


@router.get("/{customer_id}", response_model=CustomerDetail)
def customer_detail(customer_id: int) -> CustomerDetail:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            raise HTTPException(404, "customer not found")
        case_rows = conn.execute(
            """SELECT c.*, cu.canonical_name AS canonical_name FROM cases c
               LEFT JOIN customers cu ON cu.id = c.customer_id
               WHERE c.customer_id = ? AND c.trashed_at IS NULL ORDER BY c.last_modified DESC""",
            (customer_id,),
        ).fetchall()

    cases = [_row_to_summary(r, r["canonical_name"]) for r in case_rows]
    summary = _summary_from_row(row, len(cases))
    return CustomerDetail(**summary.model_dump(), cases=cases)


@router.post("", response_model=CustomerSummary)
def create_customer(payload: CustomerCreate) -> CustomerSummary:
    name = payload.canonical_name.strip()
    if not name:
        raise HTTPException(400, "canonical_name required")
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM customers WHERE canonical_name = ?", (name,)).fetchone()
        if existing:
            raise HTTPException(409, "canonical_name already exists")
        new_id = customer_resolver.create_customer(conn, name, payload.aliases, payload.notes)
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (new_id,)).fetchone()
    return _summary_from_row(row, 0)


@router.patch("/{customer_id}", response_model=CustomerSummary)
def update_customer(customer_id: int, payload: CustomerUpdate) -> CustomerSummary:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            raise HTTPException(404, "customer not found")
        customer_resolver.update_customer(
            conn,
            customer_id,
            canonical_name=payload.canonical_name,
            aliases=payload.aliases,
            notes=payload.notes,
        )
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        case_count = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE customer_id = ? AND trashed_at IS NULL", (customer_id,)
        ).fetchone()[0]
    return _summary_from_row(row, case_count)


@router.post("/{customer_id}/merge")
def merge_cases(customer_id: int, payload: CustomerMerge) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            raise HTTPException(404, "customer not found")
        # Audit: snapshot the cases that will be re-bound, then merge.
        befores = audit.snapshot_before(conn, payload.case_ids)
        moved = customer_resolver.merge_cases_to_customer(conn, customer_id, payload.case_ids)
        audit.record_after(
            conn,
            payload.case_ids,
            befores,
            op="merge_customer",
            source_route=f"/api/customers/{customer_id}/merge",
        )
    return {"customer_id": customer_id, "moved": moved}
