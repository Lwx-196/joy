"""Phase 2.2 — effect_projection 硬底线接进 run_ps_model_router_after_simulation.

测 _apply_effect_mask_anchor helper（合成图）+ 整条 PS 路线 effect 模式（fake subprocess）
真把 AI 整帧锁回原图（mask 外==原图），且 fidelity 默认逐字 BC（不跑 anchor）。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from PIL import Image

from backend import ai_generation_adapter as adp
from backend.services import procedure_region_mappings as prm


# --- _apply_effect_mask_anchor helper（合成图）---

def test_apply_effect_mask_anchor_locks_outside_to_original(tmp_path: Path):
    original = tmp_path / "after.png"
    Image.new("RGB", (200, 200), (128, 128, 128)).save(original)   # 原图灰
    ai = tmp_path / "ai.png"
    Image.new("RGB", (200, 200), (255, 0, 0)).save(ai)             # AI 整帧红（漂移代理）
    out = tmp_path / "anchored.png"
    result = adp._apply_effect_mask_anchor(
        original_path=original, ai_output_path=ai, focus_targets=["唇"], output_path=out,
    )
    assert result == out and out.is_file()
    anchored = Image.open(out).convert("RGB")
    # 上角（唇 mask 外）= 原图灰，字节级；唇区（下中部）= AI 红
    assert anchored.getpixel((5, 5)) == (128, 128, 128)
    assert anchored.getpixel((100, 156)) == (255, 0, 0)


def test_feather_mask_inward_softens_edge_keeps_outside_zero(tmp_path: Path):
    # 羽化向内：核心保 255、内缘成 alpha 渐变（消拼接缝）、椭圆外恒 0（身份铁线）。
    from PIL import Image, ImageDraw

    mask = Image.new("L", (400, 400), 0)
    ImageDraw.Draw(mask).rectangle([100, 100, 299, 299], fill=255)  # 居中白方块
    mp = tmp_path / "mask.png"
    mask.save(mp)

    feathered = Image.open(adp._feather_mask_inward(mp)).convert("L")
    # 方块外 → 字节级 0（outside_exact 保住）
    assert feathered.getpixel((10, 10)) == 0
    assert feathered.getpixel((350, 350)) == 0
    # 核心 → 仍 ~255（治疗效果不被抹）
    assert feathered.getpixel((200, 200)) >= 250
    # 内缘 → 出现中间 alpha（证明羽化成渐变，非硬阶跃）
    inner_edge = [feathered.getpixel((x, 200)) for x in range(100, 140)]
    assert any(0 < v < 255 for v in inner_edge), "feather must create an alpha ramp"


def test_apply_effect_mask_anchor_k1_fallback_on_bad_input(tmp_path: Path):
    ai = tmp_path / "ai.png"
    Image.new("RGB", (50, 50), (1, 2, 3)).save(ai)
    # 原图不存在 → 失败安全 → 返回原始 AI 输出（不抛、不阻断）
    result = adp._apply_effect_mask_anchor(
        original_path=tmp_path / "nope.png", ai_output_path=ai,
        focus_targets=["唇"], output_path=tmp_path / "out.png",
    )
    assert result == ai


# --- 整条 PS 路线 effect 模式（fake subprocess）---

def _wire_fake_ps(monkeypatch, tmp_path: Path, after_color, ai_color):
    """monkeypatch PS_ENHANCE_SCRIPT + subprocess.run + simulation_job_dir + stress off."""
    out_dir = tmp_path / "job"
    after = tmp_path / "after.png"
    Image.new("RGB", (240, 240), after_color).save(after)
    fake_ai = tmp_path / "ps_generated.png"
    Image.new("RGB", (240, 240), ai_color).save(fake_ai)

    script = tmp_path / "fake_enhance.js"
    script.write_text("// fake", encoding="utf-8")
    monkeypatch.setattr(adp, "PS_ENHANCE_SCRIPT", script)
    monkeypatch.setattr(adp, "simulation_job_dir", lambda job_id: out_dir)
    monkeypatch.setattr(adp.stress, "is_stress_mode", lambda: False)

    def _fake_run(cmd, *a, **k):
        return types.SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"success": True, "imagePath": str(fake_ai),
                               "usedModel": "gpt-image-2"}),
        )

    monkeypatch.setattr(adp.subprocess, "run", _fake_run)
    return after, fake_ai


def test_ps_route_effect_mode_runs_anchor(tmp_path: Path, monkeypatch):
    after, _ = _wire_fake_ps(monkeypatch, tmp_path, (128, 128, 128), (255, 0, 0))
    res = adp.run_ps_model_router_after_simulation(
        job_id=1, after_image_path=after, before_image_path=None,
        focus_targets=["唇"], brand="fumei",
        mode=adp.EFFECT_PROJECTION_MODE,
        effect_pairs=[(prm.PROJECT_HA_FILLER, "唇")], do_not_touch=["苹果肌"],
    )
    assert res["audit"]["policy"]["simulation_mode"] == adp.EFFECT_PROJECTION_MODE
    assert res["audit"]["policy"]["mask_anchored"] is True
    refs = {r["kind"]: r["path"] for r in res["output_refs"]}
    assert "effect_anchored" in refs
    anchored = Image.open(refs["effect_anchored"]).convert("RGB")
    # 硬底线：唇 mask 外角落 == 原图灰；raw AI 角落 == 红（证明确实锚定回原图）
    assert anchored.getpixel((5, 5)) == (128, 128, 128)
    raw = Image.open(refs["generated_raw"]).convert("RGB")
    assert raw.getpixel((5, 5)) == (255, 0, 0)


def test_ps_route_fidelity_mode_bc_no_anchor(tmp_path: Path, monkeypatch):
    after, _ = _wire_fake_ps(monkeypatch, tmp_path, (128, 128, 128), (255, 0, 0))
    res = adp.run_ps_model_router_after_simulation(
        job_id=2, after_image_path=after, before_image_path=None,
        focus_targets=["唇"], brand="fumei",   # mode 默认 fidelity
    )
    assert res["audit"]["policy"]["simulation_mode"] == adp.FIDELITY_MODE
    assert res["audit"]["policy"]["mask_anchored"] is False
    refs = {r["kind"] for r in res["output_refs"]}
    assert "effect_anchored" not in refs            # 不跑 anchor
    assert res["audit"]["effect_anchored_path"] is None


# --- Phase 1: anchor_mode 开关（raw-first 默认给 lane / mask_anchor 默认保 BC）---

def test_ps_route_effect_mode_default_is_mask_anchor_bc(tmp_path: Path, monkeypatch):
    # 不传 anchor_mode → 默认 mask_anchor（保 BC：既有 effect 行为不变）。
    after, _ = _wire_fake_ps(monkeypatch, tmp_path, (128, 128, 128), (255, 0, 0))
    res = adp.run_ps_model_router_after_simulation(
        job_id=3, after_image_path=after, before_image_path=None,
        focus_targets=["唇"], brand="fumei",
        mode=adp.EFFECT_PROJECTION_MODE,
        effect_pairs=[(prm.PROJECT_HA_FILLER, "唇")],
    )
    assert res["audit"]["policy"]["anchor_mode"] == adp.ANCHOR_MODE_MASK
    assert res["audit"]["policy"]["mask_anchored"] is True
    assert "effect_anchored" in {r["kind"] for r in res["output_refs"]}


def test_ps_route_effect_mode_raw_first_skips_anchor(tmp_path: Path, monkeypatch):
    # raw-first（owner 配方）：忠实编辑器全脸协调精修，不锚定 → 最终件 = raw AI 整帧。
    after, _ = _wire_fake_ps(monkeypatch, tmp_path, (128, 128, 128), (255, 0, 0))
    res = adp.run_ps_model_router_after_simulation(
        job_id=4, after_image_path=after, before_image_path=None,
        focus_targets=["唇"], brand="fumei",
        mode=adp.EFFECT_PROJECTION_MODE, anchor_mode=adp.ANCHOR_MODE_RAW,
        effect_pairs=[(prm.PROJECT_HA_FILLER, "唇")], do_not_touch=["苹果肌"],
    )
    assert res["audit"]["policy"]["simulation_mode"] == adp.EFFECT_PROJECTION_MODE
    assert res["audit"]["policy"]["anchor_mode"] == adp.ANCHOR_MODE_RAW
    assert res["audit"]["policy"]["mask_anchored"] is False     # raw-first 不锚定
    refs = {r["kind"]: r["path"] for r in res["output_refs"]}
    assert "effect_anchored" not in refs                        # 无锚定产物
    assert res["audit"]["effect_anchored_path"] is None
    # 角落仍是 AI 红（未锁回原图灰）→ 证明全帧 raw AI 即交付源
    raw = Image.open(refs["generated_raw"]).convert("RGB")
    assert raw.getpixel((5, 5)) == (255, 0, 0)


def test_ps_route_invalid_anchor_mode_raises(tmp_path: Path, monkeypatch):
    # 非法 anchor_mode → 启动期 fail-closed（不浪费 quota）。
    after, _ = _wire_fake_ps(monkeypatch, tmp_path, (128, 128, 128), (255, 0, 0))
    with pytest.raises(ValueError):
        adp.run_ps_model_router_after_simulation(
            job_id=5, after_image_path=after, before_image_path=None,
            focus_targets=["唇"], brand="fumei",
            mode=adp.EFFECT_PROJECTION_MODE, anchor_mode="bogus",
            effect_pairs=[(prm.PROJECT_HA_FILLER, "唇")],
        )
