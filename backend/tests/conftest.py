"""Pytest scaffolding for case-workbench backend tests.

The production DB lives at `<repo>/case-workbench.db` (set as a module-level
constant in `backend.db.DB_PATH`). To keep tests hermetic we (a) point
`DB_PATH` at a placeholder tmp file BEFORE importing `backend.main` (whose
import triggers `db.init_schema()` and queue `recover()`), and (b) per-test
monkeypatch `DB_PATH` to a fresh `tmp_path/test.db` and re-run `init_schema`
so each test starts on an empty schema.

Routes call `db.connect()` / `db.get_conn()` lazily per request — those read
the *current* `backend.db.DB_PATH`, so the cached `app` object honors the
per-test patch transparently.
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

# --- Module-level placeholder DB ------------------------------------------
# Override DB_PATH BEFORE any other backend module sees the import-time
# init_schema()/recover() calls.
_PLACEHOLDER_DIR = Path(tempfile.mkdtemp(prefix="cwb-placeholder-"))
_PLACEHOLDER_DB = _PLACEHOLDER_DIR / "placeholder.db"

import backend.db as _db  # noqa: E402

_db.DB_PATH = _PLACEHOLDER_DB

from backend.main import app  # noqa: E402  (must come after DB_PATH override)
from fastapi.testclient import TestClient  # noqa: E402


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test fresh SQLite DB. Schema initialized via `db.init_schema()`."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("backend.db.DB_PATH", db_path)
    from backend import db

    db.init_schema()
    return db_path


@pytest.fixture
def client(temp_db: Path) -> TestClient:
    """FastAPI TestClient backed by the per-test DB."""
    return TestClient(app)


@pytest.fixture
def seed_case(temp_db: Path):
    """Factory: inserts a case (and its required scan row) and returns case_id.

    Usage: `case_id = seed_case(abs_path="/tmp/case-1", category="A")`
    """
    from backend import db

    def _insert(
        abs_path: str = "/tmp/case-1",
        category: str = "A",
        template_tier: str | None = "standard",
        customer_raw: str | None = "Alice",
        notes: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            scan_id = conn.execute(
                """INSERT INTO scans (started_at, completed_at, root_paths, case_count, mode)
                   VALUES (?, ?, ?, ?, ?)""",
                (now, now, "/tmp", 1, "test"),
            ).lastrowid
            cur = conn.execute(
                """INSERT INTO cases
                   (scan_id, abs_path, customer_raw, category, template_tier,
                    blocking_issues_json, last_modified, indexed_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scan_id, abs_path, customer_raw, category, template_tier,
                 "[]", now, now, notes),
            )
            return cur.lastrowid

    return _insert


@pytest.fixture
def insert_revision(temp_db: Path):
    """Factory: inserts a case_revisions row directly (for audit tests).

    Usage: `insert_revision(case_id=1, op="patch")`
    """
    from backend import db

    def _insert(
        case_id: int,
        op: str = "patch",
        before: dict | None = None,
        after: dict | None = None,
        actor: str = "user",
        source_route: str | None = None,
        undone_at: str | None = None,
    ) -> int:
        import json
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO case_revisions
                   (case_id, changed_at, actor, op, before_json, after_json,
                    source_route, undone_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (case_id, now, actor, op,
                 json.dumps(before or {}), json.dumps(after or {}),
                 source_route, undone_at),
            )
            return cur.lastrowid

    return _insert
