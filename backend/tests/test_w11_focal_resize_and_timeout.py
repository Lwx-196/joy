"""Wave 11 — focal helper auto-resize + dynamic timeout regressions.

Manual smoke on 5/28 showed M3 focal helper:
  - case 79 (1920x1280) and case 129 (1920x1280): timed out at the static
    900s budget → silent_fail.
  - case 134 (853x1280): succeeded in 234s but the SDXL output over-smoothed
    the whole face because the v1 workflow's MediaPipeFaceMeshToSEGS +
    SAMDetectorCombined(bbox_expansion=8) chain segmented the entire face.

Wave 11 ships three coupled fixes:
  W11-1: simplify portrait_focal_enhance_v1 to consume the Python ellipse
         mask directly (covered by the JSON template change + e2e behavior
         — not directly unit-testable here).
  W11-2: dynamic timeout via ``_focal_compute_timeout``.
  W11-3: auto-resize input to ≤_FOCAL_MAX_LONG_EDGE via
         ``_focal_resize_if_needed``.

These tests guard W11-2 / W11-3.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

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


class TestFocalResizeIfNeeded:

    def test_small_image_no_resize(self, tmp_path: Path):
        src = tmp_path / "small.jpg"
        Image.new("RGB", (640, 480), color=(120, 120, 120)).save(src, "JPEG")
        out_dir = tmp_path / "work"
        out_dir.mkdir()
        work_path, w, h, ow, oh = ai._focal_resize_if_needed(src, out_dir)
        assert work_path == src  # untouched, no I/O
        assert (w, h) == (640, 480) == (ow, oh)

    def test_at_boundary_no_resize(self, tmp_path: Path):
        # Exactly _FOCAL_MAX_LONG_EDGE long-edge → no resize
        src = tmp_path / "boundary.jpg"
        Image.new("RGB", (ai._FOCAL_MAX_LONG_EDGE, 720), color=(80, 80, 80)).save(src, "JPEG")
        out_dir = tmp_path / "work"
        out_dir.mkdir()
        work_path, w, h, ow, oh = ai._focal_resize_if_needed(src, out_dir)
        assert work_path == src
        assert (w, h) == (ai._FOCAL_MAX_LONG_EDGE, 720)

    def test_oversized_landscape_resized(self, tmp_path: Path):
        # 1920x1280 → 1280x853 (long edge capped, aspect preserved)
        src = tmp_path / "big_l.jpg"
        Image.new("RGB", (1920, 1280), color=(50, 50, 50)).save(src, "JPEG")
        out_dir = tmp_path / "work"
        out_dir.mkdir()
        work_path, w, h, ow, oh = ai._focal_resize_if_needed(src, out_dir)
        assert work_path != src
        assert w == ai._FOCAL_MAX_LONG_EDGE
        # height = round(1280 * 1280/1920) = round(853.33) = 853
        assert h == 853
        assert (ow, oh) == (1920, 1280)
        assert work_path.is_file()
        with Image.open(work_path) as im:
            assert im.size == (1280, 853)

    def test_oversized_portrait_resized(self, tmp_path: Path):
        # 1280x1920 → 853x1280 (portrait orientation)
        src = tmp_path / "big_p.jpg"
        Image.new("RGB", (1280, 1920), color=(50, 50, 50)).save(src, "JPEG")
        out_dir = tmp_path / "work"
        out_dir.mkdir()
        work_path, w, h, ow, oh = ai._focal_resize_if_needed(src, out_dir)
        assert work_path != src
        assert h == ai._FOCAL_MAX_LONG_EDGE
        assert w == 853
        assert (ow, oh) == (1280, 1920)

    def test_resized_output_in_output_dir(self, tmp_path: Path):
        # Resized file must land inside output_dir, not next to input
        src = tmp_path / "elsewhere.jpg"
        Image.new("RGB", (2000, 1500), color=(10, 10, 10)).save(src, "JPEG")
        out_dir = tmp_path / "downstream_workdir"
        out_dir.mkdir()
        work_path, *_ = ai._focal_resize_if_needed(src, out_dir)
        assert work_path.parent == out_dir
        assert work_path.suffix == ".jpg"


class TestFocalEnhanceIntegratesResizeAndTimeout:
    """End-to-end behavior: oversized input gets resized + timeout extends."""

    def test_oversized_input_passes_resized_path_and_dynamic_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Confirm _run_comfyui_workflow receives the resized (1280) path +
        a timeout proportional to resized area."""
        src = tmp_path / "input_big.jpg"
        Image.new("RGB", (1920, 1280), color=(180, 130, 100)).save(src, "JPEG")

        captured: dict = {}

        def fake_workflow(input_path, *, output_dir, workflow_name,
                          workflow_parameters, focus_mask_path,
                          positive_prompt, negative_prompt,
                          timeout_seconds, **kwargs):
            with Image.open(input_path) as im:
                captured["w"], captured["h"] = im.size
            captured["timeout_seconds"] = timeout_seconds
            captured["workflow_name"] = workflow_name
            captured["focus_mask_path"] = focus_mask_path
            # Synthesise a valid downstream artifact
            generated = output_dir / "comfyui-generated.png"
            Image.new("RGB", (captured["w"], captured["h"]), color=(0, 255, 0)).save(generated, "PNG")
            return {"generated_path": str(generated)}

        monkeypatch.setattr(ai, "_run_comfyui_workflow", fake_workflow)

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["唇"], brand="md_ai", case_id=134,
        )
        assert result != src
        assert Path(result).is_file()
        # The input passed to the workflow MUST be the resized 1280-edge file
        assert max(captured["w"], captured["h"]) == ai._FOCAL_MAX_LONG_EDGE
        # Dynamic timeout must be larger than the floor base (because >256K pixels)
        # and ≤ cap. For 1280x853 it should be roughly base + ~700s.
        assert captured["timeout_seconds"] > ai._FOCAL_TIMEOUT_BASE
        assert captured["timeout_seconds"] <= ai._FOCAL_TIMEOUT_CAP
        # workflow_name unchanged
        assert captured["workflow_name"] == "portrait_focal_enhance_v1"

        # Returned PNG must be at ORIGINAL resolution (upscaled back)
        with Image.open(result) as out:
            assert out.size == (1920, 1280)

    def test_small_input_no_resize_default_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        src = tmp_path / "input_small.jpg"
        Image.new("RGB", (640, 480), color=(120, 120, 120)).save(src, "JPEG")

        captured: dict = {}

        def fake_workflow(input_path, *, output_dir, workflow_name,
                          workflow_parameters, focus_mask_path,
                          positive_prompt, negative_prompt,
                          timeout_seconds, **kwargs):
            with Image.open(input_path) as im:
                captured["w"], captured["h"] = im.size
            captured["timeout_seconds"] = timeout_seconds
            generated = output_dir / "comfyui-generated.png"
            Image.new("RGB", (captured["w"], captured["h"]), color=(0, 200, 0)).save(generated, "PNG")
            return {"generated_path": str(generated)}

        monkeypatch.setattr(ai, "_run_comfyui_workflow", fake_workflow)

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["唇"], brand="md_ai", case_id=999,
        )
        assert result != src
        # No resize happened, dims match input
        assert (captured["w"], captured["h"]) == (640, 480)
        # Timeout reverts to floor (640*480 = 307K, just above 256K floor)
        # extra_seconds = (307200 - 256000) * 800 / 1_000_000 = 40
        assert ai._FOCAL_TIMEOUT_BASE <= captured["timeout_seconds"] <= ai._FOCAL_TIMEOUT_BASE + 100

        # No upscale-back needed → output at native res
        with Image.open(result) as out:
            assert out.size == (640, 480)

    def test_caller_output_dir_still_upscales_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """W11.4: a caller-supplied output_dir must NOT skip upscale-back.

        The real production caller (render_queue.py) omits output_dir, but the
        smoke harness and the C1 pytest smoke pass one. Upscale-back is
        decoupled from path-promotion, so an oversized input is restored to
        original resolution even when stable_path == generated_path (in place).
        """
        src = tmp_path / "input_big.jpg"
        Image.new("RGB", (1920, 1280), color=(180, 130, 100)).save(src, "JPEG")
        caller_dir = tmp_path / "caller_output"
        caller_dir.mkdir()

        captured: dict = {}

        def fake_workflow(input_path, *, output_dir, workflow_name,
                          workflow_parameters, focus_mask_path,
                          positive_prompt, negative_prompt,
                          timeout_seconds, **kwargs):
            with Image.open(input_path) as im:
                captured["w"], captured["h"] = im.size
            # Synthesise the artifact at the RESIZED resolution, inside the
            # caller-provided output_dir (so generated_path lives there).
            generated = output_dir / "comfyui-generated.png"
            Image.new("RGB", (captured["w"], captured["h"]), color=(0, 255, 0)).save(generated, "PNG")
            return {"generated_path": str(generated)}

        monkeypatch.setattr(ai, "_run_comfyui_workflow", fake_workflow)

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["唇"], brand="md_ai", case_id=79,
            output_dir=caller_dir,
        )
        assert Path(result).is_file()
        # Workflow saw the resized 1280-edge input
        assert max(captured["w"], captured["h"]) == ai._FOCAL_MAX_LONG_EDGE
        # Despite the caller output_dir (in-place promotion), the returned PNG
        # must be upscaled back to the ORIGINAL resolution.
        with Image.open(result) as out:
            assert out.size == (1920, 1280)

    def test_nonresize_non8_source_restored_to_exact_dims(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """W11.5: a non-resized source whose dims aren't ÷8 must be restored.

        Source 853x1280 (long_edge=1280 == cap, so NO W11 resize → needs_upscale
        is False). SDXL/VAE snaps the working dims to a multiple of 8, so the
        generated artifact comes back 848x1280. Pre-W11.5 the ``needs_upscale``
        guard skipped restoration and the output stayed 848x1280 (5px short,
        surfaced in the C1.2 smoke for case 134). W11.5 conditions on the actual
        generated dims, so the output is restored to the exact 853x1280 source.
        """
        src = tmp_path / "input_853.jpg"
        Image.new("RGB", (853, 1280), color=(140, 110, 90)).save(src, "JPEG")

        captured: dict = {}

        def fake_workflow(input_path, *, output_dir, workflow_name,
                          workflow_parameters, focus_mask_path,
                          positive_prompt, negative_prompt,
                          timeout_seconds, **kwargs):
            with Image.open(input_path) as im:
                captured["w"], captured["h"] = im.size
            # Simulate SDXL ÷8 snap: 853 -> 848 (nearest multiple of 8 down).
            generated = output_dir / "comfyui-generated.png"
            Image.new("RGB", (848, 1280), color=(0, 255, 0)).save(generated, "PNG")
            return {"generated_path": str(generated)}

        monkeypatch.setattr(ai, "_run_comfyui_workflow", fake_workflow)

        result = ai.run_comfyui_focal_enhance(
            src, focus_targets=["泪沟"], brand="md_ai", case_id=129,
        )
        assert Path(result).is_file()
        # Workflow saw the un-resized 853-wide input (long_edge 1280 == cap).
        assert (captured["w"], captured["h"]) == (853, 1280)
        # W11.5: despite no W11 resize, the ÷8-snapped 848 output is restored
        # to the EXACT 853x1280 source dims (not left 5px short).
        with Image.open(result) as out:
            assert out.size == (853, 1280)
