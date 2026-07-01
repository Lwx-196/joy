"""Unit tests for the AI-enhanced board render integration (third render option).

Covers the file-system / dispatch logic without touching the real gemini/skill path:
  - render_executor.run_ai_enhanced_render — subprocess dispatch, AI_BOARD_RESULT
    parsing, board placement at the standard final-board.jpg, error paths.
  - render_executor._parse_ai_board_result / _ai_enhance_cli_python.
  - render_queue._parse_job_options — pulling enhance_direction/model from job meta_json.

The real gemini + skill render is exercised by a separate live end-to-end smoke.
"""
from __future__ import annotations

import json
import importlib.util
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from backend import render_executor, render_queue, render_quality


def _load_ai_enhance_script_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_ai_enhanced_boards.py"
    spec = importlib.util.spec_from_file_location("render_ai_enhanced_boards_test_module", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------
# _parse_ai_board_result
# ----------------------------------------------------------------------


def test_parse_ai_board_result_extracts_path():
    stdout = "chatter line\n  AI_BOARD_RESULT: /tmp/out/board.jpg  \ntrailing"
    assert render_executor._parse_ai_board_result(stdout) == "/tmp/out/board.jpg"


def test_parse_ai_board_result_none_when_absent():
    assert render_executor._parse_ai_board_result("no marker here\nfoo") is None


def test_parse_ai_board_evidence_extracts_json():
    stdout = 'AI_BOARD_EVIDENCE: {"generated_count": 2, "external_call_count": 1, "provider_counts": {"rsta": 1, "cache": 1}}'
    evidence = render_executor._parse_ai_board_evidence(stdout)
    assert evidence["generated_count"] == 2
    assert evidence["external_call_count"] == 1
    assert evidence["provider_counts"]["rsta"] == 1


# ----------------------------------------------------------------------
# _parse_ai_board_held (WP2 aligned-render-pipeline)
# ----------------------------------------------------------------------


def test_parse_ai_board_held_pair_with_board():
    stdout = (
        "chatter\n"
        '  AI_BOARD_HELD: {"gate": "pair", "reason": "front eye_ratio=1.5", "board": "/tmp/o/b.jpg"}  \n'
        "trailing"
    )
    held = render_executor._parse_ai_board_held(stdout)
    assert held == {"gate": "pair", "reason": "front eye_ratio=1.5", "board": "/tmp/o/b.jpg"}


def test_parse_ai_board_held_angle_board_none():
    stdout = 'AI_BOARD_HELD: {"gate": "angle", "reason": "印堂需正面|斜侧", "board": null}'
    held = render_executor._parse_ai_board_held(stdout)
    assert held["gate"] == "angle"
    assert held["board"] is None
    # reason 含 '|' 字符也不影响（JSON 编码，非分隔符方案）
    assert "|" in held["reason"]


def test_parse_ai_board_held_none_when_absent():
    assert render_executor._parse_ai_board_held("no marker\nfoo") is None


def test_parse_ai_board_held_none_when_malformed_json():
    assert render_executor._parse_ai_board_held("AI_BOARD_HELD: {not json}") is None


# ----------------------------------------------------------------------
# _ai_enhance_cli_python
# ----------------------------------------------------------------------


def test_ai_enhance_cli_python_env_override(monkeypatch):
    monkeypatch.setenv("CASE_WORKBENCH_AI_ENHANCE_PYTHON", "/custom/python")
    assert render_executor._ai_enhance_cli_python() == "/custom/python"


# ----------------------------------------------------------------------
# render_queue._parse_job_options
# ----------------------------------------------------------------------


def test_parse_job_options_pulls_enhance_fields():
    meta = json.dumps(
        {"options": {"enhance_direction": "heal", "enhance_model": "gemini-3-pro-image-preview", "no_cache": True}}
    )
    opts = render_queue._parse_job_options(meta)
    assert opts["enhance_direction"] == "heal"
    assert opts["enhance_model"] == "gemini-3-pro-image-preview"
    assert opts["no_cache"] is True


def test_parse_job_options_empty_when_no_options():
    assert render_queue._parse_job_options(json.dumps({"draft_preview": True})) == {}
    assert render_queue._parse_job_options(None) == {}
    assert render_queue._parse_job_options("not json at all") == {}


def test_constrain_selection_plan_single_keeps_only_front_slot():
    plan = {
        "policy": "source_selection_v1",
        "required_slots": ["front", "oblique", "side"],
        "renderable_slots": ["front", "side"],
        "effective_template_hint": "bi-compare",
        "selected_count": 4,
        "missing_slots": [{"view": "oblique", "missing": ["after"]}],
        "slots": {
            "front": {"before": {"filename": "front-before.jpg"}, "after": {"filename": "front-after.jpg"}},
            "side": {"before": {"filename": "side-before.jpg"}, "after": {"filename": "side-after.jpg"}},
        },
        "source_provenance": [
            {"view": "front", "filename": "front-before.jpg"},
            {"view": "front", "filename": "front-after.jpg"},
            {"view": "side", "filename": "side-before.jpg"},
            {"view": "side", "filename": "side-after.jpg"},
        ],
    }

    constrained = render_queue._constrain_selection_plan_to_template(plan, "single-compare")

    assert list(constrained["slots"]) == ["front"]
    assert constrained["required_slots"] == ["front"]
    assert constrained["renderable_slots"] == ["front"]
    assert constrained["effective_template_hint"] == "single-compare"
    assert constrained["selected_count"] == 2
    assert constrained["missing_slots"] == []
    assert [item["view"] for item in constrained["source_provenance"]] == ["front", "front"]


def test_ai_script_selection_plan_overrides_manifest_selected_slots():
    module = _load_ai_enhance_script_module()

    class FakeCaseLayout:
        ANGLE_SLOTS = ["front", "oblique", "side"]
        ANGLE_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}

        @staticmethod
        def compute_pose_delta(before_pose, after_pose):
            return {"yaw": 1.0, "pitch": 2.0, "roll": 0.5, "weighted": 3.25}

        @staticmethod
        def derive_effective_template(candidate_slots, _profile=None):
            return ("bi-compare", candidate_slots)

    manifest = {
        "status": "ok",
        "groups": [
            {
                "name": "case45",
                "entries": [
                    {"name": "old-composite.jpg", "relative_path": "old-composite.jpg", "phase": "after", "angle": "front"},
                    {"name": "术前1.JPG", "relative_path": "术前1.JPG", "phase": "before", "angle": "front", "pose": {}},
                    {"name": "术后1.JPG", "relative_path": "术后1.JPG", "phase": "after", "angle": "front", "pose": {}},
                    {"name": "术前3.JPG", "relative_path": "术前3.JPG", "phase": "before", "angle": "oblique", "pose": {}},
                    {"name": "术后3.JPG", "relative_path": "术后3.JPG", "phase": "after", "angle": "oblique", "pose": {}},
                    {"name": "术前4.JPG", "relative_path": "术前4.JPG", "phase": "before", "angle": "side", "pose": {}},
                    {"name": "术后4.JPG", "relative_path": "术后4.JPG", "phase": "after", "angle": "side", "pose": {}},
                ],
                "selected_slots": {
                    "front": {
                        "before": {"name": "术前1.JPG"},
                        "after": {"name": "old-composite.jpg"},
                    },
                    "side": {
                        "before": {"name": "术前4.JPG"},
                        "after": {"name": "术后4.JPG"},
                    },
                },
            }
        ],
    }
    selection_plan = {
        "policy": "source_selection_v1",
        "renderable_slots": ["front", "oblique"],
        "dropped_slots": [{"view": "side"}],
        "slots": {
            "front": {
                "before": {"filename": "术前1.JPG", "phase": "before", "view": "front"},
                "after": {"filename": "术后1.JPG", "phase": "after", "view": "front"},
            },
            "oblique": {
                "before": {"filename": "术前3.JPG", "phase": "before", "view": "oblique"},
                "after": {"filename": "术后3.JPG", "phase": "after", "view": "oblique"},
            },
        },
    }

    audit = module._apply_selection_plan_to_manifest(
        manifest,
        selection_plan,
        FakeCaseLayout(),
    )

    selected = manifest["groups"][0]["selected_slots"]
    assert selected["front"]["after"]["name"] == "术后1.JPG"
    assert selected["oblique"]["after"]["name"] == "术后3.JPG"
    assert "side" not in selected
    assert manifest["groups"][0]["render_slots"] == ["front", "oblique"]
    assert manifest["groups"][0]["effective_template"] == "bi-compare"
    assert audit["overrode"][0]["before"]["after"] == "old-composite.jpg"
    assert audit["removed_unplanned_slots"][0]["slot"] == "side"


