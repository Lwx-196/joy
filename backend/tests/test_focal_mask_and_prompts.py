"""Unit tests for focal_mask_generator + focal_prompt_library (Step 2 of 4-mode)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from backend.services.focal_mask_generator import generate_focus_mask
from backend.services.focal_prompt_library import build_focal_prompts


# ---------------------------------------------------------------------------
# focal_mask_generator
# ---------------------------------------------------------------------------

def _make_test_jpg(tmp_path: Path, name: str = "test.jpg", size=(800, 600)) -> Path:
    img = Image.new("RGB", size, color=(128, 128, 128))
    p = tmp_path / name
    img.save(p, format="JPEG", quality=90)
    return p


class TestMaskGenerator:

    def test_chin_focus_produces_lower_face_mask(self, tmp_path: Path):
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        out = tmp_path / "mask.png"
        result = generate_focus_mask(src, ["下巴"], output_path=out)
        assert result == out
        assert result.is_file()
        with Image.open(result) as m:
            assert m.size == (1000, 1000)
            assert m.mode == "L"  # single-channel
            # Lower-center should be white (chin region)
            assert m.getpixel((500, 850)) > 200
            # Top should be black (no chin involvement up there)
            assert m.getpixel((500, 100)) < 50

    def test_lips_focus_produces_mouth_region(self, tmp_path: Path):
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, ["唇"], output_path=tmp_path / "mask.png")
        with Image.open(result) as m:
            # Lips area (around y=780)
            assert m.getpixel((500, 780)) > 200
            # Forehead should be black
            assert m.getpixel((500, 100)) < 50

    def test_unknown_target_falls_back_to_full_face(self, tmp_path: Path):
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, ["unknown_target_xyz"], output_path=tmp_path / "mask.png")
        with Image.open(result) as m:
            # Full-face fallback centered ellipse should have center white
            assert m.getpixel((500, 500)) > 200

    def test_empty_targets_full_face_fallback(self, tmp_path: Path):
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, [], output_path=tmp_path / "mask.png")
        with Image.open(result) as m:
            assert m.getpixel((500, 500)) > 200

    def test_multi_target_union(self, tmp_path: Path):
        """Multiple targets union into a single bbox covering all."""
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, ["唇", "下巴"], output_path=tmp_path / "mask.png")
        with Image.open(result) as m:
            # Both lips area AND chin area should be inside union
            assert m.getpixel((500, 780)) > 200  # lips
            assert m.getpixel((500, 870)) > 200  # chin
            # Forehead still black
            assert m.getpixel((500, 100)) < 50

    def test_output_path_auto_temp_when_omitted(self, tmp_path: Path):
        src = _make_test_jpg(tmp_path, size=(640, 480))
        result = generate_focus_mask(src, ["chin"])
        assert result.is_file()
        assert result.suffix == ".png"
        # Cleanup since auto-temp isn't cleaned by helper
        result.unlink(missing_ok=True)

    def test_forehead_lines_focus(self, tmp_path: Path):
        # 额纹(抬头纹) — AI 术后模拟 case45 衡力术式部位，旧 _FOCAL_REGIONS 缺。
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, ["额纹"], output_path=tmp_path / "m.png")
        with Image.open(result) as m:
            assert m.getpixel((500, 200)) > 200   # upper forehead white
            assert m.getpixel((500, 850)) < 50     # chin black

    def test_glabella_focus(self, tmp_path: Path):
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, ["川字"], output_path=tmp_path / "m.png")
        with Image.open(result) as m:
            assert m.getpixel((500, 330)) > 200   # between-brows white
            assert m.getpixel((500, 500)) < 50     # mid-face black (narrow region)

    def test_taitou_alias_recognised_not_full_face(self, tmp_path: Path):
        # 「抬头」(case45 原始文件名词) 须命中 → 不掉进 full-face fallback
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        result = generate_focus_mask(src, ["抬头"], output_path=tmp_path / "m.png")
        with Image.open(result) as m:
            assert m.getpixel((500, 200)) > 200    # forehead white
            assert m.getpixel((500, 850)) < 50      # full-face fallback would be white here

    def test_separate_ellipses_precise_correspondence(self, tmp_path: Path):
        # 精准对应：4 个分散治疗区在 separate 模式下是真并集（区间留黑），
        # 而非默认 _union_regions 的单一外接框（中脸被整片覆盖）。
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        targets = ["额纹", "川字", "唇", "下巴"]
        sep = generate_focus_mask(src, targets, output_path=tmp_path / "sep.png",
                                  separate_ellipses=True)
        with Image.open(sep) as m:
            # 4 个治疗区中心全白
            assert m.getpixel((500, 200)) > 200    # 额纹
            assert m.getpixel((500, 330)) > 200    # 川字
            assert m.getpixel((500, 780)) > 200    # 唇
            assert m.getpixel((500, 870)) > 200    # 下巴
            # 川字与唇之间的中脸（鼻/颊）必须留黑（未做的区不外扩）
            assert m.getpixel((500, 520)) < 50

    def test_default_union_covers_midface_gap(self, tmp_path: Path):
        # 对照：默认（separate_ellipses=False）= 单一外接框 → 中脸被覆盖（白）。
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        targets = ["额纹", "川字", "唇", "下巴"]
        result = generate_focus_mask(src, targets, output_path=tmp_path / "u.png")
        with Image.open(result) as m:
            assert m.getpixel((500, 520)) > 200    # 单一 bbox 覆盖中脸

    def test_to_pixel_bbox_face_relative_vs_whole_image(self):
        # face_bbox 给定 → 分数相对人脸 bbox；None → 相对整图（向后兼容）。
        from backend.services.focal_mask_generator import _to_pixel_bbox

        # region 中心 (0.5,0.5) 半宽 0.5 → 相对 face(200,100,600,500)：
        # 中心 (200+0.5*400, 100+0.5*400)=(400,300)，半 (100,100) → (300,200,500,400)
        assert _to_pixel_bbox((0.5, 0.5, 0.5, 0.5), 1000, 1000, (200, 100, 600, 500)) == (
            300, 200, 500, 400
        )
        # None → 整图：中心 (500,500) 半 (250,250)
        assert _to_pixel_bbox((0.5, 0.5, 0.5, 0.5), 1000, 1000) == (250, 250, 750, 750)

    def test_face_bbox_places_region_in_face_not_image_center(self, tmp_path: Path):
        # ③ 修复核心：face_bbox 在左侧窄条 → 川字落在人脸内（x≈150）而非整图中心（x=500）。
        src = _make_test_jpg(tmp_path, size=(1000, 1000))
        out = tmp_path / "m.png"
        generate_focus_mask(src, ["川字"], output_path=out, face_bbox=(0, 0, 300, 1000))
        with Image.open(out) as m:
            # 川字 cx=0.5,cy=0.33 of face(0,0,300,1000) → (150, 330) 白
            assert m.getpixel((150, 330)) > 200
            # 整图中心（旧行为会白）现应为黑
            assert m.getpixel((500, 330)) < 50


# ---------------------------------------------------------------------------
# focal_prompt_library
# ---------------------------------------------------------------------------

class TestPromptLibrary:

    def test_known_targets_extends_positive(self):
        pos, neg = build_focal_prompts(["唇"])
        assert "lip" in pos.lower() or "唇" in pos
        assert "identity change" in neg  # negative always present

    def test_multiple_targets_combine(self):
        pos, neg = build_focal_prompts(["唇", "下巴"])
        # Both lip-related and chin-related phrases should appear
        assert ("lip" in pos.lower() or "唇" in pos)
        assert ("jawline" in pos.lower() or "chin" in pos.lower() or "下巴" in pos)

    def test_unknown_target_returns_base(self):
        pos, neg = build_focal_prompts(["unknown_xyz"])
        # No fragment added but base preservation clause still present
        assert "preserve" in pos.lower()
        assert "identity" in neg.lower()

    def test_empty_targets_returns_base(self):
        pos, neg = build_focal_prompts([])
        assert "preserve" in pos.lower()
        assert "identity" in neg.lower()

    def test_duplicate_targets_deduped(self):
        pos, _ = build_focal_prompts(["lips", "lips"])
        # The lip-specific fragment should appear exactly once
        assert pos.lower().count("natural lip definition") == 1


# ---------------------------------------------------------------------------
# run_comfyui_focal_enhance (helper-level — mock _run_comfyui_workflow)
# ---------------------------------------------------------------------------

class TestFocalEnhanceHelper:
    """Tests the high-level helper that orchestrates mask + prompt + workflow."""

    def test_silent_fail_when_workflow_raises(self, tmp_path: Path, monkeypatch):
        from backend import ai_generation_adapter

        src = _make_test_jpg(tmp_path, size=(640, 480))

        def _raise(*args, **kwargs):
            raise ConnectionError("ComfyUI down")

        monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _raise)
        result = ai_generation_adapter.run_comfyui_focal_enhance(
            src, focus_targets=["chin"], brand="md_ai", case_id=99,
            output_dir=tmp_path / "out",
        )
        assert result == src  # silent-fail returns input

    def test_silent_fail_when_no_generated_path(self, tmp_path: Path, monkeypatch):
        from backend import ai_generation_adapter

        src = _make_test_jpg(tmp_path)

        def _no_gen(*args, **kwargs):
            return {"workflow_hash": "x", "prompt_id": "y"}  # missing generated_path

        monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _no_gen)
        result = ai_generation_adapter.run_comfyui_focal_enhance(
            src, focus_targets=["chin"], brand="md_ai",
            output_dir=tmp_path / "out",
        )
        assert result == src

    def test_silent_fail_when_generated_path_missing_file(self, tmp_path: Path, monkeypatch):
        from backend import ai_generation_adapter

        src = _make_test_jpg(tmp_path)

        def _phantom(*args, **kwargs):
            return {"generated_path": str(tmp_path / "nonexistent.png")}

        monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _phantom)
        result = ai_generation_adapter.run_comfyui_focal_enhance(
            src, focus_targets=["chin"], brand="md_ai",
            output_dir=tmp_path / "out",
        )
        assert result == src

    def test_happy_path_returns_generated(self, tmp_path: Path, monkeypatch):
        from backend import ai_generation_adapter

        src = _make_test_jpg(tmp_path)
        outdir = tmp_path / "out"
        outdir.mkdir()

        generated = outdir / "generated.png"
        Image.new("RGB", (640, 480), color=(200, 200, 200)).save(generated, format="PNG")

        capture = {}
        def _ok(*args, **kwargs):
            capture["positive_prompt"] = kwargs.get("positive_prompt")
            capture["negative_prompt"] = kwargs.get("negative_prompt")
            capture["focus_mask_path"] = kwargs.get("focus_mask_path")
            capture["workflow_name"] = kwargs.get("workflow_name")
            capture["params"] = kwargs.get("workflow_parameters")
            return {"generated_path": str(generated)}

        monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _ok)
        result = ai_generation_adapter.run_comfyui_focal_enhance(
            src, focus_targets=["下巴", "唇"], brand="md_ai", case_id=99,
            output_dir=outdir,
        )
        assert result == generated
        assert capture["workflow_name"] == "portrait_focal_enhance_v1"
        assert capture["focus_mask_path"] is not None
        assert capture["focus_mask_path"].is_file()
        # Per-target prompt should mention lip / chin
        assert capture["positive_prompt"] is not None
        assert capture["params"]["denoise"] == 0.40  # FOCAL strength
        assert capture["params"]["steps"] == 20

    def test_auto_tempdir_cleanup(self, tmp_path: Path, monkeypatch):
        """When output_dir=None, auto-tempdir is cleaned up + stable copy returned."""
        from backend import ai_generation_adapter

        src = _make_test_jpg(tmp_path)

        def _ok(*args, **kwargs):
            outdir = kwargs.get("output_dir")
            gen = outdir / "out.png"
            Image.new("RGB", (320, 240), color=(180, 180, 180)).save(gen, format="PNG")
            return {"generated_path": str(gen)}

        monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _ok)
        result = ai_generation_adapter.run_comfyui_focal_enhance(
            src, focus_targets=["chin"], brand="md_ai",
            output_dir=None,
        )
        # Result should be a stable copy next to src
        assert result.parent == src.parent
        assert result.name.startswith(".comfyui-focal-out-")
        assert result.is_file()
        # No leaked tempdir
        leaked = [p for p in src.parent.iterdir() if p.name.startswith(".comfyui-focal-")]
        assert leaked == [result]  # only the stable copy, no .comfyui-focal-<ts>/ dir
        result.unlink()
