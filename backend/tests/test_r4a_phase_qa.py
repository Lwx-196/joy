"""R4a phase QA: EXIF sequence, visual diff cross-check, and review queue."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_image(path: Path, *, color: tuple[int, int, int], exif_time: str | None = None) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (96, 96), color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 18, 68, 70), outline=(40, 40, 40), width=3)
    draw.ellipse((42, 34, 48, 40), fill=(20, 20, 20))
    exif = image.getexif()
    if exif_time:
        exif[306] = exif_time
    image.save(path, format="JPEG", exif=exif)


def _seed_case_with_images(conn, seed_case, case_dir: Path, image_files: list[str]) -> int:
    case_id = seed_case(
        abs_path=str(case_dir),
        category="standard_face",
        template_tier="single",
        customer_raw=case_dir.parent.name,
    )
    conn.execute(
        "UPDATE cases SET meta_json = ?, source_count = ?, labeled_count = ? WHERE id = ?",
        (
            json.dumps({"image_files": image_files, "image_count_total": len(image_files)}, ensure_ascii=False),
            len(image_files),
            len(image_files),
            case_id,
        ),
    )
    return int(case_id)


def test_r4a_exif_sequence_conflict_marks_phase_review(client, seed_case, tmp_path: Path) -> None:
    case_dir = tmp_path / "客户A" / "2026.1.1 法令纹"
    case_dir.mkdir(parents=True)
    _write_image(case_dir / "before-front.jpg", color=(180, 150, 130), exif_time="2026:01:01 12:00:00")
    _write_image(case_dir / "after-front.jpg", color=(182, 148, 132), exif_time="2026:01:01 09:00:00")

    from backend import db

    with db.connect() as conn:
        case_id = _seed_case_with_images(conn, seed_case, case_dir, ["before-front.jpg", "after-front.jpg"])

    rescan = client.post("/api/cases/rescan-groups")
    assert rescan.status_code == 200, rescan.text
    group = client.get("/api/case-groups").json()["items"][0]
    detail = client.get(f"/api/case-groups/{group['id']}/diagnosis").json()

    assert group["status"] == "needs_review"
    assert detail["group"]["diagnosis"]["phase_qa"]["exif_sequence_conflict_count"] == 1
    observations = {item["image_path"]: item for item in detail["image_observations"]}
    assert observations["before-front.jpg"]["case_id"] == case_id
    for image_path in ("before-front.jpg", "after-front.jpg"):
        assert "phase_exif_sequence_conflict" in observations[image_path]["reasons"]
        assert observations[image_path]["confidence"] < 0.65


def test_r4a_visual_diff_cross_check_flags_near_duplicate_pair(client, seed_case, tmp_path: Path) -> None:
    case_dir = tmp_path / "客户B" / "2026.1.2 下颌线"
    case_dir.mkdir(parents=True)
    _write_image(case_dir / "before-front.jpg", color=(170, 145, 128))
    _write_image(case_dir / "after-front.jpg", color=(170, 145, 128))

    from backend import db

    with db.connect() as conn:
        _seed_case_with_images(conn, seed_case, case_dir, ["before-front.jpg", "after-front.jpg"])

    rescan = client.post("/api/cases/rescan-groups")
    assert rescan.status_code == 200, rescan.text
    group = client.get("/api/case-groups").json()["items"][0]
    detail = client.get(f"/api/case-groups/{group['id']}/diagnosis").json()

    assert group["status"] == "needs_review"
    phase_qa = detail["group"]["diagnosis"]["phase_qa"]
    assert phase_qa["visual_pair_checked_count"] == 1
    assert phase_qa["visual_diff_low_count"] == 1
    observations = {item["image_path"]: item for item in detail["image_observations"]}
    for image_path in ("before-front.jpg", "after-front.jpg"):
        assert "phase_visual_diff_too_low" in observations[image_path]["reasons"]
        assert observations[image_path]["confidence"] < 0.65


def test_r4a_unknown_and_low_confidence_observations_enter_review_queue(
    client,
    seed_case,
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "客户C" / "2026.1.3 泪沟"
    case_dir.mkdir(parents=True)
    _write_image(case_dir / "mystery-front.jpg", color=(160, 140, 120))

    from backend import db
    from backend.services.vlm_source_classifier import fetch_classification_queue

    with db.connect() as conn:
        _seed_case_with_images(conn, seed_case, case_dir, ["mystery-front.jpg"])

    rescan = client.post("/api/cases/rescan-groups")
    assert rescan.status_code == 200, rescan.text
    group = client.get("/api/case-groups").json()["items"][0]
    detail = client.get(f"/api/case-groups/{group['id']}/diagnosis").json()
    observation = detail["image_observations"][0]

    assert group["status"] == "needs_review"
    assert observation["phase"] == "unknown"
    assert observation["confidence"] < 0.65
    assert "phase_review_required" in observation["reasons"]
    assert detail["group"]["diagnosis"]["phase_qa"]["review_required_count"] == 1

    with db.connect() as conn:
        queue = fetch_classification_queue(conn, case_id=observation["case_id"], max_items=20)

    assert [item.image_path for item in queue] == ["mystery-front.jpg"]
