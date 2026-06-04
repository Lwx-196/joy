"""Unified image override writer coverage for best-pair dirty state."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_best_pair_cache(conn, *, case_id: int, source_version: int = 0) -> None:
    conn.execute(
        """INSERT INTO case_best_pairs
             (case_id, status, source_version, candidates_json, candidates_fingerprint, scanned_at, updated_at)
           VALUES (?, 'ready', ?, ?, 'fp-v1', ?, ?)""",
        (
            case_id,
            source_version,
            json.dumps([{"before": "a.jpg", "after": "b.jpg"}]),
            _now(),
            _now(),
        ),
    )


def _override_row(conn, case_id: int, filename: str):
    return conn.execute(
        """SELECT manual_phase, manual_view, manual_transform_json, updated_at
           FROM case_image_overrides WHERE case_id = ? AND filename = ?""",
        (case_id, filename),
    ).fetchone()


def _cache_row(conn, case_id: int):
    return conn.execute(
        "SELECT status, source_version FROM case_best_pairs WHERE case_id = ?",
        (case_id,),
    ).fetchone()


def test_write_image_override_inserts_and_marks_dirty(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.image_override_writer import write_image_override

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id)
        write_image_override(
            conn,
            case_id=case_id,
            filename="a.jpg",
            manual_phase="before",
            manual_view="front",
            manual_transform_json=None,
            updated_at=_now(),
        )

        row = _override_row(conn, case_id, "a.jpg")
        cache = _cache_row(conn, case_id)
        assert row["manual_phase"] == "before"
        assert row["manual_view"] == "front"
        assert row["manual_transform_json"] is None
        assert cache["status"] == "dirty"
        assert cache["source_version"] == 1


def test_write_image_override_preserves_dirty_when_skipped(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.image_override_writer import write_image_override

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id, source_version=5)
        write_image_override(
            conn,
            case_id=case_id,
            filename="a.jpg",
            manual_phase="before",
            manual_view="front",
            manual_transform_json=None,
            updated_at=_now(),
            skip_dirty_mark=True,
        )

        cache = _cache_row(conn, case_id)
        assert cache["status"] == "ready"
        assert cache["source_version"] == 5


def test_write_transform_json_for_cached_candidate_marks_dirty(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.image_override_writer import write_image_override

    case_id = seed_case()
    transform = json.dumps({"offset": {"x": 4, "y": -2}})
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id)
        write_image_override(
            conn,
            case_id=case_id,
            filename="a.jpg",
            manual_phase="before",
            manual_view="front",
            manual_transform_json=transform,
            updated_at=_now(),
        )

        row = _override_row(conn, case_id, "a.jpg")
        cache = _cache_row(conn, case_id)
        assert row["manual_transform_json"] == transform
        assert cache["status"] == "dirty"


def test_all_null_write_deletes_override_and_marks_dirty(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.image_override_writer import write_image_override

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id)
        write_image_override(
            conn,
            case_id=case_id,
            filename="a.jpg",
            manual_phase="before",
            manual_view="front",
            manual_transform_json=None,
            updated_at=_now(),
            skip_dirty_mark=True,
        )

        write_image_override(
            conn,
            case_id=case_id,
            filename="a.jpg",
            manual_phase=None,
            manual_view=None,
            manual_transform_json=None,
            updated_at=_now(),
        )

        assert _override_row(conn, case_id, "a.jpg") is None
        assert _cache_row(conn, case_id)["status"] == "dirty"


def test_delete_image_override_marks_dirty(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.image_override_writer import delete_image_override, write_image_override

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id)
        write_image_override(
            conn,
            case_id=case_id,
            filename="a.jpg",
            manual_phase="before",
            manual_view="front",
            manual_transform_json=None,
            updated_at=_now(),
            skip_dirty_mark=True,
        )

        delete_image_override(conn, case_id=case_id, filename="a.jpg")

        assert _override_row(conn, case_id, "a.jpg") is None
        assert _cache_row(conn, case_id)["status"] == "dirty"
