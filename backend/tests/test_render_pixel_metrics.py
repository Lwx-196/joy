from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from backend import render_pixel_metrics as rpm
from backend.render_quality import evaluate_render_result


def _synthetic_board(
    path: Path,
    *,
    bg_left: tuple[int, int, int] = (70, 70, 70),
    bg_right: tuple[int, int, int] = (72, 72, 72),
    face_scale: float = 0.85,
    letterbox: int = 4,
    halo: bool = False,
) -> Path:
    width, height = 1024, 684
    image = Image.new("RGB", (width, height), (236, 236, 236))
    draw = ImageDraw.Draw(image)
    for index, (x0, y0, x1, y1) in enumerate(((0, 0, width // 2, height), (width // 2, 0, width, height))):
        bg = bg_left if index == 0 else bg_right
        draw.rectangle((x0 + letterbox, y0 + letterbox, x1 - letterbox - 1, y1 - letterbox - 1), fill=bg)
        cell_w = x1 - x0 - (2 * letterbox)
        cell_h = y1 - y0 - (2 * letterbox)
        face_w = int(cell_w * face_scale)
        face_h = int(cell_h * face_scale)
        face_x0 = x0 + letterbox + ((cell_w - face_w) // 2)
        face_y0 = y0 + letterbox + ((cell_h - face_h) // 2)
        face_x1 = face_x0 + face_w
        face_y1 = face_y0 + face_h
        if halo:
            draw.rectangle((face_x0 - 12, face_y0 - 12, face_x1 + 12, face_y1 + 12), fill=(245, 245, 242))
        draw.rectangle((face_x0, face_y0, face_x1, face_y1), fill=(166, 118, 96))
    image.save(path)
    return path


def _metrics(path: Path) -> dict:
    metrics = rpm.compute_pixel_metrics(str(path))
    assert metrics["available"] is True
    assert 0.0 <= metrics["cv_penalty"] <= rpm.CV_PENALTY_CAP
    return metrics


def test_clean_board_has_no_pixel_penalty_or_flags(tmp_path: Path) -> None:
    board = _synthetic_board(tmp_path / "clean.jpg")

    metrics = _metrics(board)

    assert metrics["cv_penalty"] == 0.0
    assert metrics["flags"] == []


def test_heavy_letterbox_flags_and_caps_penalty(tmp_path: Path) -> None:
    board = _synthetic_board(tmp_path / "letterbox.jpg", letterbox=170)

    metrics = _metrics(board)

    assert "bg_letterbox" in metrics["flags"]
    assert metrics["cv_penalty"] > 0
    assert metrics["cv_penalty"] <= rpm.CV_PENALTY_CAP


def test_cell_underfill_flags_small_floating_content(tmp_path: Path) -> None:
    board = _synthetic_board(tmp_path / "underfill.jpg", face_scale=0.30)

    metrics = _metrics(board)

    assert "cell_underfill" in metrics["flags"]
    assert metrics["cv_penalty"] > 0


def test_background_mismatch_flags_different_cell_backgrounds(tmp_path: Path) -> None:
    board = _synthetic_board(tmp_path / "bg-mismatch.jpg", bg_right=(208, 190, 150))

    metrics = _metrics(board)

    assert "bg_mismatch" in metrics["flags"]
    assert metrics["cv_penalty"] > 0


def test_white_halo_flags_cutout_artifact(tmp_path: Path) -> None:
    board = _synthetic_board(tmp_path / "white-halo.jpg", halo=True)

    metrics = _metrics(board)

    assert "cutout_artifact" in metrics["flags"]
    assert metrics["cv_penalty"] > 0


def test_missing_and_broken_images_fail_open(tmp_path: Path) -> None:
    missing = rpm.compute_pixel_metrics(str(tmp_path / "missing.jpg"))
    broken_path = tmp_path / "broken.jpg"
    broken_path.write_bytes(b"not an image")
    broken = rpm.compute_pixel_metrics(str(broken_path))

    for metrics in (missing, broken):
        assert metrics["available"] is False
        assert metrics["cv_penalty"] == 0.0
        assert metrics["flags"] == []


def test_numpy_absent_fallback_keeps_metrics_fail_open_safe(monkeypatch, tmp_path: Path) -> None:
    board = _synthetic_board(tmp_path / "underfill-no-numpy.jpg", face_scale=0.30)
    monkeypatch.setattr(rpm, "np", None)

    metrics = rpm.compute_pixel_metrics(str(board))

    assert metrics["available"] is True
    assert metrics["numpy_available"] is False
    assert "cell_underfill" in metrics["flags"]
    assert 0.0 <= metrics["cv_penalty"] <= rpm.CV_PENALTY_CAP


def test_pixel_penalty_only_lowers_render_quality_score(tmp_path: Path) -> None:
    clean = _synthetic_board(tmp_path / "quality-clean.jpg")
    underfill = _synthetic_board(tmp_path / "quality-underfill.jpg", face_scale=0.30)
    base_result = {
        "status": "ok",
        "blocking_issue_count": 0,
        "warning_count": 0,
        "warnings": [],
        "composition_alerts": [],
        "ai_usage": {},
    }

    clean_quality = evaluate_render_result({**base_result, "output_path": str(clean)})
    underfill_quality = evaluate_render_result({**base_result, "output_path": str(underfill)})

    assert clean_quality["quality_score"] == 100.0
    assert clean_quality["metrics"]["pixel_metrics"]["cv_penalty"] == 0.0
    assert underfill_quality["metrics"]["pixel_metrics"]["cv_penalty"] > 0
    assert underfill_quality["quality_score"] < clean_quality["quality_score"]
    assert underfill_quality["quality_score"] == 100.0 - underfill_quality["metrics"]["pixel_metrics"]["cv_penalty"]
