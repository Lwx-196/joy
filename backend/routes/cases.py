"""Case endpoints: list, detail, files, rename-suggestion, manual edit."""
from __future__ import annotations

import json
import shlex
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import _upgrade_executor, audit, db, issue_translator, scanner, skill_bridge
from ..models import (
    CaseBatchUpdate,
    CaseDetail,
    CaseListResponse,
    CaseSummary,
    CaseUpdate,
    ImageOverride,
    ImageOverridePayload,
)

# Stage B: 单张图 phase / view 手动覆盖允许的取值。
# phase 与 skill manifest 输出对齐('before' / 'after');None 表示未覆盖。
# view 与 _extract_per_image_metadata 输出的 view_bucket / angle 对齐。
_ALLOWED_OVERRIDE_PHASES = {"before", "after"}
_ALLOWED_OVERRIDE_VIEWS = {"front", "oblique", "side"}

# Stage A: brand 与 template 的默认值,用于 manifest fallback 路径推导。
# 这里硬编码 fumei + tri-compare 是因为 case_detail 不知道用户当前选了哪个 brand;
# 如果未来需要按品牌切换,前端应通过 query string 传入,这里再读 query。
_FALLBACK_BRAND = "fumei"
_FALLBACK_TEMPLATE = "tri-compare"


def _fallback_skill_from_manifest(case_dir: str) -> dict[str, list[Any]]:
    """Stage A: 当 cases.skill_image_metadata_json 列为空(case 在新增列前已经
    upgrade 过)时,尝试直接读最近一次渲染的 manifest.final.json,实时透传
    image_metadata / blocking / warnings。

    返回 {"image_metadata": [...], "blocking_detail": [...], "warnings": [...]},
    任意错误条件全部空列表 — manifest 缺失/破损不阻塞 case 详情。
    """
    empty = {"image_metadata": [], "blocking_detail": [], "warnings": []}
    if not case_dir:
        return empty
    try:
        p = (
            Path(case_dir)
            / ".case-layout-output"
            / _FALLBACK_BRAND
            / _FALLBACK_TEMPLATE
            / "render"
            / "manifest.final.json"
        )
        if not p.is_file():
            return empty
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        groups = data.get("groups") or []
        return {
            "image_metadata": skill_bridge._extract_per_image_metadata(groups),
            "blocking_detail": [str(x) for x in (data.get("blocking_issues") or [])],
            "warnings": [str(x) for x in (data.get("warnings") or [])],
        }
    except (OSError, ValueError, TypeError):
        return empty

def _fetch_image_overrides(conn: sqlite3.Connection, case_id: int) -> dict[str, dict[str, str | None]]:
    """Stage B: 读 case_image_overrides 表,返回 {filename: {phase, view}}。

    任一字段是 None 表示该维度未覆盖。返回空 dict 表示该 case 完全没有手动覆盖。
    """
    out: dict[str, dict[str, str | None]] = {}
    rows = conn.execute(
        "SELECT filename, manual_phase, manual_view FROM case_image_overrides WHERE case_id = ?",
        (case_id,),
    ).fetchall()
    for r in rows:
        out[r["filename"]] = {
            "phase": r["manual_phase"],
            "view": r["manual_view"],
        }
    return out


def _apply_overrides_to_metadata(
    image_metadata: list[dict[str, Any]],
    overrides: dict[str, dict[str, str | None]],
) -> list[dict[str, Any]]:
    """Stage B: 把 case_image_overrides 合并到 skill_image_metadata。

    每条 entry 新增 `phase_source` / `view_source` 标 'manual' 或 'skill';manual 优先。
    在前端这两字段决定 chip 颜色或图标提示。
    """
    if not overrides and image_metadata is not None:
        # No overrides — annotate every entry as skill-sourced for UI determinism.
        for entry in image_metadata:
            entry.setdefault("phase_override_source", None)
            entry.setdefault("view_override_source", None)
        return image_metadata
    for entry in image_metadata or []:
        fname = entry.get("filename")
        ov = overrides.get(fname) if fname else None
        if ov and ov.get("phase"):
            entry["phase"] = ov["phase"]
            entry["phase_override_source"] = "manual"
        else:
            entry["phase_override_source"] = None
        if ov and ov.get("view"):
            entry["view_bucket"] = ov["view"]
            entry["angle"] = ov["view"]
            entry["view_override_source"] = "manual"
        else:
            entry["view_override_source"] = None
    return image_metadata


