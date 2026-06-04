"""Concurrency hardening tests for SQLite-backed render completion."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_running_job(conn: sqlite3.Connection, case_id: int) -> int:
    return conn.execute(
        """
        INSERT INTO render_jobs
          (case_id, brand, template, status, enqueued_at, started_at, semantic_judge)
        VALUES (?, 'fumei', 'tri-compare', 'running', ?, ?, 'auto')
        """,
        (case_id, _now_iso(), _now_iso()),
    ).lastrowid


def test_connect_enables_wal_and_busy_timeout(temp_db):
    from backend import db

    with db.connect() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout >= 5000


def test_concurrent_finish_result_does_not_drop_render_quality(temp_db, seed_case, tmp_path, monkeypatch):
    """A long-lived read transaction must not block render workers from finishing.

    In SQLite rollback-journal mode a reader can hold a shared lock that makes
    worker commits wait. WAL mode should allow these two render completions to
    commit while the reader is still open, preserving both render_quality rows.
    """
    from backend import db
    from backend.render_queue import RenderQueue

    monkeypatch.setattr("backend.render_queue._code_version_summary", lambda: {"commit": "test", "dirty": False})

    case_id = seed_case(abs_path="/tmp/case-db-concurrency")
    with db.connect() as conn:
        job_ids = [_insert_running_job(conn, case_id), _insert_running_job(conn, case_id)]

    output_paths: list[Path] = []
    for index, job_id in enumerate(job_ids, start=1):
        path = tmp_path / f"job-{job_id}-final-board-{index}.jpg"
        path.write_bytes(b"jpeg")
        output_paths.append(path)

    reader = sqlite3.connect(temp_db, timeout=0.1, check_same_thread=False)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM render_jobs").fetchone()

    queue = RenderQueue()
    errors: list[BaseException] = []
    start = threading.Barrier(3)

    def finish(job_id: int, output_path: Path) -> None:
        try:
            start.wait(timeout=2)
            queue._finish_result(
                job_id,
                case_id=case_id,
                batch_id="batch-concurrency",
                brand="fumei",
                template="tri-compare",
                result={
                    "output_path": str(output_path),
                    "manifest_path": None,
                    "status": "ok",
                    "blocking_issue_count": 0,
                    "warning_count": 0,
                    "warnings": [],
                    "blocking_issues": [],
                    "ai_usage": {},
                },
            )
        except BaseException as exc:  # noqa: BLE001 - test records worker-thread failures.
            errors.append(exc)

    threads = [
        threading.Thread(target=finish, args=(job_id, output_path), daemon=True)
        for job_id, output_path in zip(job_ids, output_paths, strict=True)
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    time.sleep(0.75)
    still_blocked = [thread for thread in threads if thread.is_alive()]

    reader.rollback()
    reader.close()
    for thread in threads:
        thread.join(timeout=3)

    assert still_blocked == []
    assert errors == []

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT j.id, j.status, rq.quality_status, rq.can_publish
            FROM render_jobs j
            LEFT JOIN render_quality rq ON rq.render_job_id = j.id
            WHERE j.id IN (?, ?)
            ORDER BY j.id
            """,
            tuple(job_ids),
        ).fetchall()

    assert [row["status"] for row in rows] == ["done", "done"]
    assert [row["quality_status"] for row in rows] == ["done", "done"]
    assert [row["can_publish"] for row in rows] == [1, 1]
