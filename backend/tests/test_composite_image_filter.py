"""Tests for composite image detection and treatment type detection."""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from backend.source_images import (
    COMPOSITE_ASPECT_RATIO_THRESHOLD,
    is_composite_by_dimensions,
    is_composite_image,
    observation_reasons_indicate_composite,
)
from backend.source_selection import (
    candidate_quality,
    detect_treatment_type,
    detect_treatment_types,
)


class TestIsCompositeByDimensions:
    def test_normal_portrait(self):
        assert not is_composite_by_dimensions(800, 1200)

    def test_normal_landscape(self):
        assert not is_composite_by_dimensions(1200, 800)

    def test_square(self):
        assert not is_composite_by_dimensions(1000, 1000)

    def test_horizontal_composite(self):
        assert is_composite_by_dimensions(3000, 1000)

    def test_vertical_composite(self):
        assert is_composite_by_dimensions(1000, 3000)

    def test_exact_threshold(self):
        assert is_composite_by_dimensions(2500, 1000)

    def test_just_below_threshold(self):
        assert not is_composite_by_dimensions(2499, 1000)

    def test_zero_dimensions(self):
        assert not is_composite_by_dimensions(0, 1000)
        assert not is_composite_by_dimensions(1000, 0)
        assert not is_composite_by_dimensions(0, 0)

    def test_negative_dimensions(self):
        assert not is_composite_by_dimensions(-1000, 1000)


def _write_png(path: Path, width: int, height: int) -> None:
    """Write a minimal valid PNG with specified dimensions."""
    import zlib

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        raw = chunk_type + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + b"\x00" * (width * 3)
    raw_data = scanline * height
    idat_data = zlib.compress(raw_data)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(_chunk(b"IHDR", ihdr_data))
        f.write(_chunk(b"IDAT", idat_data))
        f.write(_chunk(b"IEND", b""))


def _write_white_canvas_pair(path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((60, 80, 520, 700), fill=(190, 185, 176))
    draw.rectangle((680, 80, 1140, 700), fill=(196, 188, 178))
    draw.rectangle((220, 720, 360, 770), fill=(40, 40, 40))
    draw.rectangle((840, 720, 980, 770), fill=(40, 40, 40))
    img.save(path)


def _write_light_wall_photo(path: Path) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 900), (232, 232, 228))
    draw = ImageDraw.Draw(img)
    draw.ellipse((430, 180, 770, 560), fill=(207, 172, 148))
    draw.rectangle((480, 560, 720, 850), fill=(245, 245, 245))
    img.save(path)


class TestIsCompositeImage:
    def test_normal_image(self, tmp_path: Path):
        img = tmp_path / "normal.png"
        _write_png(img, 800, 1200)
        assert not is_composite_image(img)

    def test_horizontal_composite_image(self, tmp_path: Path):
        img = tmp_path / "composite.png"
        _write_png(img, 3000, 1000)
        assert is_composite_image(img)

    def test_vertical_composite_image(self, tmp_path: Path):
        img = tmp_path / "stacked.png"
        _write_png(img, 1000, 3000)
        assert is_composite_image(img)

    def test_normal_ratio_white_canvas_pair_is_composite(self, tmp_path: Path):
        img = tmp_path / "normal-ratio-canvas-pair.jpg"
        _write_white_canvas_pair(img)
        assert is_composite_image(img)

    def test_light_wall_single_photo_is_not_composite(self, tmp_path: Path):
        img = tmp_path / "light-wall-photo.jpg"
        _write_light_wall_photo(img)
        assert not is_composite_image(img)

    def test_nonexistent_file(self, tmp_path: Path):
        assert not is_composite_image(tmp_path / "missing.png")

    def test_corrupted_file(self, tmp_path: Path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not an image at all")
        assert not is_composite_image(bad)

    def test_threshold_value(self):
        assert COMPOSITE_ASPECT_RATIO_THRESHOLD == 2.5


class TestCompositeObservationReasons:
    def test_side_by_side_before_after_reason(self):
        assert observation_reasons_indicate_composite([
            "The image is a side-by-side before-and-after collage showing the treatment result."
        ])

    def test_normal_healing_reason(self):
        assert not observation_reasons_indicate_composite([
            "Localized redness at treatment site after injection."
        ])


class TestDetectTreatmentType:
    def test_rhinoplasty_chinese(self):
        assert detect_treatment_type("/path/to/云镜隆鼻2025.5.21") == "rhinoplasty"

    def test_tear_trough(self):
        assert detect_treatment_type("/path/to/泪沟填充2025") == "tear_trough"

    def test_lip(self):
        assert detect_treatment_type("/path/to/丰唇案例") == "lip"

    @pytest.mark.parametrize(
        "path",
        [
            "/path/to/2025.12.10娇兰0.5注射唇",
            "/path/to/2026.1.25玻尿酸卧蚕 唇填充",
            "/path/to/2025.3.12填充唇",
            "/path/to/2025.12.27缇颜3支唇，法令纹，口角",
        ],
    )
    def test_lip_common_source_folder_names(self, path):
        assert detect_treatment_type(path) == "lip"

    def test_multi_treatment_keeps_primary_and_lists_all_matches(self):
        path = "/path/to/25.6.4嗨体填泪沟，唇，口角溶脂"
        assert detect_treatment_type(path) == "tear_trough"
        assert detect_treatment_types(path) == ["tear_trough", "lip"]

    def test_multi_treatment_rhinoplasty_lip(self):
        path = "/path/to/25.7.2隆鼻，卧蚕，泪沟，唇"
        assert detect_treatment_type(path) == "rhinoplasty"
        assert detect_treatment_types(path) == ["rhinoplasty", "tear_trough", "lip"]

    def test_chin(self):
        assert detect_treatment_type("/path/to/下巴整形") == "chin"

    def test_shoulder(self):
        assert detect_treatment_type("/path/to/瘦肩案例") == "shoulder"

    def test_no_match(self):
        assert detect_treatment_type("/path/to/普通案例") is None

    def test_empty_string(self):
        assert detect_treatment_type("") is None


class TestTreatmentViewBoost:
    def test_rhinoplasty_boosts_side(self):
        candidate = {"view": "side", "phase": "after"}
        result = candidate_quality(candidate, "primary", treatment_type="rhinoplasty")
        score_with = result["selection_score"]

        candidate2 = {"view": "side", "phase": "after"}
        result2 = candidate_quality(candidate2, "primary", treatment_type=None)
        score_without = result2["selection_score"]

        assert score_with > score_without

    def test_rhinoplasty_no_boost_for_front(self):
        candidate = {"view": "front", "phase": "after"}
        result = candidate_quality(candidate, "primary", treatment_type="rhinoplasty")
        score_with = result["selection_score"]

        candidate2 = {"view": "front", "phase": "after"}
        result2 = candidate_quality(candidate2, "primary", treatment_type=None)
        score_without = result2["selection_score"]

        assert score_with == score_without

    def test_tear_trough_boosts_front(self):
        candidate = {"view": "front", "phase": "before"}
        result = candidate_quality(candidate, "primary", treatment_type="tear_trough")
        score_with = result["selection_score"]

        candidate2 = {"view": "front", "phase": "before"}
        result2 = candidate_quality(candidate2, "primary", treatment_type=None)
        score_without = result2["selection_score"]

        assert score_with > score_without
