"""T86 local click-review server for T80 crop/slot decisions."""
from __future__ import annotations

import json
from pathlib import Path


def _packet_files(tmp_path: Path) -> tuple[Path, Path]:
    manifest = {
        "crop_allowed_actions": [
            "needs_reselect_pair",
            "needs_replace_source",
            "defer_no_safe_alternative",
        ],
        "slot_allowed_actions": [
            "manual_phase_view_override",
            "restore_or_add_source_photos",
            "bind_or_rescan_real_source",
            "template_policy_review",
            "defer",
        ],
        "crop_review_units": [
            {
                "unit_id": "crop-case4",
                "case_id": 4,
                "ticket_ids": [7],
                "recommended_action": "needs_replace_source",
                "allowed_actions": ["needs_replace_source", "defer_no_safe_alternative"],
                "current_pair": {
                    "before": {"filename": "front-before.jpg", "asset_relative_path": "assets/crop-case4/before.jpg"},
                    "after": {"filename": "front-after.jpg", "asset_relative_path": "assets/crop-case4/after.jpg"},
                },
                "candidate_assets": {
                    "before": [{"filename": "front-before-2.jpg", "asset_relative_path": "assets/crop-case4/before-2.jpg"}],
                    "after": [{"filename": "front-after-2.jpg", "asset_relative_path": "assets/crop-case4/after-2.jpg"}],
                },
                "blocks_render": True,
                "blocks_publish": True,
            }
        ],
        "slot_fill_units": [
            {
                "unit_id": "slot-case8",
                "case_id": 8,
                "ticket_ids": [10],
                "recommended_action": "manual_phase_view_override",
                "allowed_actions": ["manual_phase_view_override", "defer"],
                "missing_slots": [{"view": "front", "missing": ["after"]}],
                "source_sample_assets": [{"filename": "sample.jpg", "asset_relative_path": "assets/slot-case8/sample.jpg"}],
                "blocks_render": True,
                "blocks_publish": True,
            }
        ],
    }
    draft = {
        "scope": "t80_crop_slot_review_decisions_draft_v1",
        "crop_decisions": [
            {
                "unit_id": "crop-case4",
                "case_id": 4,
                "ticket_ids": [7],
                "reviewer": None,
                "action": None,
                "note": None,
                "selected_before": None,
                "selected_after": None,
            }
        ],
        "slot_decisions": [
            {
                "unit_id": "slot-case8",
                "case_id": 8,
                "ticket_ids": [10],
                "reviewer": None,
                "action": None,
                "note": None,
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    draft_path = tmp_path / "review_decisions_draft.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return manifest_path, draft_path


def test_t86_review_state_uses_t80_manifest_without_fabricating_decisions(tmp_path: Path) -> None:
    from backend.scripts import t86_crop_slot_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    state = t86_crop_slot_review_server.build_review_state(manifest_path, draft_path)

    assert state["unit_count"] == 2
    assert state["filled_action_count"] == 0
    assert state["units"][0]["kind"] == "crop"
    assert state["units"][1]["kind"] == "slot"
    assert state["units"][0]["decision"]["action"] is None
    assert "accept_current_pair" in state["units"][0]["allowed_actions"]


def test_t86_review_server_saves_crop_and_slot_decisions(tmp_path: Path) -> None:
    from backend.scripts import t86_crop_slot_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    crop = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "unit_id": "crop-case4",
            "kind": "crop",
            "reviewer": "operator-a",
            "action": "needs_replace_source",
            "note": "贴边，需替换源图",
            "selected_before": "assets/crop-case4/before-2.jpg",
            "selected_after": "assets/crop-case4/after-2.jpg",
        },
    )
    slot = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "unit_id": "slot-case8",
            "kind": "slot",
            "reviewer": "operator-a",
            "action": "manual_phase_view_override",
            "note": "按人工判断补 phase/view",
        },
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))

    assert crop["ok"] is True
    assert slot["ok"] is True
    assert draft["crop_decisions"][0]["reviewer"] == "operator-a"
    assert draft["crop_decisions"][0]["action"] == "needs_replace_source"
    assert draft["crop_decisions"][0]["selected_before"] == "assets/crop-case4/before-2.jpg"
    assert draft["crop_decisions"][0]["selected_after"] == "assets/crop-case4/after-2.jpg"
    assert draft["slot_decisions"][0]["action"] == "manual_phase_view_override"
    assert draft["slot_decisions"][0]["reviewed_at"]


def test_t86_review_server_saves_accept_current_pair_for_legacy_crop_manifest(tmp_path: Path) -> None:
    from backend.scripts import t86_crop_slot_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    result = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "unit_id": "crop-case4",
            "kind": "crop",
            "reviewer": "operator-a",
            "action": "accept_current_pair",
            "note": "人工确认当前贴边裁切可接受",
        },
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert draft["crop_decisions"][0]["action"] == "accept_current_pair"


def test_t86_review_server_saves_slot_selected_source_assets(tmp_path: Path) -> None:
    from backend.scripts import t86_crop_slot_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    result = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "unit_id": "slot-case8",
            "kind": "slot",
            "reviewer": "operator-a",
            "action": "manual_phase_view_override",
            "selected_assets": ["assets/slot-case8/sample.jpg"],
        },
    )
    draft = json.loads(draft_path.read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert draft["slot_decisions"][0]["selected_assets"] == ["assets/slot-case8/sample.jpg"]
    assert result["state"]["units"][1]["decision"]["selected_assets"] == ["assets/slot-case8/sample.jpg"]


def test_t86_review_server_rejects_invalid_slot_selected_assets(tmp_path: Path) -> None:
    from backend.scripts import t86_crop_slot_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    result = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {
            "unit_id": "slot-case8",
            "kind": "slot",
            "reviewer": "operator-a",
            "action": "manual_phase_view_override",
            "selected_assets": ["assets/other-case/not-real.jpg"],
        },
    )

    assert result["ok"] is False
    assert result["error_code"] == "invalid_selected_assets"


def test_t86_review_html_exposes_clickable_image_selection() -> None:
    from backend.scripts import t86_crop_slot_review_server

    html = t86_crop_slot_review_server._render_html()

    assert "data-select-role" in html
    assert "slot-source" in html
    assert "点击图片选择" in html


def test_t86_review_server_rejects_missing_reviewer_unknown_unit_invalid_action(tmp_path: Path) -> None:
    from backend.scripts import t86_crop_slot_review_server

    manifest_path, draft_path = _packet_files(tmp_path)

    missing_reviewer = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"unit_id": "crop-case4", "kind": "crop", "reviewer": "", "action": "needs_replace_source"},
    )
    unknown = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"unit_id": "crop-case999", "kind": "crop", "reviewer": "operator-a", "action": "needs_replace_source"},
    )
    invalid = t86_crop_slot_review_server.save_review_decision(
        manifest_path,
        draft_path,
        {"unit_id": "slot-case8", "kind": "slot", "reviewer": "operator-a", "action": "needs_replace_source"},
    )

    assert missing_reviewer["ok"] is False
    assert missing_reviewer["error_code"] == "missing_reviewer"
    assert unknown["error_code"] == "unknown_unit"
    assert invalid["error_code"] == "invalid_action"
