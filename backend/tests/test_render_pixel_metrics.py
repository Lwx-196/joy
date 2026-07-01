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


def _synthetic_template_board(path: Path) -> Path:
    """Two-row formal board with large brand-template gutters around photo cells."""
    width, height = 938, 1024
    page_bg = (234, 228, 220)
    bar = (119, 99, 84)
    image = Image.new("RGB", (width, height), page_bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 18, width - 28, 148), radius=12, fill=(250, 248, 244), outline=(218, 210, 202), width=2)
    draw.rectangle((28, 160, width - 28, 198), fill=bar)
    draw.rectangle((28, 552, width - 28, 590), fill=bar)

    photo_boxes = [
        (190, 295, 370, 515),
        (568, 295, 748, 515),
        (190, 705, 370, 925),
        (568, 705, 748, 925),
    ]
    for idx, (x0, y0, x1, y1) in enumerate(photo_boxes):
        draw.rounded_rectangle((x0 - 12, y0 - 20, x1 + 12, y1 + 12), radius=8, fill=(250, 248, 244), outline=(224, 216, 208), width=2)
        draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))
        subject_w = int((x1 - x0) * 0.58)
        subject_h = int((y1 - y0) * 0.62)
        sx0 = x0 + ((x1 - x0 - subject_w) // 2)
        sy0 = y1 - subject_h
        sx1 = sx0 + subject_w
        sy1 = y1
        draw.ellipse((sx0 + 28, sy0, sx1 - 28, sy0 + subject_w), fill=(186, 142, 120))
        draw.rectangle((sx0, sy0 + subject_w // 2, sx1, sy1), fill=(72, 78, 82))
    image.save(path)
    return path


def _synthetic_postop_cast_board(path: Path, *, cast_after: bool) -> Path:
    width, height = 1024, 720
    image = Image.new("RGB", (width, height), (238, 232, 224))
    draw = ImageDraw.Draw(image)
    panel = (200, 140, 824, 500)
    draw.rectangle(panel, fill=(0, 0, 0))
    mid = (panel[0] + panel[2]) // 2
    before_face = (170, 136, 122)
    after_face = (161, 150, 136) if cast_after else (174, 148, 126)
    for idx, (x0, x1, color) in enumerate(
        (
            (panel[0] + 24, mid - 16, before_face),
            (mid + 16, panel[2] - 24, after_face),
        )
    ):
        cell_w = x1 - x0
        face_w = int(cell_w * 0.62)
        fx0 = x0 + (cell_w - face_w) // 2
        fy0 = panel[1] + 42
        fx1 = fx0 + face_w
        fy1 = fy0 + int(face_w * 1.15)
        draw.ellipse((fx0, fy0, fx1, fy1), fill=color)
        draw.rectangle((fx0 + 16, fy0 + face_w // 2, fx1 - 16, fy1 + 90), fill=color)
        draw.rectangle((fx0 - 16, fy1 + 40, fx1 + 16, panel[3]), fill=(62, 62, 66))
    image.save(path)
    return path


def _synthetic_side_scale_board(path: Path, *, mismatch: bool) -> Path:
    width, height = 938, 1024
    page_bg = (234, 228, 220)
    image = Image.new("RGB", (width, height), page_bg)
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 160, width - 28, 198), fill=(119, 99, 84))
    draw.rectangle((28, 552, width - 28, 590), fill=(119, 99, 84))
    photo_boxes = [
        (190, 235, 370, 495),
        (568, 235, 748, 495),
        (190, 665, 370, 925),
        (568, 665, 748, 925),
    ]
    for idx, (x0, y0, x1, y1) in enumerate(photo_boxes):
        draw.rounded_rectangle((x0 - 12, y0 - 18, x1 + 12, y1 + 12), radius=8, fill=(250, 248, 244))
        draw.rectangle((x0, y0, x1, y1), fill=(0, 0, 0))
        is_after_side = idx == 3
        scale = 1.28 if mismatch and is_after_side else 1.0
        face_w = int((x1 - x0) * 0.44 * scale)
        face_h = int((y1 - y0) * 0.46 * scale)
        fx0 = x0 + 12
        fy0 = y0 + int((y1 - y0) * 0.26)
        fx1 = fx0 + face_w
        fy1 = fy0 + face_h
        draw.ellipse((fx0, fy0, fx1, fy1), fill=(178, 136, 112))
        draw.rectangle((fx0 + 8, fy0 + face_h // 2, fx1 - 4, min(y1, fy1 + 80)), fill=(178, 136, 112))
        draw.rectangle((fx0 - 18, min(y1, fy1 + 55), min(x1, fx1 + 28), y1), fill=(54, 60, 64))
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


def test_t246_visible_cutout_edge_halo_real_outputs_are_flagged() -> None:
    rejected_boards = [
        Path("/Users/a1234/Desktop/T246-retry-freshAI-accepted-1case-20260628/review_rejected/case_366_job_1052_review_rejected_高雅静.jpg"),
        Path("/Users/a1234/Desktop/T246-retry-freshAI-accepted-1case-20260628/review_rejected/case_137_job_1053_review_rejected_阮素娘.jpg"),
    ]
    accepted_board = Path("/Users/a1234/Desktop/T246-retry-freshAI-accepted-1case-20260628/accepted/case_22_job_1054_accepted_李建凤.jpg")
    for board in [*rejected_boards, accepted_board]:
        assert board.is_file()

    rejected_metrics = [_metrics(board) for board in rejected_boards]
    accepted_metrics = _metrics(accepted_board)

    for metrics in rejected_metrics:
        assert "cutout_artifact" in metrics["flags"]
        assert metrics["max_mask_edge_halo_score"] > rpm.MASK_EDGE_HALO_FLAG
        assert metrics["cv_penalty"] >= 15.0
    assert "cutout_artifact" not in accepted_metrics["flags"]
    assert accepted_metrics["max_mask_edge_halo_score"] < rpm.MASK_EDGE_HALO_FLAG


def test_template_gutters_do_not_flag_blank_region(tmp_path: Path) -> None:
    board = _synthetic_template_board(tmp_path / "template-gutters.jpg")

    metrics = _metrics(board)

    assert "blank_region" not in metrics["flags"]


def test_template_labels_do_not_flag_cutout_artifact_without_halo(tmp_path: Path) -> None:
    board = _synthetic_template_board(tmp_path / "template-labels.jpg")

    metrics = _metrics(board)

    assert "cutout_artifact" not in metrics["flags"]


def test_postop_cyan_cast_flags_after_face_warmth_drop(tmp_path: Path) -> None:
    board = _synthetic_postop_cast_board(tmp_path / "postop-cast.jpg", cast_after=True)

    metrics = _metrics(board)

    assert "postop_cyan_cast" in metrics["flags"]
    assert metrics["postop_skin_cast"]["flagged"] is True
    assert metrics["postop_skin_cast"]["after"]["r_minus_g"] < 14.0


def test_balanced_postop_skin_tone_does_not_flag_cast(tmp_path: Path) -> None:
    board = _synthetic_postop_cast_board(tmp_path / "postop-balanced.jpg", cast_after=False)

    metrics = _metrics(board)

    assert "postop_cyan_cast" not in metrics["flags"]
    assert metrics["postop_skin_cast"]["flagged"] is False


def test_side_scale_mismatch_flags_bottom_photo_row(tmp_path: Path) -> None:
    board = _synthetic_side_scale_board(tmp_path / "side-scale-mismatch.jpg", mismatch=True)

    metrics = _metrics(board)

    assert "side_scale_mismatch" in metrics["flags"]
    assert metrics["side_scale_mismatch"]["flagged"] is True
    assert metrics["side_scale_mismatch"]["ratios"]["skin_area"] >= rpm.SIDE_SCALE_SKIN_AREA_RATIO_MAX


def test_balanced_side_scale_does_not_flag(tmp_path: Path) -> None:
    board = _synthetic_side_scale_board(tmp_path / "side-scale-balanced.jpg", mismatch=False)

    metrics = _metrics(board)

    assert "side_scale_mismatch" not in metrics["flags"]
    assert metrics["side_scale_mismatch"]["flagged"] is False


def test_t253_real_case33_side_scale_mismatch_is_flagged() -> None:
    board = Path("/Users/a1234/Desktop/T252-case33-欧美吟-freshAI-accepted-20260629/case33-job1057-final-board.jpg")
    assert board.is_file()

    metrics = _metrics(board)

    assert "side_scale_mismatch" in metrics["flags"]
    assert metrics["side_scale_mismatch"]["flagged"] is True
    assert metrics["side_scale_mismatch"]["ratios"]["skin_area"] >= 1.35


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
