"""Regression guard: the difference/drift scorer must be EXIF-orientation invariant.

Investigation 2026-06-01 (identity-anchor line): an owner report of "hard identity
drift 居多" on the post-op AI-enhance path turned out to be NOT identity reshaping.
The `difference_analysis` drift scores were polluted by ORIENTATION MISMATCH —
a baseline stored at one EXIF orientation compared against a candidate at another
(e.g. case 140 shot landscape but displaying portrait; a 180°-flipped output) scored
30–80 on `full_frame_change_score`, inflating the drift ranking and producing false
"hard drift" alarms. The fix (already live on main) is that `_create_difference_heatmap`
`exif_transpose`-normalises BOTH images before comparing.

This locks that fix: two images that DISPLAY identically but are STORED at opposite
EXIF orientations must score ~0 drift — never the historical 30–80. If someone drops
the `exif_transpose` calls, this test fails loudly.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from backend import ai_generation_adapter

_EXIF_ORIENTATION_TAG = 0x0112  # 274


def _distinctive(width: int = 480, height: int = 640) -> Image.Image:
    """An asymmetric image so a 180° rotation is maximally different (a solid
    colour would be rotation-invariant and make the test vacuous)."""
    img = Image.new("RGB", (width, height), (40, 40, 40))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, width, height // 2), fill=(200, 60, 60))      # top half red
    d.rectangle((0, height // 2, width, height), fill=(60, 60, 200))  # bottom half blue
    d.ellipse((20, 20, 90, 90), fill=(240, 240, 40))                  # marker, top-left
    return img


def _save_jpeg(img: Image.Image, path: Path, orientation: int) -> None:
    exif = img.getexif()
    exif[_EXIF_ORIENTATION_TAG] = orientation
    img.save(path, format="JPEG", quality=95, exif=exif)


def _full_frame_raw_mean_abs(a: Path, b: Path) -> float:
    """Mean |Δ| WITHOUT exif normalisation — what the scorer would see if the
    fix were absent (proves the two inputs genuinely differ on disk)."""
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB").resize(ia.size)
    return ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0]


def test_difference_heatmap_is_exif_orientation_invariant(tmp_path: Path) -> None:
    disp = _distinctive()

    # Candidate: disp stored upright (orientation = 1, the no-op orientation).
    candidate = tmp_path / "candidate.jpg"
    _save_jpeg(disp, candidate, orientation=1)

    # Baseline: SAME displayed image, but stored rotated 180° + EXIF orientation=3
    # ("rotated 180°"). exif_transpose recovers `disp`, so the two DISPLAY identically.
    baseline = tmp_path / "baseline.jpg"
    _save_jpeg(disp.transpose(Image.Transpose.ROTATE_180), baseline, orientation=3)

    # Precondition: exif_transpose must recover the same display for both (else the
    # construction is wrong, not the code under test).
    bt = ImageOps.exif_transpose(Image.open(baseline)).convert("RGB")
    ct = ImageOps.exif_transpose(Image.open(candidate)).convert("RGB")
    assert bt.size == ct.size == disp.size
    assert ImageStat.Stat(ImageChops.difference(bt, ct)).mean[0] < 5.0

    # Sanity: on disk (pre-transpose) the two are massively different (a 180° flip)
    # — so a ~0 drift score below can ONLY come from the scorer normalising EXIF.
    assert _full_frame_raw_mean_abs(baseline, candidate) > 20.0

    metrics = ai_generation_adapter._create_difference_heatmap(
        baseline, candidate, tmp_path / "heatmap.png", []
    )

    # The guard: orientation-only difference must score ~0, NOT the historical 30–80.
    assert metrics["full_frame_change_score"] < 5.0, (
        "drift scorer is NOT EXIF-orientation invariant — orientation mismatch is "
        f"polluting the score ({metrics['full_frame_change_score']}); "
        "the exif_transpose normalisation in _create_difference_heatmap regressed."
    )
    assert metrics["non_target_change_score"] < 5.0
