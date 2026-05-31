"""Deterministic annotation QA queue over image observations.

R4b intentionally does not ask a VLM to rediscover mistakes. It audits two
local truth sources:
- low-confidence rows in ``image_observations``
- human corrections already written to ``case_image_overrides``
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import PurePosixPath
from typing import Any

LOW_CONFIDENCE_THRESHOLD = 0.5
MISMATCHABLE_PHASES = {"before", "after", "intraop"}
MISMATCHABLE_VIEWS = {"front", "oblique", "side", "back"}


def _json_load(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return data


def _norm_path(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).replace("\\", "/").strip("/")


def _path_variants(
    image_path: str | None,
    *,
    case_abs_path: str | None = None,
    root_path: str | None = None,
) -> set[str]:
    raw = _norm_path(image_path)
    if not raw:
        return set()
    variants = {raw, PurePosixPath(raw).name}
    for base in (case_abs_path, root_path):
        norm_base = _norm_path(base)
        if norm_base and raw.startswith(norm_base.rstrip("/") + "/"):
            rel = raw[len(norm_base.rstrip("/")) + 1 :]
            variants.add(rel)
            variants.add(PurePosixPath(rel).name)
    return {item for item in variants if item}


def _override_lookup_key(row: sqlite3.Row) -> tuple[int, str]:
    return (int(row["case_id"]), str(row["filename"]))


def _fetch_overrides(conn: sqlite3.Connection) -> dict[tuple[int, str], dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT case_id, filename, manual_phase, manual_view, manual_transform_json,
               reason_json, reviewer, updated_at
        FROM case_image_overrides
        WHERE manual_phase IS NOT NULL OR manual_view IS NOT NULL
        """
    ).fetchall()
    out: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        item = {
            "case_id": int(row["case_id"]),
            "filename": str(row["filename"]),
            "manual_phase": row["manual_phase"],
            "manual_view": row["manual_view"],
            "manual_transform": _json_load(row["manual_transform_json"], None),
            "reason": _json_load(row["reason_json"], None),
            "reviewer": row["reviewer"],
            "updated_at": row["updated_at"],
        }
        filenames = _path_variants(row["filename"])
        for filename in filenames:
            out[(int(row["case_id"]), filename)] = item
        out[_override_lookup_key(row)] = item
    return out


def _manual_for_observation(
    row: sqlite3.Row,
    overrides: dict[tuple[int, str], dict[str, Any]],
) -> dict[str, Any] | None:
    case_id = row["case_id"]
    if case_id is None:
        return None
    for variant in _path_variants(
        row["image_path"],
        case_abs_path=row["case_abs_path"],
        root_path=row["root_path"],
    ):
        match = overrides.get((int(case_id), variant))
        if match:
            return match
    return None


def _mismatch_fields(row: sqlite3.Row, manual: dict[str, Any] | None) -> list[str]:
    if not manual:
        return []
    mismatches: list[str] = []
    manual_phase = manual.get("manual_phase")
    observed_phase = str(row["phase"] or "unknown")
    if (
        manual_phase
        and observed_phase in MISMATCHABLE_PHASES
        and manual_phase != observed_phase
    ):
        mismatches.append("phase")
    manual_view = manual.get("manual_view")
    observed_view = str(row["view"] or "unknown")
    if (
        manual_view
        and observed_view in MISMATCHABLE_VIEWS
        and manual_view != observed_view
    ):
        mismatches.append("view")
    return mismatches


def _recommended_patch(
    mismatch_fields: list[str],
    manual: dict[str, Any] | None,
) -> dict[str, Any]:
    if not manual:
        return {}
    patch: dict[str, Any] = {}
    if "phase" in mismatch_fields:
        patch["manual_phase"] = manual.get("manual_phase")
    if "view" in mismatch_fields:
        patch["manual_view"] = manual.get("manual_view")
    return patch


