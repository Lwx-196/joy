"""Step 4 of 4-mode plan: verify M4 composite routing wires to existing layout-only path.

The existing ``render_executor.run_render`` pipeline already handles the
tri-compare / composite layout case. Step 4 just ensures the router
dispatches ``template=tri-compare`` cases to COMPOSITE mode, and the
``_automate_md_ai_clinical_enhancements`` short-circuits BEFORE calling any
enhancement helper. The downstream ``run_render`` then renders the
composite layout normally.

This test guards the wiring: a tri-compare md_ai render must NOT call P1,
M2 archive, or M3 focal helpers — only run_render itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from backend import ai_generation_adapter, render_queue


def _make_staging(tmp_path: Path, name: str = "术后-正面.jpg") -> Path:
    staging = tmp_path / ".case-workbench-bound-render" / "job-1"
    staging.mkdir(parents=True)
    img = Image.new("RGB", (640, 480), color=(180, 180, 180))
    img.save(staging / name, format="JPEG", quality=90)
    return staging


def test_template_tri_compare_short_circuits_no_helper_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``template='tri-compare'`` must route to COMPOSITE → return without
    calling any per-image enhancement helper.
    """
    p1_calls = []
    p2_calls = []
    archive_calls = []

    monkeypatch.setattr(
        ai_generation_adapter, "run_direct_clinical_enhancement",
        lambda *a, **kw: p1_calls.append(kw) or a[0],
    )
    monkeypatch.setattr(
        ai_generation_adapter, "run_comfyui_focal_enhance",
        lambda *a, **kw: p2_calls.append(kw) or a[0],
    )
    monkeypatch.setattr(
        ai_generation_adapter, "run_clinical_archive_pipeline",
        lambda *a, **kw: archive_calls.append(kw) or a[0],
    )

    staging = _make_staging(tmp_path)
    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["面颊"]',  # has focal signal, but COMPOSITE wins via template
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
        template="tri-compare",
    )

    assert p1_calls == []
    assert p2_calls == []
    assert archive_calls == []


def test_template_other_with_focal_signal_uses_focal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Sanity: with template != tri-compare AND focus_targets, FOCAL still wins
    (priority order verifies M4 only fires on explicit composite template).
    """
    p2_calls = []
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", True)
    monkeypatch.setattr(render_queue, "should_promote", lambda case_id: True)
    monkeypatch.setattr(
        ai_generation_adapter, "run_comfyui_focal_enhance",
        lambda image_path, **kw: p2_calls.append(kw) or image_path,
    )

    staging = _make_staging(tmp_path)
    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["面颊"]',
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
        template="some-other-template",  # NOT in COMPOSITE_TEMPLATES
    )
    assert len(p2_calls) == 1


def test_archive_mode_calls_archive_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When tag indicates clinical_archive, M2 helper is called, not P1/P2."""
    p1_calls = []
    p2_calls = []
    archive_calls = []

    monkeypatch.setattr(
        ai_generation_adapter, "run_direct_clinical_enhancement",
        lambda *a, **kw: p1_calls.append(kw) or a[0],
    )
    monkeypatch.setattr(
        ai_generation_adapter, "run_comfyui_focal_enhance",
        lambda *a, **kw: p2_calls.append(kw) or a[0],
    )
    monkeypatch.setattr(
        ai_generation_adapter, "run_clinical_archive_pipeline",
        lambda image_path, **kw: archive_calls.append({"image": image_path.name, **kw}) or image_path,
    )

    staging = _make_staging(tmp_path)
    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["clinical_archive"]',  # explicit M2 routing
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
    )
    assert len(archive_calls) == 1
    assert archive_calls[0]["image"] == "术后-正面.jpg"
    assert p1_calls == []
    assert p2_calls == []


def test_rejected_mode_skips_all_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """REJECTED (e.g. fumei brand without any signals) → no helper call + warning."""
    p1_calls = []
    p2_calls = []
    archive_calls = []
    monkeypatch.setattr(ai_generation_adapter, "run_direct_clinical_enhancement",
                        lambda *a, **kw: p1_calls.append(kw) or a[0])
    monkeypatch.setattr(ai_generation_adapter, "run_comfyui_focal_enhance",
                        lambda *a, **kw: p2_calls.append(kw) or a[0])
    monkeypatch.setattr(ai_generation_adapter, "run_clinical_archive_pipeline",
                        lambda *a, **kw: archive_calls.append(kw) or a[0])

    staging = _make_staging(tmp_path)
    queue = render_queue.RenderQueue()
    import logging
    with caplog.at_level(logging.WARNING):
        queue._automate_md_ai_clinical_enhancements(
            render_case_dir=str(staging),
            brand="fumei",  # not in AI_ALLOWED_BRANDS, no tags, no template, no focus
            case_tags_json=None,
            manual_phase_lookup={},
            render_job_id=999,
            case_id=129,
        )

    assert p1_calls == p2_calls == archive_calls == []
    rejected_logs = [r for r in caplog.records if "REJECTED" in r.message]
    assert len(rejected_logs) >= 1
