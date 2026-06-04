"""T74: unified review ticket queue."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from backend import db


def _insert_ticket(
    *,
    case_id: int,
    ticket_type: str,
    reason_code: str,
    blocks_render: bool,
    blocks_publish: bool,
    evidence: dict,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        return conn.execute(
            """
            INSERT INTO review_tickets
              (case_id, ticket_type, stage, status, blocks_render, blocks_publish,
               reason_code, message, evidence_json, decision_json, created_at, updated_at)
            VALUES (?, ?, 'pre_render_gate', 'open', ?, ?, ?, 'ticket', ?, '{}', ?, ?)
            """,
            (
                case_id,
                ticket_type,
                1 if blocks_render else 0,
                1 if blocks_publish else 0,
                reason_code,
                json.dumps(evidence, ensure_ascii=False),
                now,
                now,
            ),
        ).lastrowid


def test_review_ticket_query_and_decision_writes_trace_only(client, seed_case):
    case_id = seed_case(abs_path="/tmp/t74-ticket-trace")
    ticket_id = _insert_ticket(
        case_id=case_id,
        ticket_type="reselect_pair",
        reason_code="pose_delta_large",
        blocks_render=True,
        blocks_publish=True,
        evidence={"slot": "front", "recommended_action": "reselect_pair"},
    )

    listing = client.get("/api/review-tickets", params={"status": "open", "case_id": case_id})

    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == ticket_id

    decision = client.post(
        f"/api/review-tickets/{ticket_id}/decision",
        json={"decision": "reselect_pair", "reviewer": "qa", "note": "正面重选片"},
    )

    assert decision.status_code == 200, decision.text
    with db.connect() as conn:
        row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
        ticket = conn.execute("SELECT status, decision_json FROM review_tickets WHERE id = ?", (ticket_id,)).fetchone()
    trace = json.loads(row["meta_json"])["source_group_selection"]["ticket_decisions"][0]
    assert trace["ticket_id"] == ticket_id
    assert trace["decision"] == "reselect_pair"
    assert ticket["status"] == "resolved"
    assert json.loads(ticket["decision_json"])["reviewer"] == "qa"


def test_accept_template_downgrade_only_accepts_warning_not_hard_blocker(client, seed_case):
    case_id = seed_case(abs_path="/tmp/t74-accept-template-downgrade")
    warning_ticket = _insert_ticket(
        case_id=case_id,
        ticket_type="accept_template_downgrade",
        reason_code="template_downgrade",
        blocks_render=False,
        blocks_publish=True,
        evidence={"slot": "side", "code": "template_downgrade", "selected_files": ["before-side.jpg", "after-side.jpg"]},
    )
    hard_ticket = _insert_ticket(
        case_id=case_id,
        ticket_type="source_quality_review",
        reason_code="crop_touches_frame",
        blocks_render=True,
        blocks_publish=True,
        evidence={"slot": "front", "code": "crop_touches_frame"},
    )

    ok = client.post(
        f"/api/review-tickets/{warning_ticket}/decision",
        json={"decision": "accept_template_downgrade", "reviewer": "qa", "note": "侧面降级可接受"},
    )
    blocked = client.post(
        f"/api/review-tickets/{hard_ticket}/decision",
        json={"decision": "accept_template_downgrade", "reviewer": "qa"},
    )

    assert ok.status_code == 200, ok.text
    assert blocked.status_code == 400, blocked.text
    with db.connect() as conn:
        meta = json.loads(conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()["meta_json"])
        hard = conn.execute("SELECT status FROM review_tickets WHERE id = ?", (hard_ticket,)).fetchone()
    accepted = meta["source_group_selection"]["accepted_warnings"][0]
    assert accepted["slot"] == "side"
    assert accepted["code"] == "template_downgrade"
    assert hard["status"] == "open"
