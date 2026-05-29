"""C0.5.1 schema tests for `ab_validation_units` table.

Covers:
  1. `init_schema()` creates the table with the canonical columns.
  2. Unique constraint on (workflow, base_artifact_sha256, candidate_artifact_sha256)
     rejects duplicate pairs but allows distinct workflows / candidates.
  3. `staleness_status` defaults to 'fresh' when omitted.
  4. SCHEMA_VERSION bump to 6 is recorded in `schema_versions`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import db as _db


CANONICAL_COLUMNS = {
    "id",
    "case_id",
    "workflow",
    "base_artifact_sha256",
    "candidate_artifact_sha256",
    "judge_result_id",
    "human_label",
    "generated_at",
    "source_manifest_hash",
    "staleness_status",
    "created_at",
}


@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "ab_units.db"
    monkeypatch.setattr(_db, "DB_PATH", db_path)
    _db.init_schema()
    return db_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_ab_validation_units_columns_match_canonical(tmp_db: Path):
    with sqlite3.connect(tmp_db) as conn:
        rows = conn.execute("PRAGMA table_info(ab_validation_units)").fetchall()
    columns = {row[1] for row in rows}
    assert columns == CANONICAL_COLUMNS


def test_schema_version_bumped_to_six(tmp_db: Path):
    with sqlite3.connect(tmp_db) as conn:
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE component = ?",
            (_db.SCHEMA_COMPONENT,),
        ).fetchone()
    assert row is not None
    assert row[0] == _db.SCHEMA_VERSION == 6


def test_unique_pair_rejects_duplicate(tmp_db: Path):
    with sqlite3.connect(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO ab_validation_units (
              workflow, base_artifact_sha256, candidate_artifact_sha256,
              generated_at, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("portrait_focal_enhance_v1", "sha256:aa", "sha256:bb", _now(), _now()),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ab_validation_units (
                  workflow, base_artifact_sha256, candidate_artifact_sha256,
                  generated_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ("portrait_focal_enhance_v1", "sha256:aa", "sha256:bb", _now(), _now()),
            )


def test_unique_pair_allows_distinct_candidate(tmp_db: Path):
    with sqlite3.connect(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO ab_validation_units (
              workflow, base_artifact_sha256, candidate_artifact_sha256,
              generated_at, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("portrait_focal_enhance_v1", "sha256:aa", "sha256:bb", _now(), _now()),
        )
        # Distinct candidate hash → second insert OK.
        conn.execute(
            """
            INSERT INTO ab_validation_units (
              workflow, base_artifact_sha256, candidate_artifact_sha256,
              generated_at, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("portrait_focal_enhance_v1", "sha256:aa", "sha256:cc", _now(), _now()),
        )
        count = conn.execute("SELECT COUNT(*) FROM ab_validation_units").fetchone()[0]
        assert count == 2


def test_staleness_default_is_fresh(tmp_db: Path):
    with sqlite3.connect(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO ab_validation_units (
              workflow, base_artifact_sha256, candidate_artifact_sha256,
              generated_at, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("portrait_focal_enhance_v1", "sha256:aa", "sha256:bb", _now(), _now()),
        )
        status = conn.execute(
            "SELECT staleness_status FROM ab_validation_units"
        ).fetchone()[0]
    assert status == "fresh"


def test_init_schema_is_idempotent(tmp_db: Path):
    # Re-running must not raise nor duplicate rows.
    _db.init_schema()
    with sqlite3.connect(tmp_db) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ab_validation_units'"
        ).fetchall()
    assert len(rows) == 1
