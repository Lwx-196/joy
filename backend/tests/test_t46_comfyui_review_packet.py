"""T46 human-review packet generation for real ComfyUI A/B outputs."""
from __future__ import annotations


def test_review_packet_copies_only_existing_real_assets_and_records_missing(tmp_path) -> None:
    from backend.scripts import comfyui_review_packet

    baseline = tmp_path / "baseline.jpg"
    candidate = tmp_path / "candidate.png"
    baseline.write_bytes(b"real baseline bytes")
    candidate.write_bytes(b"real candidate bytes")
    missing = tmp_path / "missing.png"
    template = {
        "decisions": [
            {
                "ab_unit_id": "case1:front:background_cleanup_v1@conservative",
                "case_id": 1,
                "view": "front",
                "workflow": "background_cleanup_v1@conservative",
                "review_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router@default",
                        "simulation_job_id": 10,
                        "output_refs": [{"kind": "ai_after_simulation", "path": str(baseline)}],
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:background_cleanup_v1@conservative",
                        "simulation_job_id": 11,
                        "output_refs": [
                            {"kind": "difference_heatmap", "path": str(missing)},
                            {"kind": "ai_after_simulation", "path": str(candidate)},
                        ],
                    },
                ],
            }
        ]
    }

    summary = comfyui_review_packet.build_review_packet(template, tmp_path / "packet")

    assert summary["review_unit_count"] == 1
    assert summary["copied_asset_count"] == 2
    assert summary["missing_asset_count"] == 0
    assert (tmp_path / "packet" / "assets" / "case1-front-background-cleanup-v1-conservative" / "baseline.jpg").is_file()
    assert (tmp_path / "packet" / "assets" / "case1-front-background-cleanup-v1-conservative" / "candidate.png").is_file()


def test_review_packet_outputs_html_manifest_and_decision_draft(tmp_path) -> None:
    from backend.scripts import comfyui_review_packet

    baseline = tmp_path / "baseline.jpg"
    candidate = tmp_path / "candidate.png"
    baseline.write_bytes(b"real baseline bytes")
    candidate.write_bytes(b"real candidate bytes")
    template = {
        "decisions": [
            {
                "ab_unit_id": "case2:side:local_region_enhance_v1@conservative",
                "case_id": 2,
                "view": "side",
                "workflow": "local_region_enhance_v1@conservative",
                "review_assets": [
                    {"role": "baseline", "variant": "ps_model_router@default", "output_refs": [{"kind": "ai_after_simulation", "path": str(baseline)}]},
                    {"role": "candidate", "variant": "comfyui_local:local_region_enhance_v1@conservative", "output_refs": [{"kind": "ai_after_simulation", "path": str(candidate)}]},
                ],
            }
        ]
    }

    summary = comfyui_review_packet.build_review_packet(template, tmp_path / "packet")

    assert summary["ready_for_review"] is True
    assert (tmp_path / "packet" / "index.html").is_file()
    assert (tmp_path / "packet" / "manifest.json").is_file()
    assert (tmp_path / "packet" / "review_decisions_draft.json").is_file()
    assert "case2:side:local_region_enhance_v1@conservative" in (tmp_path / "packet" / "index.html").read_text(encoding="utf-8")


def test_review_packet_prefers_clean_generated_raw_over_watermarked_simulation(tmp_path) -> None:
    from backend.scripts import comfyui_review_packet

    watermarked = tmp_path / "candidate-watermarked.png"
    clean = tmp_path / "candidate-clean.png"
    baseline = tmp_path / "baseline-clean.jpg"
    watermarked.write_bytes(b"watermarked candidate")
    clean.write_bytes(b"clean candidate")
    baseline.write_bytes(b"clean baseline")
    template = {
        "decisions": [
            {
                "ab_unit_id": "case3:front:local_region_enhance_v1@conservative",
                "case_id": 3,
                "view": "front",
                "workflow": "local_region_enhance_v1@conservative",
                "review_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router@default",
                        "output_refs": [{"kind": "generated_raw", "path": str(baseline), "watermarked": False}],
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:local_region_enhance_v1@conservative",
                        "output_refs": [
                            {"kind": "ai_after_simulation", "path": str(watermarked), "watermarked": True},
                            {"kind": "generated_raw", "path": str(clean), "watermarked": False},
                        ],
                    },
                ],
            }
        ]
    }

    summary = comfyui_review_packet.build_review_packet(template, tmp_path / "packet")
    manifest = __import__("json").loads((tmp_path / "packet" / "manifest.json").read_text(encoding="utf-8"))
    assets = {
        asset["role"]: asset
        for asset in manifest["review_units"][0]["packet_assets"]
    }

    assert summary["ready_for_review"] is True
    assert assets["candidate"]["kind"] == "generated_raw"
    assert assets["candidate"]["watermarked"] is False
    assert (tmp_path / "packet" / "assets" / "case3-front-local-region-enhance-v1-conservative" / "candidate.png").read_bytes() == b"clean candidate"


def test_review_packet_does_not_use_materialized_simulation_input_as_generated_output(tmp_path) -> None:
    from backend.scripts import comfyui_review_packet

    materialized_input = tmp_path / ".case-workbench-simulation-inputs" / "stamp" / "after-normalized.jpg"
    materialized_input.parent.mkdir(parents=True)
    materialized_input.write_bytes(b"normalized source")
    generated = tmp_path / "after-ai-enhanced.jpg"
    generated.write_bytes(b"real generated output")
    candidate = tmp_path / "candidate.png"
    candidate.write_bytes(b"candidate output")
    template = {
        "decisions": [
            {
                "ab_unit_id": "case4:side:local_region_enhance_v1@conservative",
                "case_id": 4,
                "view": "side",
                "workflow": "local_region_enhance_v1@conservative",
                "review_assets": [
                    {
                        "role": "baseline",
                        "variant": "ps_model_router:gpt-image-2",
                        "output_refs": [
                            {"kind": "generated_raw", "path": str(materialized_input), "watermarked": False},
                            {"kind": "ai_after_simulation", "path": str(generated), "watermarked": True},
                        ],
                    },
                    {
                        "role": "candidate",
                        "variant": "comfyui_local:local_region_enhance_v1@conservative",
                        "output_refs": [{"kind": "generated_raw", "path": str(candidate), "watermarked": False}],
                    },
                ],
            }
        ]
    }

    summary = comfyui_review_packet.build_review_packet(template, tmp_path / "packet")
    manifest = __import__("json").loads((tmp_path / "packet" / "manifest.json").read_text(encoding="utf-8"))
    assets = {
        asset["role"]: asset
        for asset in manifest["review_units"][0]["packet_assets"]
    }

    assert summary["ready_for_review"] is True
    assert assets["baseline"]["kind"] == "ai_after_simulation"
    assert (tmp_path / "packet" / "assets" / "case4-side-local-region-enhance-v1-conservative" / "baseline.jpg").read_bytes() == b"real generated output"
