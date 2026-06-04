"""Manual render source and preview endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *
from .cases_catalog import case_detail

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.post("/{case_id}/manual-render-sources", response_model=ManualRenderSourcesResponse)
def prepare_manual_render_sources(
    case_id: int,
    payload: ManualRenderSourcesRequest,
) -> ManualRenderSourcesResponse:
    """Materialize a user-selected before/after pair as standard named sources.

    The render skill pairs images while building the manifest. Writing only a
    late manual phase/view override is not enough for previously unlabeled
    cases, so this endpoint copies/saves the chosen images into the case
    directory using names the skill already understands, then rescans the case.
    """
    view = payload.view
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    created: list[str] = []
    try:
        with db.connect() as conn:
            case_dir = _case_dir_for_update(conn, case_id)
            befores = audit.snapshot_before(conn, [case_id])
            before_name = _materialize_manual_image(case_dir, payload.before, "before", view, stamp)
            created.append(before_name)
            after_name = _materialize_manual_image(case_dir, payload.after, "after", view, stamp)
            created.append(after_name)

            try:
                scanner.rescan_one(conn, case_id)
            except ValueError as e:
                raise HTTPException(400, str(e))

            # The scanner caps meta.image_files at 50. Keep the just-created
            # manual files visible in the detail page even for very large cases.
            row = conn.execute("SELECT meta_json FROM cases WHERE id = ?", (case_id,)).fetchone()
            meta = json.loads(row["meta_json"] or "{}") if row else {}
            image_files = source_images.filter_source_image_files(
                [str(x) for x in (meta.get("image_files") if isinstance(meta, dict) else []) if x]
            )
            if isinstance(image_files, list):
                merged = list(dict.fromkeys([*created, *[str(x) for x in image_files]]))
                meta["image_files"] = merged[:50]
                conn.execute(
                    "UPDATE cases SET meta_json = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=False), case_id),
                )

            now = datetime.now(timezone.utc).isoformat()
            before_transform = _decode_manual_transform(_manual_transform_to_json(payload.before_transform))
            _write_image_override(
                conn,
                case_id,
                before_name,
                "before",
                view,
                now,
                manual_transform=before_transform,
            )
            _write_image_override(conn, case_id, after_name, "after", view, now)
            audit.record_after(
                conn,
                [case_id],
                befores,
                op="rescan",
                source_route=f"/api/cases/{case_id}/manual-render-sources",
                actor="user",
            )
    except Exception:
        for name in created:
            try:
                (case_dir / name).unlink(missing_ok=True)  # type: ignore[possibly-undefined]
            except Exception:
                pass
        raise

    return ManualRenderSourcesResponse(
        case_id=case_id,
        view=view,
        created_files=created,
        manual_overrides=[
            ImageOverride(
                case_id=case_id,
                filename=created[0],
                manual_phase="before",
                manual_view=view,
                manual_transform=before_transform,
                updated_at=now,
            ),
            ImageOverride(
                case_id=case_id,
                filename=created[1],
                manual_phase="after",
                manual_view=view,
                manual_transform=None,
                updated_at=now,
            ),
        ],
        detail=case_detail(case_id),
    )


@router.post("/{case_id}/manual-render-preview", response_model=ManualRenderPreviewResponse)
def preview_manual_render(
    case_id: int,
    payload: ManualRenderPreviewRequest,
) -> ManualRenderPreviewResponse:
    """Generate a temporary one-view formal preview before saving sources.

    This intentionally does not copy files into the case source list, does not
    rescan, and does not enqueue a render job. The preview lives under a hidden
    workbench directory and is only used for quick visual comparison.
    """
    preview_id = uuid.uuid4().hex
    with db.connect() as conn:
        case_dir = _case_dir_for_update(conn, case_id)
    preview_dir = _preview_root(case_dir) / preview_id
    preview_dir.mkdir(parents=True, exist_ok=False)
    try:
        before_path = _materialize_preview_image(case_dir, preview_dir, payload.before, "before")
        after_path = _materialize_preview_image(case_dir, preview_dir, payload.after, "after")
        before_transform = _decode_manual_transform(_manual_transform_to_json(payload.before_transform))
        result = render_executor.run_manual_render_preview(
            case_dir=case_dir,
            preview_dir=preview_dir,
            brand=payload.brand or _FALLBACK_BRAND,
            view=payload.view,
            before_path=before_path,
            after_path=after_path,
            before_transform=before_transform,
        )
        _prune_preview_dirs(case_dir)
    except Exception:
        shutil.rmtree(preview_dir, ignore_errors=True)
        raise

    return ManualRenderPreviewResponse(
        case_id=case_id,
        preview_id=preview_id,
        view=payload.view,
        output_path=str(result.get("output_path") or ""),
        manifest_path=result.get("manifest_path"),
        render_plan=result.get("render_plan") or {},
        warnings=[str(x) for x in (result.get("warnings") or [])],
    )


@router.get("/{case_id}/manual-render-preview/{preview_id}/file")
def manual_render_preview_file(case_id: int, preview_id: str) -> FileResponse:
    if not _PREVIEW_ID_RE.match(preview_id):
        raise HTTPException(400, "invalid preview id")
    with db.connect() as conn:
        row = conn.execute("SELECT abs_path, trashed_at FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    if row["trashed_at"]:
        raise HTTPException(410, "case has been moved to trash")
    case_dir = Path(row["abs_path"]).resolve()
    target = (_preview_root(case_dir) / preview_id / "preview.jpg").resolve()
    try:
        target.relative_to(_preview_root(case_dir).resolve())
    except ValueError:
        raise HTTPException(400, "invalid preview path")
    if not target.is_file():
        raise HTTPException(404, "preview not found")
    return FileResponse(target)
