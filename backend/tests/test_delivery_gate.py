"""Tests for backend.services.delivery_gate.

Each test seeds a minimal cases / render_jobs / render_quality triple, then
exercises DeliveryGate against the per-test SQLite (`temp_db` fixture).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import db
from backend.services.delivery_gate import (
    DeliverableItem,
    DeliveryGate,
    P0_THRESHOLD,
    P1_THRESHOLD,
    classify_tier,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_render(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    output_path: str,
    quality_score: float,
    can_publish: int = 1,
    quality_status: str = "done",
    blocking_count: int = 0,
    warning_count: int = 0,
) -> int:
    """Insert a render_jobs + render_quality pair, return render_job_id."""
    now = _now()
    cur = conn.execute(
        """INSERT INTO render_jobs
           (case_id, brand, template, status, enqueued_at, output_path)
           VALUES (?, 'brand-a', 'tri-compare', 'done', ?, ?)""",
        (case_id, now, output_path),
    )
    job_id = cur.lastrowid
    conn.execute(
        """INSERT INTO render_quality
           (render_job_id, quality_status, quality_score, can_publish,
            artifact_mode, blocking_count, warning_count, metrics_json,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, 'real_layout', ?, ?, '{}', ?, ?)""",
        (job_id, quality_status, quality_score, can_publish,
         blocking_count, warning_count, now, now),
    )
    conn.commit()
    return job_id


def _make_output_file(tmp_path: Path, case_id: int, exists: bool = True) -> str:
    """Create a placeholder final-board.jpg under tmp_path, return its abs path."""
    fp = tmp_path / f"case-{case_id}.jpg"
    if exists:
        fp.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return str(fp)


# ---------------------------------------------------------------------------
# tier classification
# ---------------------------------------------------------------------------


def test_classify_tier_boundaries() -> None:
    assert classify_tier(P0_THRESHOLD) == "P0"
    assert classify_tier(P0_THRESHOLD - 0.1) == "P1"
    assert classify_tier(P1_THRESHOLD) == "P1"
    assert classify_tier(P1_THRESHOLD - 0.1) == "P2"
    assert classify_tier(0.0) == "P2"
    assert classify_tier(100.0) == "P0"


def test_quality_gate_tier_classification(temp_db: Path, seed_case, tmp_path: Path) -> None:
    case_id = seed_case(abs_path="/cases/c-1")
    with db.connect() as conn:
        out = _make_output_file(tmp_path, case_id)
        j90 = _seed_render(conn, case_id=case_id, output_path=out, quality_score=90.0)
        j78 = _seed_render(conn, case_id=case_id, output_path=out, quality_score=78.0)
        j77 = _seed_render(
            conn, case_id=case_id, output_path=out, quality_score=77.9, can_publish=0
        )

        gate = DeliveryGate(conn)
        assert gate.quality_gate(j90)["tier"] == "P0"
        assert gate.quality_gate(j78)["tier"] == "P1"
        assert gate.quality_gate(j77)["tier"] == "P2"
        assert gate.quality_gate(j77)["can_publish"] is False


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_check_passes_for_existing_render(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-a/c-1")
    with db.connect() as conn:
        out = _make_output_file(tmp_path, case_id, exists=True)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=95.0)
        passed, reasons = DeliveryGate(conn).preflight_check(case_id)
    assert passed is True
    assert reasons == []


def test_preflight_check_fails_when_output_missing(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/c-2")
    with db.connect() as conn:
        out = _make_output_file(tmp_path, case_id, exists=False)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=95.0)
        passed, reasons = DeliveryGate(conn).preflight_check(case_id)
    assert passed is False
    assert any("missing" in r for r in reasons)


def test_preflight_check_fails_for_unknown_case(temp_db: Path) -> None:
    with db.connect() as conn:
        passed, reasons = DeliveryGate(conn).preflight_check(99999)
    assert passed is False
    assert any("not found" in r for r in reasons)


# ---------------------------------------------------------------------------
# list_deliverables — dedup + missing-file filter
# ---------------------------------------------------------------------------


def test_list_deliverables_dedup_keeps_highest_score(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-a/winner")
    with db.connect() as conn:
        out = _make_output_file(tmp_path, case_id)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=80.0)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=99.0)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=88.0)
        items = DeliveryGate(conn).list_deliverables()
    assert len(items) == 1
    assert items[0].quality_score == 99.0
    assert items[0].tier == "P0"
    assert items[0].customer == "customer-a"


def test_list_deliverables_skips_when_output_missing(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-b/ghost")
    with db.connect() as conn:
        out = _make_output_file(tmp_path, case_id, exists=False)
        _seed_render(conn, case_id=case_id, output_path=out, quality_score=95.0)
        items = DeliveryGate(conn).list_deliverables()
    assert items == []


def test_list_deliverables_filters_can_publish_zero(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-c/locked")
    with db.connect() as conn:
        out = _make_output_file(tmp_path, case_id)
        _seed_render(
            conn, case_id=case_id, output_path=out, quality_score=70.0, can_publish=0
        )
        items = DeliveryGate(conn).list_deliverables()
    assert items == []


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _item(case_id: int = 1, score: float = 95.0, customer: str = "alice") -> DeliverableItem:
    return DeliverableItem(
        case_id=case_id,
        customer=customer,
        case_name="case/name",
        category="standard_face",
        template_tier="bi",
        quality_score=score,
        quality_status="done",
        artifact_mode="real_layout",
        blocking_count=0,
        warning_count=0,
        source_path="/dummy/source.jpg",
        job_id=1,
    )


def test_export_creates_correct_filename_and_path(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    src.write_bytes(b"pretend-jpeg")
    item = _item()
    object.__setattr__(item, "source_path", str(src))  # frozen dataclass

    out_dir = tmp_path / "delivery"
    dest = DeliveryGate.export(item, out_dir, dry_run=False)

    expected = out_dir / "alice" / "case_name__score95_bi.jpg"
    assert dest == expected
    assert expected.is_file()
    assert expected.read_bytes() == src.read_bytes()


def test_export_dry_run_no_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.jpg"
    src.write_bytes(b"pretend-jpeg")
    item = _item()
    object.__setattr__(item, "source_path", str(src))

    out_dir = tmp_path / "delivery"
    dest = DeliveryGate.export(item, out_dir, dry_run=True)
    assert not dest.exists()
    assert not out_dir.exists()


# ---------------------------------------------------------------------------
# script-level smoke: schema parity with the legacy CSV header
# ---------------------------------------------------------------------------


def test_export_script_manifest_fields_snapshot() -> None:
    """Lock the manifest field order; if you change the header, change this."""
    from backend.scripts.export_delivery_batch import MANIFEST_FIELDS

    assert MANIFEST_FIELDS == [
        "case_id", "customer", "case_name", "category",
        "template_tier", "quality_score", "quality_status",
        "artifact_mode", "tier", "dest_path", "source_path",
    ]
