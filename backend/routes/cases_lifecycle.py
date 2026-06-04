"""Case lifecycle endpoints."""
from __future__ import annotations

# ruff: noqa: F403,F405

from .cases_support import *
from .cases_catalog import case_detail

router = APIRouter(prefix="/api/cases", tags=["cases"])


@router.post("/trash", response_model=CaseTrashResponse)
def trash_cases(payload: CaseTrashRequest) -> CaseTrashResponse:
    stress.assert_destructive_allowed("case trash")
    case_ids = list(dict.fromkeys(payload.case_ids))
    if not case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    reason = payload.reason.strip() if payload.reason else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    trashed_at = datetime.now(timezone.utc).isoformat()
    trashed: list[int] = []
    skipped: list[CaseTrashSkipped] = []

    with db.connect() as conn:
        placeholders = ",".join("?" * len(case_ids))
        rows = conn.execute(
            f"SELECT id, abs_path, trashed_at FROM cases WHERE id IN ({placeholders})",
            case_ids,
        ).fetchall()
        by_id = {int(row["id"]): row for row in rows}

        for case_id in case_ids:
            row = by_id.get(case_id)
            if not row:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason="case_not_found"))
                continue
            if row["trashed_at"]:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason="already_trashed"))
                continue
            try:
                _trash_case_directory(
                    conn,
                    case_id=case_id,
                    abs_path=row["abs_path"],
                    reason=reason,
                    stamp=stamp,
                    trashed_at=trashed_at,
                )
            except FileNotFoundError:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason="directory_missing"))
                continue
            except OSError as e:
                skipped.append(CaseTrashSkipped(case_id=case_id, reason=f"io_error: {e}"))
                continue
            trashed.append(case_id)

    return CaseTrashResponse(trashed=len(trashed), case_ids=trashed, skipped=skipped)


@router.post("/{case_id}/reveal", response_model=CaseRevealResponse)
def reveal_case_path(case_id: int, payload: CaseRevealRequest) -> CaseRevealResponse:
    """Open a local Finder window for a safe case-owned path."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT abs_path FROM cases WHERE id = ? AND trashed_at IS NULL",
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")

    case_dir = Path(row["abs_path"]).resolve()
    if not case_dir.is_dir():
        raise HTTPException(404, "case directory not found")

    if payload.target == "case_root":
        target = case_dir
    else:
        brand = payload.brand or _FALLBACK_BRAND
        template = payload.template or _FALLBACK_TEMPLATE
        target = (case_dir / ".case-layout-output" / brand / template / "render").resolve()
        try:
            target.relative_to(case_dir)
        except ValueError:
            raise HTTPException(400, "invalid path")
        if not target.is_dir():
            raise HTTPException(404, "render output directory not found")

    try:
        subprocess.run(["open", str(target)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HTTPException(500, f"open failed: {exc}") from exc
    return CaseRevealResponse(opened=True, path=str(target))


@router.get("/{case_id}/rename-suggestion")
def rename_suggestion(case_id: int, dry_run: bool = True) -> dict:
    """Return a rename hint for non_labeled cases.

    `dry_run=true` (default and currently the only mode) means the response includes
    the candidate filenames that *would* be touched but doesn't actually rename. We
    keep `dry_run` in the surface so the UI can promise "not applied yet" loudly.
    """
    with db.connect() as conn:
        row = conn.execute(
            """SELECT abs_path, COALESCE(manual_category, category) AS cat,
                      meta_json
               FROM cases WHERE id = ?""",
            (case_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    if row["cat"] != "non_labeled":
        return {
            "command": None,
            "note": "当前案例已有标准命名，无需重命名",
            "dry_run": True,
            "affected_count": 0,
            "affected_files": [],
        }
    base = shlex.quote(row["abs_path"])
    meta = json.loads(row["meta_json"] or "{}")
    image_files: list[str] = source_images.filter_source_image_files([str(x) for x in (meta.get("image_files") or []) if x])
    affected = [
        f for f in image_files
        if not any(tok in f for tok in scanner.LABELED_TOKENS)
    ]
    return {
        "command": f"# 在 {base} 下，把术前/术后图片改成：术前-正面.jpg / 术后-正面.jpg / 术前-右45侧.jpg / ...",
        "note": "本阶段不直接执行，仅给出建议命令模板",
        "dry_run": dry_run,
        "affected_count": len(affected),
        "affected_files": affected[:20],
    }


@router.post("/{case_id}/upgrade")
def upgrade_case(case_id: int, brand: str = "fumei") -> CaseDetail:
    """Run case-layout-board's `build_manifest()` and persist the v3 results.

    Synchronous path (5-30s blocking). The shared core lives in
    `_upgrade_executor.execute_upgrade` so the upgrade_queue worker uses the
    exact same logic.
    """
    try:
        _upgrade_executor.execute_upgrade(
            case_id, brand, source_route=f"/api/cases/{case_id}/upgrade"
        )
    except ValueError:
        raise HTTPException(404, "case not found")
    except FileNotFoundError as e:
        raise HTTPException(404, f"case directory missing: {e}")
    except RuntimeError as e:
        raise HTTPException(503, f"skill unavailable: {e}")
    except Exception as e:  # noqa: BLE001 — skill can raise many flavors
        raise HTTPException(500, f"skill upgrade failed: {e}")
    return case_detail(case_id)


@router.post("/{case_id}/rescan")
def rescan_case(case_id: int) -> CaseDetail:
    """Re-run lite scanner on a single case dir.

    Useful after the user manually renamed files on disk and wants the auto-judged
    category/tier/blocking refreshed without scanning the whole library.
    """
    with db.connect() as conn:
        existing = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "case not found")
        # Audit before/after the scanner write.
        befores = audit.snapshot_before(conn, [case_id])
        try:
            scanner.rescan_one(conn, case_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        audit.record_after(
            conn,
            [case_id],
            befores,
            op="rescan",
            source_route=f"/api/cases/{case_id}/rescan",
            actor="scan",
        )
    return case_detail(case_id)
