"""Schema migration hardening tests."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path


def _create_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE scans (
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at    TIMESTAMP NOT NULL,
              completed_at  TIMESTAMP,
              root_paths    TEXT NOT NULL,
              case_count    INTEGER,
              mode          TEXT NOT NULL
            );

            CREATE TABLE customers (
              id              INTEGER PRIMARY KEY AUTOINCREMENT,
              canonical_name  TEXT NOT NULL UNIQUE,
              aliases_json    TEXT NOT NULL DEFAULT '[]',
              notes           TEXT,
              created_at      TIMESTAMP NOT NULL,
              updated_at      TIMESTAMP NOT NULL
            );

            CREATE TABLE cases (
              id                    INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_id               INTEGER NOT NULL REFERENCES scans(id),
              abs_path              TEXT NOT NULL UNIQUE,
              customer_raw          TEXT,
              customer_id           INTEGER REFERENCES customers(id),
              category              TEXT NOT NULL,
              template_tier         TEXT,
              blocking_issues_json  TEXT,
              pose_delta_max        REAL,
              sharp_ratio_min       REAL,
              source_count          INTEGER,
              labeled_count         INTEGER,
              meta_json             TEXT,
              last_modified         TIMESTAMP NOT NULL,
              indexed_at            TIMESTAMP NOT NULL
            );

            CREATE TABLE case_image_overrides (
              case_id       INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
              filename      TEXT NOT NULL,
              manual_phase  TEXT,
              manual_view   TEXT,
              updated_at    TIMESTAMP NOT NULL,
              PRIMARY KEY (case_id, filename)
            );

            CREATE TABLE simulation_jobs (
              id                INTEGER PRIMARY KEY AUTOINCREMENT,
              group_id          INTEGER REFERENCES case_groups(id) ON DELETE SET NULL,
              case_id           INTEGER REFERENCES cases(id) ON DELETE SET NULL,
              status            TEXT NOT NULL,
              focus_targets_json TEXT NOT NULL DEFAULT '[]',
              policy_json       TEXT NOT NULL DEFAULT '{}',
              model_plan_json   TEXT NOT NULL DEFAULT '{}',
              input_refs_json   TEXT NOT NULL DEFAULT '[]',
              output_refs_json  TEXT NOT NULL DEFAULT '[]',
              watermarked       INTEGER NOT NULL DEFAULT 1,
              audit_json        TEXT NOT NULL DEFAULT '{}',
              error_message     TEXT,
              created_at        TIMESTAMP NOT NULL,
              updated_at        TIMESTAMP NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_init_schema_records_current_schema_version(temp_db):
    from backend import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE component = 'main'"
        ).fetchone()

    assert row is not None
    assert row["version"] == db.SCHEMA_VERSION


def test_concurrent_init_schema_two_processes_no_missing_column(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "legacy-concurrent.db"
    _create_legacy_db(db_path)
    code = textwrap.dedent(
        """
        from backend import db
        db.init_schema()
        with db.connect() as conn:
            required = {
                "cases": ["skill_image_metadata_json", "trashed_at"],
                "case_image_overrides": ["manual_transform_json", "reason_json", "reviewer"],
                "case_best_pair_selections": ["selection_reason"],
                "simulation_jobs": ["review_status", "can_publish"],
                "render_jobs": ["recovery_token", "recovery_claimed_at"],
                "upgrade_jobs": ["recovery_token", "recovery_claimed_at"],
                "ab_feedback": ["render_job_id", "verdict", "hard_defect_tags_json", "reviewer"],
            }
            missing = []
            for table, cols in required.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                missing.extend(f"{table}.{col}" for col in cols if col not in existing)
            version = conn.execute("SELECT version FROM schema_versions WHERE component = 'main'").fetchone()
            assert not missing, missing
            assert version is not None and version["version"] == db.SCHEMA_VERSION
        print("ok")
        """
    )
    env = {**os.environ, "CASE_WORKBENCH_DB_PATH": str(db_path)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=repo_root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [proc.communicate(timeout=15) + (proc.returncode,) for proc in procs]

    assert [(stdout, stderr, code) for stdout, stderr, code in results if code != 0] == []

    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute(
            "SELECT version FROM schema_versions WHERE component = 'main'"
        ).fetchone()
    finally:
        conn.close()
    assert version is not None
