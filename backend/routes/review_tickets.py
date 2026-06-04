"""Unified review ticket queue endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db
from ..services import review_ticket_service as tickets

router = APIRouter(tags=["review-tickets"])


class ReviewTicketDecisionRequest(BaseModel):
    decision: str = Field(..., min_length=1, max_length=64)
    reviewer: str = Field(..., min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    payload: dict[str, Any] | None = None


@router.get("/api/review-tickets")
def list_review_tickets(
    status: str | None = Query(default=None),
    ticket_type: str | None = Query(default=None),
    case_id: int | None = Query(default=None),
    render_job_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    with db.connect() as conn:
        return tickets.list_tickets(
            conn,
            status=status,
            ticket_type=ticket_type,
            case_id=case_id,
            render_job_id=render_job_id,
            limit=limit,
        )


@router.post("/api/review-tickets/{ticket_id}/decision")
def decide_review_ticket(ticket_id: int, payload: ReviewTicketDecisionRequest) -> dict[str, Any]:
    try:
        with db.connect() as conn:
            return tickets.apply_ticket_decision(
                conn,
                ticket_id=ticket_id,
                decision=payload.decision.strip(),
                reviewer=payload.reviewer.strip(),
                note=payload.note,
                payload=payload.payload,
            )
    except ValueError as exc:
        message = str(exc)
        if message == "ticket not found":
            raise HTTPException(404, message)
        raise HTTPException(400, message)