def test_ai_script_selection_plan_persists_unplanned_removal_for_unmatched_group():
    module = _load_ai_enhance_script_module()

    class FakeCaseLayout:
        ANGLE_SLOTS = ["front", "oblique", "side"]
        ANGLE_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}

        @staticmethod
        def compute_pose_delta(before_pose, after_pose):
            return {"yaw": 1.0, "pitch": 2.0, "roll": 0.5, "weighted": 3.25}

        @staticmethod
        def derive_effective_template(candidate_slots, _profile=None):
            return ("bi-compare", candidate_slots) if candidate_slots else (None, [])

    manifest = {
        "status": "ok",
        "groups": [
            {
                "name": "planned",
                "entries": [
                    {"name": "术前正面.JPG", "relative_path": "术前正面.JPG", "phase": "before", "angle": "front", "pose": {}},
                    {"name": "术后正面.JPG", "relative_path": "术后正面.JPG", "phase": "after", "angle": "front", "pose": {}},
                    {"name": "术前45.JPG", "relative_path": "术前45.JPG", "phase": "before", "angle": "oblique", "pose": {}},
                    {"name": "术后45.JPG", "relative_path": "术后45.JPG", "phase": "after", "angle": "oblique", "pose": {}},
                ],
                "selected_slots": {},
            },
            {
                "name": "side-only",
                "status": "ok",
                "entries": [
                    {"name": "术前侧面.JPG", "relative_path": "术前侧面.JPG", "phase": "before", "angle": "side", "pose": {}},
                    {"name": "术后侧面.JPG", "relative_path": "术后侧面.JPG", "phase": "after", "angle": "side", "pose": {}},
                ],
                "selected_slots": {
                    "side": {
                        "before": {"name": "术前侧面.JPG"},
                        "after": {"name": "术后侧面.JPG"},
                    },
                },
                "render_slots": ["side"],
                "effective_template": "single-compare",
            },
        ],
    }
    selection_plan = {
        "policy": "source_selection_v1",
        "renderable_slots": ["front", "oblique"],
        "dropped_slots": [{"view": "side"}],
        "slots": {
            "front": {
                "before": {"filename": "术前正面.JPG", "phase": "before", "view": "front"},
                "after": {"filename": "术后正面.JPG", "phase": "after", "view": "front"},
            },
            "oblique": {
                "before": {"filename": "术前45.JPG", "phase": "before", "view": "oblique"},
                "after": {"filename": "术后45.JPG", "phase": "after", "view": "oblique"},
            },
        },
    }

    audit = module._apply_selection_plan_to_manifest(
        manifest,
        selection_plan,
        FakeCaseLayout(),
    )

    assert set(manifest["groups"][0]["selected_slots"]) == {"front", "oblique"}
    side_group = manifest["groups"][1]
    assert side_group["selected_slots"] == {}
    assert side_group["render_slots"] == []
    assert "effective_template" not in side_group
    assert any(item["group"] == "side-only" and item["slot"] == "side" for item in audit["removed_unplanned_slots"])


