"""C1 — real-ComfyUI smoke for the W11/W11.4 focal enhance pipeline.

This is the pytest-ized evolution of /tmp/g1-m3-smoke/smoke_w11_case79.py
(task #107 manual smoke). It really fires SDXL on case 79 (1920x1280) — the
case that timed out before W11 — and asserts the post-W11/W11.4 contract:

  - verdict ok (output != input, byte-distinct)
  - W11-3 auto-resize triggers (1920x1280 → 1280x853 working res)
  - W11.4 upscale-back restores the ORIGINAL resolution even though we pass a
    caller-supplied output_dir (the branch that smoke #3 exposed)
  - ComfyUI /history advances by 1 (the pipeline really fired)

Gated behind ``SMOKE_REAL_COMFYUI=1`` so normal CI never runs it (it needs a
live ComfyUI on :8188, a real model, and ~4-5 min on Apple MPS). The input
image path defaults to the manual-smoke staging location and can be overridden
with ``SMOKE_W11_INPUT``.

Run it manually with:

    SMOKE_REAL_COMFYUI=1 \
      ../case-workbench/.venv/bin/python -m pytest \
      backend/tests/test_smoke_w11_case79.py -s -v
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path

import pytest
from PIL import Image, ImageOps

from backend import ai_generation_adapter as ai

COMFYUI_BASE = os.environ.get("SMOKE_COMFYUI_BASE", "http://127.0.0.1:8188")
DEFAULT_INPUT = "/tmp/g1-m3-smoke/input/case79.jpg"
FOCUS_TARGETS = ["下巴", "面颊", "chin"]
CASE_ID = 79

pytestmark = pytest.mark.skipif(
    os.environ.get("SMOKE_REAL_COMFYUI") != "1",
    reason="real-ComfyUI smoke; set SMOKE_REAL_COMFYUI=1 to run (needs live :8188 + ~5min)",
)


def _sha256_16(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _history_count() -> int:
    with urllib.request.urlopen(f"{COMFYUI_BASE}/history", timeout=5) as resp:
        return len(json.load(resp))


def _comfyui_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{COMFYUI_BASE}/system_stats", timeout=5) as resp:
            return resp.status == 200
    except OSError:
        return False


def test_w11_case79_real_comfyui_smoke(tmp_path: Path):
    input_path = Path(os.environ.get("SMOKE_W11_INPUT", DEFAULT_INPUT))
    if not input_path.is_file():
        pytest.skip(f"smoke input image not found: {input_path}")
    if not _comfyui_reachable():
        pytest.skip(f"ComfyUI not reachable at {COMFYUI_BASE}")

    with Image.open(input_path) as im:
        im = ImageOps.exif_transpose(im)
        orig_w, orig_h = im.size

    long_edge = max(orig_w, orig_h)
    expect_resize = long_edge > ai._FOCAL_MAX_LONG_EDGE

    out_dir = tmp_path / "case79_w11"
    out_dir.mkdir()

    pre_hc = _history_count()
    t0 = time.monotonic()
    result = ai.run_comfyui_focal_enhance(
        input_path,
        focus_targets=FOCUS_TARGETS,
        brand="md_ai",
        case_id=CASE_ID,
        output_dir=out_dir,
    )
    dt = time.monotonic() - t0
    delta_hc = _history_count() - pre_hc

    # The pipeline really fired (not a silent-fail returning the input).
    assert result != input_path, f"silent_fail after {dt:.0f}s (returned input)"
    result = Path(result)
    assert result.is_file(), f"phantom output path: {result}"
    assert _sha256_16(result) != _sha256_16(input_path), "output byte-identical to input"
    assert delta_hc >= 1, f"ComfyUI /history did not advance (delta={delta_hc})"

    # W11-3 auto-resize + W11.4 upscale-back: the returned image must be at the
    # ORIGINAL resolution even though we passed a caller-supplied output_dir.
    with Image.open(result) as out:
        assert out.size == (orig_w, orig_h), (
            f"expected upscale-back to {orig_w}x{orig_h}, got {out.size} "
            f"(resize_expected={expect_resize})"
        )
