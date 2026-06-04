"""Core case catalog and detail endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *
from .cases_source_group import case_source_group

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("", response_model=CaseListResponse)
def list_cases(
    category: str | None = None,
    tier: str | None = None,
    customer_id: int | None = None,
    review_status: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    since: str | None = None,
    blocking: str | None = None,
    include_held: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=2000),
) -> CaseListResponse:
    where: list[str] = ["c.trashed_at IS NULL"]
    params: list[Any] = []
    if category:
        where.append("COALESCE(c.manual_category, c.category) = ?")
        params.append(category)
    if tier:
        where.append("COALESCE(c.manual_template_tier, c.template_tier) = ?")
        params.append(tier)
    if customer_id is not None:
        where.append("c.customer_id = ?")
        params.append(customer_id)
    if review_status:
        if review_status == "unreviewed":
            where.append("(c.review_status IS NULL OR c.review_status = 'pending')")
        else:
            where.append("c.review_status = ?")
            params.append(review_status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append(
            "("
            "c.abs_path LIKE ? OR "
            "COALESCE(cu.canonical_name, '') LIKE ? OR "
            "COALESCE(c.customer_raw, '') LIKE ? OR "
            "COALESCE(c.notes, '') LIKE ?"
            ")"
        )
        params.extend([like, like, like, like])

    if tag and tag.strip():
        # tags_json 是 JSON 数组,LIKE '%"<tag>"%' 精确匹配 token(避免子串误匹配)
        where.append("c.tags_json LIKE ?")
        params.append(f'%"{tag.strip()}"%')

    if since == "today":
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        where.append("c.indexed_at >= ?")
        params.append(today_start.isoformat())

    if blocking == "open":
        # 用 json_array_length 判定数组非空,避免依赖 "无空白序列化" 的隐式不变量
        where.append("(c.blocking_issues_json IS NOT NULL AND json_array_length(c.blocking_issues_json) > 0)")

    if not include_held:
        # held_until 未来 = 挂起;NULL or 过去 = 不挂起
        now_iso = datetime.now(timezone.utc).isoformat()
        where.append("(c.held_until IS NULL OR c.held_until < ?)")
        params.append(now_iso)

    where_sql = " WHERE " + " AND ".join(where) if where else ""

    with db.connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM cases c LEFT JOIN customers cu ON cu.id = c.customer_id{where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT c.*, cu.canonical_name AS canonical_name,
                   latest.status AS latest_render_status,
                   rq.quality_status AS latest_render_quality_status,
                   rq.quality_score AS latest_render_quality_score
            FROM cases c
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN render_jobs latest ON latest.id = (
                SELECT j.id FROM render_jobs j
                WHERE j.case_id = c.id
                ORDER BY j.enqueued_at DESC, j.id DESC
                LIMIT 1
            )
            LEFT JOIN render_quality rq ON rq.render_job_id = latest.id
            {where_sql}
            ORDER BY c.id DESC
            LIMIT ? OFFSET ?
            """,
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

    items = [_row_to_summary(r, r["canonical_name"]) for r in rows]
    return CaseListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/stats")
def stats() -> dict:
    with db.connect() as conn:
        cat_rows = conn.execute(
            "SELECT COALESCE(manual_category, category) AS cat, COUNT(*) AS n "
            "FROM cases WHERE trashed_at IS NULL GROUP BY cat"
        ).fetchall()
        tier_rows = conn.execute(
            "SELECT COALESCE(manual_template_tier, template_tier) AS tier, COUNT(*) AS n "
            "FROM cases WHERE trashed_at IS NULL "
            "AND COALESCE(manual_template_tier, template_tier) IS NOT NULL GROUP BY tier"
        ).fetchall()
        review_rows = conn.execute(
            "SELECT COALESCE(review_status, 'unreviewed') AS status, COUNT(*) AS n "
            "FROM cases WHERE trashed_at IS NULL GROUP BY status"
        ).fetchall()
        manual_count = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE trashed_at IS NULL "
            "AND (manual_category IS NOT NULL OR manual_template_tier IS NOT NULL)"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM cases WHERE trashed_at IS NULL").fetchone()[0]
    return {
        "total": total,
        "by_category": {r["cat"]: r["n"] for r in cat_rows},
        "by_tier": {r["tier"]: r["n"] for r in tier_rows},
        "by_review_status": {r["status"]: r["n"] for r in review_rows},
        "manual_override_count": manual_count,
    }


@router.post("/batch")
def batch_update(payload: CaseBatchUpdate) -> dict:
    if not payload.case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    with db.connect() as conn:
        placeholders = ",".join("?" * len(payload.case_ids))
        rows = conn.execute(
            f"SELECT id FROM cases WHERE trashed_at IS NULL AND id IN ({placeholders})", payload.case_ids
        ).fetchall()
        valid_ids = [r["id"] for r in rows]
        if not valid_ids:
            raise HTTPException(404, "no matching cases")
        # Audit: snapshot before, apply, snapshot after — one revision per case.
        befores = audit.snapshot_before(conn, valid_ids)
        _apply_update(conn, valid_ids, payload.update)
        audit.record_after(
            conn, valid_ids, befores, op="batch", source_route="/api/cases/batch"
        )
    return {"updated": len(valid_ids), "case_ids": valid_ids}


