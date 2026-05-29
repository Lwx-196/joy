"""Focal-enhance: crop-at-native-res + composite-back + dynamic timeout.

History:
  - Wave 11 (W11-2/W11-3) added a dynamic timeout + a *whole-image* resize to
    ≤_FOCAL_MAX_LONG_EDGE to stop large inputs timing out.
  - That whole-image resize round-trip softened the focal region on large
    images — the C2.2 mini-eval judged the focal output 0/3 vs its own input
    (face Δsharp −47/−69%). See ``delivery/focal-crop-baseline.md``.
  - The crop-at-native-res rewrite (`crisp-focal-crop` plan) replaces the
    whole-image resize: crop the mask bbox, inpaint the crop at native res
    (downscaling only a crop that is itself >1280), feather-composite back
    onto the pristine full-res original.

These tests guard:
  - ``_focal_compute_timeout`` (unchanged; now fed the CROP dims).
  - ``_focal_crop_bbox`` (padded white-region bbox, clamp, empty fallback).
  - ``_composite_focal`` (unmasked pixels pristine, masked region replaced,
    output always = base dims).
  - ``run_comfyui_focal_enhance`` end-to-end: workflow sees a CROP, output is
    the original size with the background pristine and the focal region changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from backend import ai_generation_adapter as ai


class TestFocalComputeTimeout:

    def test_floor_returns_base(self):
        # 512x500 = 256000 = floor → 0 extra
        assert ai._focal_compute_timeout(512, 500) == ai._FOCAL_TIMEOUT_BASE

    def test_below_floor_still_returns_base(self):
        # 100x100 well below floor → 0 extra → base
        assert ai._focal_compute_timeout(100, 100) == ai._FOCAL_TIMEOUT_BASE

    def test_linear_above_floor(self):
        # 1280x1024 = 1.31M pixels → extra = 1.31M - 256K = 1.054M
        # extra_seconds = 1054 * 800 / 1000 = 843
        expected = ai._FOCAL_TIMEOUT_BASE + (((1280 * 1024) - ai._FOCAL_PIXEL_FLOOR)
                                              * ai._FOCAL_TIMEOUT_PER_MEGAPIXEL) // 1_000_000
        assert ai._focal_compute_timeout(1280, 1024) == min(ai._FOCAL_TIMEOUT_CAP, expected)

    def test_cap_at_4k(self):
        # 4096x4096 = 16.7M pixels — extra blows past cap
        assert ai._focal_compute_timeout(4096, 4096) == ai._FOCAL_TIMEOUT_CAP

    def test_zero_dims_returns_base(self):
        assert ai._focal_compute_timeout(0, 0) == ai._FOCAL_TIMEOUT_BASE

    def test_negative_dims_clamped(self):
        # We expect _focal_compute_timeout to be defensive — pixels coerced to ≥0
        assert ai._focal_compute_timeout(-100, 200) == ai._FOCAL_TIMEOUT_BASE


def _mask(tmp_path: Path, size: tuple[int, int], ellipse: tuple[int, int, int, int] | None) -> Path:
    """Write a single-channel mask PNG (white ellipse = focus)."""
    m = Image.new("L", size, 0)
    if ellipse is not None:
        ImageDraw.Draw(m).ellipse(ellipse, fill=255)
    p = tmp_path / "mask.png"
    m.save(p, "PNG")
    return p


class TestFocalCropBbox:

    def test_empty_mask_full_image_fallback(self, tmp_path: Path):
        # All-black mask → degenerate full-frame box
        p = _mask(tmp_path, (400, 300), None)
        assert ai._focal_crop_bbox(p) == (0, 0, 400, 300)

    def test_centered_ellipse_padded_and_clamped(self, tmp_path: Path):
        # Ellipse bbox (100,100,300,200) on a 400x400 image, pad_frac=0.25.
        p = _mask(tmp_path, (400, 400), (100, 100, 300, 200))
        left, top, right, bottom = ai._focal_crop_bbox(p, pad_frac=0.25)
        # getbbox is inclusive of the white pixels; pad = 0.25 * (w or h)
        # w ≈ 200 → pad_x ≈ 50; h ≈ 100 → pad_y ≈ 25. Allow ±2 for ellipse AA.
        assert 45 <= left <= 55
        assert 70 <= top <= 80
        assert 345 <= right <= 355
        assert 220 <= bottom <= 230
        # Always inside the image
        assert left >= 0 and top >= 0 and right <= 400 and bottom <= 400

    def test_edge_touching_ellipse_clamped_to_zero(self, tmp_path: Path):
        # Ellipse hugging the top-left corner → padding clamps at 0
        p = _mask(tmp_path, (400, 400), (0, 0, 120, 120))
        left, top, right, bottom = ai._focal_crop_bbox(p, pad_frac=0.5)
        assert left == 0 and top == 0
        assert right <= 400 and bottom <= 400


class TestCompositeFocal:
    """Pristine background + replaced focal region + base-sized output."""

    def _setup(self, tmp_path: Path, *, inpaint_size: tuple[int, int] | None = None):
        base = Image.new("RGB", (200, 200), (100, 100, 100))
        base_path = tmp_path / "base.png"
        base.save(base_path, "PNG")
        bbox = (50, 50, 150, 150)  # crop 100x100
        cw, ch = 100, 100
        # crop mask: centered ellipse (20,20,80,80) within the crop
        cmask = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(cmask).ellipse((20, 20, 80, 80), fill=255)
        cmask_path = tmp_path / "cmask.png"
        cmask.save(cmask_path, "PNG")
        # inpainted crop: solid red (possibly a mismatched ÷8-ish size)
        isz = inpaint_size or (cw, ch)
        inpainted = Image.new("RGB", isz, (255, 0, 0))
        inpainted_path = tmp_path / "inpainted.png"
        inpainted.save(inpainted_path, "PNG")
        return base_path, inpainted_path, cmask_path, bbox

    def test_unmasked_pristine_masked_replaced(self, tmp_path: Path):
        base_path, inpainted_path, cmask_path, bbox = self._setup(tmp_path)
        out = tmp_path / "out.png"
        ai._composite_focal(base_path, inpainted_path, cmask_path, bbox, out)
        with Image.open(out) as im:
            assert im.size == (200, 200)  # base dims
            px = im.convert("RGB").load()
            # Far outside the crop bbox → pristine base gray
            assert px[5, 5] == (100, 100, 100)
            # Inside the crop but outside the focus ellipse → still base gray
            #   global (55,55) = crop-local (5,5), ellipse starts at 20
            assert px[55, 55] == (100, 100, 100)
            # Just outside the ellipse boundary (crop-local (14,50) = global
            #   (64,100), ~6px left of the ellipse edge at x=20): the feathered
            #   alpha may bleed a little but must NOT turn the pixel red — bounds
            #   the seam halo (catches an exploded feather radius / inverted mask).
            r0, g0, b0 = px[64, 100]
            assert g0 > 45 and b0 > 45 and r0 < 190
            # Ellipse center (crop-local 50,50 = global 100,100) → red inpaint
            r, g, b = px[100, 100]
            assert r > 200 and g < 60 and b < 60

    def test_dim_mismatch_inpaint_resized_no_crash(self, tmp_path: Path):
        # SDXL ÷8 snap: workflow returned a 96x96 crop for a 100x100 bbox
        base_path, inpainted_path, cmask_path, bbox = self._setup(
            tmp_path, inpaint_size=(96, 96),
        )
        out = tmp_path / "out.png"
        ai._composite_focal(base_path, inpainted_path, cmask_path, bbox, out)
        with Image.open(out) as im:
            assert im.size == (200, 200)
            r, g, b = im.convert("RGB").load()[100, 100]
            assert r > 200 and g < 60 and b < 60

    def test_in_place_overwrite_atomic(self, tmp_path: Path):
        # output_path == inpainted_crop_path (caller-output_dir case): must not
        # corrupt — composite reads then atomically replaces the same file.
        base_path, inpainted_path, cmask_path, bbox = self._setup(tmp_path)
        ai._composite_focal(base_path, inpainted_path, cmask_path, bbox, inpainted_path)
        with Image.open(inpainted_path) as im:
            assert im.size == (200, 200)  # now the composited full-res output
            assert im.convert("RGB").load()[5, 5] == (100, 100, 100)


class TestFocalEnhanceCropComposite:
    """End-to-end: workflow receives a CROP; output is original-sized with a
    pristine background and a changed focal region."""

    def _fake_workflow_factory(self, captured: dict, *, snap: int = 0, fill=(255, 0, 0)):
        def fake_workflow(input_path, *, output_dir, workflow_name,
                          workflow_parameters, focus_mask_path,
                          positive_prompt, negative_prompt,
                          timeout_seconds, **kwargs):
            with Image.open(input_path) as im:
                captured["in_w"], captured["in_h"] = im.size
            with Image.open(focus_mask_path) as mk:
                captured["mask_w"], captured["mask_h"] = mk.size
            captured["timeout_seconds"] = timeout_seconds
            captured["workflow_name"] = workflow_name
            # Synthesise an "inpainted" crop the size the workflow received,
            # optionally ÷8-snapped smaller to simulate SDXL/VAE rounding.
            gw, gh = captured["in_w"] - snap, captured["in_h"] - snap
            generated = output_dir / "comfyui-generated.png"
            Image.new("RGB", (gw, gh), fill).save(generated, "PNG")
            return {"generated_path": str(generated)}
        return fake_workflow

    def test_oversized_input_crops_and_composites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        src = tmp_path / "input_big.jpg"
        Image.new("RGB", (1920, 1280), (100, 100, 100)).save(src, "JPEG")
        captured: dict = {}
        monkeypatch.setattr(ai, "_run_comfyui_workflow",
                            self._fake_workflow_factory(captured))

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["唇"], brand="md_ai", case_id=134,
        )
        assert result != src and Path(result).is_file()
        # Workflow saw a CROP — strictly smaller than the whole image, and the
        # mask it received matches the crop dims.
        assert max(captured["in_w"], captured["in_h"]) < 1920
        assert (captured["mask_w"], captured["mask_h"]) == (captured["in_w"], captured["in_h"])
        # A single small feature is under the native pixel budget → inpainted at
        # NATIVE res (no downscale), so the focal region is not re-softened.
        assert captured["in_w"] * captured["in_h"] <= ai._FOCAL_CROP_MAX_PIXELS
        # Timeout from CROP dims, within bounds.
        assert ai._FOCAL_TIMEOUT_BASE <= captured["timeout_seconds"] <= ai._FOCAL_TIMEOUT_CAP
        assert captured["workflow_name"] == "portrait_focal_enhance_v1"
        with Image.open(result) as out:
            out = out.convert("RGB")
            assert out.size == (1920, 1280)  # original dims, composited onto base
            px = out.load()
            assert px[5, 5] == (100, 100, 100)            # corner pristine
            r, g, b = px[960, 998]                        # "唇" ellipse center
            assert r > 200 and g < 80 and b < 80          # focal region replaced

    def test_small_input_native_res_pristine_corner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        src = tmp_path / "input_small.jpg"
        Image.new("RGB", (640, 480), (120, 120, 120)).save(src, "JPEG")
        captured: dict = {}
        monkeypatch.setattr(ai, "_run_comfyui_workflow",
                            self._fake_workflow_factory(captured))

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["唇"], brand="md_ai", case_id=999,
        )
        assert result != src
        assert max(captured["in_w"], captured["in_h"]) <= ai._FOCAL_MAX_LONG_EDGE
        with Image.open(result) as out:
            out = out.convert("RGB")
            assert out.size == (640, 480)
            assert out.load()[5, 5] == (120, 120, 120)  # corner pristine

    def test_snapped_crop_output_still_exact_original_dims(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Replaces the W11.5 intent: even when the workflow returns a crop a
        few px smaller (÷8 snap), the composited output is EXACT original dims
        because it is pasted onto the pristine full-res base."""
        src = tmp_path / "input_853.jpg"
        Image.new("RGB", (853, 1280), (140, 110, 90)).save(src, "JPEG")
        captured: dict = {}
        monkeypatch.setattr(ai, "_run_comfyui_workflow",
                            self._fake_workflow_factory(captured, snap=5))

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["泪沟"], brand="md_ai", case_id=129,
        )
        with Image.open(result) as out:
            out = out.convert("RGB")
            assert out.size == (853, 1280)  # exact, despite the 5px-snapped crop
            # The 5px-snapped crop must be resized back + pasted at the right
            # offset: the 泪沟 ellipse centre (0.5w, 0.40h) carries the inpaint.
            r, g, b = out.load()[426, 512]
            assert r > 200 and g < 80 and b < 80

    def test_wide_band_target_runs_native_res(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression lock for review w6inp3wzz (confirmed High). A 面颊 (cheek)
        crop on a 1920×1280 portrait is ~1920px WIDE but only ~1.29 MP. The old
        FIXED 1280 long-edge cap would have downscaled it (re-softening the focal
        region exactly like W11); the pixel-area budget keeps it NATIVE because
        its area is under _FOCAL_CROP_MAX_PIXELS."""
        src = tmp_path / "input_band.jpg"
        Image.new("RGB", (1920, 1280), (90, 90, 90)).save(src, "JPEG")
        captured: dict = {}
        monkeypatch.setattr(ai, "_run_comfyui_workflow",
                            self._fake_workflow_factory(captured))

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["面颊"], brand="md_ai", case_id=79,
        )
        # NATIVE res: the crop long-edge exceeds the OLD fixed cap (proving the
        # old code WOULD have downscaled it) yet its area is within budget so it
        # is NOT downscaled.
        assert max(captured["in_w"], captured["in_h"]) > ai._FOCAL_MAX_LONG_EDGE
        assert captured["in_w"] * captured["in_h"] <= ai._FOCAL_CROP_MAX_PIXELS
        with Image.open(result) as out:
            assert out.size == (1920, 1280)

    def test_fullframe_target_downscaled_to_pixel_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Honest residual (crisp-focal-crop P2/P3): a full-frame focus target
        makes the crop ≈ the whole 1920×1280 frame (~2.46 MP > budget), so it IS
        area-downscaled for the inpaint (the focal region softens; the
        background still composites back pristine). The fix bounds this to the
        few genuinely-full-frame targets — not every large crop."""
        src = tmp_path / "input_full.jpg"
        Image.new("RGB", (1920, 1280), (90, 90, 90)).save(src, "JPEG")
        captured: dict = {}
        monkeypatch.setattr(ai, "_run_comfyui_workflow",
                            self._fake_workflow_factory(captured))

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["face"], brand="md_ai", case_id=79,
        )
        # Over budget → downscaled (area-preserving) to fit the native budget.
        assert captured["in_w"] * captured["in_h"] <= ai._FOCAL_CROP_MAX_PIXELS
        assert max(captured["in_w"], captured["in_h"]) < 1920  # genuinely shrunk
        with Image.open(result) as out:
            assert out.size == (1920, 1280)  # composited back to full res

    def test_caller_output_dir_in_place_composite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Caller-supplied output_dir → stable_path == generated_path; the
        composite overwrites the raw crop in place and returns full-res."""
        src = tmp_path / "input_big3.jpg"
        Image.new("RGB", (1920, 1280), (100, 100, 100)).save(src, "JPEG")
        caller_dir = tmp_path / "caller_output"
        caller_dir.mkdir()
        captured: dict = {}
        monkeypatch.setattr(ai, "_run_comfyui_workflow",
                            self._fake_workflow_factory(captured))

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["唇"], brand="md_ai", case_id=79,
            output_dir=caller_dir,
        )
        assert Path(result).is_file()
        with Image.open(result) as out:
            out = out.convert("RGB")
            assert out.size == (1920, 1280)
            assert out.load()[5, 5] == (100, 100, 100)  # corner pristine


class TestFocalEnhanceContractInvariants:
    """K-1 silent-fail, production temp-dir cleanup, EXIF normalisation."""

    def _ok_workflow(self):
        def fake_workflow(input_path, *, output_dir, **kwargs):
            with Image.open(input_path) as im:
                w, h = im.size
            generated = output_dir / "comfyui-generated.png"
            Image.new("RGB", (w, h), (0, 200, 0)).save(generated, "PNG")
            return {"generated_path": str(generated)}
        return fake_workflow

    def test_production_no_output_dir_artifact_survives_and_tempdir_cleaned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The production caller (render_queue) omits output_dir. The returned
        artifact must be a sibling of the input (NOT inside the mkdtemp work dir,
        which the finally block rmtrees), and no work dir may be left behind."""
        src = tmp_path / "prod_input.jpg"
        Image.new("RGB", (900, 700), (130, 90, 60)).save(src, "JPEG")
        monkeypatch.setattr(ai, "_run_comfyui_workflow", self._ok_workflow())

        result = ai.run_comfyui_focal_enhance(src, focus_targets=["唇"], case_id=1)
        result = Path(result)
        # Returned artifact persists and is a sibling of the input...
        assert result.is_file()
        assert result.parent == src.parent
        # ...not inside a ".comfyui-focal-*" mkdtemp work dir.
        assert not result.parent.name.startswith(".comfyui-focal-")
        # The mkdtemp work DIR was cleaned up (no stray temp dirs).
        leftover_dirs = [p for p in src.parent.glob(".comfyui-focal-*") if p.is_dir()]
        assert leftover_dirs == []

    def test_silent_fail_when_composite_raises_returns_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """K-1 residual gap (review w6inp3wzz): if the new _composite_focal step
        raises, the helper must still return the ORIGINAL image_path, not crash."""
        src = tmp_path / "input.jpg"
        Image.new("RGB", (800, 600), (100, 100, 100)).save(src, "JPEG")
        monkeypatch.setattr(ai, "_run_comfyui_workflow", self._ok_workflow())

        def boom(*a, **k):
            raise RuntimeError("composite failed")
        monkeypatch.setattr(ai, "_composite_focal", boom)

        result = ai.run_comfyui_focal_enhance(src, focus_targets=["唇"], case_id=2)
        assert result == src  # K-1: original returned, no raise

    def test_exif_orientation_normalised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Step 0 EXIF-transposes the base. An input tagged Orientation=6
        (stored 800×600, displayed 600×800) must yield a 600×800 output."""
        src = tmp_path / "exif.jpg"
        img = Image.new("RGB", (800, 600), (100, 100, 100))
        exif = img.getexif()
        exif[274] = 6  # Orientation: rotate 90° CW for display → 600×800
        img.save(src, "JPEG", exif=exif)
        monkeypatch.setattr(ai, "_run_comfyui_workflow", self._ok_workflow())

        result = ai.run_comfyui_focal_enhance(src, focus_targets=["唇"], case_id=3)
        with Image.open(result) as out:
            assert out.size == (600, 800)  # orientation applied, dims swapped
