"""Case file and image asset endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *
from .cases_catalog import case_detail

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.get("/{case_id}/files")
def case_file(case_id: int, name: str):
    with db.connect() as conn:
        row = conn.execute("SELECT abs_path FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    base = Path(row["abs_path"]).resolve()
    target = (base / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


@router.patch("/{case_id}/images/{filename:path}", response_model=ImageOverride)
def patch_image_override(
    case_id: int, filename: str, payload: ImageOverridePayload
) -> ImageOverride:
    """Stage B: 单张源图 phase / view 手动覆盖。

    - filename 是 case 目录下图片相对路径(不接受绝对路径或 ..);非法返回 400
    - manual_phase 必须 ∈ _ALLOWED_OVERRIDE_PHASES 或 ""(清除);其它返回 400
    - manual_view 必须 ∈ _ALLOWED_OVERRIDE_VIEWS 或 "";其它返回 400
    - 字段省略(None)= 不修改该维度;空字符串 = 清除该维度回到 skill 自动判读
    - 两个字段都清完 → 删除整行
    """
    def _norm(v: str | None, allowed: set[str], label: str) -> tuple[bool, str | None]:
        """Returns (touch?, value-to-write). Empty string → clear."""
        if v is None:
            return False, None
        if v == "":
            return True, None
        if v not in allowed:
            raise HTTPException(400, f"invalid {label}: {v!r}")
        return True, v

    touch_phase, phase_val = _norm(payload.manual_phase, _ALLOWED_OVERRIDE_PHASES, "manual_phase")
    touch_view, view_val = _norm(payload.manual_view, _ALLOWED_OVERRIDE_VIEWS, "manual_view")
    touch_transform = "manual_transform" in payload.model_fields_set
    if not touch_phase and not touch_view and not touch_transform:
        raise HTTPException(400, "no fields to update")

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        case_row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        if not case_row:
            raise HTTPException(404, "case not found")
        case_dir = Path(case_row["abs_path"]).resolve()
        filename = _validate_relative_image_name(case_dir, filename)
        existing = conn.execute(
            "SELECT manual_phase, manual_view, manual_transform_json FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        ).fetchone()
        new_phase = phase_val if touch_phase else (existing["manual_phase"] if existing else None)
        new_view = view_val if touch_view else (existing["manual_view"] if existing else None)
        transform_arg = payload.manual_transform if touch_transform else _UNSET
        _write_image_override(conn, case_id, filename, new_phase, new_view, now, manual_transform=transform_arg)
        reviewer = (payload.reviewer or "operator").strip() or "operator"
        new_transform = (
            _decode_manual_transform(_manual_transform_to_json(payload.manual_transform))
            if touch_transform
            else (_decode_manual_transform(existing["manual_transform_json"]) if existing else None)
        )
        reason_parts: list[str] = []
        if new_phase:
            reason_parts.append("phase")
        if new_view:
            reason_parts.append("view")
        if new_transform:
            reason_parts.append("transform")
        default_reason = f"manual_override:{','.join(reason_parts)}" if reason_parts else "manual_override"
        reason = (payload.reason or default_reason).strip() or default_reason
        conn.execute(
            "UPDATE case_image_overrides SET reviewer = ?, reason_json = ? WHERE case_id = ? AND filename = ?",
            (reviewer, json.dumps({"reason": reason}, ensure_ascii=False), case_id, filename),
        )
    return ImageOverride(
        case_id=case_id,
        filename=filename,
        manual_phase=new_phase,
        manual_view=new_view,
        manual_transform=new_transform,
        reason=reason,
        reviewer=reviewer,
        updated_at=now,
    )


@router.post("/{case_id}/image-review/{filename:path}", response_model=ImageReviewResponse)
def review_case_image(
    case_id: int,
    filename: str,
    payload: ImageReviewPayload,
) -> ImageReviewResponse:
    """Record a source-image quality review decision without moving files.

    The decision lives under `cases.meta_json.image_review_states` and is
    tracked by the normal case revision audit. `excluded` is non-destructive:
    the file stays in place, but future formal renders skip it during scanning.
    """
    now = datetime.now(timezone.utc).isoformat()
    reviewer = (payload.reviewer or "operator").strip() or "operator"
    note = payload.note.strip() if payload.note else None
    layer = payload.layer.strip() if payload.layer else None
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        filename = _validate_relative_image_name(case_dir, filename)
        _resolve_existing_source(case_dir, filename)
        befores = audit.snapshot_before(conn, [case_id])
        row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
        meta: dict[str, Any] = {}
        if row and row["meta_json"]:
            try:
                parsed = json.loads(row["meta_json"])
                if isinstance(parsed, dict):
                    meta = parsed
            except (TypeError, ValueError):
                meta = {}
        states = _image_review_states_from_meta(meta)
        if payload.verdict == "reopen":
            states.pop(filename, None)
            review_state = None
        else:
            review_state = {
                "verdict": payload.verdict,
                "label": _IMAGE_REVIEW_VERDICT_LABEL[payload.verdict],
                "reviewer": reviewer,
                "note": note,
                "layer": layer,
                "render_excluded": payload.verdict == "excluded",
                "reviewed_at": now,
            }
            states[filename] = review_state
        if states:
            meta[_IMAGE_REVIEW_META_KEY] = states
        else:
            meta.pop(_IMAGE_REVIEW_META_KEY, None)
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="image_review",
            source_route=f"/api/cases/{case_id}/image-review/{filename}",
        )
    return ImageReviewResponse(
        case_id=case_id,
        filename=filename,
        review_state=review_state,
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/images/trash", response_model=ImageTrashResponse)
def trash_case_image(case_id: int, payload: ImageTrashRequest) -> ImageTrashResponse:
    stress.assert_destructive_allowed("image trash")
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        befores = audit.snapshot_before(conn, [case_id])
        original, trash_path = _trash_image(conn, case_id, case_dir, payload.filename)
        try:
            scanner.rescan_one(conn, case_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="rescan",
            source_route=f"/api/cases/{case_id}/images/trash",
            actor="user",
        )
    return ImageTrashResponse(
        case_id=case_id,
        original_filename=original,
        trash_path=trash_path,
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/images/restore", response_model=ImageRestoreResponse)
def restore_case_image(case_id: int, payload: ImageRestoreRequest) -> ImageRestoreResponse:
    stress.assert_destructive_allowed("image restore")
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
        befores = audit.snapshot_before(conn, [case_id])
        restored = _restore_trashed_image(case_dir, payload.trash_path, payload.restore_to)
        try:
            scanner.rescan_one(conn, case_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="rescan",
            source_route=f"/api/cases/{case_id}/images/restore",
            actor="user",
        )
    return ImageRestoreResponse(
        case_id=case_id,
        trash_path=payload.trash_path,
        restored_filename=restored,
        detail=case_detail(case_id),
    )
