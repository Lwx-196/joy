"""Core v3 upgrade execution — shared by the sync /upgrade endpoint and the
async upgrade_queue worker.

Workflow (mirrors the original routes/cases.py:295-363 logic):
1. Look up the case row and snapshot tracked columns BEFORE upgrade (audit).
2. Run skill_bridge.upgrade_case_to_v3 (mediapipe-backed, 5-30s).
3. Merge meta_extras into cases.meta_json (preserving scanner-set fields).
4. UPDATE tracked columns + indexed_at.
5. Write audit op='upgrade' so apply_undo can roll back via the standard path.

Exception contract: callers map the raised exception to their environment.
- ValueError              → case row not found
- FileNotFoundError       → case directory missing on disk
- RuntimeError            → skill unavailable / load failure
- other Exception         → skill execution failure
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from . import audit, db, skill_bridge


def execute_upgrade(
    case_id: int,
    brand: str,
    source_route: str | None = None,
) -> dict[str, Any]:
    """Run v3 upgrade for a single case. Returns a summary dict suitable for
    upgrade_jobs.meta_json (category / template_tier / counts / skill_status).

    Performs the heavy subprocess inside the same DB connection used for the
    snapshot+update+audit so the original event ordering is preserved. SQLite
    only takes a write lock for the final UPDATE/INSERT, so holding the
    connection during the subprocess does not block other readers/writers.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, abs_path FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"case {case_id} not found")
        case_dir = row["abs_path"]
        befores = audit.snapshot_before(conn, [case_id])

        # Heavy mediapipe subprocess (5-30s). Errors propagate to the caller
        # untouched so they can map to HTTPException / job failure.
        payload = skill_bridge.upgrade_case_to_v3(case_dir, brand=brand)

        existing_meta_row = conn.execute(
            "SELECT meta_json FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        try:
            existing_meta = json.loads(existing_meta_row["meta_json"] or "{}")
        except (TypeError, ValueError):
            existing_meta = {}
        merged_meta = {**existing_meta, **payload["meta_extras"]}

        conn.execute(
            """UPDATE cases SET
                category = ?,
                template_tier = ?,
                blocking_issues_json = ?,
                pose_delta_max = ?,
                sharp_ratio_min = ?,
                meta_json = ?,
                skill_image_metadata_json = ?,
                skill_blocking_detail_json = ?,
                skill_warnings_json = ?,
                indexed_at = ?
               WHERE id = ?""",
            (
                payload["category"],
                payload["template_tier"],
                payload["blocking_issues_json"],
                payload["pose_delta_max"],
                payload["sharp_ratio_min"],
                json.dumps(merged_meta, ensure_ascii=False),
                payload.get("skill_image_metadata_json"),
                payload.get("skill_blocking_detail_json"),
                payload.get("skill_warnings_json"),
                datetime.now(timezone.utc).isoformat(),
                case_id,
            ),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="upgrade",
            source_route=source_route or f"/api/cases/{case_id}/upgrade",
            actor="skill_upgrade",
        )

    extras = payload.get("meta_extras") or {}
    return {
        "category": payload.get("category"),
        "template_tier": payload.get("template_tier"),
        "blocking_count": extras.get("skill_blocking_issue_count"),
        "warning_count": extras.get("skill_warning_count"),
        "skill_status": extras.get("skill_status"),
        "case_mode": extras.get("skill_case_mode"),
        "skill_template": extras.get("skill_template"),
    }