def test_ai_script_selection_plan_builds_cross_group_manifest_when_before_after_split():
    module = _load_ai_enhance_script_module()

    class FakeCaseLayout:
        ANGLE_SLOTS = ["front", "oblique", "side"]
        ANGLE_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}

        @staticmethod
        def compute_pose_delta(before_pose, after_pose):
            return {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "weighted": 0.0}

        @staticmethod
        def derive_effective_template(candidate_slots, _profile=None):
            return ("bi-compare", candidate_slots)

    manifest = {
        "status": "error",
        "groups": [
            {
                "name": "术前",
                "status": "error",
                "blocking_issues": ["only before images"],
                "entries": [
                    {"name": "正面.jpg", "relative_path": "术前/正面.jpg", "path": "/case/术前/正面.jpg", "phase": "before", "angle": "front", "pose": {}},
                    {"name": "45度.jpg", "relative_path": "术前/45度.jpg", "path": "/case/术前/45度.jpg", "phase": "before", "angle": "oblique", "pose": {}},
                ],
                "selected_slots": {},
            },
            {
                "name": "术后",
                "status": "error",
                "blocking_issues": ["only after images"],
                "entries": [
                    {"name": "正面.jpg", "relative_path": "术后/正面.jpg", "path": "/case/术后/正面.jpg", "phase": "after", "angle": "front", "pose": {}},
                    {"name": "45度.jpg", "relative_path": "术后/45度.jpg", "path": "/case/术后/45度.jpg", "phase": "after", "angle": "oblique", "pose": {}},
                ],
                "selected_slots": {},
            },
        ],
    }
    selection_plan = {
        "policy": "source_selection_v1",
        "renderable_slots": ["front", "oblique"],
        "effective_template_hint": "bi-compare",
        "slots": {
            "front": {
                "before": {"filename": "术前/正面.jpg", "phase": "before", "view": "front"},
                "after": {"filename": "术后/正面.jpg", "phase": "after", "view": "front"},
            },
            "oblique": {
                "before": {"filename": "术前/45度.jpg", "phase": "before", "view": "oblique"},
                "after": {"filename": "术后/45度.jpg", "phase": "after", "view": "oblique"},
            },
        },
    }

    audit = module._apply_selection_plan_to_manifest(
        manifest,
        selection_plan,
        FakeCaseLayout(),
    )

    synthetic = manifest["groups"][0]
    assert synthetic["name"] == "source_selection_cross_group"
    assert synthetic["status"] == "ok"
    assert synthetic["render_slots"] == ["front", "oblique"]
    assert synthetic["effective_template"] == "bi-compare"
    assert synthetic["selected_slots"]["front"]["before"]["relative_path"] == "术前/正面.jpg"
    assert synthetic["selected_slots"]["front"]["after"]["relative_path"] == "术后/正面.jpg"
    assert len(audit["applied_slots"]) == 2
    assert audit["cross_group_group_created"] is True


def test_ai_script_selection_plan_clears_group_error_after_successful_override():
    module = _load_ai_enhance_script_module()

    class FakeCaseLayout:
        ANGLE_SLOTS = ["front", "oblique", "side"]
        ANGLE_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}

        @staticmethod
        def compute_pose_delta(before_pose, after_pose):
            return {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "weighted": 0.0}

        @staticmethod
        def derive_effective_template(candidate_slots, _profile=None):
            return ("single-compare", candidate_slots)

    manifest = {
        "status": "error",
        "blocking_issues": ["missing_phase"],
        "groups": [
            {
                "name": "case",
                "status": "error",
                "blocking_issues": ["missing_phase"],
                "entries": [
                    {"name": "术前1.jpg", "relative_path": "术前1.jpg", "phase": "before", "angle": "front", "pose": {}},
                    {"name": "术后1.jpg", "relative_path": "术后1.jpg", "phase": "after", "angle": "front", "pose": {}},
                ],
                "selected_slots": {},
            }
        ],
    }
    selection_plan = {
        "policy": "source_selection_v1",
        "renderable_slots": ["front"],
        "slots": {
            "front": {
                "before": {"filename": "术前1.jpg", "phase": "before", "view": "front"},
                "after": {"filename": "术后1.jpg", "phase": "after", "view": "front"},
            }
        },
    }

    module._apply_selection_plan_to_manifest(
        manifest,
        selection_plan,
        FakeCaseLayout(),
    )

    group = manifest["groups"][0]
    assert group["status"] == "ok"
    assert group["blocking_issues"] == []
    assert manifest["status"] == "ok"
    assert manifest["blocking_issues"] == []


def test_ai_script_selection_plan_matches_heic_transcoded_jpeg(tmp_path):
    module = _load_ai_enhance_script_module()
    entry = {
        "name": "2757术前.jpg",
        "relative_path": ".case-workbench-heic-jpeg/2757术前.jpg",
        "group_relative_path": ".case-workbench-heic-jpeg/2757术前.jpg",
        "path": str(tmp_path / ".case-workbench-heic-jpeg" / "2757术前.jpg"),
    }
    candidate = {
        "filename": "2757术前.HEIC",
        "render_filename": "2757术前.HEIC",
    }

    assert module._find_group_entry({"entries": [entry]}, candidate, tmp_path) is entry


def test_ai_script_selection_plan_matches_staged_render_filename_when_source_path_has_phase_dir():
    module = _load_ai_enhance_script_module()
    render_filename = "case129-治疗项目-术前-正面.JPG"
    entry = {
        "name": render_filename,
        "path": f"/tmp/job-1061/{render_filename}",
        "phase": "before",
        "angle": "front",
    }
    candidate = {
        "filename": "术前/正面.JPG",
        "render_filename": render_filename,
        "phase": "before",
        "view": "front",
    }

    assert module._find_group_entry({"entries": [entry]}, candidate) is entry


