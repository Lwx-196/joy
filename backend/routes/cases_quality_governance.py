"""Simulation and render quality governance endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("/simulation-jobs/quality-queue")
def list_simulation_quality_queue(
    status: str = Query("review_required"),
    recommendation: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    """Central queue for AI after-image simulation QA.

    This is intentionally separate from render_quality: AI-enhanced artifacts
    are never treated as real case renders, even when shown in the same QA UI.
    """
    status = status.strip() or "review_required"
    where_sql, params = _simulation_queue_condition(status)
    recommendation = (recommendation or "").strip() or None
    allowed_recommendations = {"approved", "needs_recheck", "rejected", "manual_override", "aligned"}
    if recommendation and recommendation not in allowed_recommendations:
        raise HTTPException(400, f"recommendation must be one of {sorted(allowed_recommendations)}")
    scan_limit = 2000 if recommendation else limit
    with db.connect() as conn:
        total = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            WHERE {where_sql}
            """,
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT
              s.*,
              c.abs_path AS case_abs_path,
              c.customer_raw AS case_customer_raw,
              cu.canonical_name AS case_customer_canonical
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            LEFT JOIN customers cu ON cu.id = c.customer_id
            WHERE {where_sql}
            ORDER BY
              CASE
                WHEN s.status = 'failed' THEN 0
                WHEN s.status = 'done_with_issues' THEN 1
                ELSE 2
              END,
              s.updated_at DESC,
              s.id DESC
            LIMIT ?
            """,
            [*params, scan_limit],
        ).fetchall()
        counts: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT s.status AS status, COUNT(*) AS n
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            WHERE (c.id IS NULL OR c.trashed_at IS NULL)
              AND s.status IN ('done', 'done_with_issues', 'failed')
            GROUP BY s.status
            """
        ).fetchall():
            counts[str(row["status"])] = int(row["n"])
        for row in conn.execute(
            """
            SELECT s.review_status AS status, COUNT(*) AS n
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            WHERE (c.id IS NULL OR c.trashed_at IS NULL)
              AND s.review_status IS NOT NULL
            GROUP BY s.review_status
            """
        ).fetchall():
            counts[str(row["status"])] = int(row["n"])
        counts["reviewed"] = sum(
            counts.get(key, 0) for key in ("approved", "needs_recheck", "rejected")
        )

    items: list[dict[str, Any]] = []
    matched_total = 0
    for row in rows:
        job = _simulation_row_to_model(row)
        decision = job.review_decision if isinstance(job.review_decision, dict) else {}
        recommended = str(decision.get("recommended_verdict") or "unknown")
        if recommendation == "manual_override":
            if not job.review_status or job.review_status == recommended:
                continue
        elif recommendation == "aligned":
            if not job.review_status or job.review_status != recommended:
                continue
        elif recommendation and recommended != recommendation:
            continue
        matched_total += 1
        if len(items) >= limit:
            continue
        issues, warnings = _simulation_issue_summary(job)
        case_id = row["case_id"]
        items.append(
            {
                "job": job.model_dump(),
                "case": (
                    {
                        "id": case_id,
                        "abs_path": row["case_abs_path"],
                        "customer_raw": row["case_customer_raw"],
                        "customer_canonical": row["case_customer_canonical"],
                    }
                    if case_id is not None
                    else None
                ),
                "reviewable": job.status in {"done", "done_with_issues"},
                "issue_summary": issues,
                "warning_summary": warnings,
            }
        )
    if recommendation:
        total = matched_total
    return {
        "items": items,
        "total": total,
        "counts": counts,
        "status": status,
        "recommendation": recommendation,
        "limit": limit,
    }


@router.get("/simulation-jobs/review-policy")
def get_simulation_review_policy() -> dict[str, Any]:
    return simulation_quality.load_ai_review_policy()


