from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_group(conn, *, case_id: int, root_path: str = "/tmp/case-root") -> int:
    now = _now()
    return conn.execute(
        """
        INSERT INTO case_groups
          (group_key, primary_case_id, customer_raw, title, root_path,
           case_ids_json, status, diagnosis_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"group-{case_id}",
            case_id,
            "Alice",
            "Alice case",
            root_path,
            json.dumps([case_id]),
            "auto",
            "{}",
            now,
            now,
        ),
    ).lastrowid


def _seed_observation(
    conn,
    *,
    group_id: int,
    case_id: int,
    image_path: str,
    phase: str = "before",
    view: str = "front",
    confidence: float = 0.92,
) -> int:
    now = _now()
    return conn.execute(
        """
        INSERT INTO image_observations
          (group_id, case_id, image_path, phase, body_part, view, quality_json,
           confidence, source, reasons_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            group_id,
            case_id,
            image_path,
            phase,
            "face",
            view,
            "{}",
            confidence,
            "skill_v3",
            "[]",
            now,
            now,
        ),
    ).lastrowid


def _seed_override(
    conn,
    *,
    case_id: int,
    filename: str,
    manual_phase: str | None = None,
    manual_view: str | None = None,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO case_image_overrides
          (case_id, filename, manual_phase, manual_view, reason_json, reviewer, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            filename,
            manual_phase,
            manual_view,
            json.dumps({"source": "operator_audit"}),
            "operator",
            now,
        ),
    )


def test_annotation_audit_queue_flags_confidence_below_half(temp_db, seed_case):
    from backend import db
    from backend.services.annotation_audit import build_annotation_audit_queue

    case_id = seed_case()
    with db.connect() as conn:
        group_id = _seed_group(conn, case_id=case_id)
        low_id = _seed_observation(
            conn,
            group_id=group_id,
            case_id=case_id,
            image_path="/tmp/case-root/low-front.jpg",
            confidence=0.49,
        )
        _seed_observation(
            conn,
            group_id=group_id,
            case_id=case_id,
            image_path="/tmp/case-root/high-front.jpg",
            confidence=0.5,
        )

        result = build_annotation_audit_queue(conn)

    assert result["summary"]["deterministic"] is True
    assert result["summary"]["low_confidence_count"] == 1
    assert result["summary"]["queued_count"] == 1
    assert result["items"][0]["observation_id"] == low_id
    assert result["items"][0]["reasons"] == ["low_confidence"]


def test_annotation_audit_queue_flags_label_mismatch_from_overrides(temp_db, seed_case):
    from backend import db
    from backend.services.annotation_audit import build_annotation_audit_queue

    case_dir = Path("/tmp/case125")
    case_id = seed_case(abs_path=str(case_dir), customer_raw="case125")
    with db.connect() as conn:
        group_id = _seed_group(conn, case_id=case_id, root_path=str(case_dir))
        observation_id = _seed_observation(
            conn,
            group_id=group_id,
            case_id=case_id,
            image_path=str(case_dir / "front-labeled.jpg"),
            phase="before",
            view="front",
            confidence=0.96,
        )
        _seed_override(
            conn,
            case_id=case_id,
            filename="front-labeled.jpg",
            manual_phase="after",
            manual_view="side",
        )

        result = build_annotation_audit_queue(conn)

    assert result["summary"]["label_mismatch_count"] == 1
    assert result["summary"]["manual_feedback_count"] == 1
    assert result["summary"]["mismatch_field_counts"] == {"phase": 1, "view": 1}
    item = result["items"][0]
    assert item["observation_id"] == observation_id
    assert item["reasons"] == ["label_mismatch"]
    assert item["mismatch_fields"] == ["phase", "view"]
    assert item["manual_feedback_fields"] == ["phase", "view"]
    assert item["recommended_patch"] == {
        "manual_phase": "after",
        "manual_view": "side",
    }
    assert item["manual_override"]["reviewer"] == "operator"


def test_annotation_audit_queue_keeps_matching_override_as_feedback(temp_db, seed_case):
    from backend import db
    from backend.services.annotation_audit import build_annotation_audit_queue

    case_id = seed_case()
    with db.connect() as conn:
        group_id = _seed_group(conn, case_id=case_id)
        _seed_observation(
            conn,
            group_id=group_id,
            case_id=case_id,
            image_path="/tmp/case-root/matched.jpg",
            phase="before",
            view="front",
            confidence=0.95,
        )
        _seed_override(
            conn,
            case_id=case_id,
            filename="matched.jpg",
            manual_phase="before",
            manual_view="front",
        )

        result = build_annotation_audit_queue(conn)

    assert result["summary"]["label_mismatch_count"] == 0
    assert result["summary"]["manual_feedback_count"] == 1
    assert result["summary"]["queued_count"] == 1
    assert result["items"][0]["reasons"] == ["manual_feedback"]
    assert result["items"][0]["mismatch_fields"] == []
    assert result["items"][0]["manual_feedback_fields"] == ["phase", "view"]
