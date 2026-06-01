from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

from backend.scripts import export_delivery_batch
from backend.services.delivery_gate import DeliverableItem
from backend.services.single_image_delivery import (
    SingleImageBuildError,
    build_enhanced_after,
    closeup_filename,
)


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE cases (id INTEGER PRIMARY KEY, abs_path TEXT NOT NULL)")
    return conn


def _item(case_id: int = 1, *, case_name: str = "2026泪沟") -> DeliverableItem:
    return DeliverableItem(
        case_id=case_id,
        customer="客户A",
        case_name=case_name,
        category="standard_face",
        template_tier="tri",
        quality_score=91.8,
        quality_status="done",
        artifact_mode="real_layout",
        blocking_count=0,
        warning_count=0,
        source_path="/tmp/final-board.jpg",
        job_id=101,
    )


def _write_rgb(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 90), color).save(path)


def _enhance(after_path: Path, focus_targets: list[str], out_dir: Path) -> Path:
    assert focus_targets == ["泪沟"]
    out_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(after_path) as img:
        enhanced = img.convert("RGB")
    draw = ImageDraw.Draw(enhanced)
    draw.rectangle((50, 25, 70, 35), fill=(245, 245, 245))
    out = out_dir / "classical_enhanced.png"
    enhanced.save(out, format="PNG")
    return out


def test_build_enhanced_after_uses_frontal_after_and_stages_full_res(tmp_path: Path) -> None:
    case_dir = tmp_path / "客户A" / "2026.01.01泪沟"
    _write_rgb(case_dir / "术前1.jpg", (20, 20, 20))
    _write_rgb(case_dir / "side-after.jpg", (40, 40, 40))
    _write_rgb(case_dir / "front-after.jpg", (80, 80, 80))
    conn = _mem()
    conn.execute("INSERT INTO cases (id, abs_path) VALUES (?, ?)", (1, str(case_dir)))

    enhanced = build_enhanced_after(_item(), tmp_path / "scratch", conn, enhance_fn=_enhance)

    assert enhanced.source_after_path.name == "front-after.jpg"
    assert enhanced.enhanced_path.is_file()
    assert enhanced.raw_path.is_file()
    assert enhanced.mask_path.is_file()
    assert enhanced.judge_baseline_path.is_file()
    assert enhanced.judge_candidate_path.is_file()
    assert enhanced.focus_targets == ("泪沟",)
    assert enhanced.enhanced_path.suffix == ".png"


def test_build_enhanced_after_holds_when_after_unreachable(tmp_path: Path) -> None:
    case_dir = tmp_path / "客户A" / "2026.01.01泪沟"
    _write_rgb(case_dir / "术前1.jpg", (20, 20, 20))
    conn = _mem()
    conn.execute("INSERT INTO cases (id, abs_path) VALUES (?, ?)", (1, str(case_dir)))

    try:
        build_enhanced_after(_item(), tmp_path / "scratch", conn, enhance_fn=_enhance)
    except SingleImageBuildError as exc:
        assert "no source after image" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("missing after image must be held")


def test_closeup_naming_and_export_path() -> None:
    item = _item(case_name="case/name")
    assert closeup_filename(item) == "case_name__closeup_score91.png"
    assert (
        export_delivery_batch._single_image_dest_path(Path("/delivery"), item)
        == Path("/delivery/客户A/closeups/case_name__closeup_score91.png")
    )


def test_single_image_default_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("CASE_WORKBENCH_SINGLE_IMAGE_DELIVERY", raising=False)
    assert export_delivery_batch._single_image_enabled_default() is False
    monkeypatch.setenv("CASE_WORKBENCH_SINGLE_IMAGE_DELIVERY", "1")
    assert export_delivery_batch._single_image_enabled_default() is True
