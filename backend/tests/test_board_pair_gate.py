"""G2 板级配对 gate 单测：灾难级阈值 + 标定实数回放 + fail-open 边界。"""

from __future__ import annotations

from backend.services import board_pair_gate as gate


def _plan(slots):
    return {"version": 1, "slots": slots}


def _front(ratio, **extra):
    rec = {
        "slot": "front",
        "strategy": "independent_scale_zoom_after",
        "pair_eye_signal": {"valid": True, "eye_ratio": ratio},
    }
    rec.update(extra)
    return rec


# === C 灾难 3 板实测值必须 HELD（2026-06-11 标定）===

def test_zengyuqin_catastrophe_held():
    r = gate.evaluate_pair_coverage(_plan([_front(1.666)]))
    assert r["verdict"] == "held"
    assert r["violations"][0]["eye_ratio"] == 1.666


def test_gaoyajing_catastrophe_held():
    assert gate.evaluate_pair_coverage(_plan([_front(0.677)]))["verdict"] == "held"


def test_huangjingrong_catastrophe_held():
    assert gate.evaluate_pair_coverage(_plan([_front(0.632)]))["verdict"] == "held"


# === clean 集极值必须 pass（零误伤：胡志超 0.863 / 郭璟琳 1.069）===

def test_clean_low_extreme_passes():
    r = gate.evaluate_pair_coverage(_plan([_front(0.863)]))
    assert r["verdict"] == "pass"
    assert not r["fail_open"]


def test_clean_high_extreme_passes():
    assert gate.evaluate_pair_coverage(_plan([_front(1.069)]))["verdict"] == "pass"


def test_boundary_inclusive():
    assert gate.evaluate_pair_coverage(_plan([_front(0.78)]))["verdict"] == "pass"
    assert gate.evaluate_pair_coverage(_plan([_front(1.30)]))["verdict"] == "pass"
    assert gate.evaluate_pair_coverage(_plan([_front(0.779)]))["verdict"] == "held"
    assert gate.evaluate_pair_coverage(_plan([_front(1.301)]))["verdict"] == "held"


# === 非 front 槽不评（oblique/side 眼距是 yaw 噪音，标定证实）===

def test_side_extreme_ignored_fail_open():
    rec = {
        "slot": "side",
        "pair_eye_signal": {"valid": True, "eye_ratio": 0.632},
    }
    r = gate.evaluate_pair_coverage(_plan([rec]))
    assert r["verdict"] == "pass"
    assert r["fail_open"]


def test_front_held_even_with_clean_side():
    side = {"slot": "side", "pair_eye_signal": {"valid": True, "eye_ratio": 1.0}}
    r = gate.evaluate_pair_coverage(_plan([side, _front(1.666)]))
    assert r["verdict"] == "held"


# === manual override 槽跳过（人工已修不得永久 HELD）===

def test_manual_transform_slot_skipped():
    rec = _front(1.666, manual_preop_transform={"enabled": True})
    r = gate.evaluate_pair_coverage(_plan([rec]))
    assert r["verdict"] == "pass"
    assert r["fail_open"]  # 唯一 front 信号被跳过 → 无可评信号


def test_manual_transform_on_separate_record_skips_slot():
    sig = _front(1.666)
    manual = {
        "slot": "front",
        "strategy": "manual_layer_transform_after_auto_alignment",
        "manual_preop_transform": {"enabled": True},
    }
    r = gate.evaluate_pair_coverage(_plan([sig, manual]))
    assert r["verdict"] == "pass"


# === fail-open 边界 ===

def test_missing_render_plan_fail_open():
    r = gate.evaluate_pair_coverage(None)
    assert r["verdict"] == "pass"
    assert r["fail_open"]


def test_empty_slots_fail_open():
    assert gate.evaluate_pair_coverage(_plan([]))["fail_open"]


def test_invalid_signal_fail_open():
    rec = {"slot": "front", "pair_eye_signal": {"valid": False, "reason": "eye_distance_unavailable"}}
    r = gate.evaluate_pair_coverage(_plan([rec]))
    assert r["verdict"] == "pass"
    assert r["fail_open"]


def test_zero_ratio_fail_open():
    rec = {"slot": "front", "pair_eye_signal": {"valid": True, "eye_ratio": 0}}
    assert gate.evaluate_pair_coverage(_plan([rec]))["fail_open"]


def test_malformed_records_fail_open():
    r = gate.evaluate_pair_coverage({"slots": ["not-a-dict", 42]})
    assert r["verdict"] == "pass"
    assert r["fail_open"]


def test_signals_observability():
    r = gate.evaluate_pair_coverage(_plan([_front(1.05)]))
    assert r["signals"] == {"front": 1.05}
