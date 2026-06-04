"""Unified write/delete entrypoint for case image overrides."""
from __future__ import annotations

import sqlite3

from .best_pair_dirty import mark_best_pair_dirty

_UNSET = object()

_ALL_FIELDS: tuple[str, ...] = (
    "manual_phase",
    "manual_view",
    "manual_transform_json",
)


def _changed_fields(
    *,
    manual_phase: str | None,
    manual_view: str | None,
    manual_transform_json: str | None,
) -> tuple[str, ...]:
    fields: list[str] = []
    if manual_phase is not None:
        fields.append("manual_phase")
    if manual_view is not None:
        fields.append("manual_view")
    if manual_transform_json is not None:
        fields.append("manual_transform_json")
    return tuple(fields) if fields else _ALL_FIELDS


def write_image_override(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    manual_phase: str | None,
    manual_view: str | None,
    manual_transform_json: str | None,
    updated_at: str,
    reason_json: str | None | object = _UNSET,
    reviewer: str | None | object = _UNSET,
    skip_dirty_mark: bool = False,
) -> None:
    """Upsert an override row, or delete it when all manual values are null."""
    all_null = manual_phase is None and manual_view is None and manual_transform_json is None
    if all_null:
        conn.execute(
            "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        )
    else:
        if reason_json is _UNSET or reviewer is _UNSET:
            existing = conn.execute(
                "SELECT reason_json, reviewer FROM case_image_overrides WHERE case_id = ? AND filename = ?",
                (case_id, filename),
            ).fetchone()
            if reason_json is _UNSET:
                reason_json = existing["reason_json"] if existing else None
            if reviewer is _UNSET:
                reviewer = existing["reviewer"] if existing else None
        conn.execute(
            """INSERT INTO case_image_overrides
                 (case_id, filename, manual_phase, manual_view, manual_transform_json,
                  reason_json, reviewer, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(case_id, filename) DO UPDATE SET
                 manual_phase = excluded.manual_phase,
                 manual_view = excluded.manual_view,
                 manual_transform_json = excluded.manual_transform_json,
                 reason_json = excluded.reason_json,
                 reviewer = excluded.reviewer,
                 updated_at = excluded.updated_at""",
            (
                case_id,
                filename,
                manual_phase,
                manual_view,
                manual_transform_json,
                reason_json,
                reviewer,
                updated_at,
            ),
        )
    if not skip_dirty_mark:
        mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename=filename,
            changed_fields=_changed_fields(
                manual_phase=manual_phase,
                manual_view=manual_view,
                manual_transform_json=manual_transform_json,
            ),
        )


def delete_image_override(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    skip_dirty_mark: bool = False,
) -> None:
    conn.execute(
        "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
        (case_id, filename),
    )
    if not skip_dirty_mark:
        mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename=filename,
            changed_fields=_ALL_FIELDS,
        )
