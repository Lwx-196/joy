"""Source group and source blocker endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("/source-blockers")
def list_source_blockers(
    reason: str = Query("all"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    allowed = {
        "all",
        "missing_source_files",
        "no_real_source_photos",
        "insufficient_source_photos",
        "missing_before_after_pair",
    }
    if reason not in allowed:
        raise HTTPException(400, f"reason must be one of {sorted(allowed)}")
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.abs_path, c.customer_raw, c.customer_id, c.meta_json, c.tags_json,
                   c.manual_blocking_issues_json, c.notes,
                   latest.status AS latest_render_status,
                   rq.quality_status AS latest_render_quality_status
            FROM cases c
            LEFT JOIN render_jobs latest ON latest.id = (
                SELECT j.id FROM render_jobs j
                WHERE j.case_id = c.id
                ORDER BY j.enqueued_at DESC, j.id DESC
                LIMIT 1
            )
            LEFT JOIN render_quality rq ON rq.render_job_id = latest.id
            WHERE c.trashed_at IS NULL
            ORDER BY c.id DESC
            """
        ).fetchall()
        items_all = [item for row in rows if (item := _source_blocker_item(conn, row)) is not None]
    counts = {
        "total": len(items_all),
        "missing_source_files": sum(1 for item in items_all if item["reason"] == "missing_source_files"),
        "no_real_source_photos": sum(1 for item in items_all if item["reason"] == "no_real_source_photos"),
        "insufficient_source_photos": sum(1 for item in items_all if item["reason"] == "insufficient_source_photos"),
        "missing_before_after_pair": sum(1 for item in items_all if item["reason"] == "missing_before_after_pair"),
        "marked_not_source": sum(1 for item in items_all if item["marked_not_source"]),
    }
    filtered = items_all if reason == "all" else [item for item in items_all if item["reason"] == reason]
    return {
        "items": filtered[:limit],
        "total": len(filtered),
        "counts": counts,
        "reason": reason,
        "limit": limit,
    }


@router.post("/source-blockers/{case_id}/action")
def apply_source_blocker_action(case_id: int, payload: SourceBlockerActionRequest) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, tags_json, manual_blocking_issues_json, notes
            FROM cases
            WHERE id = ? AND trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        tags = _json_field(row, "tags_json", [])
        manual_issues = _json_field(row, "manual_blocking_issues_json", [])
        if not isinstance(tags, list):
            tags = []
        if not isinstance(manual_issues, list):
            manual_issues = []
        codes: list[str] = []
        for item in manual_issues:
            raw_code = item.get("code") if isinstance(item, dict) else item
            code = str(raw_code or "").strip()
            if code:
                codes.append(code)
        note = row["notes"] or ""
        befores = audit.snapshot_before(conn, [case_id])
        if payload.action == "mark_not_source":
            if source_images.CASE_NOT_SOURCE_TAG not in tags:
                tags.append(source_images.CASE_NOT_SOURCE_TAG)
            if source_images.CASE_NOT_SOURCE_CODE not in codes:
                codes.append(source_images.CASE_NOT_SOURCE_CODE)
            extra = (payload.note or "").strip()
            reviewer = (payload.reviewer or "人工整理").strip() or "人工整理"
            line = f"[源目录治理 {now}] {reviewer} 标记为素材归档/非案例源目录"
            if extra:
                line += f"：{extra}"
            note = f"{note.rstrip()}\n{line}".strip()
        elif payload.action == "clear_not_source":
            tags = [str(item) for item in tags if str(item) != source_images.CASE_NOT_SOURCE_TAG]
            codes = [code for code in codes if code != source_images.CASE_NOT_SOURCE_CODE]
            extra = (payload.note or "").strip()
            reviewer = (payload.reviewer or "人工整理").strip() or "人工整理"
            line = f"[源目录治理 {now}] {reviewer} 恢复为待检查"
            if extra:
                line += f"：{extra}"
            note = f"{note.rstrip()}\n{line}".strip()
        conn.execute(
            """
            UPDATE cases
            SET tags_json = ?, manual_blocking_issues_json = ?, notes = ?
            WHERE id = ?
            """,
            (
                json.dumps(tags, ensure_ascii=False),
                json.dumps(codes, ensure_ascii=False),
                note or None,
                case_id,
            ),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_blocker_action",
            source_route=f"/api/cases/source-blockers/{case_id}/action",
        )
    return {
        "case_id": case_id,
        "action": payload.action,
        "marked_not_source": source_images.case_marked_not_source(tags, codes),
        "tags": tags,
        "manual_blocking_codes": codes,
    }


