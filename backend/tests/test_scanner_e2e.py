"""End-to-end tests for `scanner.scan()` — directory walk + DB upsert.

Builds a fake library tree under tmp_path with a mix of:
  - leaf dirs containing labeled images (standard_face)
  - leaf dirs containing only frame_xxx (fragment_only)
  - body keyword dirs (body short-circuit)
  - generated artefact dirs (must be skipped)

Then runs `scanner.scan(conn, [root])` and asserts the cases / scans rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backend import scanner


def _make_case(parent: Path, name: str, files: list[str]) -> Path:
    case_dir = parent / name
    case_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        (case_dir / f).write_bytes(b"x")
    return case_dir


def test_scan_inserts_new_cases(temp_db, tmp_path):
    from backend import db

    library = tmp_path / "library"
    _make_case(library / "客户A", "case-1", ["术前-正面.jpg", "术后-正面.jpg"])
    _make_case(library / "客户B", "case-2", ["frame_001.jpg", "frame_002.jpg"])

    with db.connect() as conn:
        result = scanner.scan(conn, roots=[library], mode="full")
    assert result["new_count"] == 2
    assert result["updated_count"] == 0
    assert result["skipped_count"] == 0

    with db.connect() as conn:
        rows = conn.execute("SELECT abs_path, category, customer_raw FROM cases ORDER BY abs_path").fetchall()
    assert len(rows) == 2
    by_path = {Path(r["abs_path"]).name: r for r in rows}
    assert by_path["case-1"]["category"] == "standard_face"
    assert by_path["case-1"]["customer_raw"] == "客户A"
    assert by_path["case-2"]["category"] == "fragment_only"
    assert by_path["case-2"]["customer_raw"] == "客户B"


def test_scan_incremental_skips_unchanged_cases(temp_db, tmp_path):
    """Second scan on identical mtime → skipped, not updated."""
    from backend import db

    library = tmp_path / "library"
    _make_case(library / "客户", "case", ["术前-正面.jpg", "术后-正面.jpg"])

    with db.connect() as conn:
        first = scanner.scan(conn, roots=[library], mode="full")
        second = scanner.scan(conn, roots=[library], mode="incremental")
    assert first["new_count"] == 1
    assert second["new_count"] == 0
    assert second["updated_count"] == 0
    assert second["skipped_count"] == 1


def test_scan_full_mode_updates_even_when_unchanged(temp_db, tmp_path):
    """`mode='full'` re-runs inference on every case regardless of mtime."""
    from backend import db

    library = tmp_path / "library"
    _make_case(library / "客户", "case", ["术前-正面.jpg", "术后-正面.jpg"])

    with db.connect() as conn:
        scanner.scan(conn, roots=[library], mode="full")
        second = scanner.scan(conn, roots=[library], mode="full")
    assert second["new_count"] == 0
    assert second["updated_count"] == 1
    assert second["skipped_count"] == 0


def test_scan_skips_generated_artefact_dirs(temp_db, tmp_path):
    """Files inside `.case-layout-output` must not become cases."""
    from backend import db

    library = tmp_path / "library"
    _make_case(library / ".case-layout-output", "leaked", ["x.jpg"])
    _make_case(library / "客户", "real-case", ["术前-正面.jpg", "术后-正面.jpg"])

    with db.connect() as conn:
        scanner.scan(conn, roots=[library], mode="full")
        rows = conn.execute("SELECT abs_path FROM cases").fetchall()
    paths = [Path(r["abs_path"]).name for r in rows]
    assert "real-case" in paths
    assert "leaked" not in paths


def test_scan_records_scan_row(temp_db, tmp_path):
    """Each scan() call inserts a `scans` row with started_at + completed_at +
    case_count.
    """
    from backend import db

    library = tmp_path / "library"
    _make_case(library / "客户", "case", ["术前-正面.jpg", "术后-正面.jpg"])

    with db.connect() as conn:
        result = scanner.scan(conn, roots=[library], mode="full")
        scan_row = conn.execute(
            "SELECT * FROM scans WHERE id = ?", (result["scan_id"],)
        ).fetchone()
    assert scan_row["started_at"] is not None
    assert scan_row["completed_at"] is not None
    assert scan_row["case_count"] == 1
    assert scan_row["mode"] == "full"


def test_scan_caps_image_files_meta_at_50(temp_db, tmp_path):
    """meta_json.image_files is capped at 50 entries to bound storage."""
    from backend import db

    library = tmp_path / "library"
    files = [f"img-{i:03d}.jpg" for i in range(80)]
    _make_case(library / "客户", "many-files", files)

    with db.connect() as conn:
        scanner.scan(conn, roots=[library], mode="full")
        row = conn.execute("SELECT meta_json FROM cases").fetchone()
    meta = json.loads(row["meta_json"])
    assert len(meta["image_files"]) == 50
    assert meta["image_count_total"] == 80


def test_scan_body_keyword_short_circuits_to_body_category(temp_db, tmp_path):
    """A path containing 颈纹 / 直角肩 etc. flips category to 'body' regardless
    of file labels.
    """
    from backend import db

    library = tmp_path / "library"
    _make_case(library / "客户" / "颈纹", "case", ["frame_001.jpg", "frame_002.jpg"])

    with db.connect() as conn:
        scanner.scan(conn, roots=[library], mode="full")
        row = conn.execute("SELECT category, template_tier FROM cases").fetchone()
    assert row["category"] == "body"
    assert row["template_tier"] == "body-dual-compare"


def test_scan_empty_root_inserts_no_cases_but_records_scan(temp_db, tmp_path):
    """Empty root → new_count=0, updated_count=0, but scans row still inserted."""
    from backend import db

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with db.connect() as conn:
        result = scanner.scan(conn, roots=[empty_root], mode="full")
        scan_count = conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    assert result["new_count"] == 0
    assert result["updated_count"] == 0
    assert scan_count == 1
