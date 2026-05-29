"""Tests for backend/scripts/focal_p4_packet_builder.py (P4 formal-gate builder).

Covers the wiring P4 T4.2 owns (the enhancement adapters + the real layout
render have their own tests; here we mock both and assert builder behaviour):
  - resolve_focus_targets: folder-name → anatomical regions (substring, dedupe, order)
  - discover_cases: finds before/after + focus-eligible dirs; skips the rest
  - select_cases: focus-region diversity round-robin, multi-focus preference, n cap
  - build_arm: scratch isolation (ORIGINALS UNTOUCHED), enhance applied, board returned
  - build_packet: t51_blind_judge_packet_v1 shape, both arms per item
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.scripts import focal_p4_packet_builder as builder

# A trimmed anatomical-keyword map (real values from ai_generation_adapter
# are irrelevant to substring matching).
KW = {"泪沟": "x", "面颊": "x", "下巴": "x", "唇": "x", "法令纹": "x", "卧蚕": "x"}


def _phase_fn(name: str) -> str | None:
    if "术前" in name or "before" in name:
        return "before"
    if "术后" in name or "after" in name:
        return "after"
    return None


def _make_case(root: Path, folder: str, *, before=True, after=True) -> Path:
    case_dir = root / folder
    case_dir.mkdir(parents=True, exist_ok=True)
    if before:
        (case_dir / "术前.jpg").write_bytes(b"before-bytes")
    if after:
        (case_dir / "术后.jpg").write_bytes(b"after-bytes")
    return case_dir


# --- resolve_focus_targets -------------------------------------------------

def test_resolve_focus_single():
    assert builder.resolve_focus_targets("2026.4.1弗缦1.0注射泪沟", KW) == ["泪沟"]


def test_resolve_focus_multi_order_preserved_and_deduped():
    # Keyword iteration order (dict order) governs output, deduped.
    got = builder.resolve_focus_targets("玻尿酸注射面颊，下巴，下巴", KW)
    assert got == ["面颊", "下巴"]


def test_resolve_focus_none_when_no_match():
    assert builder.resolve_focus_targets("童颜针全脸", KW) == []


# --- discover_cases --------------------------------------------------------

def test_discover_finds_focus_eligible_pairs(tmp_path):
    _make_case(tmp_path, "王嘉琦/25.6.26卧蚕，泪沟")
    _make_case(tmp_path, "郭若煊/2026.4.1注射泪沟")
    specs = builder.discover_cases(tmp_path, KW, _phase_fn)
    folders = {s.case_dir.name for s in specs}
    assert folders == {"25.6.26卧蚕，泪沟", "2026.4.1注射泪沟"}
    bywenj = next(s for s in specs if "卧蚕" in s.case_dir.name)
    # KW iteration order (泪沟 before 卧蚕) governs the result order.
    assert bywenj.focus_targets == ["泪沟", "卧蚕"]


def test_discover_skips_no_after(tmp_path):
    _make_case(tmp_path, "无后/注射泪沟", after=False)
    assert builder.discover_cases(tmp_path, KW, _phase_fn) == []


def test_discover_skips_no_focus_in_name(tmp_path):
    _make_case(tmp_path, "张三/童颜针全脸")  # has pair, no focus keyword
    assert builder.discover_cases(tmp_path, KW, _phase_fn) == []


def test_discover_skips_layout_output_dirs(tmp_path):
    case = _make_case(tmp_path, "李四/注射下巴")
    # A nested render-output dir must never be treated as a case.
    out = case / ".case-layout-output" / "fumei" / "single-compare"
    out.mkdir(parents=True)
    (out / "术前.jpg").write_bytes(b"x")
    (out / "术后.jpg").write_bytes(b"x")
    specs = builder.discover_cases(tmp_path, KW, _phase_fn)
    assert [s.case_dir.name for s in specs] == ["注射下巴"]


# --- select_cases ----------------------------------------------------------

def _spec(folder: str, focus: list[str]) -> builder.CaseSpec:
    p = Path("/x") / folder
    return builder.CaseSpec(case_dir=p, before_path=p / "b", after_path=p / "a", focus_targets=focus)


def test_select_caps_at_n():
    specs = [_spec(f"c{i}", ["泪沟"]) for i in range(20)]
    assert len(builder.select_cases(specs, 5)) == 5


def test_select_diversifies_regions():
    specs = [_spec(f"lei{i}", ["泪沟"]) for i in range(10)]
    specs += [_spec("xiaba", ["下巴"]), _spec("miajia", ["面颊"])]
    sel = builder.select_cases(specs, 3)
    primaries = {s.focus_targets[0] for s in sel}
    # Round-robin must surface the rare regions, not 3× 泪沟.
    assert "下巴" in primaries and "面颊" in primaries


def test_select_empty():
    assert builder.select_cases([], 5) == []
    assert builder.select_cases([_spec("c", ["泪沟"])], 0) == []


# --- build_arm: scratch isolation + render wiring --------------------------

def test_build_arm_leaves_originals_untouched(tmp_path):
    _make_case(tmp_path / "src", "王某/注射泪沟")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    orig_after_bytes = spec.after_path.read_bytes()

    rendered = {}

    def fake_render(case_dir: Path, brand: str, template: str):
        # The render sees the (enhanced) scratch copy, not the original.
        rendered["case_dir"] = case_dir
        board = case_dir / "board.jpg"
        board.write_bytes(b"board")
        return {"output_path": str(board)}

    def fake_enhance(after_path: Path, arm: str, sp):
        # Simulate enhancement writing a new file the builder copies back in.
        enhanced = after_path.parent / "enhanced.jpg"
        enhanced.write_bytes(b"ENHANCED")
        return enhanced

    board = builder.build_arm(
        spec, "candidate", tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=fake_enhance, render_fn=fake_render,
    )
    assert board.is_file()
    # Original after image is byte-identical (never mutated).
    assert spec.after_path.read_bytes() == orig_after_bytes
    # Scratch after image WAS replaced by the enhanced bytes.
    scratch_after = rendered["case_dir"] / spec.after_path.name
    assert scratch_after.read_bytes() == b"ENHANCED"


def test_build_arm_stub_skips_enhance(tmp_path):
    _make_case(tmp_path / "src", "王某/注射下巴")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]

    def fake_render(case_dir: Path, brand: str, template: str):
        board = case_dir / "board.jpg"
        board.write_bytes(b"board")
        return {"output_path": str(board)}

    board = builder.build_arm(
        spec, "baseline", tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=None, render_fn=fake_render,  # stub: no enhancement
    )
    assert board.is_file()


def test_build_arm_raises_on_missing_output(tmp_path):
    _make_case(tmp_path / "src", "王某/注射唇")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]

    def bad_render(case_dir: Path, brand: str, template: str):
        return {"status": "ok"}  # no output_path

    with pytest.raises(RuntimeError, match="no output_path"):
        builder.build_arm(
            spec, "candidate", tmp_path / "scratch",
            brand="fumei", template="single-compare",
            enhance_fn=None, render_fn=bad_render,
        )


# --- build_packet: t51 shape -----------------------------------------------

def test_build_packet_shape(tmp_path):
    _make_case(tmp_path / "src", "A/注射泪沟")
    _make_case(tmp_path / "src", "B/注射下巴")
    specs = builder.discover_cases(tmp_path / "src", KW, _phase_fn)

    def fake_render(case_dir: Path, brand: str, template: str):
        board = case_dir / "board.jpg"
        board.write_bytes(b"board")
        return {"output_path": str(board)}

    packet = builder.build_packet(
        specs, tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=None, render_fn=fake_render, stub=True,
    )
    assert packet["scope"] == "t51_blind_judge_packet_v1"
    assert packet["judge_item_count"] == 2
    assert "STUB" in packet["note"]
    for item in packet["judge_items"]:
        assert set(item) >= {"ab_unit_id", "focus_targets", "baseline", "candidate"}
        assert item["baseline"]["source_path"].endswith("board.jpg")
        assert item["candidate"]["source_path"].endswith("board.jpg")
        # baseline + candidate boards live in distinct arm scratch dirs.
        assert "/baseline/" in item["baseline"]["source_path"]
        assert "/candidate/" in item["candidate"]["source_path"]
