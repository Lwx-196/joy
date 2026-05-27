"""Review ticket persistence and decision helpers."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import audit, db as _db
from .. import source_selection

TICKET_TYPES = {
    "slot_fill",
    "reselect_pair",
    "accept_template_downgrade",
    "manual_quality_review",
    "identity_review",
    "source_quality_review",
}
OPEN_STATUSES = {"open"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dedupe_key(
    *,
    case_id: int,
    render_job_id: int | None,
    ticket_type: str,
    stage: str,
    reason_code: str,
    slot: str | None,
    source_filename: str | None,
) -> str:
    payload = {
        "case_id": int(case_id),
        "render_job_id": int(render_job_id or 0),
        "ticket_type": ticket_type,
        "stage": stage,
        "reason_code": reason_code,
        "slot": slot or "",
        "source_filename": source_filename or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def ticket_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "render_job_id": row["render_job_id"],
        "ticket_type": row["ticket_type"],
        "stage": row["stage"],
        "status": row["status"],
        "blocks_render": bool(row["blocks_render"]),
        "blocks_publish": bool(row["blocks_publish"]),
        "reason_code": row["reason_code"],
        "slot": row["slot"],
        "source_filename": row["source_filename"],
        "message": row["message"],
        "evidence": _json_dict(row["evidence_json"]),
        "decision": _json_dict(row["decision_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "resolved_at": row["resolved_at"],
    }


def upsert_open_ticket(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    ticket_type: str,
    stage: str,
    reason_code: str,
    message: str,
    evidence: dict[str, Any],
    blocks_render: bool,
    blocks_publish: bool,
    render_job_id: int | None = None,
    slot: str | None = None,
    source_filename: str | None = None,
) -> dict[str, Any]:
    if ticket_type not in TICKET_TYPES:
        raise ValueError(f"unsupported ticket_type: {ticket_type}")
    key = _dedupe_key(
        case_id=case_id,
        render_job_id=render_job_id,
        ticket_type=ticket_type,
        stage=stage,
        reason_code=reason_code,
        slot=slot,
        source_filename=source_filename,
    )
    existing = conn.execute(
        """
        SELECT *
        FROM review_tickets
        WHERE dedupe_key = ? AND status = 'open'
        ORDER BY id DESC
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    now = now_iso()
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    if existing:
        conn.execute(
            """
            UPDATE review_tickets
            SET message = ?,
                evidence_json = ?,
                blocks_render = ?,
                blocks_publish = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (message, evidence_json, 1 if blocks_render else 0, 1 if blocks_publish else 0, now, existing["id"]),
        )
        row = conn.execute("SELECT * FROM review_tickets WHERE id = ?", (existing["id"],)).fetchone()
        return ticket_row_to_dict(row)
    cur = conn.execute(
        """
        INSERT INTO review_tickets
          (case_id, render_job_id, ticket_type, stage, status, blocks_render, blocks_publish,
           reason_code, slot, source_filename, message, evidence_json, decision_json,
           dedupe_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)
        """,
        (
            case_id,
            render_job_id,
            ticket_type,
            stage,
            1 if blocks_render else 0,
            1 if blocks_publish else 0,
            reason_code,
            slot,
            source_filename,
            message,
            evidence_json,
            key,
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM review_tickets WHERE id = ?", (cur.lastrowid,)).fetchone()
    return ticket_row_to_dict(row)


def list_tickets(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    ticket_type: str | None = None,
    case_id: int | None = None,
    render_job_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if ticket_type:
        where.append("ticket_type = ?")
        params.append(ticket_type)
    if case_id is not None:
        where.append("case_id = ?")
        params.append(case_id)
    if render_job_id is not None:
        where.append("render_job_id = ?")
        params.append(render_job_id)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM review_tickets {sql_where}", params).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT *
        FROM review_tickets
        {sql_where}
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return {"items": [ticket_row_to_dict(row) for row in rows], "total": int(total), "limit": limit}


def get_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM review_tickets WHERE id = ?", (ticket_id,)).fetchone()


def _case_meta(conn: sqlite3.Connection, case_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT meta_json FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)).fetchone()
    if not row:
        raise ValueError("case not found")
    return _json_dict(row["meta_json"])


def _write_case_meta(conn: sqlite3.Connection, case_id: int, meta: dict[str, Any], *, op: str) -> None:
    before = audit.snapshot_before(conn, [case_id])
    conn.execute(
        "UPDATE cases SET meta_json = ? WHERE id = ?",
        (json.dumps(meta, ensure_ascii=False), case_id),
    )
    audit.record_after(
        conn,
        [case_id],
        before,
        op=op,
        source_route=f"/api/review-tickets/{op}",
    )


def _controls(meta: dict[str, Any]) -> dict[str, Any]:
    controls = source_selection.selection_controls_from_meta(meta)
    controls.setdefault("locked_slots", {})
    controls.setdefault("accepted_warnings", [])
    controls.setdefault("ticket_decisions", [])
    return controls


def apply_ticket_decision(
    conn: sqlite3.Connection,
    *,
    ticket_id: int,
    decision: str,
    reviewer: str,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = get_ticket(conn, ticket_id)
    if not row:
        raise ValueError("ticket not found")
    if row["status"] != "open":
        raise ValueError("ticket is not open")
    if not reviewer.strip():
        raise ValueError("reviewer cannot be blank")
    evidence = _json_dict(row["evidence_json"])
    payload = payload if isinstance(payload, dict) else {}
    now = now_iso()
    case_id = int(row["case_id"])
    meta = _case_meta(conn, case_id)
    controls = _controls(meta)

    if decision == "accept_template_downgrade":
        if row["ticket_type"] != "accept_template_downgrade" or int(row["blocks_render"] or 0):
            raise ValueError("accept_template_downgrade cannot resolve hard blockers")
        slot = str(payload.get("slot") or evidence.get("slot") or "").strip()
        code = str(payload.get("code") or evidence.get("code") or row["reason_code"] or "").strip()
        if slot not in source_selection.SLOTS or not code:
            raise ValueError("accept_template_downgrade requires slot and code")
        selected_files = [str(item) for item in (payload.get("selected_files") or evidence.get("selected_files") or []) if item]
        acceptance = {
            "job_id": row["render_job_id"],
            "slot": slot,
            "code": code,
            "message_contains": str(payload.get("message_contains") or evidence.get("message_contains") or ""),
            "reviewer": reviewer.strip(),
            "note": note.strip() if note else None,
            "accepted_at": now,
        }
        if selected_files:
            acceptance["selected_files"] = list(dict.fromkeys(selected_files))
        existing = [
            item
            for item in controls.get("accepted_warnings", [])
            if not (
                item.get("slot") == acceptance["slot"]
                and item.get("code") == acceptance["code"]
                and str(item.get("message_contains") or "") == acceptance["message_contains"]
            )
        ]
        existing.append(acceptance)
        controls["accepted_warnings"] = existing
    elif decision in {"reselect_pair", "slot_fill", "identity_review", "source_quality_review", "manual_quality_review"}:
        trace = {
            "ticket_id": ticket_id,
            "decision": decision,
            "reviewer": reviewer.strip(),
            "note": note.strip() if note else None,
            "ticket_type": row["ticket_type"],
            "reason_code": row["reason_code"],
            "stage": row["stage"],
            "evidence": evidence,
            "decided_at": now,
        }
        controls["ticket_decisions"] = [*controls.get("ticket_decisions", []), trace]
    else:
        raise ValueError(f"unsupported decision: {decision}")

    meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
    _write_case_meta(conn, case_id, meta, op=f"review_ticket_{decision}")

    decision_payload = {
        "decision": decision,
        "reviewer": reviewer.strip(),
        "note": note.strip() if note else None,
        "payload": payload,
        "decided_at": now,
    }
    conn.execute(
        """
        UPDATE review_tickets
        SET status = 'resolved',
            decision_json = ?,
            updated_at = ?,
            resolved_at = ?
        WHERE id = ?
        """,
        (json.dumps(decision_payload, ensure_ascii=False), now, now, ticket_id),
    )
    updated = get_ticket(conn, ticket_id)
    return ticket_row_to_dict(updated)


def connect() -> sqlite3.Connection:
    return _db.get_conn()
