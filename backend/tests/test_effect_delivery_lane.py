"""anchored-sim Phase 4 — effect 投影交付 lane（export_delivery_batch 接线）.

验：opt-in 默认 OFF / 全件 held（BLOCKER-C：judge winner=candidate 仍 held、passed==[]）/
生成缓存命中不重烧 gpt-image-2 / scope gate 只投正脸清晰类型（profile 鼻背 skip）。
判官/出图/discover 全 fake，0 quota。
"""
from __future__ import annotations

import types
from pathlib import Path

from PIL import Image

from backend import ai_generation_adapter as aga
from backend.scripts import export_delivery_batch as mod
from backend.scripts import focal_p4_packet_builder as fp4


def _spec(tmp_path: Path, customer: str, case_leaf: str):
    case_dir = tmp_path / customer / case_leaf
    case_dir.mkdir(parents=True, exist_ok=True)
    before = case_dir / "before.png"
    Image.new("RGB", (64, 64), (128, 128, 128)).save(before)
    return types.SimpleNamespace(
        case_dir=case_dir, before_path=before, slug=f"{customer}__{case_leaf}"
    )


class _FakeVerdict:
    def __init__(self, verdict="pass", winner_role="candidate", hard_veto_reason=None):
        self.verdict = verdict
        self.winner_role = winner_role
        self.hard_veto_reason = hard_veto_reason
        self.confidence = 0.85
        self.content_hash = "fakehash"

    @property
    def deliverable(self) -> bool:  # winner=candidate→pass→True（若信任会 auto-ship）
        return self.verdict == "pass"

    @property
    def reason(self) -> str:
        return f"judge={self.winner_role}"


class _FakeQA:
    def __init__(self, verdict=None):
        self._v = verdict or _FakeVerdict()
        self.calls = 0

    def assess(self, **kw):
        self.calls += 1
        return self._v


def _wire(monkeypatch, tmp_path: Path, specs, *, gen_counter: list):
    monkeypatch.setattr(fp4, "discover_cases", lambda *a, **k: specs)
    cache = tmp_path / "_cache"
    monkeypatch.setattr(mod, "_effect_cache_dir", lambda: cache)

    def _fake_run(**kw):
        gen_counter.append(1)
        raw = tmp_path / f"gen_{len(gen_counter)}.png"
        Image.new("RGB", (64, 64), (200, 50, 50)).save(raw)  # 不同于 baseline 灰
        return {"output_refs": [{"kind": "generated_raw", "path": str(raw)}], "audit": {}}

    monkeypatch.setattr(aga, "run_ps_model_router_after_simulation", _fake_run)
    return cache


def test_effect_lane_opt_in_default_off(monkeypatch):
    monkeypatch.delenv("CASE_WORKBENCH_EFFECT_DELIVERY", raising=False)
    assert mod._effect_projection_enabled_default() is False
    monkeypatch.setenv("CASE_WORKBENCH_EFFECT_DELIVERY", "1")
    assert mod._effect_projection_enabled_default() is True


def test_effect_lane_all_held_even_when_judge_says_candidate(tmp_path, monkeypatch):
    # BLOCKER-C：judge winner=candidate（deliverable=True）→ 该件仍 held，passed 恒空。
    specs = [_spec(tmp_path, "郭若煊", "郭若煊__2026.4.1弗缦1.0注射泪沟")]
    gen: list = []
    _wire(monkeypatch, tmp_path, specs, gen_counter=gen)
    qa = _FakeQA(_FakeVerdict(verdict="pass", winner_role="candidate"))
    out = tmp_path / "delivery"
    passed, held = mod._run_effect_delivery(out, dry_run=False, qa=qa, cases_root=tmp_path)
    assert passed == []  # 全件 held（lane 层硬覆盖，不读 verdict.deliverable）
    assert len(held) == 1
    assert held[0]["advisory_judge"]["winner_role"] == "candidate"
    assert (out / "郭若煊" / "effect-projection").is_dir()


def test_effect_lane_generation_cache_skips_reburn(tmp_path, monkeypatch):
    specs = [_spec(tmp_path, "郭若煊", "郭若煊__2026.4.1弗缦1.0注射泪沟")]
    gen: list = []
    _wire(monkeypatch, tmp_path, specs, gen_counter=gen)
    qa = _FakeQA()
    out = tmp_path / "delivery"
    mod._run_effect_delivery(out, dry_run=True, qa=qa, cases_root=tmp_path)
    assert len(gen) == 1  # 第一次真生成
    mod._run_effect_delivery(out, dry_run=True, qa=qa, cases_root=tmp_path)
    assert len(gen) == 1  # 第二次命中缓存，不重烧 gpt-image-2 quota


def test_effect_lane_scope_gate_skips_profile(tmp_path, monkeypatch):
    # 侧脸主导（鼻背）case → 无 in-scope pair → 不投影、不入 held。
    specs = [_spec(tmp_path, "测试", "测试__海魅鼻背")]
    gen: list = []
    _wire(monkeypatch, tmp_path, specs, gen_counter=gen)
    qa = _FakeQA()
    passed, held = mod._run_effect_delivery(tmp_path / "d", dry_run=True, qa=qa, cases_root=tmp_path)
    assert passed == [] and held == []  # 鼻背=profile → scope gate skip
    assert gen == []  # 没投影 → 不烧 quota


def test_effect_lane_no_visible_change_held_hard_veto(tmp_path, monkeypatch):
    specs = [_spec(tmp_path, "郭若煊", "郭若煊__2026.4.1弗缦1.0注射泪沟")]
    gen: list = []
    _wire(monkeypatch, tmp_path, specs, gen_counter=gen)
    from backend.services.effect_delivery_qa import REASON_NO_VISIBLE_CHANGE

    qa = _FakeQA(_FakeVerdict(verdict="fail", winner_role="", hard_veto_reason=REASON_NO_VISIBLE_CHANGE))
    passed, held = mod._run_effect_delivery(tmp_path / "d2", dry_run=True, qa=qa, cases_root=tmp_path)
    assert passed == [] and len(held) == 1
    assert held[0]["advisory_judge"]["hard_veto_reason"] == REASON_NO_VISIBLE_CHANGE