router = APIRouter(prefix="/api/cases", tags=["cases"])


def _row_to_summary(row: sqlite3.Row, customer_canonical: str | None = None) -> CaseSummary:
    # B2: blocking_issues_json may be v1 strings or v2 objects; merge_codes handles both.
    auto_raw = json.loads(row["blocking_issues_json"] or "[]")
    manual_raw = json.loads(row["manual_blocking_issues_json"] or "[]") if "manual_blocking_issues_json" in row.keys() else []
    effective_blocking = issue_translator.merge_codes([*auto_raw, *manual_raw])
    auto_cat = row["category"]
    auto_tier = row["template_tier"]
    manual_cat = row["manual_category"] if "manual_category" in row.keys() else None
    manual_tier = row["manual_template_tier"] if "manual_template_tier" in row.keys() else None
    tags = json.loads(row["tags_json"] or "[]") if "tags_json" in row.keys() and row["tags_json"] else []
    return CaseSummary(
        id=row["id"],
        abs_path=row["abs_path"],
        customer_raw=row["customer_raw"],
        customer_id=row["customer_id"],
        customer_canonical=customer_canonical,
        auto_category=auto_cat,
        auto_template_tier=auto_tier,
        manual_category=manual_cat,
        manual_template_tier=manual_tier,
        category=manual_cat or auto_cat,
        template_tier=manual_tier or auto_tier,
        source_count=row["source_count"],
        labeled_count=row["labeled_count"],
        blocking_issue_count=len(effective_blocking),
        notes=row["notes"] if "notes" in row.keys() else None,
        tags=tags,
        review_status=row["review_status"] if "review_status" in row.keys() else None,
        reviewed_at=row["reviewed_at"] if "reviewed_at" in row.keys() else None,
        held_until=row["held_until"] if "held_until" in row.keys() else None,
        hold_reason=row["hold_reason"] if "hold_reason" in row.keys() else None,
        last_modified=row["last_modified"],
        indexed_at=row["indexed_at"],
    )


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
    where: list[str] = []
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
            SELECT c.*, cu.canonical_name AS canonical_name
            FROM cases c
            LEFT JOIN customers cu ON cu.id = c.customer_id
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
            "SELECT COALESCE(manual_category, category) AS cat, COUNT(*) AS n FROM cases GROUP BY cat"
        ).fetchall()
        tier_rows = conn.execute(
            "SELECT COALESCE(manual_template_tier, template_tier) AS tier, COUNT(*) AS n "
            "FROM cases WHERE COALESCE(manual_template_tier, template_tier) IS NOT NULL GROUP BY tier"
        ).fetchall()
        review_rows = conn.execute(
            "SELECT COALESCE(review_status, 'unreviewed') AS status, COUNT(*) AS n FROM cases GROUP BY status"
        ).fetchall()
        manual_count = conn.execute(
            "SELECT COUNT(*) FROM cases WHERE manual_category IS NOT NULL OR manual_template_tier IS NOT NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    return {
        "total": total,
        "by_category": {r["cat"]: r["n"] for r in cat_rows},
        "by_tier": {r["tier"]: r["n"] for r in tier_rows},
        "by_review_status": {r["status"]: r["n"] for r in review_rows},
        "manual_override_count": manual_count,
    }


@router.get("/{case_id}", response_model=CaseDetail)
def case_detail(case_id: int) -> CaseDetail:
    with db.connect() as conn:
        row = conn.execute(
            """SELECT c.*, cu.canonical_name AS canonical_name FROM cases c
               LEFT JOIN customers cu ON cu.id = c.customer_id WHERE c.id = ?""",
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

    # Stage B: 合并 case_image_overrides — 手动覆盖优先于 skill 自动判读。
    with db.connect() as ov_conn:
        overrides = _fetch_image_overrides(ov_conn, case_id)
    skill_image_metadata = _apply_overrides_to_metadata(skill_image_metadata, overrides)

    summary = _row_to_summary(row, row["canonical_name"])
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
    )


@router.patch("/{case_id}", response_model=CaseDetail)
def update_case(case_id: int, payload: CaseUpdate) -> CaseDetail:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        # Audit: snapshot before, apply, snapshot after.
        befores = audit.snapshot_before(conn, [case_id])
        _apply_update(conn, [case_id], payload)
        audit.record_after(
            conn, [case_id], befores, op="patch", source_route=f"/api/cases/{case_id}"
        )
    return case_detail(case_id)


