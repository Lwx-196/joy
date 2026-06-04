"""Unit tests for run_clinical_archive_pipeline (M2 path) — Step 1 of 4-mode plan.

This pipeline must:
  * Honour EXIF orientation (exif_transpose)
  * Optionally apply gray-world LAB white balance (apply_white_balance kwarg)
  * Never raise — silent-fail returns input on any error
  * Cleanup tempdir when caller didn't provide output_dir
  * Honour caller-provided output_dir (NO auto-cleanup, returns generated path directly)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.ai_generation_adapter import run_clinical_archive_pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jpg(tmp_path: Path, name: str, size: tuple[int, int] = (640, 480),
              color: tuple[int, int, int] = (128, 128, 128)) -> Path:
    """Create a plain RGB JPG for testing."""
    img = Image.new("RGB", size, color=color)
    p = tmp_path / name
    img.save(p, format="JPEG", quality=90)
    return p


def _make_jpg_with_exif_rotation(tmp_path: Path, name: str, orientation: int = 6) -> Path:
    """Create JPG with EXIF orientation tag.

    Orientation values per EXIF spec:
      1 = top-left (default, no rotation)
      6 = rotate 90° CW
      8 = rotate 90° CCW
    """
    img = Image.new("RGB", (640, 480), color=(200, 100, 50))
    # PIL doesn't have a clean direct EXIF API; use piexif if available, else
    # fall back to manually writing EXIF bytes via Image.save with exif=
    try:
        import piexif
        zeroth = {piexif.ImageIFD.Orientation: orientation}
        exif_bytes = piexif.dump({"0th": zeroth, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None})
    except ImportError:
        # Minimal raw EXIF blob with orientation tag (TIFF + IFD0)
        # This is fragile but works for orientation-only test.
        exif_bytes = bytes()
    p = tmp_path / name
    if exif_bytes:
        img.save(p, format="JPEG", quality=90, exif=exif_bytes)
    else:
        img.save(p, format="JPEG", quality=90)
    return p


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestArchivePipelineHappyPath:

    def test_no_exif_rotation_outputs_same_dimensions(self, tmp_path: Path):
        """Image with no EXIF rotation → output has same dimensions as input."""
        src = _make_jpg(tmp_path, "no_exif.jpg", size=(800, 600))
        outdir = tmp_path / "out"
        result = run_clinical_archive_pipeline(src, output_dir=outdir)
        assert result.is_file()
        with Image.open(result) as out:
            assert out.size == (800, 600)
        assert result.suffix == ".png"  # output is PNG (lossless)

    def test_caller_provided_output_dir_preserves_output(self, tmp_path: Path):
        """When output_dir is provided, helper does NOT clean it up."""
        src = _make_jpg(tmp_path, "input.jpg")
        outdir = tmp_path / "persistent_out"
        result = run_clinical_archive_pipeline(src, output_dir=outdir)
        assert result.is_file()
        # outdir should still exist + contain the result file
        assert outdir.is_dir()
        assert any(p == result for p in outdir.iterdir())

    def test_auto_tempdir_is_cleaned_up_and_stable_path_returned(self, tmp_path: Path):
        """When output_dir=None, helper creates tempdir + cleans it + leaves stable copy."""
        src = _make_jpg(tmp_path, "input.jpg")
        result = run_clinical_archive_pipeline(src, output_dir=None)
        assert result.is_file()
        # Result lives next to input (.archive-out-* prefix)
        assert result.parent == src.parent
        assert result.name.startswith(".archive-out-")
        # No leaked .archive-pipeline-* tempdir
        leaked = [p for p in src.parent.iterdir() if p.name.startswith(".archive-pipeline-")]
        assert leaked == []
        # Cleanup
        result.unlink()


# ---------------------------------------------------------------------------
# EXIF orientation handling
# ---------------------------------------------------------------------------

class TestExifTranspose:

    def test_rotated_jpg_gets_corrected(self, tmp_path: Path):
        """EXIF orientation=6 (rotate 90° CW) → output dimensions swap."""
        try:
            import piexif  # noqa: F401
        except ImportError:
            pytest.skip("piexif not installed; can't write EXIF for this test")

        src = _make_jpg_with_exif_rotation(tmp_path, "rot90.jpg", orientation=6)
        # Source on disk has 640x480 with EXIF saying "rotate 90 CW"
        # After exif_transpose, output should be 480x640 (rotated)
        outdir = tmp_path / "out"
        result = run_clinical_archive_pipeline(src, output_dir=outdir)
        with Image.open(result) as out:
            # The 90° rotation should swap dimensions
            assert out.size == (480, 640)

    def test_orientation_1_default_no_change(self, tmp_path: Path):
        """EXIF orientation=1 (no rotation needed) → dimensions unchanged."""
        try:
            import piexif  # noqa: F401
        except ImportError:
            pytest.skip("piexif not installed")
        src = _make_jpg_with_exif_rotation(tmp_path, "normal.jpg", orientation=1)
        outdir = tmp_path / "out"
        result = run_clinical_archive_pipeline(src, output_dir=outdir)
        with Image.open(result) as out:
            assert out.size == (640, 480)


# ---------------------------------------------------------------------------
# White balance opt-in
# ---------------------------------------------------------------------------

class TestWhiteBalance:

    def test_wb_disabled_does_not_alter_colors_significantly(self, tmp_path: Path):
        """apply_white_balance=False → minimal color change (only re-encode)."""
        src = _make_jpg(tmp_path, "input.jpg", color=(150, 150, 150))  # neutral grey
        outdir = tmp_path / "out"
        result = run_clinical_archive_pipeline(src, output_dir=outdir, apply_white_balance=False)
        with Image.open(result) as out:
            # ImageStat.mean is C-level fast; sum(getdata(), ()) is O(n^2) quadratic
            # tuple concat — would hang for 95s on 640x480 input.
            from PIL.ImageStat import Stat
            mean_r, mean_g, mean_b = Stat(out.convert("RGB")).mean
            avg = (mean_r + mean_g + mean_b) / 3
            assert 130 < avg < 170  # close to 150 input

    def test_wb_enabled_shifts_color_cast(self, tmp_path: Path):
        """apply_white_balance=True on warm-cast image → neutralises."""
        # Warm cast: more red, less blue → mean a* > 128 (red), b* > 128 (yellow)
        src = _make_jpg(tmp_path, "warm.jpg", color=(200, 130, 80))
        outdir = tmp_path / "out"
        result = run_clinical_archive_pipeline(src, output_dir=outdir, apply_white_balance=True)
        # Compare against no-WB version
        result_no_wb = run_clinical_archive_pipeline(src, output_dir=tmp_path / "out2", apply_white_balance=False)
        with Image.open(result) as wb_im, Image.open(result_no_wb) as raw_im:
            # WB version should have shifted ‘a’ channel away from red side
            from PIL.ImageStat import Stat
            wb_lab = wb_im.convert("LAB")
            raw_lab = raw_im.convert("LAB")
            assert Stat(wb_lab).mean[1] < Stat(raw_lab).mean[1]  # less red-shift after WB


# ---------------------------------------------------------------------------
# Silent-fail contract
# ---------------------------------------------------------------------------

class TestSilentFail:

    def test_missing_input_returns_input_path(self, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist.jpg"
        result = run_clinical_archive_pipeline(nonexistent, output_dir=tmp_path / "out")
        assert result == nonexistent  # silent-fail returns input

    def test_non_image_file_returns_input_path(self, tmp_path: Path):
        notimg = tmp_path / "fake.jpg"
        notimg.write_text("this is not an image", encoding="utf-8")
        result = run_clinical_archive_pipeline(notimg, output_dir=tmp_path / "out")
        # PIL raises on non-image → silent-fail returns input
        assert result == notimg

    def test_unwritable_output_dir_returns_input(self, tmp_path: Path, monkeypatch):
        """If output write fails (e.g., disk full), silent-fail."""
        src = _make_jpg(tmp_path, "input.jpg")
        outdir = tmp_path / "out"
        outdir.mkdir()
        # Force the save to fail by monkeypatching Image.save to raise
        from PIL import Image as PILImage
        original_save = PILImage.Image.save
        def _raise_save(self, *args, **kwargs):
            raise OSError("disk full simulation")
        monkeypatch.setattr(PILImage.Image, "save", _raise_save)
        result = run_clinical_archive_pipeline(src, output_dir=outdir)
        # Restore
        monkeypatch.setattr(PILImage.Image, "save", original_save)
        assert result == src  # silent-fail


# ---------------------------------------------------------------------------
# Tempdir cleanup contract (mirrors K-1 of run_comfyui_inline_enhance)
# ---------------------------------------------------------------------------

def test_auto_tempdir_cleanup_even_on_exception(tmp_path: Path, monkeypatch):
    """Even if PIL raises mid-process, the auto-tempdir must be cleaned up."""
    src = _make_jpg(tmp_path, "input.jpg")
    from PIL import ImageOps
    def _raise_transpose(im):
        raise RuntimeError("simulated transpose failure")
    monkeypatch.setattr(ImageOps, "exif_transpose", _raise_transpose)
    result = run_clinical_archive_pipeline(src, output_dir=None)
    assert result == src  # silent-fail returns input
    # No leaked tempdir
    leaked = [p for p in src.parent.iterdir() if p.name.startswith(".archive-pipeline-")]
    assert leaked == []
