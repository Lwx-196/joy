"""Tests for `render_quality._split_transient_errors` and its integration
into `evaluate_render_result` / `persist_render_quality` / `quality_row_to_dict`.

The transient-error split keeps upstream API hiccups (HTTP 403/429, quota
exhaustion, rate-limit) out of the user-facing `display_warnings` list,
without changing `can_publish` semantics.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend import render_quality as rq


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_result(warnings: list[str], *, output_path: str | None = None) -> dict:
    return {
        "status": "ok",
        "blocking_issue_count": 0,
        "warning_count": len(warnings),
        "output_path": output_path,
        "warnings": warnings,
        "ai_usage": {},
    }


# ---------------------------------------------------------------------------
# Splitter unit tests
# ---------------------------------------------------------------------------


def test_429_extracted_to_transient() -> None:
    kept, transient = rq._split_transient_errors(
        ["HTTP 429 too many requests", "blur detected on slot front"]
    )
    assert transient == ["HTTP 429 too many requests"]
    assert kept == ["blur detected on slot front"]


def test_403_with_quota_keyword_extracted() -> None:
    kept, transient = rq._split_transient_errors(
        ["403 Forbidden: quota exceeded for project X"]
    )
    assert transient == ["403 Forbidden: quota exceeded for project X"]
    assert kept == []


def test_rate_limit_keyword_extracted() -> None:
    kept, transient = rq._split_transient_errors(
        ["Vertex AI rate-limit hit", "OpenAI api error: timeout"]
    )
    assert len(transient) == 2
    assert kept == []


def test_normal_warning_stays_in_display_warnings() -> None:
    kept, transient = rq._split_transient_errors(
        ["pose delta exceeds threshold", "background fill missing"]
    )
    assert transient == []
    assert kept == ["pose delta exceeds threshold", "background fill missing"]


def test_empty_input_returns_empty_lists() -> None:
    kept, transient = rq._split_transient_errors([])
    assert kept == []
    assert transient == []


def test_bare_status_number_does_not_false_positive() -> None:
    """A message containing '403' as part of unrelated text (e.g. case id) must
    NOT be classified as transient. The regex requires status-code anchors."""
    kept, transient = rq._split_transient_errors([
        "case 403 pose ok",
        "score is 429 out of 1000",
        "blur on 403rd frame",
    ])
    assert transient == []
    assert len(kept) == 3


# ---------------------------------------------------------------------------
# evaluate_render_result integration
# ---------------------------------------------------------------------------


def test_evaluate_render_result_splits_transient_from_display(tmp_path: Path) -> None:
    out = tmp_path / "final-board.jpg"
    out.write_bytes(b"\xff\xd8jpeg")
    result = _make_result(
        ["HTTP 429 quota exhausted", "blur on slot front"],
        output_path=str(out),
    )
    envelope = rq.evaluate_render_result(result)
    metrics = envelope["metrics"]
    assert metrics["system_transient_errors"] == ["HTTP 429 quota exhausted"]
    assert "429" not in " ".join(metrics["display_warnings"])
    assert "blur on slot front" in metrics["display_warnings"]


def test_can_publish_unchanged_when_only_transient_errors(tmp_path: Path) -> None:
    """A pure transient-error render with status=ok / no blocking / no warnings
    must still be publishable — the API hiccup is unrelated to artifact quality."""
    out = tmp_path / "final-board.jpg"
    out.write_bytes(b"\xff\xd8jpeg")
    result = _make_result(
        ["429 rate limit"], output_path=str(out),
    )
    # warning_count must equal 0 to clear the actionable threshold
    result["warning_count"] = 0
    envelope = rq.evaluate_render_result(result)
    # Score path: status=ok, no blocking, no actionable warnings → done
    assert envelope["quality_status"] == "done"
    assert envelope["can_publish"] is True


# ---------------------------------------------------------------------------
# persist + read-back: PRAGMA fallback when optional column missing
# ---------------------------------------------------------------------------


def _bootstrap_minimal_schema(conn: sqlite3.Connection, *, with_optional: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE render_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              case_id INTEGER NOT NULL,
              brand TEXT NOT NULL,
              template TEXT NOT NULL DEFAULT 'tri-compare',
              status TEXT NOT NULL,
              enqueued_at TIMESTAMP NOT NULL,
              output_path TEXT
           )"""
    )
    extra_col = ", system_transient_errors_json TEXT" if with_optional else ""
    conn.execute(
        f"""CREATE TABLE render_quality (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              render_job_id INTEGER NOT NULL UNIQUE REFERENCES render_jobs(id),
              quality_status TEXT NOT NULL,
              quality_score REAL NOT NULL DEFAULT 0,
              can_publish INTEGER NOT NULL DEFAULT 0,
              artifact_mode TEXT NOT NULL DEFAULT 'real_layout',
              manifest_status TEXT,
              blocking_count INTEGER NOT NULL DEFAULT 0,
              warning_count INTEGER NOT NULL DEFAULT 0,
              metrics_json TEXT NOT NULL DEFAULT '{{}}',
              review_verdict TEXT,
              reviewer TEXT,
              review_note TEXT,
              reviewed_at TIMESTAMP,
              created_at TIMESTAMP NOT NULL,
              updated_at TIMESTAMP NOT NULL
              {extra_col}
           )"""
    )


def _seed_job(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO render_jobs (case_id, brand, status, enqueued_at) VALUES (1, 'b', 'done', '2026-01-01')"
    )
    conn.commit()
    return cur.lastrowid


def test_persist_skips_when_column_absent(tmp_path: Path) -> None:
    """`persist_render_quality` must not raise when the optional
    `system_transient_errors_json` column is missing — verifies the PRAGMA
    feature-flag branch."""
    db_path = tmp_path / "absent.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _bootstrap_minimal_schema(conn, with_optional=False)
    job_id = _seed_job(conn)

    quality = {
        "quality_status": "done",
        "quality_score": 95.0,
        "can_publish": True,
        "artifact_mode": "real_layout",
        "manifest_status": "ok",
        "blocking_count": 0,
        "warning_count": 0,
        "metrics": {"system_transient_errors": ["429 quota"]},
    }
    rq.persist_render_quality(conn, job_id, quality)
    conn.commit()

    # Round-trip read-back: column doesn't exist, fall back to metrics_json
    row = conn.execute(
        "SELECT * FROM render_quality WHERE render_job_id = ?", (job_id,)
    ).fetchone()
    parsed = rq.quality_row_to_dict(row)
    assert parsed is not None
    assert parsed["system_transient_errors"] == ["429 quota"]


def test_persist_writes_when_column_present(tmp_path: Path) -> None:
    db_path = tmp_path / "present.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _bootstrap_minimal_schema(conn, with_optional=True)
    job_id = _seed_job(conn)

    quality = {
        "quality_status": "done",
        "quality_score": 92.0,
        "can_publish": True,
        "artifact_mode": "real_layout",
        "manifest_status": "ok",
        "blocking_count": 0,
        "warning_count": 0,
        "metrics": {"system_transient_errors": ["403 forbidden"]},
    }
    rq.persist_render_quality(conn, job_id, quality)
    conn.commit()

    raw = conn.execute(
        "SELECT system_transient_errors_json FROM render_quality WHERE render_job_id = ?",
        (job_id,),
    ).fetchone()[0]
    assert json.loads(raw) == ["403 forbidden"]