@router.patch("/{case_id}/images/{filename}", response_model=ImageOverride)
def patch_image_override(
    case_id: int, filename: str, payload: ImageOverridePayload
) -> ImageOverride:
    """Stage B: 单张源图 phase / view 手动覆盖。

    - filename 是 case 目录下 basename(不接受相对路径或 ..);非法返回 400
    - manual_phase 必须 ∈ _ALLOWED_OVERRIDE_PHASES 或 ""(清除);其它返回 400
    - manual_view 必须 ∈ _ALLOWED_OVERRIDE_VIEWS 或 "";其它返回 400
    - 字段省略(None)= 不修改该维度;空字符串 = 清除该维度回到 skill 自动判读
    - 两个字段都清完 → 删除整行
    """
    if "/" in filename or "\\" in filename or filename in {"", ".", ".."}:
        raise HTTPException(400, "filename must be a bare basename")

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
    if not touch_phase and not touch_view:
        raise HTTPException(400, "no fields to update")

    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        case_row = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case_row:
            raise HTTPException(404, "case not found")
        existing = conn.execute(
            "SELECT manual_phase, manual_view FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        ).fetchone()
        new_phase = phase_val if touch_phase else (existing["manual_phase"] if existing else None)
        new_view = view_val if touch_view else (existing["manual_view"] if existing else None)
        if new_phase is None and new_view is None:
            conn.execute(
                "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
                (case_id, filename),
            )
        else:
            conn.execute(
                """INSERT INTO case_image_overrides
                       (case_id, filename, manual_phase, manual_view, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(case_id, filename) DO UPDATE SET
                       manual_phase = excluded.manual_phase,
                       manual_view = excluded.manual_view,
                       updated_at = excluded.updated_at""",
                (case_id, filename, new_phase, new_view, now),
            )
    return ImageOverride(
        case_id=case_id,
        filename=filename,
        manual_phase=new_phase,
        manual_view=new_view,
        updated_at=now,
    )


@router.post("/batch")
def batch_update(payload: CaseBatchUpdate) -> dict:
    if not payload.case_ids:
        raise HTTPException(400, "case_ids cannot be empty")
    with db.connect() as conn:
        placeholders = ",".join("?" * len(payload.case_ids))
        rows = conn.execute(
            f"SELECT id FROM cases WHERE id IN ({placeholders})", payload.case_ids
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


def _apply_update(conn: sqlite3.Connection, case_ids: list[int], payload: CaseUpdate) -> None:
    sets: list[str] = []
    values: list[Any] = []
    clear = set(payload.clear_fields or [])

    def set_or_clear(field_db: str, value: Any | None, clear_key: str, json_encode: bool = False):
        if clear_key in clear:
            sets.append(f"{field_db} = NULL")
            return
        if value is None:
            return
        sets.append(f"{field_db} = ?")
        values.append(json.dumps(value, ensure_ascii=False) if json_encode else value)

    set_or_clear("manual_category", payload.manual_category, "manual_category")
    set_or_clear("manual_template_tier", payload.manual_template_tier, "manual_template_tier")
    set_or_clear(
        "manual_blocking_issues_json",
        payload.manual_blocking_codes,
        "manual_blocking_codes",
        json_encode=True,
    )
    set_or_clear("notes", payload.notes, "notes")
    set_or_clear("tags_json", payload.tags, "tags", json_encode=True)
    set_or_clear("review_status", payload.review_status, "review_status")
    set_or_clear("customer_id", payload.customer_id, "customer_id")
    # 三态之 "挂起"
    set_or_clear("held_until", payload.held_until, "held_until")
    set_or_clear("hold_reason", payload.hold_reason, "hold_reason")

    if payload.review_status == "reviewed":
        sets.append("reviewed_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
    elif "review_status" in clear or payload.review_status in {"pending", "needs_recheck"}:
        sets.append("reviewed_at = NULL")

    if not sets:
        return

    placeholders = ",".join("?" * len(case_ids))
    sql = f"UPDATE cases SET {', '.join(sets)} WHERE id IN ({placeholders})"
    conn.execute(sql, [*values, *case_ids])


@router.get("/{case_id}/files")
def case_file(case_id: int, name: str):
    with db.connect() as conn:
        row = conn.execute("SELECT abs_path FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "case not found")
    base = Path(row["abs_path"]).resolve()
    target = (base / name).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, "invalid path")
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "file not found")
    return FileResponse(target)


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
    image_files: list[str] = meta.get("image_files") or []
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
