"""Best-pair schema migration coverage."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list('{table}')")}


def test_case_best_pair_tables_are_created(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        pair_cols = _columns(conn, "case_best_pairs")
        selection_cols = _columns(conn, "case_best_pair_selections")
        pair_indexes = _indexes(conn, "case_best_pairs")
        selection_indexes = _indexes(conn, "case_best_pair_selections")

    assert {
        "case_id",
        "status",
        "skipped_reason",
        "candidates_json",
        "candidates_fingerprint",
        "source_version",
        "scanned_at",
        "updated_at",
    } <= pair_cols
    assert {
        "id",
        "case_id",
        "before_filename",
        "after_filename",
        "delta_deg",
        "candidates_fingerprint",
        "candidates_fingerprint_snapshot",
        "before_override_before_json",
        "after_override_before_json",
        "view",
        "selected_at",
        "selected_by",
    } <= selection_cols
    assert "is_current" not in selection_cols
    assert "idx_case_best_pairs_status" in pair_indexes
    assert "idx_cbps_case_at" in selection_indexes


def test_render_jobs_best_pair_columns_are_created(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        cols = _columns(conn, "render_jobs")

    assert {"render_mode", "best_pair_selection_id", "candidates_fingerprint_snapshot"} <= cols


def test_render_mode_defaults_to_ai(temp_db: Path, seed_case) -> None:
    from backend import db

    case_id = seed_case()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO render_jobs
               (case_id, brand, template, status, enqueued_at, semantic_judge)
               VALUES (?, 'fumei', 'tri-compare', 'queued', '2026-05-10T00:00:00+08:00', 'auto')""",
            (case_id,),
        )
        row = conn.execute(
            "SELECT render_mode, best_pair_selection_id, candidates_fingerprint_snapshot FROM render_jobs"
        ).fetchone()

    assert row["render_mode"] == "ai"
    assert row["best_pair_selection_id"] is None
    assert row["candidates_fingerprint_snapshot"] is None


def test_best_pair_schema_helpers_are_idempotent(temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        db._ensure_best_pair_tables(conn)
        db._ensure_best_pair_tables(conn)
        db._ensure_render_job_best_pair_columns(conn)
        db._ensure_render_job_best_pair_columns(conn)

    with sqlite3.connect(temp_db) as conn:
        assert "case_best_pairs" in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"render_mode", "best_pair_selection_id", "candidates_fingerprint_snapshot"} <= _columns(
            conn, "render_jobs"
        )
