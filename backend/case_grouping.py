"""Case grouping + diagnosis service.

This module is intentionally deterministic and local-first: it uses path,
filename, existing scanner metadata, and any skill metadata already persisted on
`cases`. No cloud model is called here. Low-confidence rows are surfaced to the
UI so a VLM/manual pass can be layered on later without changing the public API.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import scanner

STAGE_DIR_TOKENS = (
    "术前",
    "术后",
    "术中",
    "before",
    "after",
    "pre",
    "post",
    "治疗前",
    "治疗后",
)

VIEW_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("front", ("正面", "front", "frontal", "中面")),
    ("oblique", ("45", "斜", "侧45", "oblique")),
    ("side", ("侧面", "侧脸", "profile", "side")),
    ("back", ("背", "后背", "back")),
)

BODY_PART_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("body", tuple(scanner.BODY_KEYWORDS)),
    ("face", ("脸", "面部", "下颌", "口角", "苹果肌", "太阳穴", "脸颊", "法令纹")),
)

FACE_TEMPLATE_BY_SLOT_COUNT = {
    1: "single-compare",
    2: "bi-compare",
    3: "tri-compare",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _is_stage_name(name: str) -> bool:
    lowered = name.lower()
    return any(tok.lower() in lowered for tok in STAGE_DIR_TOKENS)


def _group_root_for_case(abs_path: str) -> Path:
    path = Path(abs_path)
    if _is_stage_name(path.name):
        return path.parent
    return path


def _phase_from_text(text: str) -> tuple[str, float, str]:
    lowered = text.lower()
    if any(tok in text for tok in ("术前", "治疗前")) or "before" in lowered or re.search(r"\bpre\b", lowered):
        return "before", 0.92, "path_or_filename_phase_token"
    if any(tok in text for tok in ("术后", "治疗后")) or "after" in lowered or "post" in lowered:
        return "after", 0.92, "path_or_filename_phase_token"
    if "术中" in text:
        return "intraop", 0.85, "path_or_filename_phase_token"
    return "unknown", 0.25, "phase_missing"


def _view_from_text(text: str) -> tuple[str, float, str]:
    lowered = text.lower()
    for view, tokens in VIEW_RULES:
        if any(tok.lower() in lowered for tok in tokens):
            return view, 0.86, "path_or_filename_view_token"
    return "unknown", 0.35, "view_missing"


def _body_part_from_text(text: str) -> tuple[str, str]:
    lowered = text.lower()
    for part, tokens in BODY_PART_RULES:
        if any(tok.lower() in lowered for tok in tokens):
            return part, "path_or_filename_body_part_token"
    return "unknown", "body_part_missing"


def _quality_from_skill(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "sharpness_score": meta.get("sharpness_score"),
        "sharpness_level": meta.get("sharpness_level"),
        "pose": meta.get("pose"),
        "angle_confidence": meta.get("angle_confidence"),
        "rejection_reason": meta.get("rejection_reason"),
        "issues": meta.get("issues") or [],
    }


def _relative_image_path(case_path: str, group_root: Path, image_name: str) -> str:
    image = Path(image_name)
    if image.is_absolute():
        try:
            return str(image.relative_to(group_root))
        except ValueError:
            return image.name
    case_dir = Path(case_path)
    try:
        prefix = case_dir.relative_to(group_root)
    except ValueError:
        prefix = Path()
    return str(prefix / image)


def _observations_for_case(row: sqlite3.Row, group_root: Path) -> list[dict[str, Any]]:
    meta = _json_load(row["meta_json"], {})
    image_files = [str(x) for x in (meta.get("image_files") or []) if x]
    skill_items = _json_load(row["skill_image_metadata_json"] if "skill_image_metadata_json" in row.keys() else None, [])
    skill_by_name = {
        str(item.get("filename")): item
        for item in skill_items
        if isinstance(item, dict) and item.get("filename")
    }

    observations: list[dict[str, Any]] = []
    for image_name in image_files:
        rel_path = _relative_image_path(row["abs_path"], group_root, image_name)
        text = f"{rel_path} {Path(row['abs_path']).name}"
        skill_meta = skill_by_name.get(Path(image_name).name) or skill_by_name.get(image_name)

        reasons: list[str] = []
        source = "rules"
        phase, phase_conf, phase_reason = _phase_from_text(text)
        view, view_conf, view_reason = _view_from_text(text)
        body_part, body_reason = _body_part_from_text(text)
        quality: dict[str, Any] = {
            "sharpness_score": None,
            "sharpness_level": None,
            "pose": None,
            "angle_confidence": None,
            "rejection_reason": None,
            "issues": [],
        }

        if isinstance(skill_meta, dict):
            source = "skill_v3"
            skill_phase = skill_meta.get("phase")
            if skill_phase in {"before", "after"}:
                phase = str(skill_phase)
                phase_conf = 0.96
                phase_reason = "skill_phase"
            view_bucket = skill_meta.get("view_bucket") or skill_meta.get("angle")
            if view_bucket in {"front", "oblique", "side", "back"}:
                view = str(view_bucket)
                angle_conf = skill_meta.get("angle_confidence")
                view_conf = float(angle_conf) if isinstance(angle_conf, (int, float)) else 0.9
                view_reason = "skill_view"
            quality = _quality_from_skill(skill_meta)
            if skill_meta.get("rejection_reason"):
                reasons.append(str(skill_meta["rejection_reason"]))

        reasons.extend([phase_reason, view_reason, body_reason])
        if body_part == "unknown" and any(kw in str(row["abs_path"]) for kw in scanner.BODY_KEYWORDS):
            body_part = "body"
        if body_part == "unknown" and row["category"] == "body":
            body_part = "body"
        if body_part == "unknown" and row["category"] == "standard_face":
            body_part = "face"

        confidence = round(min(phase_conf, view_conf), 3)
        observations.append({
            "case_id": row["id"],
            "image_path": rel_path,
            "phase": phase,
            "body_part": body_part,
            "view": view,
            "quality": quality,
            "confidence": confidence,
            "source": source,
            "reasons": sorted({r for r in reasons if r}),
        })
    return observations


def _best_by_view(observations: list[dict[str, Any]], phase: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for obs in observations:
        if obs["phase"] != phase:
            continue
        view = obs["view"]
        if view == "unknown":
            continue
        current = out.get(view)
        if current is None or obs["confidence"] > current["confidence"]:
            out[view] = obs
    return out


def _build_pair_candidates(observations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    before = _best_by_view(observations, "before")
    after = _best_by_view(observations, "after")
    candidate_slots = ["front", "oblique", "side", "back"]
    pairs: list[dict[str, Any]] = []
    ok_slots = 0
    body_seen = any(obs["body_part"] == "body" for obs in observations)

    for slot in candidate_slots:
        b = before.get(slot)
        a = after.get(slot)
        if not b and not a:
            continue
        has_pair = b is not None and a is not None
        score = round(((b or {}).get("confidence", 0) + (a or {}).get("confidence", 0)) / 2, 3) if has_pair else 0
        status = "ok" if has_pair and score >= 0.65 else "low_confidence" if has_pair else "missing_pair"
        if status == "ok":
            ok_slots += 1
        pairs.append({
            "slot": slot,
            "before_image_path": b["image_path"] if b else None,
            "after_image_path": a["image_path"] if a else None,
            "score": score,
            "metrics": {
                "phase_complete": has_pair,
                "view": slot,
                "body_part": "body" if body_seen else "face",
                "needs_vlm": status != "ok",
            },
            "status": status,
        })

    if body_seen and before.get("front") and after.get("front") and before.get("back") and after.get("back"):
        template = "body-dual-compare"
    elif ok_slots >= 3:
        template = FACE_TEMPLATE_BY_SLOT_COUNT[3]
    elif ok_slots >= 2:
        template = FACE_TEMPLATE_BY_SLOT_COUNT[2]
    elif ok_slots >= 1:
        template = FACE_TEMPLATE_BY_SLOT_COUNT[1]
    else:
        template = "manual-review"

    for pair in pairs:
        pair["template_hint"] = template
    return pairs, template


def _diagnosis_for(observations: list[dict[str, Any]], pairs: list[dict[str, Any]], suggested_template: str | None) -> dict[str, Any]:
    low_conf = [
        obs for obs in observations
        if obs["confidence"] < 0.65 or obs["phase"] == "unknown" or obs["view"] == "unknown"
    ]
    blocking = [
        pair for pair in pairs
        if pair["status"] in {"missing_pair", "low_confidence"}
    ]
    phase_counts: dict[str, int] = {}
    view_counts: dict[str, int] = {}
    for obs in observations:
        phase_counts[obs["phase"]] = phase_counts.get(obs["phase"], 0) + 1
        view_counts[obs["view"]] = view_counts.get(obs["view"], 0) + 1
    return {
        "image_count": len(observations),
        "low_confidence_count": len(low_conf),
        "blocking_pair_count": len(blocking),
        "phase_counts": phase_counts,
        "view_counts": view_counts,
        "suggested_template": suggested_template,
        "needs_review": len(low_conf) > 0 or suggested_template == "manual-review",
        "model_policy": {
            "rule_layer": "enabled",
            "local_cv_layer": "uses_existing_skill_metadata_when_available",
            "vlm_layer": "only_for_low_confidence_queue",
            "cloud_upload": "disabled_by_default",
        },
    }


def rebuild_case_groups(conn: sqlite3.Connection) -> dict[str, Any]:
    """Rebuild group diagnosis from the current `cases` table."""
    now = _now_iso()
    rows = conn.execute("SELECT * FROM cases WHERE trashed_at IS NULL ORDER BY abs_path, id").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        root = _group_root_for_case(row["abs_path"])
        groups.setdefault(str(root), []).append(row)

    conn.execute("DELETE FROM case_groups")
    created = 0
    obs_count = 0
    pair_count = 0
    low_conf_groups = 0

    for root_path, case_rows in groups.items():
        root = Path(root_path)
        case_ids = [int(r["id"]) for r in case_rows]
        primary = next((r for r in case_rows if Path(r["abs_path"]) == root), case_rows[0])
        customer_raw = primary["customer_raw"]
        observations: list[dict[str, Any]] = []
        for row in case_rows:
            observations.extend(_observations_for_case(row, root))
        pairs, suggested_template = _build_pair_candidates(observations)
        diagnosis = _diagnosis_for(observations, pairs, suggested_template)
        if diagnosis["needs_review"]:
            low_conf_groups += 1
        status = "needs_review" if diagnosis["needs_review"] else "auto"

        cur = conn.execute(
            """
            INSERT INTO case_groups
              (group_key, primary_case_id, customer_raw, title, root_path,
               case_ids_json, status, diagnosis_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_path,
                primary["id"],
                customer_raw,
                root.name,
                root_path,
                json.dumps(case_ids, ensure_ascii=False),
                status,
                json.dumps(diagnosis, ensure_ascii=False),
                now,
                now,
            ),
        )
        group_id = cur.lastrowid
        created += 1

        for obs in observations:
            conn.execute(
                """
                INSERT INTO image_observations
                  (group_id, case_id, image_path, phase, body_part, view,
                   quality_json, confidence, source, reasons_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    obs["case_id"],
                    obs["image_path"],
                    obs["phase"],
                    obs["body_part"],
                    obs["view"],
                    json.dumps(obs["quality"], ensure_ascii=False),
                    obs["confidence"],
                    obs["source"],
                    json.dumps(obs["reasons"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            obs_count += 1
        for pair in pairs:
            conn.execute(
                """
                INSERT INTO pair_candidates
                  (group_id, slot, before_image_path, after_image_path, score,
                   metrics_json, status, template_hint, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    pair["slot"],
                    pair["before_image_path"],
                    pair["after_image_path"],
                    pair["score"],
                    json.dumps(pair["metrics"], ensure_ascii=False),
                    pair["status"],
                    pair["template_hint"],
                    now,
                    now,
                ),
            )
            pair_count += 1

    return {
        "group_count": created,
        "image_observation_count": obs_count,
        "pair_candidate_count": pair_count,
        "low_confidence_group_count": low_conf_groups,
    }


