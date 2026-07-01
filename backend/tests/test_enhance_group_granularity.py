"""defect① 修复单测：manifest 顶级 status!=ok 不再整板静默跳增强——组粒度跳过 + WARNING。

旧行为：main() 按 manifest 顶级 status 整板拦 _enhance_manifest_sources，单组 blocking
连累全板零增强但板照常渲出、无任何痕迹（江佳慧 .fullres-enhance 空目录根因）。
新行为：error 组 WARNING + skipped 计数跳过，ok 组照常增强。
"""
from __future__ import annotations

import io

from PIL import Image

from backend.scripts import render_ai_enhanced_boards as rab


def _mk_img(tmp_path, name: str):
    p = tmp_path / name
    Image.new("RGB", (64, 80), (200, 180, 160)).save(p)
    return p


def _group(name: str, status: str, slots: dict) -> dict:
    return {
        "name": name,
        "status": status,
        "blocking_issues": ["front pose 剔除"] if status == "error" else [],
        "selected_slots": slots,
    }


def _selection(path) -> dict:
    return {"after": {"path": str(path)}}


def _fake_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, format="PNG")
    return buf.getvalue()


def _run(manifest, tmp_path, monkeypatch, calls):
    def fake_gen(providers, png, prompt, mime="image/png", size_override=None,
                 progress_callback=None, **kwargs):
        calls.append(prompt)
        return _fake_png(), "fake"

    monkeypatch.setattr(
        "backend.services.image_providers.generate_with_fallback", fake_gen)
    monkeypatch.setattr(rab, "AI_CACHE_DIR", tmp_path / "cache")
    stats = {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "locked": 0}
    rab._enhance_manifest_sources(
        manifest, [], "prompt", stats,
        enhance_dir=tmp_path / "out", use_cache=False,
    )
    return stats


def test_error_group_skipped_ok_group_enhanced(tmp_path, monkeypatch, caplog):
    """混合 manifest（顶级 error）：error 组跳过有痕迹，ok 组照常增强。"""
    src_bad = _mk_img(tmp_path, "bad.png")
    src_ok = _mk_img(tmp_path, "ok.png")
    manifest = {
        "status": "error",
        "groups": [
            _group("g-err", "error", {"front": _selection(src_bad)}),
            _group("g-ok", "ok", {"front": _selection(src_ok)}),
        ],
    }
    calls: list = []
    with caplog.at_level("WARNING"):
        stats = _run(manifest, tmp_path, monkeypatch, calls)

    assert len(calls) == 1, "只有 ok 组该烧增强"
    assert stats["skipped"] == 1, "error 组 1 槽计入 skipped"
    assert stats["ok"] == 1 and stats["total"] == 1
    assert any("跳过该组" in r.message for r in caplog.records), "跳过必须留 WARNING"
    # error 组源图完全未动；ok 组写回 enhanced_path
    assert "enhancement" not in manifest["groups"][0]["selected_slots"]["front"]["after"]
    enh = manifest["groups"][1]["selected_slots"]["front"]["after"]["enhancement"]
    assert enh["enhanced_path"].endswith("_front_enhanced.png")


def test_all_ok_groups_full_enhance_no_warning(tmp_path, monkeypatch, caplog):
    """全 ok manifest：行为与旧版一致，零 skipped 零 WARNING。"""
    manifest = {
        "status": "ok",
        "groups": [
            _group("g1", "ok", {"front": _selection(_mk_img(tmp_path, "a.png"))}),
            _group("g2", "ok", {"left45": _selection(_mk_img(tmp_path, "b.png"))}),
        ],
    }
    calls: list = []
    with caplog.at_level("WARNING"):
        stats = _run(manifest, tmp_path, monkeypatch, calls)

    assert len(calls) == 2 and stats["ok"] == 2
    assert stats["skipped"] == 0
    assert not any("跳过该组" in r.message for r in caplog.records)


def test_all_error_groups_zero_enhance_with_trace(tmp_path, monkeypatch, caplog):
    """全 error manifest：零增强但每组都留 WARNING + skipped 全计数（不再静默）。"""
    manifest = {
        "status": "error",
        "groups": [
            _group("g1", "error", {
                "front": _selection(_mk_img(tmp_path, "a.png")),
                "left45": _selection(_mk_img(tmp_path, "b.png")),
            }),
        ],
    }
    calls: list = []
    with caplog.at_level("WARNING"):
        stats = _run(manifest, tmp_path, monkeypatch, calls)

    assert calls == []
    assert stats["skipped"] == 2
    assert sum("跳过该组" in r.message for r in caplog.records) == 1


def test_group_missing_status_defaults_ok(tmp_path, monkeypatch, caplog):
    """BC：group 无 status 字段（旧 manifest）按 ok 处理，照常增强。"""
    manifest = {
        "groups": [
            {"name": "legacy", "selected_slots": {
                "front": _selection(_mk_img(tmp_path, "a.png"))}},
        ],
    }
    calls: list = []
    stats = _run(manifest, tmp_path, monkeypatch, calls)
    assert len(calls) == 1 and stats["ok"] == 1 and stats["skipped"] == 0
