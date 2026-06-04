"""Dirty marker for cached best-pair candidates."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_best_pair_cache(
    conn,
    *,
    case_id: int,
    status: str = "ready",
    source_version: int = 0,
    candidates: list[dict] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO case_best_pairs
             (case_id, status, source_version, candidates_json, candidates_fingerprint, scanned_at, updated_at)
           VALUES (?, ?, ?, ?, 'fp-v1', ?, ?)""",
        (case_id, status, source_version, json.dumps(candidates or []), _now(), _now()),
    )


def _cache_row(conn, case_id: int):
    return conn.execute(
        "SELECT status, source_version FROM case_best_pairs WHERE case_id = ?",
        (case_id,),
    ).fetchone()


def test_dirty_when_manual_phase_changes_even_for_unrelated_filename(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.best_pair_dirty import mark_best_pair_dirty

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id, source_version=2, candidates=[{"before": "a.jpg", "after": "b.jpg"}])

        updated = mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename="other.jpg",
            changed_fields=("manual_phase",),
        )

        row = _cache_row(conn, case_id)
        assert updated is True
        assert row["status"] == "dirty"
        assert row["source_version"] == 3


def test_dirty_when_manual_view_changes_even_for_unrelated_filename(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.best_pair_dirty import mark_best_pair_dirty

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id, candidates=[{"before": "a.jpg", "after": "b.jpg"}])

        updated = mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename="elsewhere.jpg",
            changed_fields=("manual_view",),
        )

        row = _cache_row(conn, case_id)
        assert updated is True
        assert row["status"] == "dirty"
        assert row["source_version"] == 1


def test_dirty_when_transform_changes_cached_candidate(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.best_pair_dirty import mark_best_pair_dirty

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id, candidates=[{"before": "a.jpg", "after": "b.jpg"}])

        updated = mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename="a.jpg",
            changed_fields=("manual_transform_json",),
        )

        row = _cache_row(conn, case_id)
        assert updated is True
        assert row["status"] == "dirty"
        assert row["source_version"] == 1


def test_transform_only_on_unrelated_file_does_not_dirty_cache(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.best_pair_dirty import mark_best_pair_dirty

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id, source_version=7, candidates=[{"before": "a.jpg", "after": "b.jpg"}])

        updated = mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename="other.jpg",
            changed_fields=("manual_transform_json",),
        )

        row = _cache_row(conn, case_id)
        assert updated is False
        assert row["status"] == "ready"
        assert row["source_version"] == 7


def test_dirty_marker_is_lazy_when_cache_row_is_absent(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.best_pair_dirty import mark_best_pair_dirty

    case_id = seed_case()
    with db.connect() as conn:
        updated = mark_best_pair_dirty(
            conn,
            case_id=case_id,
            filename="a.jpg",
            changed_fields=("manual_phase", "manual_view"),
        )
        count = conn.execute("SELECT COUNT(*) AS n FROM case_best_pairs WHERE case_id = ?", (case_id,)).fetchone()

    assert updated is False
    assert count["n"] == 0


def test_empty_changed_fields_do_not_dirty_cache(temp_db: Path, seed_case) -> None:
    from backend import db
    from backend.services.best_pair_dirty import mark_best_pair_dirty

    case_id = seed_case()
    with db.connect() as conn:
        _seed_best_pair_cache(conn, case_id=case_id, source_version=2, candidates=[{"before": "a.jpg", "after": "b.jpg"}])

        updated = mark_best_pair_dirty(conn, case_id=case_id, filename="a.jpg", changed_fields=())

        row = _cache_row(conn, case_id)
        assert updated is False
        assert row["status"] == "ready"
        assert row["source_version"] == 2
