from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend.services.triptych_composer import compose_triptych


def _solid(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def test_compose_triptych_resizes_three_panels_to_common_height(tmp_path: Path):
    before = _solid(tmp_path / "before.png", (100, 50), (200, 0, 0))
    immediate = _solid(tmp_path / "immediate.png", (10, 20), (0, 180, 0))
    recovery = _solid(tmp_path / "recovery.png", (30, 30), (0, 0, 220))

    out = compose_triptych(
        [before, immediate, recovery],
        tmp_path / "out" / "triptych.png",
        height=20,
        gap=3,
        bg=(245, 245, 245),
    )

    assert out == tmp_path / "out" / "triptych.png"
    image = Image.open(out).convert("RGB")
    assert image.size == (76, 20)
    assert image.getpixel((0, 0)) == (200, 0, 0)
    assert image.getpixel((40, 0)) == (245, 245, 245)
    assert image.getpixel((43, 0)) == (0, 180, 0)
    assert image.getpixel((53, 0)) == (245, 245, 245)
    assert image.getpixel((56, 0)) == (0, 0, 220)


def test_compose_triptych_supports_two_panel_scene(tmp_path: Path):
    before = _solid(tmp_path / "before.png", (20, 10), (10, 20, 30))
    recovery = _solid(tmp_path / "recovery.png", (10, 10), (40, 50, 60))

    out = compose_triptych([before, recovery], tmp_path / "two.png", height=10, gap=5)

    image = Image.open(out).convert("RGB")
    assert image.size == (35, 10)
    assert image.getpixel((0, 5)) == (10, 20, 30)
    assert image.getpixel((20, 5)) == (245, 245, 245)
    assert image.getpixel((25, 5)) == (40, 50, 60)


def test_compose_triptych_labels_add_clean_title_bar(tmp_path: Path):
    before = _solid(tmp_path / "before.png", (20, 10), (200, 0, 0))
    recovery = _solid(tmp_path / "recovery.png", (10, 10), (0, 0, 200))

    out = compose_triptych(
        [before, recovery],
        tmp_path / "labeled.png",
        height=10,
        gap=2,
        labels=["术前", "AI预测恢复期"],
    )

    image = Image.open(out).convert("RGB")
    assert image.size == (32, 74)
    assert image.getpixel((0, 63)) == (245, 245, 245)
    assert image.getpixel((0, 64)) == (200, 0, 0)
    assert image.getpixel((22, 64)) == (0, 0, 200)


@pytest.mark.parametrize("panel_count", [0, 1, 4])
def test_compose_triptych_rejects_invalid_panel_count(tmp_path: Path, panel_count: int):
    panels = [
        _solid(tmp_path / f"panel-{idx}.png", (10, 10), (idx, idx, idx))
        for idx in range(panel_count)
    ]

    with pytest.raises(ValueError, match="2 or 3 panels"):
        compose_triptych(panels, tmp_path / "invalid.png")


def test_compose_triptych_requires_label_count_to_match_panels(tmp_path: Path):
    before = _solid(tmp_path / "before.png", (10, 10), (1, 2, 3))
    recovery = _solid(tmp_path / "recovery.png", (10, 10), (4, 5, 6))

    with pytest.raises(ValueError, match="labels length"):
        compose_triptych([before, recovery], tmp_path / "invalid.png", labels=["术前"])