@router.get("/{case_id}", response_model=CaseDetail)
def case_detail(case_id: int) -> CaseDetail:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, cu.canonical_name AS canonical_name,
                   latest.status AS latest_render_status,
                   rq.quality_status AS latest_render_quality_status,
                   rq.quality_score AS latest_render_quality_score
            FROM cases c
            LEFT JOIN customers cu ON cu.id = c.customer_id
            LEFT JOIN render_jobs latest ON latest.id = (
                SELECT j.id FROM render_jobs j
                WHERE j.case_id = c.id
                ORDER BY j.enqueued_at DESC, j.id DESC
                LIMIT 1
            )
            LEFT JOIN render_quality rq ON rq.render_job_id = latest.id
            WHERE c.id = ? AND c.trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")

    auto_raw = json.loads(row["blocking_issues_json"] or "[]")
    manual_raw = json.loads(row["manual_blocking_issues_json"] or "[]") if row["manual_blocking_issues_json"] else []
    # manual_blocking_codes stays as bare strings (frontend chip-toggle UI uses code-only)
    manual_codes = [
        issue_translator.normalize_issue(it)["code"]
        for it in manual_raw
        if issue_translator.normalize_issue(it)["code"]
    ]
    effective = issue_translator.merge_codes([*auto_raw, *manual_raw])
    meta = json.loads(row["meta_json"] or "{}")

    # Stage A: skill 透传字段(只在 upgrade 后非空)
    def _safe_list(col: str) -> list[Any]:
        if col not in row.keys():
            return []
        raw = row[col]
        if not raw:
            return []
        try:
            val = json.loads(raw)
            return val if isinstance(val, list) else []
        except (TypeError, ValueError):
            return []

    skill_image_metadata = _safe_list("skill_image_metadata_json")
    skill_blocking_detail = _safe_list("skill_blocking_detail_json")
    skill_warnings = _safe_list("skill_warnings_json")
    # 兼容 Stage A 之前已升级的 case:db 列为空时直接读 manifest.final.json
    if not skill_image_metadata and not skill_blocking_detail and not skill_warnings:
        fb = _fallback_skill_from_manifest(row["abs_path"])
        skill_image_metadata = fb["image_metadata"]
        skill_blocking_detail = fb["blocking_detail"]
        skill_warnings = fb["warnings"]
    raw_image_files = [str(x) for x in (meta.get("image_files") or []) if x]
    image_files = source_images.filter_source_image_files(raw_image_files)
    with db.connect() as bind_conn:
        bound_rows = _bound_rows(bind_conn, _source_binding_case_ids(meta))
    if bound_rows:
        effective_raw_image_files: list[str] = []
        for source_row in [row, *bound_rows]:
            source_meta = _json_field(source_row, "meta_json", {})
            effective_raw_image_files.extend(_case_image_files_from_meta(source_meta))
        meta["source_case_bindings_effective_profile"] = source_images.classify_source_profile(effective_raw_image_files)
        meta["source_case_bindings_bound_case_ids"] = [int(item["id"]) for item in bound_rows]
    image_review_states = _image_review_states_from_meta(meta)
    if image_files:
        active_files = set(image_files)
        skill_image_metadata = [
            item
            for item in skill_image_metadata
            if str(item.get("filename") or item.get("relative_path") or "") in active_files
        ]

    # Stage B: 合并 case_image_overrides — 手动覆盖优先于 skill 自动判读。
    with db.connect() as ov_conn:
        overrides = _fetch_image_overrides(ov_conn, case_id)
    skill_image_metadata = _apply_overrides_to_metadata(
        skill_image_metadata,
        overrides,
        image_files=image_files,
    )
    skill_image_metadata = _apply_review_states_to_metadata(skill_image_metadata, image_review_states)
    summary = _row_to_summary(row, row["canonical_name"])
    classification_preflight = _build_classification_preflight(
        image_files=image_files,
        raw_image_files=raw_image_files,
        image_metadata=skill_image_metadata,
        case_id=case_id,
        case_category=summary.category,
    )
    if bound_rows:
        classification_preflight = _apply_source_group_authority_to_preflight(
            classification_preflight,
            case_source_group(case_id),
        )

    return CaseDetail(
        **summary.model_dump(),
        auto_blocking_issues=issue_translator.translate_list(auto_raw),
        manual_blocking_codes=manual_codes,
        blocking_issues=issue_translator.translate_list(effective),
        pose_delta_max=row["pose_delta_max"],
        sharp_ratio_min=row["sharp_ratio_min"],
        meta=meta,
        rename_suggestion=None,
        skill_image_metadata=skill_image_metadata,
        skill_blocking_detail=skill_blocking_detail,
        skill_warnings=skill_warnings,
        classification_preflight=classification_preflight,
    )


@router.patch("/{case_id}", response_model=CaseDetail)
def update_case(case_id: int, payload: CaseUpdate) -> CaseDetail:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        # Audit: snapshot before, apply, snapshot after.
        befores = audit.snapshot_before(conn, [case_id])
        _apply_update(conn, [case_id], payload)
        audit.record_after(
            conn, [case_id], befores, op="patch", source_route=f"/api/cases/{case_id}"
        )
    return case_detail(case_id)
