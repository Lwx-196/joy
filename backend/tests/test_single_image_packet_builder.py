"""Tests for backend/scripts/single_image_packet_builder.py (L-140 single-image gate).

Discovery / selection are reused from focal_p4_packet_builder (tested there); here
we cover the single-image-specific wiring: item shape (raw vs enhanced + probes +
prescreen + judge_profile), stub path, no-op drop, packet assembly, originals
untouched, and the保真 judge-prompt profile switch.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from backend.scripts import single_image_packet_builder as sib
from backend.scripts.focal_p4_packet_builder import discover_cases

KW = {"泪沟": "x", "面颊": "x", "下巴": "x", "苹果肌": "x"}


def _phase_fn(name: str):
    if "术前" in name:
        return "before"
    if "术后" in name:
        return "after"
    return None


def _photo(path, size=(600, 800), seed=2):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0 : size[1], 0 : size[0]]
    base = np.clip(150 + 25 * np.sin(xx / 22.0) + rng.integers(-10, 10, size=(size[1], size[0])), 0, 255).astype(np.uint8)
    Image.fromarray(np.stack([base, np.clip(base * 0.8 + 20, 0, 255).astype(np.uint8), np.clip(base * 0.6 + 40, 0, 255).astype(np.uint8)], axis=-1), "RGB").save(path, quality=95)


def _make_case(root, folder):
    case = root / folder
    case.mkdir(parents=True, exist_ok=True)
    _photo(case / "术前.jpg", seed=1)
    _photo(case / "术后.jpg", seed=2)
    return case


def _focal_fake_enhance(after_path, focus, out_dir):
    """A real (focal-only) sharpen so probes compute on genuine pixels."""
    from backend.services.focal_mask_generator import generate_focus_mask

    out_dir.mkdir(parents=True, exist_ok=True)
    raw = Image.open(after_path).convert("RGB")
    sharp = raw.filter(ImageFilter.UnsharpMask(2, 150, 2))
    mask_p = generate_focus_mask(after_path, focus, output_path=out_dir / "m.png")
    m = np.asarray(Image.open(mask_p).convert("L").resize(raw.size)) > 127
    arr = np.asarray(raw).copy()
    arr[m] = np.asarray(sharp)[m]
    out = out_dir / "enhanced.png"
    Image.fromarray(arr, "RGB").save(out)
    return out


def test_build_item_shape_with_probes(tmp_path):
    _make_case(tmp_path / "src", "徐某/注射泪沟")
    spec = discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    orig = spec.after_path.read_bytes()

    item = sib.build_item(
        spec, arm="classical", scratch_root=tmp_path / "scratch",
        enhance_fn=_focal_fake_enhance, require_enhancement=True,
    )
    assert item["ab_unit_id"] == spec.slug
    assert item["judge_profile"] == "single_image_fidelity"
    assert item["judge_view"] == "focal"
    assert item["criteria"] == sib.ARM_CRITERIA
    # source_path = bounded judge crop; full_res_path = native deliverable.
    assert item["baseline"]["source_path"].endswith("judge_baseline.jpg")
    assert "raw__" in item["baseline"]["full_res_path"]
    assert item["candidate"]["source_path"].endswith("judge_candidate.jpg")
    assert item["candidate"]["full_res_path"].endswith("enhanced.png")
    assert len(item["focal_bbox"]) == 4
    assert item["prescreen"]["probes"] is not None
    assert item["prescreen"]["verdict"]["passed"] in (True, False)
    # Original after image never mutated.
    assert spec.after_path.read_bytes() == orig


def test_build_item_stub_is_raw_copy(tmp_path):
    _make_case(tmp_path / "src", "李某/注射面颊")
    spec = discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    item = sib.build_item(
        spec, arm="classical", scratch_root=tmp_path / "scratch",
        enhance_fn=None, require_enhancement=False,
    )
    assert item["candidate"]["source_path"].endswith("judge_candidate.jpg")
    assert "enhanced__" in item["candidate"]["full_res_path"]
    assert item["prescreen"]["probes"] is None


def test_build_item_noop_raises(tmp_path):
    _make_case(tmp_path / "src", "王某/注射下巴")
    spec = discover_cases(tmp_path / "src", KW, _phase_fn)[0]

    def noop(after_path, focus, out_dir):
        return after_path  # silent-fail contract

    import pytest

    with pytest.raises(RuntimeError, match="no-op"):
        sib.build_item(
            spec, arm="classical", scratch_root=tmp_path / "scratch",
            enhance_fn=noop, require_enhancement=True,
        )


def test_build_packet_drops_noop_and_counts(tmp_path):
    _make_case(tmp_path / "src", "A/注射泪沟")
    _make_case(tmp_path / "src", "B/注射面颊")
    specs = discover_cases(tmp_path / "src", KW, _phase_fn)

    calls = {"n": 0}

    def enhance_first_noop_second(after_path, focus, out_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            return after_path  # first case no-ops → dropped
        return _focal_fake_enhance(after_path, focus, out_dir)

    packet = sib.build_packet(
        specs, arm="classical", scratch_root=tmp_path / "scratch",
        enhance_fn=enhance_first_noop_second, stub=False,
    )
    assert packet["scope"] == "single_image_fidelity_packet_v1"
    assert packet["arm"] == "classical"
    assert packet["judge_item_count"] == 1
    assert packet["dropped_count"] == 1
    assert packet["judge_item_count"] + packet["dropped_count"] == len(specs)
    assert packet["prescreen_pass"] + packet["prescreen_fail"] <= packet["judge_item_count"]


def test_build_packet_stub_shape(tmp_path):
    _make_case(tmp_path / "src", "A/注射泪沟")
    specs = discover_cases(tmp_path / "src", KW, _phase_fn)
    packet = sib.build_packet(
        specs, arm="classical", scratch_root=tmp_path / "scratch",
        enhance_fn=None, stub=True,
    )
    assert packet["judge_item_count"] == 1
    assert "STUB" in packet["note"]
    item = packet["judge_items"][0]
    assert item["judge_profile"] == "single_image_fidelity"


# --- judge prompt profile switch -------------------------------------------

def test_fidelity_judge_profile_switches_framing():
    from backend.scripts import comfyui_vlm_judge_runner as judge

    fid_item = {
        "ab_unit_id": "x", "judge_profile": "single_image_fidelity",
        "focus_targets": ["泪沟"], "criteria": ["sharpness_clarity: ..."],
    }
    board_item = {"ab_unit_id": "y", "criteria": ["overall delivery quality"]}

    fid_prompt = judge._judge_prompt(fid_item)
    board_prompt = judge._judge_prompt(board_item)

    assert "FIDELITY" in fid_prompt
    assert "ENHANCED version" in fid_prompt
    assert "delivery quality judge" not in fid_prompt
    # Default (no profile) keeps the original board framing.
    assert "delivery quality judge" in board_prompt
    assert "FIDELITY judge" not in board_prompt