@router.get("/{case_id}/source-binding-candidates")
def source_binding_candidates(
    case_id: int,
    limit: int = Query(8, ge=1, le=50),
) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, abs_path, customer_raw, customer_id, meta_json, skill_image_metadata_json
            FROM cases
            WHERE id = ? AND trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        source_profile = _profile_for_case_row(row)
        effective_profile, bound_case_ids = _effective_profile_for_case(conn, row)
        candidates = _binding_candidate_rows(conn, row, limit)
    return {
        "case_id": case_id,
        "source_profile": source_profile,
        "effective_source_profile": effective_profile,
        "bound_case_ids": bound_case_ids,
        "candidates": candidates,
    }


@router.post("/{case_id}/source-bindings")
def bind_source_directories(case_id: int, payload: SourceDirectoryBindRequest) -> dict[str, Any]:
    deduped: list[int] = []
    for raw in payload.source_case_ids:
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid == case_id:
            raise HTTPException(400, "cannot bind case to itself")
        if cid > 0 and cid not in deduped:
            deduped.append(cid)
    if not deduped:
        raise HTTPException(400, "source_case_ids cannot be empty")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, abs_path, customer_raw, customer_id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        bound = _bound_rows(conn, deduped)
        found_ids = {int(item["id"]) for item in bound}
        missing = [cid for cid in deduped if cid not in found_ids]
        if missing:
            raise HTTPException(404, f"source cases not found: {missing}")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_images.SOURCE_BINDINGS_META_KEY] = {
            "case_ids": deduped,
            "reviewer": payload.reviewer or "source-binding-workbench",
            "note": payload.note,
            "updated_at": now,
        }
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_directory_bind",
            source_route=f"/api/cases/{case_id}/source-bindings",
        )
        effective_profile = _merged_profile_for_rows([row, *bound])
        effective_profile["bound_case_ids"] = deduped
    return {
        "case_id": case_id,
        "bound_case_ids": deduped,
        "effective_source_profile": effective_profile,
    }


@router.delete("/{case_id}/source-bindings")
def clear_source_directory_bindings(case_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        befores = audit.snapshot_before(conn, [case_id])
        meta.pop(source_images.SOURCE_BINDINGS_META_KEY, None)
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_directory_unbind",
            source_route=f"/api/cases/{case_id}/source-bindings",
        )
    return {"case_id": case_id, "bound_case_ids": []}


@router.post("/{case_id}/source-group/slot-locks")
def lock_source_group_slot(case_id: int, payload: SourceGroupSlotLockRequest) -> dict[str, Any]:
    view = payload.view.strip()
    if view not in _ALLOWED_OVERRIDE_VIEWS:
        raise HTTPException(400, "view must be one of front, oblique, side")
    now = datetime.now(timezone.utc).isoformat()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    reason = payload.reason.strip() if payload.reason else None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        before = _validate_source_group_lock_image(conn, primary_case_id=case_id, primary_meta=meta, image=payload.before)
        after = _validate_source_group_lock_image(conn, primary_case_id=case_id, primary_meta=meta, image=payload.after)
        controls = source_selection.selection_controls_from_meta(meta)
        locked_slots = controls.setdefault("locked_slots", {})
        locked_slots[view] = {
            "before": before,
            "after": after,
            "reviewer": reviewer,
            "reason": reason,
            "updated_at": now,
        }
        controls["accepted_warnings"] = controls.get("accepted_warnings") or []
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_group_slot_lock",
            source_route=f"/api/cases/{case_id}/source-group/slot-locks",
        )
    return case_source_group(case_id)


@router.delete("/{case_id}/source-group/slot-locks/{view}")
def clear_source_group_slot_lock(case_id: int, view: str) -> dict[str, Any]:
    view = view.strip()
    if view not in _ALLOWED_OVERRIDE_VIEWS:
        raise HTTPException(400, "view must be one of front, oblique, side")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        controls = source_selection.selection_controls_from_meta(meta)
        locked_slots = controls.setdefault("locked_slots", {})
        locked_slots.pop(view, None)
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_group_slot_unlock",
            source_route=f"/api/cases/{case_id}/source-group/slot-locks/{view}",
        )
    return case_source_group(case_id)