@router.put("/simulation-jobs/review-policy")
def put_simulation_review_policy(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return simulation_quality.save_ai_review_policy(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/simulation-jobs/review-policy/preview")
def preview_simulation_review_policy(
    payload: dict[str, Any],
    limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    try:
        simulation_quality.preview_ai_review_policy(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db.connect() as conn:
        return _simulation_policy_preview(conn, payload, limit)


@router.get("/quality-report")
def quality_report(limit: int = Query(300, ge=1, le=2000)) -> dict[str, Any]:
    with db.connect() as conn:
        return _quality_report(conn, limit)


@router.get("/quality-report/publishable-items")
def quality_report_publishable_items(
    limit: int = Query(100, ge=1, le=2000)
) -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT
              j.id,
              j.case_id,
              j.status,
              j.brand,
              j.template,
              j.enqueued_at,
              j.finished_at,
              j.output_path,
              j.manifest_path,
              j.error_message,
              j.semantic_judge,
              j.render_mode,
              j.best_pair_selection_id,
              j.candidates_fingerprint_snapshot,
              c.abs_path,
              c.customer_raw,
              cu.canonical_name AS customer_canonical,
              rq.quality_status,
              rq.quality_score,
              rq.can_publish,
              rq.review_verdict,
              rq.reviewer,
              rq.review_note,
              rq.reviewed_at,
              rq.metrics_json,
              rq.manifest_status,
              rq.blocking_count,
              rq.warning_count,
              rq.artifact_mode
            FROM render_jobs j
            JOIN cases c ON c.id = j.case_id
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE c.trashed_at IS NULL
              AND j.status IN ('done', 'done_with_issues', 'blocked', 'failed')
              AND rq.can_publish = 1
            ORDER BY COALESCE(j.finished_at, j.enqueued_at) DESC, j.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        items = []
        unique_cases = set()
        for row in rows:
            case_id = row["case_id"]
            unique_cases.add(case_id)

            quality_dict = None
            if row["quality_status"] is not None:
                quality_dict = {
                    "id": row["id"],
                    "render_job_id": row["id"],
                    "quality_status": row["quality_status"],
                    "quality_score": row["quality_score"],
                    "can_publish": bool(row["can_publish"]),
                    "manifest_status": row["manifest_status"],
                    "blocking_count": row["blocking_count"],
                    "warning_count": row["warning_count"],
                    "metrics": json.loads(row["metrics_json"] or "{}"),
                    "review_verdict": row["review_verdict"],
                    "reviewer": row["reviewer"],
                    "review_note": row["review_note"],
                    "reviewed_at": row["reviewed_at"],
                    "artifact_mode": row["artifact_mode"] or "real_layout",
                }

            job_dict = {
                "id": row["id"],
                "case_id": case_id,
                "brand": row["brand"],
                "template": row["template"],
                "status": row["status"],
                "enqueued_at": row["enqueued_at"],
                "finished_at": row["finished_at"],
                "output_path": row["output_path"],
                "manifest_path": row["manifest_path"],
                "error_message": row["error_message"],
                "semantic_judge": row["semantic_judge"],
                "render_mode": row["render_mode"] or "ai",
                "best_pair_selection_id": row["best_pair_selection_id"],
                "candidates_fingerprint_snapshot": row["candidates_fingerprint_snapshot"],
                "quality": quality_dict,
            }

            delivery_envelope = {
                "class": "formal_ready" if row["status"] == "done" else "experimental_blocked",
                "can_deliver": row["status"] == "done",
                "source": "render_quality",
                "gate_status": "ready_to_publish" if row["status"] == "done" else "blocked",
                "reasons": [] if row["status"] == "done" else ["job_status_not_done"],
            }

            items.append({
                "kind": "render",
                "delivery_envelope": delivery_envelope,
                "sort_at": row["finished_at"] or row["enqueued_at"],
                "job": job_dict,
                "case": {
                    "id": case_id,
                    "abs_path": row["abs_path"],
                    "customer_raw": row["customer_raw"],
                    "customer_canonical": row["customer_canonical"],
                }
            })

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": "formal_publishable_v1",
            "limit": limit,
            "total": len(items),
            "unique_case_count": len(unique_cases),
            "counts": {
                "render": len(items),
                "simulation": 0,
            },
            "items": items,
        }


@router.get("/simulation-jobs/legacy-publishable-risk")
def get_simulation_legacy_publishable_risk(
    limit: int = Query(200, ge=1, le=1000)
) -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT
              s.*,
              c.abs_path AS case_abs_path,
              c.customer_raw AS case_customer_raw,
              cu.canonical_name AS case_customer_canonical
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            LEFT JOIN customers cu ON cu.id = c.customer_id
            WHERE (c.id IS NULL OR c.trashed_at IS NULL)
              AND s.status IN ('done', 'done_with_issues')
              AND s.can_publish = 1
            ORDER BY s.updated_at DESC, s.id DESC
            """
        ).fetchall()

        policy = simulation_quality.load_ai_review_policy()

        affected_items = []
        affected_ids = []
        for row in rows:
            job = _simulation_row_to_model(row, policy)
            if not job.effective_can_publish:
                # This job is at risk!
                affected_ids.append(job.id)
                case_id = row["case_id"]
                affected_items.append({
                    "kind": "simulation",
                    "risk_status": "legacy_publishable_risk",
                    "job": job,
                    "case": (
                        {
                            "id": case_id,
                            "abs_path": row["case_abs_path"],
                            "customer_raw": row["case_customer_raw"],
                            "customer_canonical": row["case_customer_canonical"],
                        }
                        if case_id is not None
                        else None
                    )
                })

        # Handle limit
        truncated_items = affected_items[:limit]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": "legacy_simulation_publishable_risk_v1",
            "limit": limit,
            "affected_count": len(affected_items),
            "affected_job_ids": affected_ids,
            "items": truncated_items,
        }


@router.post("/simulation-jobs/legacy-publishable-risk/quarantine")
def quarantine_legacy_simulation_publishable_risk(
    payload: LegacySimulationQuarantineRequest,
) -> dict[str, Any]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT
              s.*,
              c.abs_path AS case_abs_path,
              c.customer_raw AS case_customer_raw,
              cu.canonical_name AS case_customer_canonical
            FROM simulation_jobs s
            LEFT JOIN cases c ON c.id = s.case_id
            LEFT JOIN customers cu ON cu.id = c.customer_id
            WHERE (c.id IS NULL OR c.trashed_at IS NULL)
              AND s.status IN ('done', 'done_with_issues')
              AND s.can_publish = 1
            ORDER BY s.updated_at DESC, s.id DESC
            """
        ).fetchall()

        policy = simulation_quality.load_ai_review_policy()

        affected_items = []
        affected_ids = []
        for row in rows:
            job = _simulation_row_to_model(row, policy)
            if not job.effective_can_publish:
                # This job is at risk!
                affected_ids.append(job.id)
                case_id = row["case_id"]
                affected_items.append({
                    "kind": "simulation",
                    "risk_status": "legacy_publishable_risk",
                    "job": job,
                    "case": (
                        {
                            "id": case_id,
                            "abs_path": row["case_abs_path"],
                            "customer_raw": row["case_customer_raw"],
                            "customer_canonical": row["case_customer_canonical"],
                        }
                        if case_id is not None
                        else None
                    )
                })

        if not payload.dry_run:
            # We must apply the quarantine update in the DB!
            now_iso = datetime.now(timezone.utc).isoformat()
            for item in affected_items:
                job_id = item["job"].id
                existing_job = item["job"]
                audit = existing_job.audit or {}
                audit["legacy_publish_quarantine"] = {
                    "previous_review_status": existing_job.review_status,
                    "applied_at": now_iso,
                    "reviewer": payload.reviewer,
                    "note": payload.note or "正式发布版隔离 legacy AI 可发布污染",
                }

                conn.execute(
                    """
                    UPDATE simulation_jobs
                    SET can_publish = 0,
                        review_status = 'needs_recheck',
                        reviewer = ?,
                        review_note = ?,
                        audit_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload.reviewer,
                        payload.note or "正式发布版隔离 legacy AI 可发布污染",
                        json.dumps(audit, ensure_ascii=False),
                        now_iso,
                        job_id,
                    ),
                )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": "legacy_simulation_publishable_risk_v1",
            "affected_count": len(affected_ids),
            "affected_job_ids": affected_ids,
            "dry_run": payload.dry_run,
            "items": affected_items,
        }