def _manual_feedback_fields(manual: dict[str, Any] | None) -> list[str]:
    if not manual:
        return []
    fields: list[str] = []
    if manual.get("manual_phase"):
        fields.append("phase")
    if manual.get("manual_view"):
        fields.append("view")
    return fields


def build_annotation_audit_queue(
    conn: sqlite3.Connection,
    *,
    confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return the deterministic R4b queue.

    Queue reasons:
    - ``low_confidence``: ``image_observations.confidence < threshold``
    - ``label_mismatch``: a human override conflicts with phase/view observed in
      ``image_observations`` for the same case image.
    - ``manual_feedback``: a human override has already been fed back for the
      image, even if the current observation row now matches it.
    """
    observations = conn.execute(
        """
        SELECT io.id, io.group_id, io.case_id, io.image_path, io.phase, io.view,
               io.body_part, io.quality_json, io.confidence, io.source,
               io.reasons_json, io.created_at, io.updated_at,
               cg.title AS group_title, cg.root_path,
               c.abs_path AS case_abs_path, c.customer_raw
        FROM image_observations io
        LEFT JOIN case_groups cg ON cg.id = io.group_id
        LEFT JOIN cases c ON c.id = io.case_id
        ORDER BY io.confidence ASC, io.updated_at DESC, io.id ASC
        """
    ).fetchall()
    overrides = _fetch_overrides(conn)

    items: list[dict[str, Any]] = []
    low_confidence_count = 0
    label_mismatch_count = 0
    manual_feedback_count = 0
    mismatch_field_counts: dict[str, int] = {"phase": 0, "view": 0}
    for row in observations:
        confidence = float(row["confidence"] or 0.0)
        manual = _manual_for_observation(row, overrides)
        mismatch_fields = _mismatch_fields(row, manual)
        manual_feedback_fields = _manual_feedback_fields(manual)
        reasons: list[str] = []
        if confidence < confidence_threshold:
            reasons.append("low_confidence")
            low_confidence_count += 1
        if mismatch_fields:
            reasons.append("label_mismatch")
            label_mismatch_count += 1
            for field in mismatch_fields:
                mismatch_field_counts[field] = mismatch_field_counts.get(field, 0) + 1
        if manual_feedback_fields:
            manual_feedback_count += 1
            if not mismatch_fields:
                reasons.append("manual_feedback")
        if not reasons:
            continue
        item = {
            "observation_id": int(row["id"]),
            "group_id": int(row["group_id"]),
            "case_id": int(row["case_id"]) if row["case_id"] is not None else None,
            "image_path": row["image_path"],
            "filename": PurePosixPath(str(row["image_path"]).replace("\\", "/")).name,
            "case_abs_path": row["case_abs_path"],
            "customer_raw": row["customer_raw"],
            "group_title": row["group_title"],
            "phase": row["phase"],
            "view": row["view"],
            "body_part": row["body_part"],
            "confidence": round(confidence, 4),
            "source": row["source"],
            "reasons": reasons,
            "source_reasons": _json_load(row["reasons_json"], []),
            "quality": _json_load(row["quality_json"], {}),
            "manual_override": manual,
            "mismatch_fields": mismatch_fields,
            "manual_feedback_fields": manual_feedback_fields,
            "recommended_patch": _recommended_patch(mismatch_fields, manual),
            "updated_at": row["updated_at"],
        }
        items.append(item)

    queued_count = len(items)
    if limit is not None:
        items = items[: max(limit, 0)]

    return {
        "summary": {
            "confidence_threshold": confidence_threshold,
            "total_observations": len(observations),
            "queued_count": queued_count,
            "returned_count": len(items),
            "low_confidence_count": low_confidence_count,
            "label_mismatch_count": label_mismatch_count,
            "mismatch_field_counts": mismatch_field_counts,
            "manual_feedback_count": manual_feedback_count,
            "source": "image_observations+case_image_overrides",
            "deterministic": True,
        },
        "items": items,
    }
