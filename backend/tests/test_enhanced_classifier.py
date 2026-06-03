"""Tests for enhanced multi-tier classification orchestrator."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import db
from backend.services.enhanced_classifier import (
    ALL_TIERS,
    ObservationRecord,
    fetch_case_observations,
    run_enhanced_classification,
    _run_path_rules_tier,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_group_and_observations(
    case_id: int,
    images: list[dict],
    root_path: str = "/tmp/test-group",
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    import uuid
    group_key = f"test-{uuid.uuid4().hex[:8]}"
    with db.connect() as conn:
        group_id = conn.execute(
            """INSERT INTO case_groups
               (group_key, title, root_path, customer_raw, case_ids_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (group_key, "Test Group", root_path, "test-customer",
             json.dumps([case_id]), "active", now, now),
        ).lastrowid
        for img in images:
            conn.execute(
                """INSERT INTO image_observations
                   (group_id, case_id, image_path, phase, body_part, view,
                    confidence, source, reasons_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    group_id,
                    case_id,
                    img.get("image_path", "img.jpg"),
                    img.get("phase", "unknown"),
                    img.get("body_part", "face"),
                    img.get("view", "front"),
                    img.get("confidence", 0.25),
                    img.get("source", "rules"),
                    json.dumps(img.get("reasons", [])),
                    now,
                    now,
                ),
            )
        return group_id


# ---------------------------------------------------------------------------
# fetch_case_observations
# ---------------------------------------------------------------------------

class TestFetchCaseObservations:
    def test_empty_case(self, seed_case):
        case_id = seed_case()
        with db.connect() as conn:
            obs = fetch_case_observations(conn, case_id)
        assert obs == []

    def test_returns_observations(self, seed_case):
        case_id = seed_case(abs_path="/tmp/术前test")
        _seed_group_and_observations(case_id, [
            {"image_path": "术前/front.jpg", "phase": "before", "confidence": 0.92},
            {"image_path": "术后/front.jpg", "phase": "after", "confidence": 0.92},
        ])
        with db.connect() as conn:
            obs = fetch_case_observations(conn, case_id)
        assert len(obs) == 2
        assert all(isinstance(o, ObservationRecord) for o in obs)
        assert obs[0].image_path == "术前/front.jpg"
        assert obs[0].phase == "before"
        assert obs[1].image_path == "术后/front.jpg"
        assert obs[1].phase == "after"


# ---------------------------------------------------------------------------
# _run_path_rules_tier
# ---------------------------------------------------------------------------

class TestRunPathRulesTier:
    def test_detects_phase_from_path(self):
        obs = [
            ObservationRecord(1, 1, 1, "术前/front.jpg", Path("/tmp/case/术前/front.jpg"), "unknown", 0.25, "rules"),
            ObservationRecord(2, 1, 1, "术后/side.jpg", Path("/tmp/case/术后/side.jpg"), "unknown", 0.25, "rules"),
            ObservationRecord(3, 1, 1, "other.jpg", Path("/tmp/case/other.jpg"), "unknown", 0.25, "rules"),
        ]
        signals = _run_path_rules_tier(obs)
        assert signals["术前/front.jpg"]["phase"] == "before"
        assert signals["术前/front.jpg"]["confidence"] == 0.92
        assert signals["术后/side.jpg"]["phase"] == "after"
        assert signals["other.jpg"]["phase"] == "unknown"

    def test_filename_takes_precedence_over_parent_dir(self):
        """When parent dir contains both 术前 and 术后, filename wins."""
        obs = [
            ObservationRecord(
                1, 1, 1,
                "2025.8.28术前术中术后即刻/术后-正面.jpeg",
                Path("/tmp/case/2025.8.28术前术中术后即刻/术后-正面.jpeg"),
                "unknown", 0.25, "rules",
            ),
            ObservationRecord(
                2, 1, 1,
                "2025.8.28术前术中术后即刻/术前-正面.jpeg",
                Path("/tmp/case/2025.8.28术前术中术后即刻/术前-正面.jpeg"),
                "unknown", 0.25, "rules",
            ),
            ObservationRecord(
                3, 1, 1,
                "2025.8.28术前术中术后即刻/术后即刻5.jpeg",
                Path("/tmp/case/2025.8.28术前术中术后即刻/术后即刻5.jpeg"),
                "unknown", 0.25, "rules",
            ),
        ]
        signals = _run_path_rules_tier(obs)
        assert signals["2025.8.28术前术中术后即刻/术后-正面.jpeg"]["phase"] == "after"
        assert signals["2025.8.28术前术中术后即刻/术前-正面.jpeg"]["phase"] == "before"
        assert signals["2025.8.28术前术中术后即刻/术后即刻5.jpeg"]["phase"] == "after"


# ---------------------------------------------------------------------------
# run_enhanced_classification (dry-run = path_rules + exif only)
# ---------------------------------------------------------------------------

class TestRunEnhancedClassification:
    def test_no_observations(self, seed_case):
        case_id = seed_case()
        with db.connect() as conn:
            result = run_enhanced_classification(conn, case_id, mode="dry-run")
        assert result["case_id"] == case_id
        assert result["image_count"] == 0
        assert result["results"] == []
        assert result["summary"]["total"] == 0

    def test_dry_run_excludes_vlm_tiers(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "术前/front.jpg", "phase": "before", "confidence": 0.92},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(conn, case_id, mode="dry-run")
        assert "vlm_single" not in result["tiers_enabled"]
        assert "vlm_pair" not in result["tiers_enabled"]

    def test_path_rules_fusion_before(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "术前/front.jpg", "phase": "unknown", "confidence": 0.25},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(
                conn, case_id, mode="dry-run", tiers=["path_rules"],
            )
        assert len(result["results"]) == 1
        img_result = result["results"][0]
        assert img_result["fusion"]["phase"] == "before"
        assert img_result["fusion"]["confidence"] >= 0.92
        assert img_result["tier_signals"]["path_rules"]["phase"] == "before"

    def test_path_rules_fusion_unknown_held(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "photo123.jpg", "phase": "unknown", "confidence": 0.25},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(
                conn, case_id, mode="dry-run", tiers=["path_rules"],
            )
        img_result = result["results"][0]
        assert img_result["fusion"]["phase"] == "unknown"
        assert img_result["tier_signals"]["path_rules"]["phase"] == "unknown"

    def test_summary_counts(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "术前/a.jpg"},
            {"image_path": "术后/b.jpg"},
            {"image_path": "unknown.jpg"},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(
                conn, case_id, mode="dry-run", tiers=["path_rules"],
            )
        assert result["summary"]["total"] == 3
        assert result["summary"]["before"] == 1
        assert result["summary"]["after"] == 1
        assert result["summary"]["unknown_held"] == 1

    def test_tier_selection(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "术前/a.jpg"},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(
                conn, case_id, mode="dry-run", tiers=["path_rules"],
            )
        assert result["tiers_enabled"] == ["path_rules"]

    def test_invalid_tiers_filtered(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "術前/a.jpg"},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(
                conn, case_id, mode="dry-run", tiers=["bogus", "path_rules"],
            )
        assert "bogus" not in result["tiers_enabled"]
        assert "path_rules" in result["tiers_enabled"]

    def test_result_includes_current_phase(self, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "術前/a.jpg", "phase": "after", "confidence": 0.80},
        ])
        with db.connect() as conn:
            result = run_enhanced_classification(
                conn, case_id, mode="dry-run", tiers=["path_rules"],
            )
        assert result["results"][0]["current_phase"] == "after"
        assert result["results"][0]["current_confidence"] == 0.80


# ---------------------------------------------------------------------------
# Route integration
# ---------------------------------------------------------------------------

class TestClassificationRoutes:
    def test_enhanced_classify_case_not_found(self, client):
        resp = client.post("/api/classification/99999/enhanced", json={"mode": "dry-run"})
        assert resp.status_code == 404

    def test_enhanced_classify_dry_run(self, client, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "術前/front.jpg", "phase": "before", "confidence": 0.92},
        ])
        resp = client.post(
            f"/api/classification/{case_id}/enhanced",
            json={"mode": "dry-run", "tiers": ["path_rules"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["case_id"] == case_id
        assert data["mode"] == "dry-run"
        assert len(data["results"]) == 1

    def test_enhanced_classify_invalid_mode(self, client, seed_case):
        case_id = seed_case()
        resp = client.post(
            f"/api/classification/{case_id}/enhanced",
            json={"mode": "invalid"},
        )
        assert resp.status_code == 400

    def test_signals_endpoint(self, client, seed_case):
        case_id = seed_case()
        _seed_group_and_observations(case_id, [
            {"image_path": "front.jpg", "phase": "before", "confidence": 0.92},
            {"image_path": "side.jpg", "phase": "after", "confidence": 0.85},
        ])
        resp = client.get(f"/api/classification/{case_id}/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert data["case_id"] == case_id
        assert data["image_count"] == 2
        assert len(data["observations"]) == 2

    def test_signals_case_not_found(self, client):
        resp = client.get("/api/classification/99999/signals")
        assert resp.status_code == 404
