"""anchored-sim Phase 4 — effect 投影交付 lane selector.

resolve_effect_pairs（迁移自 calibration harness，反臆造 fail-closed）+ 上线 scope gate
（只放行 owner greenlight 的正脸清晰类型，profile/expression/out-of-scope skip 有原因）。
"""
from __future__ import annotations

from backend.services import effect_delivery_selector as sel
from backend.services import procedure_region_mappings as prm


# --- resolve_effect_pairs（反臆造 fail-closed）---

def test_resolve_effect_pairs_real_tear_trough():
    # 弗缦 = 胶原蛋白填充（2026-06-02 web 核查修正，原误标 HA）；胶原在泪沟复用 HA 视觉行
    # → pair 仍产出，发货不回归。
    pairs, _parsed = sel.resolve_effect_pairs("郭若煊__2026.4.1弗缦1.0注射泪沟")
    assert (prm.PROJECT_COLLAGEN_FILLER, "泪沟") in pairs


def test_resolve_effect_pairs_unknown_brand_drops_fail_closed():
    pairs, parsed = sel.resolve_effect_pairs("测试__某未知牌子注射泪沟")
    assert pairs == []                                  # 未注册品牌 → 不臆造
    assert parsed.get("needs_human_review") or parsed.get("unknown_segments")


# --- scope_gate（上线 scope）---

def test_scope_gate_keeps_launch_clear_types():
    pairs = [
        (prm.PROJECT_HA_FILLER, "泪沟"),
        (prm.PROJECT_HA_FILLER, "苹果肌"),
        (prm.PROJECT_HA_FILLER, "唇"),
        (prm.PROJECT_HA_FILLER, "法令纹"),
        (prm.PROJECT_HA_FILLER, "卧蚕"),
    ]
    in_scope, skipped = sel.scope_gate(pairs)
    assert in_scope == pairs
    assert skipped == []


def test_scope_gate_skips_profile_expression_and_out_of_scope():
    pairs = [
        (prm.PROJECT_HA_FILLER, "泪沟"),       # keep
        (prm.PROJECT_HA_FILLER, "鼻背"),       # profile
        (prm.PROJECT_HA_FILLER, "下巴"),       # profile
        (prm.PROJECT_BOTOX, "川字"),           # expression
        (prm.PROJECT_HA_FILLER, "太阳穴"),     # 已注册 frontal 但未greenlight
        (prm.PROJECT_BOTOX, "咬肌"),           # 已注册 frontal 但未greenlight（12周肉毒）
    ]
    in_scope, skipped = sel.scope_gate(pairs)
    assert in_scope == [(prm.PROJECT_HA_FILLER, "泪沟")]
    joined = " ".join(skipped)
    assert "profile_only:鼻背" in joined
    assert "profile_only:下巴" in joined
    assert "expression_only:川字" in joined
    assert "out_of_launch_scope:太阳穴" in joined
    assert "out_of_launch_scope:咬肌" in joined


# --- select_effect_eligible（lane discover）---

def test_select_effect_eligible_classifies_with_reasons():
    res = sel.select_effect_eligible(
        [
            "郭若煊__2026.4.1弗缦1.0注射泪沟",   # eligible（泪沟 in scope）
            "测试__某未知牌子注射泪沟",            # ineligible（无 pair）
        ]
    )
    by_name = {r["case_name"]: r for r in res}
    tear = by_name["郭若煊__2026.4.1弗缦1.0注射泪沟"]
    assert tear["eligible"] is True
    assert (prm.PROJECT_COLLAGEN_FILLER, "泪沟") in tear["effect_pairs"]  # 弗缦=胶原（6-02 修正）
    unknown = by_name["测试__某未知牌子注射泪沟"]
    assert unknown["eligible"] is False
    assert any("no_evidence_anchored_pairs" in r for r in unknown["skip_reasons"])


def test_scope_gate_profile_only_yields_no_in_scope():
    # 只有侧脸主导部位 → 无 in-scope pair（lane 该 case ineligible）。
    in_scope, skipped = sel.scope_gate([(prm.PROJECT_HA_FILLER, "鼻背")])
    assert in_scope == []
    assert skipped and "profile_only:鼻背" in skipped[0]


# --- 源图质量门（人脸计数；mediapipe 懒加载 + fail-open，逻辑用 monkeypatch 测，CI 安全）---

def test_source_quality_single_face_ok(monkeypatch):
    # 干净术前单图 = 1 张脸 → 放行（None）。
    monkeypatch.setattr(sel, "count_baseline_faces", lambda _p: 1)
    assert sel.source_quality_suspect("clean.png") is None


def test_source_quality_multi_face_flagged(monkeypatch):
    # 术前｜术后双拼板 = 2 张脸 → 标 suspect（康巧佳唇案的真实失败模式）。
    monkeypatch.setattr(sel, "count_baseline_faces", lambda _p: 2)
    assert sel.source_quality_suspect("board.png") == sel.SOURCE_MULTIFACE_REASON


def test_source_quality_fail_open_when_face_count_unavailable(monkeypatch):
    # 人脸数不可测（None）→ fail-OPEN 放行（held 队列兜底，不静默拦干净 case）。
    monkeypatch.setattr(sel, "count_baseline_faces", lambda _p: None)
    assert sel.source_quality_suspect("unknown.png") is None


def test_source_quality_zero_faces_not_a_board(monkeypatch):
    # 0 脸（纯色/检测不到）不是「板」信号 → 放行。
    monkeypatch.setattr(sel, "count_baseline_faces", lambda _p: 0)
    assert sel.source_quality_suspect("noface.png") is None


def test_count_baseline_faces_fail_open_without_mediapipe(monkeypatch):
    # 模拟 CI（无 mediapipe）：懒 import 抛错 → None（保模块可 import、CI collection 不崩）。
    import builtins

    real_import = builtins.__import__

    def _no_mediapipe(name, *args, **kwargs):
        if name == "mediapipe" or name.startswith("mediapipe."):
            raise ImportError("simulated: no mediapipe in CI venv")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_mediapipe)
    assert sel.count_baseline_faces("any.png") is None