def list_case_groups(conn: sqlite3.Connection, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = []
    where = ""
    if status:
        where = "WHERE g.status = ?"
        params.append(status)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT g.*, c.category, c.template_tier
        FROM case_groups g
        LEFT JOIN cases c ON c.id = g.primary_case_id
        {where}
        ORDER BY g.updated_at DESC, g.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_group_row_to_dict(r) for r in rows]


def _group_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "group_key": row["group_key"],
        "primary_case_id": row["primary_case_id"],
        "customer_raw": row["customer_raw"],
        "title": row["title"],
        "root_path": row["root_path"],
        "case_ids": _json_load(row["case_ids_json"], []),
        "status": row["status"],
        "diagnosis": _json_load(row["diagnosis_json"], {}),
        "category": row["category"] if "category" in row.keys() else None,
        "template_tier": row["template_tier"] if "template_tier" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_case_group_diagnosis(conn: sqlite3.Connection, group_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT g.*, c.category, c.template_tier
        FROM case_groups g
        LEFT JOIN cases c ON c.id = g.primary_case_id
        WHERE g.id = ?
        """,
        (group_id,),
    ).fetchone()
    if not row:
        return None
    obs_rows = conn.execute(
        "SELECT * FROM image_observations WHERE group_id = ? ORDER BY image_path",
        (group_id,),
    ).fetchall()
    pair_rows = conn.execute(
        "SELECT * FROM pair_candidates WHERE group_id = ? ORDER BY slot",
        (group_id,),
    ).fetchall()
    case_ids = _json_load(row["case_ids_json"], [])
    case_rows = []
    if case_ids:
        placeholders = ",".join("?" * len(case_ids))
        case_rows = conn.execute(
            f"SELECT id, abs_path, category, template_tier FROM cases "
            f"WHERE trashed_at IS NULL AND id IN ({placeholders}) ORDER BY id",
            case_ids,
        ).fetchall()
    return {
        "group": _group_row_to_dict(row),
        "cases": [dict(r) for r in case_rows],
        "image_observations": [
            {
                "id": r["id"],
                "case_id": r["case_id"],
                "image_path": r["image_path"],
                "phase": r["phase"],
                "body_part": r["body_part"],
                "view": r["view"],
                "quality": _json_load(r["quality_json"], {}),
                "confidence": r["confidence"],
                "source": r["source"],
                "reasons": _json_load(r["reasons_json"], []),
                "updated_at": r["updated_at"],
            }
            for r in obs_rows
        ],
        "pair_candidates": [
            {
                "id": r["id"],
                "slot": r["slot"],
                "before_image_path": r["before_image_path"],
                "after_image_path": r["after_image_path"],
                "score": r["score"],
                "metrics": _json_load(r["metrics_json"], {}),
                "status": r["status"],
                "template_hint": r["template_hint"],
                "updated_at": r["updated_at"],
            }
            for r in pair_rows
        ],
    }


def update_group_confirmation(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    status: str,
    category: str | None = None,
    template_tier: str | None = None,
    note: str | None = None,
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM case_groups WHERE id = ?", (group_id,)).fetchone()
    if not row:
        return None
    diagnosis = _json_load(row["diagnosis_json"], {})
    diagnosis["manual_confirmation"] = {
        "category": category,
        "template_tier": template_tier,
        "note": note,
        "confirmed_at": _now_iso(),
    }
    conn.execute(
        """
        UPDATE case_groups
        SET status = ?, diagnosis_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, json.dumps(diagnosis, ensure_ascii=False), _now_iso(), group_id),
    )
    if row["primary_case_id"] and (category or template_tier):
        sets: list[str] = []
        values: list[Any] = []
        if category:
            sets.append("manual_category = ?")
            values.append(category)
        if template_tier:
            sets.append("manual_template_tier = ?")
            values.append(template_tier)
        conn.execute(
            f"UPDATE cases SET {', '.join(sets)} WHERE id = ?",
            [*values, row["primary_case_id"]],
        )
    return get_case_group_diagnosis(conn, group_id)