def test_ai_script_selection_plan_keeps_front_when_plan_uses_heic_alias(tmp_path):
    module = _load_ai_enhance_script_module()

    class FakeCaseLayout:
        ANGLE_SLOTS = ["front", "oblique", "side"]
        ANGLE_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}

        @staticmethod
        def compute_pose_delta(before_pose, after_pose):
            return {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "weighted": 0.0}

        @staticmethod
        def derive_effective_template(candidate_slots, _profile=None):
            return ("bi-compare", candidate_slots)

    heic_dir = tmp_path / ".case-workbench-heic-jpeg"
    heic_dir.mkdir()
    manifest = {
        "status": "ok",
        "groups": [
            {
                "name": "case407",
                "entries": [
                    {
                        "name": "2757术前.jpg",
                        "relative_path": ".case-workbench-heic-jpeg/2757术前.jpg",
                        "path": str(heic_dir / "2757术前.jpg"),
                        "phase": "before",
                        "angle": "front",
                        "pose": {},
                    },
                    {
                        "name": "2775术后.jpg",
                        "relative_path": ".case-workbench-heic-jpeg/2775术后.jpg",
                        "path": str(heic_dir / "2775术后.jpg"),
                        "phase": "after",
                        "angle": "front",
                        "pose": {},
                    },
                    {"name": "2777恢复侧面术前.jpg", "relative_path": "2777恢复侧面术前.jpg", "phase": "before", "angle": None, "pose": {}},
                    {"name": "2777恢复侧面术后.jpg", "relative_path": "2777恢复侧面术后.jpg", "phase": "after", "angle": "side", "pose": {}},
                ],
                "selected_slots": {
                    "front": {
                        "before": {"name": "2757术前.jpg"},
                        "after": {"name": "2775术后.jpg"},
                    },
                },
            }
        ],
    }
    selection_plan = {
        "policy": "source_selection_v1",
        "renderable_slots": ["front", "side"],
        "slots": {
            "front": {
                "before": {"filename": "2757术前.HEIC", "phase": "before", "view": "front"},
                "after": {"filename": "2775术后.HEIC", "phase": "after", "view": "front"},
            },
            "side": {
                "before": {"filename": "2777恢复侧面术前.jpg", "phase": "before", "view": "side"},
                "after": {"filename": "2777恢复侧面术后.jpg", "phase": "after", "view": "side"},
            },
        },
    }

    audit = module._apply_selection_plan_to_manifest(
        manifest,
        selection_plan,
        FakeCaseLayout(),
        case_root=tmp_path,
    )

    selected = manifest["groups"][0]["selected_slots"]
    assert set(selected) == {"front", "side"}
    assert selected["front"]["before"]["name"] == "2757术前.jpg"
    assert selected["front"]["after"]["name"] == "2775术后.jpg"
    assert selected["side"]["before"]["name"] == "2777恢复侧面术前.jpg"
    assert audit["missing_entries"] == []


def test_ai_script_side_face_failure_uses_contain_fallback():
    module = _load_ai_enhance_script_module()

    class FakeFaceAlign:
        @staticmethod
        def harmonize_pair(before_arr, after_arr):
            return before_arr, after_arr

        @staticmethod
        def lift_face_shadows(after_arr, slot):
            return after_arr

    class FakeCaseLayout:
        FACE_ALIGN = FakeFaceAlign()

        @staticmethod
        def cv_to_pil(arr):
            return Image.fromarray(arr)

    class FakeRenderModule:
        CASE_LAYOUT = FakeCaseLayout()

        @staticmethod
        def render_aligned_pair(*_args, **_kwargs):
            raise ValueError("未检测到可用侧面人脸")

        @staticmethod
        def render_side_profile_contain_cell(image_path, size):
            return np.zeros((size[1], size[0], 3), dtype=np.uint8)

        @staticmethod
        def whiten_background(img):
            return img

    render_mod = FakeRenderModule()
    module._install_side_contain_fallback(render_mod)
    render_plan_records = []

    before, after = render_mod.render_aligned_pair(
        "/tmp/before.jpg",
        ["/tmp/after.jpg"],
        (24, 32),
        "side",
        render_plan_records=render_plan_records,
    )

    assert before.size == (24, 32)
    assert after.size == (24, 32)
    assert render_plan_records[0]["strategy"] == "side_profile_contain_after_face_detection_error"
    assert render_plan_records[0]["composition_diagnostic"]["alerts"][0]["code"] == "side_face_alignment_fallback"


def test_ai_script_single_case_entrypoint_treats_phase_split_dir_as_treatment(tmp_path):
    module = _load_ai_enhance_script_module()
    customer_dir = tmp_path / "曾亦男"
    treatment_dir = customer_dir / "2025.3.12泪沟填充"
    before_dir = treatment_dir / "术前"
    after_dir = treatment_dir / "术后"
    before_dir.mkdir(parents=True)
    after_dir.mkdir()
    (before_dir / "正面.jpg").write_bytes(b"before")
    (after_dir / "正面.jpg").write_bytes(b"after")

    case_dirs, treatment_name = module._single_case_entrypoint(treatment_dir)

    assert case_dirs == [customer_dir.resolve()]
    assert treatment_name == treatment_dir.name


# ----------------------------------------------------------------------
# run_ai_enhanced_render (mocked subprocess)
# ----------------------------------------------------------------------


