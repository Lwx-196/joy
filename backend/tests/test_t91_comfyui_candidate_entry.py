"""T91 controlled ComfyUI candidate entry for local-region retest."""
from __future__ import annotations

import json

from backend import db


def test_simulate_after_accepts_t90_allowed_comfyui_candidate(client, seed_case, monkeypatch, tmp_path):
    from backend import ai_generation_adapter

    case_dir = tmp_path / "case-t91-comfyui"
    case_dir.mkdir()
    (case_dir / "before-front.jpg").write_bytes(b"real before bytes")
    (case_dir / "after-front.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")
    captured: dict[str, object] = {}

    def fake_run_after_simulation(**kwargs):
        captured.update(kwargs)
        return {
            "status": "done_with_issues",
            "output_refs": [
                {
                    "kind": "generated_raw",
                    "path": str(tmp_path / "generated.png"),
                    "watermarked": False,
                }
            ],
            "audit": {
                "provider": "comfyui_local",
                "workflow_name": "local_region_enhance_v1",
                "workflow_profile_name": "local_region_enhance_v1@conservative",
                "fallback_used": False,
            },
            "watermarked": False,
            "error_message": "candidate retest artifact is not publishable",
        }

    monkeypatch.setattr(ai_generation_adapter, "run_after_simulation", fake_run_after_simulation)

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "after-front.jpg",
            "before_image_path": "before-front.jpg",
            "focus_targets": ["口角/下颌线局部轻量增强"],
            "focus_regions": [{"x": 0.3, "y": 0.42, "width": 0.4, "height": 0.22}],
            "ai_generation_authorized": True,
            "provider": "comfyui_local",
            "model_name": "local_region_enhance_v1@conservative",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "comfyui_local"
    assert body["model_name"] == "local_region_enhance_v1@conservative"
    assert body["audit"]["workflow_profile_name"] == "local_region_enhance_v1@conservative"
    assert captured["provider"] == "comfyui_local"
    assert captured["model_name"] == "local_region_enhance_v1@conservative"

    with db.connect() as conn:
        sim = conn.execute("SELECT status, model_plan_json, audit_json FROM simulation_jobs").fetchone()
        ai = conn.execute("SELECT provider, model_name, status FROM ai_runs").fetchone()
    assert sim["status"] == "done_with_issues"
    assert json.loads(sim["model_plan_json"])["provider"] == "comfyui_local"
    assert json.loads(sim["audit_json"])["provider"] == "comfyui_local"
    assert ai["provider"] == "comfyui_local"
    assert ai["model_name"] == "local_region_enhance_v1@conservative"


def test_simulate_after_rejects_t90_blocked_comfyui_workflow(client, seed_case, tmp_path):
    case_dir = tmp_path / "case-t91-blocked"
    case_dir.mkdir()
    (case_dir / "after-front.jpg").write_bytes(b"real after bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "after-front.jpg",
            "focus_targets": ["背景清理"],
            "focus_regions": [],
            "ai_generation_authorized": True,
            "provider": "comfyui_local",
            "model_name": "background_cleanup_v1@conservative",
        },
    )

    assert resp.status_code == 400
    assert "ComfyUI workflow not allowed by T90 gate" in resp.json()["detail"]
