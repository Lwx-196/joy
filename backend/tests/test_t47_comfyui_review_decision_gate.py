"""T47 human-review decision validation before importing A/B winners."""
from __future__ import annotations


def _records(count: int) -> list[dict]:
    records: list[dict] = []
    for index in range(count):
        unit_id = f"unit-{index}"
        records.extend(
            [
                {
                    "ab_unit_id": unit_id,
                    "case_id": 100 + index,
                    "variant": "ps_model_router@default",
                    "variant_role": "baseline",
                    "ok": True,
                    "status": "done",
                    "dry_run": False,
                },
                {
                    "ab_unit_id": unit_id,
                    "case_id": 100 + index,
                    "variant": "comfyui_local:local_region_enhance_v1@conservative",
                    "variant_role": "candidate",
                    "ok": True,
                    "status": "done_with_issues",
                    "dry_run": False,
                },
            ]
        )
    return records


def _manifest(count: int) -> dict:
    return {
        "review_units": [
            {
                "ab_unit_id": f"unit-{index}",
                "variants": [
                    "ps_model_router@default",
                    "comfyui_local:local_region_enhance_v1@conservative",
                ],
                "packet_assets": [
                    {"role": "baseline", "variant": "ps_model_router@default"},
                    {"role": "candidate", "variant": "comfyui_local:local_region_enhance_v1@conservative"},
                ],
                "ready_for_review": True,
            }
            for index in range(count)
        ]
    }


def _candidate_decisions(count: int) -> dict:
    return {
        "decisions": [
            {
                "ab_unit_id": f"unit-{index}",
                "winner_role": "candidate",
                "winner_variant": "comfyui_local:local_region_enhance_v1@conservative",
                "reviewer": "human-reviewer",
            }
            for index in range(count)
        ]
    }


def test_empty_review_decisions_stay_unverified_and_sanitize_nothing() -> None:
    from backend.scripts import comfyui_review_decision_gate

    decisions = {
        "decisions": [
            {"ab_unit_id": f"unit-{index}", "winner_role": None, "winner_variant": None, "reviewer": None}
            for index in range(20)
        ]
    }

    validation = comfyui_review_decision_gate.validate_review_decisions(
        decisions,
        _manifest(20),
        min_pairs=20,
    )

    assert validation["validation_status"] == "unverified_missing_human_decisions"
    assert validation["accepted_decision_count"] == 0
    assert validation["rejected_decision_count"] == 20
    assert validation["sanitized_decisions"]["decisions"] == []
    assert "reviewer" in validation["rejected_decisions"][0]["reason"]


def test_review_decision_gate_rejects_unknown_duplicate_and_variant_mismatch() -> None:
    from backend.scripts import comfyui_review_decision_gate

    decisions = {
        "decisions": [
            {
                "ab_unit_id": "unit-0",
                "winner_role": "candidate",
                "winner_variant": "unexpected",
                "reviewer": "human-reviewer",
            },
            {
                "ab_unit_id": "missing-unit",
                "winner_role": "candidate",
                "winner_variant": "comfyui_local:local_region_enhance_v1@conservative",
                "reviewer": "human-reviewer",
            },
            {
                "ab_unit_id": "unit-1",
                "winner_role": "candidate",
                "winner_variant": "comfyui_local:local_region_enhance_v1@conservative",
                "reviewer": "human-reviewer",
            },
            {
                "ab_unit_id": "unit-1",
                "winner_role": "baseline",
                "winner_variant": "ps_model_router@default",
                "reviewer": "human-reviewer",
            },
        ]
    }

    validation = comfyui_review_decision_gate.validate_review_decisions(
        decisions,
        _manifest(2),
        min_pairs=2,
    )

    assert validation["accepted_decision_count"] == 1
    assert validation["rejected_decision_count"] == 3
    assert [item["ab_unit_id"] for item in validation["sanitized_decisions"]["decisions"]] == ["unit-1"]
    assert {item["reason_code"] for item in validation["rejected_decisions"]} == {
        "winner_variant_not_in_role",
        "unknown_ab_unit",
        "duplicate_ab_unit",
    }


def test_twenty_candidate_wins_build_ready_report_without_promotion() -> None:
    from backend.scripts import comfyui_review_decision_gate

    validation = comfyui_review_decision_gate.validate_review_decisions(
        _candidate_decisions(20),
        _manifest(20),
        min_pairs=20,
    )
    report = comfyui_review_decision_gate.build_ab_report_from_validation(
        _records(20),
        validation,
        min_pairs=20,
        target_pairs=30,
    )

    assert validation["validation_status"] == "ready_for_report"
    assert validation["accepted_decision_count"] == 20
    assert validation["candidate_win_count"] == 20
    assert report["validation_status"] == "ready_for_human_review"
    assert report["winner_evidence_count"] == 20
    assert report["candidate_win_count"] == 20
    assert report["promote_to_default"] is False


def test_ab_validation_gate_defaults_to_t47_report() -> None:
    from backend import ai_generation_adapter

    assert ai_generation_adapter._DEFAULT_COMFYUI_AB_VALIDATION_REPORT_PATH.name == "t47_comfyui_ab_report.json"