def _fake_proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_run_ai_enhanced_render_places_board_at_final(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "produced_board.jpg"
    produced.write_bytes(b"enhanced-board-bytes")
    out_root = tmp_path / "outroot"
    manual_overrides = {"术前/a.jpg": {"phase": "before", "view": "front"}}
    selection_plan = {
        "slots": {
            "front": {
                "before": {"filename": "术前/a.jpg", "phase": "before", "view": "front"},
                "after": {"filename": "术后/a.jpg", "phase": "after", "view": "front"},
            }
        }
    }

    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        # WP1 (aligned-render-pipeline): 接通 non-native 增强路。
        assert "--case-dir" in args
        assert "--enhance-direction" in args and "heal" in args
        assert "--enhance-model" in args and "gemini-3-pro-image-preview" in args
        # 去 native 局部重绘 + 去 no-cache（复用全局内容寻址 cache）。
        assert "--native-enhance" not in args
        assert "--no-cache" not in args
        # 前端 per-click 不烧 judge 钱。
        assert "--no-board-qa" in args
        manual_path = Path(args[args.index("--manual-overrides-file") + 1])
        selection_path = Path(args[args.index("--selection-plan-file") + 1])
        output_dir = Path(args[args.index("--output-dir") + 1])
        assert json.loads(manual_path.read_text(encoding="utf-8")) == manual_overrides
        assert json.loads(selection_path.read_text(encoding="utf-8")) == selection_plan
        (output_dir / "ai_enhance_progress.jsonl").write_text(
            "\n".join([
                '{"event":"slot_external_call_start","slot":"front","provider_order":["rsta"]}',
                '{"event":"provider_attempt_success","slot":"front","provider":"rsta","attempt":1}',
            ]),
            encoding="utf-8",
        )
        captured["extra_env"] = extra_env
        evidence = json.dumps(
            {
                "generated_count": 3,
                "total_slots": 3,
                "cache_hit_count": 2,
                "external_call_count": 1,
                "provider_counts": {"cache": 2, "rsta": 1},
                "provider_order": ["rsta"],
                "errors": [{"slot": "front", "error": "transient"}],
            },
            ensure_ascii=False,
        )
        return _fake_proc(f"chatter\nAI_BOARD_EVIDENCE: {evidence}\nAI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        enhance_direction="heal",
        enhance_model="gemini-3-pro-image-preview",
        manual_overrides=manual_overrides,
        selection_plan=selection_plan,
    )

    assert result["status"] == "done"
    assert result["case_mode"] == "ai_enhanced_board"
    assert result["enhance"] == {"direction": "heal", "model": "gemini-3-pro-image-preview"}
    final = out_root / "final-board.jpg"
    assert result["output_path"] == str(final)
    assert final.read_bytes() == b"enhanced-board-bytes"
    assert result["ai_usage"]["used_after_enhancement"] is True
    assert result["ai_usage"]["generated_artifact_count"] == 3
    assert result["ai_usage"]["external_call_count"] == 1
    assert result["ai_usage"]["cache_hit_count"] == 2
    assert result["ai_usage"]["cache_disabled"] is False
    assert result["ai_usage"]["enhancement_evidence"]["provider_order"] == ["rsta"]
    assert result["ai_usage"]["enhancement_evidence"]["errors"][0]["slot"] == "front"
    assert result["ai_usage"]["enhancement_evidence"]["progress"]["last_event"]["event"] == "provider_attempt_success"
    assert result["ai_usage"]["enhancement_evidence"]["progress"]["last_event"]["provider"] == "rsta"
    # 默认透传自适应 4K（跟 4K 批量对齐），子进程 env 干净不继承父进程。
    assert captured["extra_env"]["CASE_WORKBENCH_ADAPTIVE_4K"] == "1"


def test_run_ai_enhanced_render_keeps_default_provider_order_for_gemini_label(tmp_path, monkeypatch):
    """Gemini model labels must not override the production provider chain."""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "produced_board.jpg"
    produced.write_bytes(b"enhanced-board-bytes")
    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        captured["args"] = args
        return _fake_proc(f"AI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")

    render_executor.run_ai_enhanced_render(
        case_dir,
        enhance_model="gemini-3-pro-image-preview",
    )

    args = captured["args"]
    assert "--provider-order" in args
    assert args[args.index("--provider-order") + 1] == "rsta,tuzi,flashapi,77code"


def test_run_ai_enhanced_render_passes_workbench_title_context(tmp_path, monkeypatch):
    """Bound staging dirs must render with the real workbench case title, not job-*."""
    case_dir = tmp_path / "real-case" / ".case-workbench-bound-render" / "job-1006"
    case_dir.mkdir(parents=True)
    produced = tmp_path / "produced_board.jpg"
    produced.write_bytes(b"enhanced-board-bytes")
    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        captured["args"] = args
        return _fake_proc(f"AI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")

    result = render_executor.run_ai_enhanced_render(
        case_dir,
        customer_name="黄宝凤",
        date="2025.6.26",
        project="泪沟",
    )

    args = captured["args"]
    assert "--customer-name" in args
    assert args[args.index("--customer-name") + 1] == "黄宝凤"
    assert "--case-date" in args
    assert args[args.index("--case-date") + 1] == "2025.6.26"
    assert "--case-project" in args
    assert args[args.index("--case-project") + 1] == "泪沟"
    assert result["ai_usage"]["board_title_context"] == {
        "customer_name": "黄宝凤",
        "date": "2025.6.26",
        "project": "泪沟",
    }


def test_run_ai_enhanced_render_provider_order_env_override(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "produced_board.jpg"
    produced.write_bytes(b"enhanced-board-bytes")
    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        captured["args"] = args
        return _fake_proc(f"AI_BOARD_RESULT: {produced}\n")

    monkeypatch.setenv("CASE_WORKBENCH_AI_ENHANCE_PROVIDER_ORDER", "ai_studio")
    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")

    render_executor.run_ai_enhanced_render(
        case_dir,
        enhance_model="gemini-3-pro-image-preview",
    )

    args = captured["args"]
    assert args[args.index("--provider-order") + 1] == "ai_studio"


def test_run_ai_enhanced_render_timeout_carries_command_metadata(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()

    def fake_sub(args, timeout, extra_env=None):
        output_dir = Path(args[args.index("--output-dir") + 1])
        (output_dir / "ai_enhance_progress.jsonl").write_text(
            "\n".join([
                '{"event":"slot_external_call_start","slot":"front","provider_order":["rsta","tuzi"]}',
                '{"event":"provider_attempt_start","slot":"front","provider":"rsta","attempt":1}',
            ]),
            encoding="utf-8",
        )
        exc = subprocess.TimeoutExpired(cmd=args, timeout=timeout, output="partial stdout", stderr="partial stderr")
        exc.stdout = "partial stdout"
        exc.stderr = "partial stderr"
        raise exc

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        render_executor.run_ai_enhanced_render(
            case_dir,
            enhance_model="gemini-3-pro-image",
            allow_burn=True,
            no_cache=True,
        )

    exc = exc_info.value
    command = exc.ai_enhance_command
    assert command["provider_order"] == ["rsta", "tuzi", "flashapi", "77code"]
    assert command["no_cache"] is True
    assert command["allow_burn"] is True
    assert command["timeout_sec"] == render_executor.DEFAULT_AI_ENHANCE_TIMEOUT_SEC
    assert "--provider-order" in command["args"]
    assert exc.ai_enhance_stdout_tail == "partial stdout"
    assert exc.ai_enhance_stderr_tail == "partial stderr"
    assert exc.ai_enhance_progress["event_count"] == 2
    assert exc.ai_enhance_progress["last_event"]["event"] == "provider_attempt_start"
    assert exc.ai_enhance_progress["last_event"]["provider"] == "rsta"


def test_render_ai_enhanced_boards_applies_gemini_model_to_ai_studio_env():
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_ai_enhanced_boards.py"
    spec = importlib.util.spec_from_file_location("render_ai_enhanced_boards", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    env = {"GOOGLE_GENAI_API_KEY": "dummy"}
    resolved = module._apply_enhance_model_to_provider_env(
        env,
        ["ai_studio"],
        "gemini-3-pro-image-preview",
    )

    assert resolved["AI_STUDIO_IMAGE_MODEL"] == "gemini-3-pro-image-preview"
    assert env.get("AI_STUDIO_IMAGE_MODEL") is None


def test_render_ai_enhanced_boards_does_not_reuse_vlm_proxy_key_for_ai_studio(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "render_ai_enhanced_boards.py"
    spec = importlib.util.spec_from_file_location("render_ai_enhanced_boards", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    env_file = tmp_path / "t52_vlm_judge.local.env"
    env_file.write_text(
        "\n".join(
            [
                "CASE_WORKBENCH_VLM_JUDGE_PROVIDER=openai-compatible",
                "CASE_WORKBENCH_VLM_JUDGE_BASE_URL=https://ai.flashapi.top/v1",
                "CASE_WORKBENCH_VLM_JUDGE_API_KEY=flashapi-proxy-key",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(module, "_find_env_file", lambda filename: env_file if filename == "t52_vlm_judge.local.env" else None)

    env = module._load_all_provider_envs(["ai_studio"])

    assert "GOOGLE_GENAI_API_KEY" not in env
    assert "GEMINI_API_KEY" not in env


def test_render_ai_enhanced_single_case_entrypoint_accepts_case_root_and_treatment_dir(tmp_path):
    module = _load_ai_enhance_script_module()

    archive_root = tmp_path / "陈院案例(1)"
    customer_root = archive_root / "江李欣"
    treatment_dir = customer_root / "2026.03.31治疗"
    treatment_dir.mkdir(parents=True)
    (treatment_dir / "术前1.jpg").write_bytes(b"before")

    case_dirs, single_treatment_name = module._single_case_entrypoint(customer_root)
    assert case_dirs == [customer_root.resolve()]
    assert single_treatment_name is None

    case_dirs, single_treatment_name = module._single_case_entrypoint(treatment_dir)
    assert case_dirs == [customer_root.resolve()]
    assert single_treatment_name == treatment_dir.name


def test_render_ai_enhanced_title_override_hides_bound_staging_name(tmp_path):
    module = _load_ai_enhance_script_module()
    treatment_dir = tmp_path / ".case-workbench-bound-render" / "job-1006"
    treatment_dir.mkdir(parents=True)

    class FakeRenderMod:
        @staticmethod
        def parse_case_meta(_treatment_dir):
            return {"date": "job-1006", "project": ""}

        @staticmethod
        def parse_title_b(project, customer):
            return [customer, project]

    title_meta = module._resolve_board_title(
        FakeRenderMod,
        treatment_dir,
        ".case-workbench-bound-render",
        "job-1006",
        customer_name="黄宝凤",
        case_date="2025.6.26",
        case_project="泪沟",
    )

    assert title_meta["title"] == "黄宝凤 2025.6.26 泪沟"
    assert ".case-workbench-bound-render" not in title_meta["title"]
    assert "job-1006" not in title_meta["title"]


def test_render_ai_enhanced_applies_title_override_to_render_manifest(tmp_path):
    module = _load_ai_enhance_script_module()
    treatment_dir = tmp_path / ".case-workbench-bound-render" / "job-1046"
    treatment_dir.mkdir(parents=True)
    manifest = {
        "case_dir": str(treatment_dir),
        "meta": {
            "customer_name": ".case-workbench-bound-render",
            "date": "job-1046",
            "project": "job-1046",
        },
    }

    module._apply_board_title_to_manifest(
        manifest,
        {
            "customer": "刘柏玲",
            "date": "2025.06.04",
            "project": "娇兰丰唇",
            "title": "刘柏玲 2025.06.04 娇兰丰唇",
            "title_lines": ["娇兰 ▸ 丰唇"],
        },
    )

    assert manifest["meta"] == {
        "customer_name": "刘柏玲",
        "date": "2025.06.04",
        "project": "娇兰丰唇",
    }
    assert manifest["board_title"] == "刘柏玲 2025.06.04 娇兰丰唇"
    assert manifest["title_lines"] == ["娇兰 ▸ 丰唇"]
    render_title_text = json.dumps(
        {
            "meta": manifest["meta"],
            "title": manifest["title"],
            "board_title": manifest["board_title"],
            "title_lines": manifest["title_lines"],
        },
        ensure_ascii=False,
    )
    assert ".case-workbench-bound-render" not in render_title_text
    assert "job-1046" not in render_title_text


def test_run_ai_enhanced_render_forwards_adaptive_4k_env_override(tmp_path, monkeypatch):
    """server env CASE_WORKBENCH_ADAPTIVE_4K=0 → 透传 0（运行时开关，owner 可翻档）。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "b.jpg"
    produced.write_bytes(b"x")
    monkeypatch.setenv("CASE_WORKBENCH_ADAPTIVE_4K", "0")
    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return _fake_proc(f"AI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "o")
    render_executor.run_ai_enhanced_render(case_dir)
    assert captured["extra_env"]["CASE_WORKBENCH_ADAPTIVE_4K"] == "0"


def test_run_ai_enhanced_render_raises_on_nonzero(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(
        render_executor, "_run_render_subprocess", lambda a, t, extra_env=None: _fake_proc("boom", returncode=1)
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")
    with pytest.raises(RuntimeError, match="ai-enhance subprocess exit=1"):
        render_executor.run_ai_enhanced_render(case_dir)


def test_run_ai_enhanced_render_raises_when_no_marker(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(
        render_executor, "_run_render_subprocess", lambda a, t, extra_env=None: _fake_proc("finished but no marker")
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")
    with pytest.raises(RuntimeError, match="no board"):
        render_executor.run_ai_enhanced_render(case_dir)


def test_run_ai_enhanced_render_reports_manifest_failure_when_no_board(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    out_root = tmp_path / "outroot"

    def fake_sub(args, timeout, extra_env=None):
        output_dir = Path(args[args.index("--output-dir") + 1])
        boards_manifest = {
            "boards": [
                {
                    "customer": "稀饭",
                    "treatment": "2025.6.26注射",
                    "status": "RENDER_FAILED",
                    "error": "没有可渲染的角度槽位",
                }
            ]
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "boards_manifest.json").write_text(
            json.dumps(boards_manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        return _fake_proc(
            "==== AI 增强板渲染完成 ====\n完成: 0/1\n",
            stderr=(
                "INFO __main__: [pose] front: 排除 (phase_missing)\n"
                "ERROR __main__:   ❌ 渲染失败: 没有可渲染的角度槽位\n"
            ),
        )

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    with pytest.raises(RuntimeError) as exc:
        render_executor.run_ai_enhanced_render(case_dir)

    message = str(exc.value)
    assert "ai-enhance produced no board" in message
    assert "boards_manifest=" in message
    assert "稀饭 2025.6.26注射 status=RENDER_FAILED" in message
    assert "没有可渲染的角度槽位" in message
    assert "phase_missing" in message


def test_run_ai_enhanced_render_missing_case_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        render_executor.run_ai_enhanced_render(tmp_path / "does-not-exist")


# ----------------------------------------------------------------------
# run_ai_enhanced_render HELD → status="blocked" (WP2: gate HELD ≠ 渲染失败)
# ----------------------------------------------------------------------


def test_run_ai_enhanced_render_pair_held_blocks_with_diagnostic_board(tmp_path, monkeypatch):
    """G2 配对门 HELD：保留诊断板 → status='blocked' 不抛错，诊断板落 final-board。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    diag = tmp_path / "diag_board.jpg"
    diag.write_bytes(b"diagnostic-board-bytes")
    out_root = tmp_path / "outroot"

    held_line = json.dumps(
        {"gate": "pair", "reason": "front eye_ratio=1.46（允许 [0.78, 1.3]）", "board": str(diag)},
        ensure_ascii=False,
    )

    def fake_sub(args, timeout, extra_env=None):
        return _fake_proc(f"chatter\nAI_BOARD_HELD: {held_line}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)

    assert result["status"] == "blocked"
    assert result["held_gate"] == "pair"
    assert "eye_ratio" in result["held_reason"]
    assert result["render_error"] == result["held_reason"]
    assert result["blocking_issue_count"] == 1
    final = out_root / "final-board.jpg"
    assert result["output_path"] == str(final)
    assert final.read_bytes() == b"diagnostic-board-bytes"


def test_run_ai_enhanced_render_angle_held_blocks_without_board(tmp_path, monkeypatch):
    """G1 角度门 HELD：出板前短路无诊断板 → status='blocked'，output_path=None。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    out_root = tmp_path / "outroot"

    held_line = json.dumps(
        {"gate": "angle", "reason": "印堂需正面|斜侧（板上实有 ['front']）", "board": None},
        ensure_ascii=False,
    )

    def fake_sub(args, timeout, extra_env=None):
        return _fake_proc(f"AI_BOARD_HELD: {held_line}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)

    assert result["status"] == "blocked"
    assert result["held_gate"] == "angle"
    assert result["output_path"] is None
    assert result["blocking_issue_count"] == 1
    # G1 短路无板 → 不创建 final-board
    assert not (out_root / "final-board.jpg").exists()


def test_run_ai_enhanced_render_cache_miss_needs_confirmation(tmp_path, monkeypatch):
    """F2：cache-miss 未授权 → status='needs_confirmation' + 缺槽/预估，不真烧、output_path=None。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    out_root = tmp_path / "outroot"
    miss_line = json.dumps(
        {"miss_slots": ["front", "side"], "miss_count": 2, "total_slots": 3,
         "est_cost_usd": 0.114, "est_seconds": 300, "board": None},
        ensure_ascii=False,
    )
    seen = {}

    def fake_sub(args, timeout, extra_env=None):
        seen["args"] = args
        return _fake_proc(f"chatter\nAI_BOARD_CACHE_MISS: {miss_line}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)

    assert result["status"] == "needs_confirmation"
    assert result["cache_miss_count"] == 2
    assert result["cache_miss_total"] == 3
    assert result["cache_miss_est_cost_usd"] == 0.114
    assert result["cache_miss_est_seconds"] == 300
    assert result["output_path"] is None
    # 默认未授权 → 不带烧钱 flag
    assert "--allow-cache-miss-burn" not in seen["args"]


def test_run_ai_enhanced_render_allow_burn_passes_flag(tmp_path, monkeypatch):
    """F2：用户确认 allow_burn=True → 子进程带 --allow-cache-miss-burn 授权真烧。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    out_root = tmp_path / "outroot"
    produced = tmp_path / "board.jpg"
    produced.write_bytes(b"final")
    seen = {}

    def fake_sub(args, timeout, extra_env=None):
        seen["args"] = args
        return _fake_proc(f"AI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir, allow_burn=True)

    assert result["status"] == "done"
    assert "--allow-cache-miss-burn" in seen["args"]


def test_run_ai_enhanced_render_no_cache_passes_fresh_flag(tmp_path, monkeypatch):
    """T189：本次重新深图 → 子进程带 --no-cache，并保留 cache_disabled 证据。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    out_root = tmp_path / "outroot"
    produced = tmp_path / "board.jpg"
    produced.write_bytes(b"fresh")
    seen = {}

    def fake_sub(args, timeout, extra_env=None):
        seen["args"] = args
        evidence = json.dumps(
            {
                "generated_count": 2,
                "total_slots": 2,
                "cache_hit_count": 0,
                "external_call_count": 2,
                "provider_counts": {"rsta": 2},
            },
            ensure_ascii=False,
        )
        return _fake_proc(f"AI_BOARD_EVIDENCE: {evidence}\nAI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir, allow_burn=True, no_cache=True)

    assert result["status"] == "done"
    assert "--no-cache" in seen["args"]
    assert "--allow-cache-miss-burn" in seen["args"]
    assert result["ai_usage"]["cache_disabled"] is True
    assert result["ai_usage"]["external_call_count"] == 2
    assert result["ai_usage"]["cache_hit_count"] == 0


def test_evaluate_render_result_needs_confirmation_maps_through(tmp_path):
    """F2：执行器 status='needs_confirmation' → quality_status 同名（非 blocked/done），
    且 cache_miss_* 进 metrics 供前端确认卡展示。"""
    quality = render_quality.evaluate_render_result(
        {
            "status": "needs_confirmation",
            "output_path": None,
            "blocking_issue_count": 0,
            "cache_miss_count": 2,
            "cache_miss_total": 3,
            "cache_miss_est_cost_usd": 0.114,
            "cache_miss_est_seconds": 300,
        }
    )
    assert quality["quality_status"] == "needs_confirmation"
    assert quality["can_publish"] is False
    assert quality["metrics"]["cache_miss_count"] == 2
    assert quality["metrics"]["cache_miss_est_cost_usd"] == 0.114
    assert quality["metrics"]["cache_miss_est_seconds"] == 300


def test_evaluate_render_result_held_maps_to_blocked_even_with_board(tmp_path):
    """render_quality：held_gate 存在 → quality_status='blocked'，即使诊断板 output 存在（G2）。"""
    board = tmp_path / "final-board.jpg"
    board.write_bytes(b"x")
    quality = render_quality.evaluate_render_result(
        {
            "status": "blocked",
            "output_path": str(board),
            "blocking_issue_count": 1,
            "held_gate": "pair",
            "held_reason": "front eye_ratio=1.46",
            "render_error": "front eye_ratio=1.46",
        }
    )
    assert quality["quality_status"] == "blocked"
    assert quality["quality_score"] <= 60.0
    assert quality["can_publish"] is False
    assert quality["metrics"]["held_gate"] == "pair"
    assert quality["metrics"]["held_reason"] == "front eye_ratio=1.46"


def test_evaluate_render_result_done_when_no_held(tmp_path):
    """无 held_gate + 干净出板 → 仍走原 done 逻辑（不回归）。"""
    board = tmp_path / "final-board.jpg"
    board.write_bytes(b"x")
    quality = render_quality.evaluate_render_result(
        {"status": "ok", "output_path": str(board), "blocking_issue_count": 0}
    )
    assert quality["quality_status"] in {"done", "done_with_issues"}
    assert quality["metrics"]["held_gate"] == ""


def test_run_ai_enhanced_render_board_result_wins_over_absent_held(tmp_path, monkeypatch):
    """成功路（有 AI_BOARD_RESULT）仍 status='done'，不被 HELD 逻辑干扰。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "ok.jpg"
    produced.write_bytes(b"ok-bytes")
    out_root = tmp_path / "outroot"

    monkeypatch.setattr(
        render_executor,
        "_run_render_subprocess",
        lambda a, t, extra_env=None: _fake_proc(f"AI_BOARD_RESULT: {produced}\n"),
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)
    assert result["status"] == "done"
    assert "held_gate" not in result


# ----------------------------------------------------------------------
# enqueue() defaults enhance_direction="heal" for render_mode="ai"
# ----------------------------------------------------------------------


def _enqueue_options(options: dict | None, render_mode: str = "ai") -> dict:
    """Simulate enqueue()'s options defaulting without touching the DB."""
    if render_mode == "ai":
        resolved = dict(options or {})
        resolved.setdefault("enhance_direction", "heal")
        return resolved
    return dict(options or {})


def test_enqueue_defaults_heal_when_no_options():
    assert _enqueue_options(None, render_mode="ai")["enhance_direction"] == "heal"


def test_enqueue_defaults_heal_when_options_omit_direction():
    opts = _enqueue_options({"enhance_model": "gemini"}, render_mode="ai")
    assert opts["enhance_direction"] == "heal"
    assert opts["enhance_model"] == "gemini"


def test_enqueue_best_pair_does_not_default_heal():
    assert _enqueue_options(None, render_mode="best-pair").get("enhance_direction") is None


def test_enqueue_explicit_direction_preserved():
    assert _enqueue_options({"enhance_direction": "sharpen"})["enhance_direction"] == "sharpen"


def test_enqueue_explicit_empty_direction_preserved():
    assert _enqueue_options({"enhance_direction": ""})["enhance_direction"] == ""


def test_runtime_resolve_explicit_empty_direction_disables_ai_enhance():
    assert render_queue._resolve_ai_enhance_direction({"enhance_direction": ""}, "ai") == ""


def test_runtime_resolve_missing_direction_defaults_heal_for_ai_mode():
    assert render_queue._resolve_ai_enhance_direction({}, "ai") == "heal"