@router.post("/{case_id}/source-group/accepted-warnings")
def accept_source_group_warning(case_id: int, payload: SourceGroupWarningAcceptanceRequest) -> dict[str, Any]:
    slot = payload.slot.strip()
    if slot not in _ALLOWED_OVERRIDE_VIEWS:
        raise HTTPException(400, "slot must be one of front, oblique, side")
    code = payload.code.strip()
    if not code:
        raise HTTPException(400, "code cannot be blank")
    now = datetime.now(timezone.utc).isoformat()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    acceptance = {
        "job_id": payload.job_id,
        "slot": slot,
        "code": code,
        "message_contains": (payload.message_contains or "").strip(),
        "reviewer": reviewer,
        "note": payload.note.strip() if payload.note else None,
        "accepted_at": now,
    }
    with db.connect() as conn:
        if payload.job_id is not None:
            job_row = conn.execute(
                """
                SELECT id, manifest_path
                FROM render_jobs
                WHERE id = ? AND case_id = ?
                """,
                (payload.job_id, case_id),
            ).fetchone()
            if not job_row:
                raise HTTPException(404, "render job not found")
            scope = _selected_pair_scope_from_manifest(job_row["manifest_path"], slot)
            if not scope.get("selected_files"):
                raise HTTPException(400, "selected pair not found in render manifest")
            acceptance.update(scope)
        row = conn.execute(
            "SELECT id, meta_json FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        controls = source_selection.selection_controls_from_meta(meta)
        accepted = [
            item
            for item in (controls.get("accepted_warnings") or [])
            if not (
                item.get("slot") == slot
                and item.get("code") == code
                and str(item.get("message_contains") or "") == acceptance["message_contains"]
            )
        ]
        accepted.append(acceptance)
        controls["accepted_warnings"] = accepted
        controls.setdefault("locked_slots", {})
        befores = audit.snapshot_before(conn, [case_id])
        meta[source_selection.SOURCE_GROUP_SELECTION_META_KEY] = controls
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="source_group_warning_accept",
            source_route=f"/api/cases/{case_id}/source-group/accepted-warnings",
        )
    return case_source_group(case_id)


@router.get("/{case_id}/source-group")
def case_source_group(case_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, abs_path, customer_raw, customer_id, meta_json, skill_image_metadata_json
            FROM cases
            WHERE id = ? AND trashed_at IS NULL
            """,
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        meta = _json_field(row, "meta_json", {})
        if not isinstance(meta, dict):
            meta = {}
        binding_ids = _source_binding_case_ids(meta)
        bound_rows = _bound_rows(conn, binding_ids)
        found_bound_ids = [int(item["id"]) for item in bound_rows]
        missing_bound_ids = [cid for cid in binding_ids if cid not in set(found_bound_ids)]
        sources = [
            _source_group_row_payload(conn, row, role="primary"),
            *[_source_group_row_payload(conn, bound_row, role="bound") for bound_row in bound_rows],
        ]
        source_profile = sources[0]["source_profile"]
        effective_profile = _merged_profile_for_source_payloads(sources) if bound_rows else source_profile
        if bound_rows:
            effective_profile["bound_case_ids"] = found_bound_ids
        total_images = sum(int(source.get("image_count") or 0) for source in sources)
        missing_image_count = sum(int(source.get("missing_image_count") or 0) for source in sources)
        binding_meta = meta.get(source_images.SOURCE_BINDINGS_META_KEY)
        binding_meta = binding_meta if isinstance(binding_meta, dict) else None
        render_feedback = source_selection.render_feedback_from_history(conn, case_id)
        selection_controls = source_selection.selection_controls_from_meta(meta)
        primary_render_metadata = _source_group_metadata_index(_json_field(row, "skill_image_metadata_json", []))
        preflight = _source_group_preflight(
            sources,
            effective_profile,
            render_feedback,
            selection_controls,
            primary_render_metadata,
        )
    return {
        "case_id": case_id,
        "source_profile": source_profile,
        "effective_source_profile": effective_profile,
        "bound_case_ids": found_bound_ids,
        "missing_bound_case_ids": missing_bound_ids,
        "binding": binding_meta,
        "source_count": len(sources),
        "image_count": total_images,
        "missing_image_count": missing_image_count,
        "sources": sources,
        "preflight": preflight,
        "audit": {
            "bound_source_case_ids": found_bound_ids,
            "binding_reviewer": (binding_meta or {}).get("reviewer") if binding_meta else None,
            "binding_updated_at": (binding_meta or {}).get("updated_at") if binding_meta else None,
            "binding_note": (binding_meta or {}).get("note") if binding_meta else None,
            "source_group_selection": selection_controls,
        },
    }


@router.post("/{case_id}/classify-images")
def classify_case_images(case_id: int, payload: ClassifyImagesRequest) -> dict[str, Any]:
    # Resolve PLAN P0-2 三态 mode：mode 优先；legacy dry_run=True→dry-run; False→apply
    if payload.mode:
        resolved_mode = payload.mode
    elif payload.dry_run is False:
        resolved_mode = "apply"
    else:
        resolved_mode = "dry-run"
    if resolved_mode not in {"dry-run", "live-no-apply", "apply"}:
        raise HTTPException(400, f"invalid mode: {resolved_mode!r}")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        provider = None if resolved_mode == "dry-run" else VLMProvider(env=dict(os.environ))
        return vlm_source_classifier.run_classification(
            conn,
            provider=provider,
            case_id=case_id,
            max_items=payload.max_items,
            mode=resolved_mode,
            concurrency=payload.concurrency,
            timeout=payload.timeout_seconds,
        )
