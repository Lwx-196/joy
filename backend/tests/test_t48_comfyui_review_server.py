"""T48 local click-review server for ComfyUI A/B decisions."""
from __future__ import annotations

import json
from pathlib import Path


def _packet_files(tmp_path: Path) -> tuple[Path, Path]:
    manifest = {
        "review_units": [
            {
                "ab_unit_id": "unit-1",
                "case_id": 101,
                "view": "front",
                "workflow": "background_cleanup_v1@conservative",
                "disagreement_type": "false_candidate_promotion",
                "prior_human_winner_role": "baseline",
                "vlm_winner_role": "candidate",
                "risk_assessment": "high_gate_risk",
                "visual_reaudit_note": "candidate 只有轻微降红，需二次复审。",
                "packet_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router@default",
                        "packet_relative_path": "assets/unit-1/baseline.jpg",
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:background_cleanup_v1@conservative",
                        "packet_relative_path": "assets/unit-1/candidate.png",
                    },
                ],
                "ready_for_review": True,
            }
        ]
    }
    draft = {
        "scope": "t46_comfyui_ab_review_decisions_draft_v1",
        "decisions": [
            {
                "ab_unit_id": "unit-1",
                "case_id": 101,
                "view": "front",
                "workflow": "background_cleanup_v1@conservative",
                "winner_role": None,
                "winner_variant": None,
                "reviewer": None,
                "review_note": None,
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    draft_path = tmp_path / "review_decisions_draft.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    return manifest_path, draft_path


def test_review_server_state_uses_packet_assets_without_fabricating_winners(tmp_path) -> None:
    from backend.scripts import comfyui_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    state = comfyui_review_server.build_review_state(manifest_path, draft_path)

    assert state["decision_count"] == 1
    assert state["filled_winner_count"] == 0
    assert state["units"][0]["ab_unit_id"] == "unit-1"
    assert state["units"][0]["roles"]["candidate"]["variant"] == "comfyui_local:background_cleanup_v1@conservative"
    assert state["units"][0]["decision"]["winner_role"] is None
    assert state["units"][0]["disagreement_type"] == "false_candidate_promotion"
    assert state["units"][0]["risk_assessment"] == "high_gate_risk"
    assert "轻微降红" in state["units"][0]["visual_reaudit_note"]


def test_review_server_saves_candidate_baseline_and_skip_decisions(tmp_path) -> None:
    from backend.scripts import comfyui_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    candidate = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "ab_unit_id": "unit-1",
            "winner_role": "candidate",
            "reviewer": "doctor-a",
            "review_note": "candidate wins",
        },
    )
    saved = json.loads(draft_path.read_text(encoding="utf-8"))["decisions"][0]

    assert candidate["decision"]["winner_role"] == "candidate"
    assert saved["winner_variant"] == "comfyui_local:background_cleanup_v1@conservative"
    assert saved["reviewer"] == "doctor-a"

    baseline = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"ab_unit_id": "unit-1", "winner_role": "baseline", "reviewer": "doctor-a"},
    )
    skipped = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"ab_unit_id": "unit-1", "winner_role": "skip", "reviewer": "doctor-a"},
    )
    saved = json.loads(draft_path.read_text(encoding="utf-8"))["decisions"][0]

    assert baseline["decision"]["winner_variant"] == "ps_model_router@default"
    assert skipped["decision"]["winner_role"] is None
    assert saved["winner_variant"] is None
    assert saved["reviewer"] == "doctor-a"


def test_review_server_saves_manual_review_decision_without_variant(tmp_path) -> None:
    from backend.scripts import comfyui_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    manual = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"ab_unit_id": "unit-1", "winner_role": "manual_review", "reviewer": "doctor-a"},
    )
    saved = json.loads(draft_path.read_text(encoding="utf-8"))["decisions"][0]

    assert manual["ok"] is True
    assert manual["decision"]["winner_role"] == "manual_review"
    assert manual["decision"]["review_status"] == "manual_review"
    state = comfyui_review_server.build_review_state(manifest_path, draft_path)
    assert state["filled_review_status_count"] == 1
    assert state["manual_review_count"] == 1
    assert state["filled_winner_count"] == 0
    assert saved["winner_variant"] is None


def test_review_server_rejects_missing_reviewer_unknown_unit_and_invalid_role(tmp_path) -> None:
    from backend.scripts import comfyui_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    missing_reviewer = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"ab_unit_id": "unit-1", "winner_role": "candidate", "reviewer": ""},
    )
    unknown = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"ab_unit_id": "missing", "winner_role": "candidate", "reviewer": "doctor-a"},
    )
    invalid = comfyui_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"ab_unit_id": "unit-1", "winner_role": "other", "reviewer": "doctor-a"},
    )

    assert missing_reviewer["ok"] is False
    assert missing_reviewer["error_code"] == "missing_reviewer"
    assert unknown["error_code"] == "unknown_ab_unit"
    assert invalid["error_code"] == "invalid_winner_role"
