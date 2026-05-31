from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from PIL import Image

from backend import render_queue, source_selection


def test_layout_reselect_bias_penalizes_crop_touches_frame_and_ar_mismatch():
    risky = {
        "filename": "wide-cropped.jpg",
        "render_filename": "wide-cropped.jpg",
        "selection_score": 88,
        "selection_reasons": ["基础候选"],
        "quality_warnings": [],
        "risk_level": "ok",
        "crop_touches_frame": True,
        "source_aspect_ratio": 1.50,
    }
    safe = {
        "filename": "portrait-safe.jpg",
        "render_filename": "portrait-safe.jpg",
        "selection_score": 72,
        "selection_reasons": ["基础候选"],
        "quality_warnings": [],
        "risk_level": "ok",
        "source_aspect_ratio": 0.82,
    }
    visible_letterbox = {
        "filename": "phone-portrait.jpg",
        "render_filename": "phone-portrait.jpg",
        "selection_score": 72,
        "selection_reasons": ["基础候选"],
        "quality_warnings": [],
        "risk_level": "ok",
        "source_aspect_ratio": 0.75,
    }

    render_queue._apply_layout_reselect_bias(risky)
    render_queue._apply_layout_reselect_bias(safe)
    render_queue._apply_layout_reselect_bias(visible_letterbox)

    assert risky["selection_score"] < safe["selection_score"]
    assert risky["risk_level"] == "review"
    assert risky["layout_reselect_penalty"] == 52
    assert visible_letterbox["risk_level"] == "review"
    assert visible_letterbox["layout_reselect_penalty"] == 6
    assert visible_letterbox["source_aspect_ratio_delta"] == 0.0769
    assert {warning["code"] for warning in risky["quality_warnings"]} == {
        "layout_crop_touches_frame",
        "layout_aspect_ratio_mismatch",
    }
    assert source_selection.candidate_rank(safe) < source_selection.candidate_rank(risky)
    assert source_selection.candidate_rank(safe) < source_selection.candidate_rank(visible_letterbox)


def test_layout_operator_flags_surface_selected_layout_risks():
    flags = render_queue._layout_operator_flags(
        [
            {
                "case_id": 18,
                "filename": "术前5.jpg",
                "render_filename": "术前5.jpg",
                "view": "front",
                "phase": "before",
                "source_aspect_ratio": 0.75,
                "source_aspect_ratio_delta": 0.0769,
                "layout_reselect_penalty": 6,
                "quality_warnings": [{"code": "layout_aspect_ratio_mismatch", "severity": "review"}],
            },
            {
                "case_id": 18,
                "filename": "术后2.jpg",
                "view": "front",
                "phase": "after",
                "source_aspect_ratio": 0.82,
                "quality_warnings": [],
            },
        ]
    )

    assert flags == [
        {
            "view": "front",
            "phase": "before",
            "case_id": 18,
            "filename": "术前5.jpg",
            "render_filename": "术前5.jpg",
            "source_aspect_ratio": 0.75,
            "source_aspect_ratio_delta": 0.0769,
            "crop_touches_frame": None,
            "face_crop_touches_frame": None,
            "layout_reselect_penalty": 6,
            "warnings": [{"code": "layout_aspect_ratio_mismatch", "severity": "review"}],
            "recommended_action": "operator_retake_or_reselect_source_image",
        }
    ]


def test_latest_render_selection_metadata_backfills_unlabeled_phase_view(tmp_path: Path):
    manifest = {
        "render_selection_plan": {
            "slots": {
                "front": {
                    "before": {"filename": "001.jpg", "phase": "before", "view": "front"},
                    "after": {"filename": "009.jpg", "phase": "after", "view": "front"},
                }
            }
        }
    }
    manifest_path = tmp_path / "manifest.final.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE render_jobs (
            case_id INTEGER,
            status TEXT,
            meta_json TEXT,
            manifest_path TEXT,
            finished_at TEXT,
            enqueued_at TEXT,
            id INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO render_jobs
            (case_id, status, meta_json, manifest_path, finished_at, enqueued_at, id)
        VALUES
            (88, 'done', '{}', ?, '2026-05-31T00:00:00Z', '2026-05-31T00:00:00Z', 1)
        """,
        (str(manifest_path),),
    )

    metadata = render_queue._latest_render_selection_metadata_by_file(conn, 88)
    current = {"phase": None, "view_bucket": "front", "angle": "front"}
    merged = render_queue._selection_metadata_with_fallback(current, metadata["009.jpg"])

    assert merged["phase"] == "after"
    assert merged["view_bucket"] == "front"
    assert merged["selection_metadata_source"] == "latest_render_selection_plan"


def test_layout_quality_evidence_from_crop_ticket_marks_matching_files():
    controls = {
        "ticket_decisions": [
            {
                "reason_code": "crop_touches_frame",
                "evidence": {
                    "before": {
                        "filename": "front-before.jpg",
                        "crop_touches_frame": True,
                        "face_crop_touches_frame": True,
                        "crop_margin": 0.0,
                    }
                },
            }
        ]
    }
    evidence = render_queue._layout_quality_evidence_by_file(controls)
    candidate = {
        "filename": "front-before.jpg",
        "render_filename": "front-before.jpg",
    }

    render_queue._apply_layout_quality_evidence(candidate, evidence)

    assert candidate["crop_touches_frame"] is True
    assert candidate["face_crop_touches_frame"] is True
    assert candidate["crop_margin"] == 0.0
    assert candidate["layout_quality_evidence_source"] == "source_group_ticket"


def test_source_image_aspect_ratio_uses_real_image_dimensions(tmp_path: Path):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    Image.new("RGB", (300, 200), "white").save(case_dir / "wide.jpg")

    ratio = render_queue._source_image_aspect_ratio(str(case_dir), "wide.jpg")

    assert ratio == 1.5
