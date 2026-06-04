"""T42 risk guards for ComfyUI validation and dirty worktree scope."""
from __future__ import annotations


def test_comfyui_ab_report_marks_small_real_sample_unverified() -> None:
    from backend.scripts import comfyui_ab_report

    records = [
        {
            "case_id": 88,
            "variant": "background_cleanup_v1@conservative",
            "ok": True,
            "dry_run": False,
            "qa_scores": {"halo_score": 3.7, "mask_outside_delta": 1.4},
        },
        {
            "case_id": 88,
            "variant": "ps_model_router",
            "ok": True,
            "dry_run": False,
            "qa_scores": {"halo_score": 4.2, "mask_outside_delta": 1.6},
        },
    ]

    report = comfyui_ab_report.summarize_ab_records(records, min_pairs=20, target_pairs=30)

    assert report["validation_status"] == "unverified_insufficient_real_ab"
    assert report["comparable_pair_count"] == 1
    assert report["required_real_ab_pairs_min"] == 20
    assert "未验证" in report["decision"]
    assert report["promote_to_default"] is False


def test_comfyui_ab_report_marks_dry_run_as_unverified_without_fabricated_wins() -> None:
    from backend.scripts import comfyui_ab_report

    report = comfyui_ab_report.summarize_ab_records(
        [{"case_id": 126, "variant": "portrait_front_compare_v1@conservative", "dry_run": True}],
        min_pairs=20,
        target_pairs=30,
    )

    assert report["validation_status"] == "dry_run_only"
    assert report["comparable_pair_count"] == 0
    assert report["wins_by_variant"] == {}
    assert "无法获取" in report["decision"]


def test_comfyui_model_registry_exposes_missing_sdpose_as_candidate_gap(tmp_path) -> None:
    from backend import ai_generation_adapter

    model_root = tmp_path / "ComfyUI" / "models"
    (model_root / "controlnet").mkdir(parents=True)
    (model_root / "controlnet" / "control_v11p_sd15_openpose.pth").write_bytes(b"real openpose bytes")
    registry = {
        "models": [
            {
                "id": "control_openpose",
                "capability": "control_pose",
                "relative_path": "controlnet/control_v11p_sd15_openpose.pth",
                "required_for_production": True,
            },
            {
                "id": "comfy_org_sdpose",
                "capability": "sdpose_candidate",
                "relative_path": "checkpoints/sdpose.safetensors",
                "source_model": "Comfy-Org/SDPose",
                "required_for_production": False,
                "production_candidate": True,
                "install_state_note": "Candidate only; current portrait workflow uses face keypoints + ControlNet OpenPose.",
            },
        ]
    }
    object_info = {
        "ControlNetLoader": {
            "input": {"required": {"control_net_name": [["control_v11p_sd15_openpose.pth"]]}}
        }
    }

    profile = ai_generation_adapter._build_comfyui_model_profile(
        registry,
        model_root=model_root,
        object_info=object_info,
    )

    assert profile["capabilities"]["control_pose"]["ready"] is True
    assert profile["production_ready"] is True
    assert profile["capabilities"]["sdpose_candidate"]["ready"] is False
    assert profile["candidate_capability_gaps"][0]["capability"] == "sdpose_candidate"
    assert profile["candidate_capability_gaps"][0]["required_for_production"] is False
    assert "Comfy-Org/SDPose" in profile["candidate_capability_gaps"][0]["install_hint"]


def test_dirty_scope_manifest_separates_t42_paths_from_preexisting_dirty() -> None:
    from backend.scripts import dirty_scope_manifest

    baseline = [
        " M .gitignore",
        " M backend/ai_generation_adapter.py",
        "?? backend/services/",
        " M frontend/src/api.ts",
    ]
    current = [
        " M .gitignore",
        " M backend/ai_generation_adapter.py",
        "?? backend/scripts/comfyui_ab_report.py",
        " M frontend/src/api.ts",
        " M tasks/todo.md",
    ]

    manifest = dirty_scope_manifest.build_scope_manifest(
        baseline,
        current,
        t42_paths=[
            "backend/ai_generation_adapter.py",
            "backend/scripts/comfyui_ab_report.py",
            "tasks/todo.md",
        ],
    )

    assert manifest["scope_status"] == "ok"
    assert ".gitignore" in manifest["preexisting_dirty_untouched_paths"]
    assert "frontend/src/api.ts" in manifest["preexisting_dirty_untouched_paths"]
    assert "backend/ai_generation_adapter.py" in manifest["t42_touched_preexisting_dirty_paths"]
    assert "backend/scripts/comfyui_ab_report.py" in manifest["t42_only_paths"]
    assert manifest["new_dirty_not_declared_paths"] == []
