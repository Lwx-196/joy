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

import json
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

    def fake_render(case_dir: Path, brand: str, template: str, selection_plan=None):
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

    def fake_render(case_dir: Path, brand: str, template: str, selection_plan=None):
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

    def bad_render(case_dir: Path, brand: str, template: str, selection_plan=None):
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

    def fake_render(case_dir: Path, brand: str, template: str, selection_plan=None):
        board = case_dir / "board.jpg"
        board.write_bytes(b"board")
        return {"output_path": str(board)}

    packet = builder.build_packet(
        specs, tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=None, render_fn=fake_render, stub=True,
        baseline_strategy="render",  # this test exercises the dual-render path
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


# --- existing-board baseline strategy (owner decision 2026-05-29) ------------

def _make_case_with_board(root: Path, folder: str, brand="fumei", template="single-compare") -> Path:
    case = _make_case(root, folder)
    board_dir = case / ".case-layout-output" / brand / template / "render"
    board_dir.mkdir(parents=True)
    (board_dir / "final-board.jpg").write_bytes(b"SHIPPED-PRODUCT-BOARD")
    return case


def test_build_packet_existing_board_baseline_no_render_of_baseline(tmp_path):
    _make_case_with_board(tmp_path / "src", "A/注射泪沟")
    specs = builder.discover_cases(tmp_path / "src", KW, _phase_fn)

    rendered_arms = []

    def fake_render(case_dir: Path, brand: str, template: str, selection_plan=None):
        # scratch layout: <scratch>/<arm>/<slug>/<case_dir.name>
        rendered_arms.append(case_dir.parent.parent.name)  # "baseline"/"candidate"
        board = case_dir / "board.jpg"
        board.write_bytes(b"render")
        return {"output_path": str(board)}

    packet = builder.build_packet(
        specs, tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=None, render_fn=fake_render, stub=True,
        baseline_strategy="existing-board",
    )
    assert packet["judge_item_count"] == 1
    item = packet["judge_items"][0]
    # Baseline = the shipped board, NOT a re-render.
    assert item["baseline"]["source_path"].endswith("final-board.jpg")
    assert item["baseline"]["source_path"].count(".case-layout-output") == 1
    assert "shipped" in item["baseline"]["role_note"]
    # Only the CANDIDATE arm was rendered (baseline arm never touched render).
    assert rendered_arms == ["candidate"]


def test_candidate_focal_noop_is_dropped(tmp_path):
    # FOCAL silently fails (ComfyUI down) → returns input unchanged → the case
    # must be DROPPED, never yield a candidate board identical to the raw input.
    _make_case_with_board(tmp_path / "src", "A/注射泪沟")
    specs = builder.discover_cases(tmp_path / "src", KW, _phase_fn)

    def noop_enhance(after_path: Path, arm: str, sp):
        return after_path  # K-1 silent-fail contract: returns input unchanged

    def fake_render(case_dir: Path, brand: str, template: str, selection_plan=None):
        board = case_dir / "board.jpg"
        board.write_bytes(b"render")
        return {"output_path": str(board)}

    packet = builder.build_packet(
        specs, tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=noop_enhance, render_fn=fake_render, stub=False,
        baseline_strategy="existing-board",
    )
    assert packet["judge_item_count"] == 0
    assert packet["dropped_count"] == 1
    assert "no-op" in packet["dropped"][0]["reason"]


def test_detect_existing_render_parses_brand_template_and_selected_afters(tmp_path):
    case = _make_case(tmp_path / "src", "林某/注射面颊，下巴")
    render_dir = case / ".case-layout-output" / "fumei" / "tri-compare" / "render"
    render_dir.mkdir(parents=True)
    (render_dir / "final-board.jpg").write_bytes(b"board")
    (render_dir / "manifest.final.json").write_text(json.dumps({
        "groups": [{"selected_slots": {
            "front": {"after": {"name": "术后1.jpg"}},
            "oblique": {"after": {"name": "术后2.jpg"}},
            "side": {"after": {"render_filename": "术后3.jpg"}},
        }}]
    }), encoding="utf-8")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    det = builder.detect_existing_render(spec)
    assert det is not None
    assert det["brand"] == "fumei" and det["template"] == "tri-compare"
    assert det["after_names"] == ["术后1.jpg", "术后2.jpg", "术后3.jpg"]


def test_build_arm_after_names_override_limits_enhancement(tmp_path):
    # Case with many after images; override → only the listed ones get enhanced.
    case = tmp_path / "src" / "多图" / "注射下巴"
    case.mkdir(parents=True)
    (case / "术前.jpg").write_bytes(b"b")
    for i in range(1, 6):
        (case / f"术后{i}.jpg").write_bytes(b"a")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    enhanced_names = []

    def fake_enhance(after_path: Path, arm: str, sp):
        enhanced_names.append(after_path.name)
        out = after_path.parent / f"enh_{after_path.name}"
        out.write_bytes(b"E")
        return out

    def fake_render(case_dir, brand, template, selection_plan=None):
        b = case_dir / "board.jpg"
        b.write_bytes(b"x")
        return {"output_path": str(b)}

    builder.build_arm(
        spec, "candidate", tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=fake_enhance, render_fn=fake_render,
        after_names_override=["术后1.jpg", "术后3.jpg"],
    )
    assert sorted(enhanced_names) == ["术后1.jpg", "术后3.jpg"]


def test_build_packet_existing_board_missing_is_dropped(tmp_path):
    # A case WITHOUT an existing board → dropped under existing-board strategy.
    _make_case(tmp_path / "src", "B/注射下巴")  # no .case-layout-output
    specs = builder.discover_cases(tmp_path / "src", KW, _phase_fn)

    def fake_render(case_dir: Path, brand: str, template: str, selection_plan=None):
        board = case_dir / "board.jpg"
        board.write_bytes(b"render")
        return {"output_path": str(board)}

    packet = builder.build_packet(
        specs, tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=None, render_fn=fake_render, stub=True,
        baseline_strategy="existing-board",
    )
    assert packet["judge_item_count"] == 0
    assert packet["dropped_count"] == 1
    assert "no existing final-board" in packet["dropped"][0]["reason"]


# --- F-3 fix: candidate reuses the shipped board's exact slot selection -------

def test_detect_existing_render_recovers_selection_plan(tmp_path):
    case = _make_case(tmp_path / "src", "林某/注射面颊，下巴")
    render_dir = case / ".case-layout-output" / "fumei" / "tri-compare" / "render"
    render_dir.mkdir(parents=True)
    (render_dir / "final-board.jpg").write_bytes(b"board")
    (render_dir / "manifest.final.json").write_text(json.dumps({
        "groups": [{"selected_slots": {
            "front": {
                "before": {"name": "术前1.jpg", "render_filename": "术前1.jpg",
                           "path": "/abs/术前1.jpg"},
                "after": {"name": "术后1.jpg", "render_filename": "术后1.jpg"},
            },
            "oblique": {
                "before": {"name": "术前2.jpg"},
                "after": {"name": "术后2.jpg"},
            },
        }}]
    }), encoding="utf-8")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    det = builder.detect_existing_render(spec)
    plan = det["selection_plan"]
    assert plan is not None
    assert plan["policy"] == "p4_baseline_slot_reuse"
    assert set(plan["slots"]) == {"front", "oblique"}
    assert plan["slots"]["front"]["before"]["name"] == "术前1.jpg"
    assert plan["slots"]["front"]["after"]["render_filename"] == "术后1.jpg"
    # Stale absolute path is NOT pinned — only lean match keys carry.
    assert "path" not in plan["slots"]["front"]["before"]


def test_detect_existing_render_no_plan_when_slot_lacks_before(tmp_path):
    # A slot with only `after` (no before) cannot be pinned → no plan (falls back
    # to auto-selection); after_names still recovered for enhancement targeting.
    case = _make_case(tmp_path / "src", "林某/注射下巴")
    render_dir = case / ".case-layout-output" / "fumei" / "single-compare" / "render"
    render_dir.mkdir(parents=True)
    (render_dir / "final-board.jpg").write_bytes(b"board")
    (render_dir / "manifest.final.json").write_text(json.dumps({
        "groups": [{"selected_slots": {"front": {"after": {"name": "术后1.jpg"}}}}]
    }), encoding="utf-8")
    spec = builder.discover_cases(tmp_path / "src", KW, _phase_fn)[0]
    det = builder.detect_existing_render(spec)
    assert det["selection_plan"] is None
    assert det["after_names"] == ["术后1.jpg"]


def test_build_packet_candidate_reuses_baseline_slot_plan(tmp_path):
    case = _make_case_with_board(tmp_path / "src", "A/注射下巴")
    render_dir = case / ".case-layout-output" / "fumei" / "single-compare" / "render"
    (render_dir / "manifest.final.json").write_text(json.dumps({
        "groups": [{"selected_slots": {
            "front": {
                "before": {"name": "术前.jpg", "render_filename": "术前.jpg"},
                "after": {"name": "术后.jpg", "render_filename": "术后.jpg"},
            },
        }}]
    }), encoding="utf-8")
    specs = builder.discover_cases(tmp_path / "src", KW, _phase_fn)
    seen = {}

    def fake_render(case_dir, brand, template, selection_plan=None):
        seen["plan"] = selection_plan
        board = case_dir / "board.jpg"
        board.write_bytes(b"render")
        return {"output_path": str(board)}

    packet = builder.build_packet(
        specs, tmp_path / "scratch",
        brand="fumei", template="single-compare",
        enhance_fn=None, render_fn=fake_render, stub=True,
        baseline_strategy="existing-board",
    )
    assert packet["judge_item_count"] == 1
    item = packet["judge_items"][0]
    assert item["candidate"]["slot_reuse"] is True
    assert "baseline-slot-reuse" in item["candidate"]["role_note"]
    # The candidate render actually received the recovered slot plan (F-3 fix).
    assert seen["plan"] is not None
    assert seen["plan"]["slots"]["front"]["after"]["render_filename"] == "术后.jpg"
    assert "reuses baseline slots (F-3 fix) for 1/1" in packet["note"]
