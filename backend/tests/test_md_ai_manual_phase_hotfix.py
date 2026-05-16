"""Regression tests for the MD-AI manual_phase hot-fix.

Bug being protected: ``_automate_md_ai_clinical_enhancements`` originally
identified ``after`` images purely by filename token matching (BEFORE_TOKENS /
AFTER_TOKENS in source_images), silently bypassing operator-corrected phase
labels stored in ``case_image_overrides.manual_phase`` (added in stage 22,
2026-05-01). On md_ai / meiji_ai brands this meant the manual override system
was dead code in the enhancement pipeline.

These tests pin three behaviours of the patched scanner:
  1. manual_phase_lookup ``after`` value triggers enhancement even when the
     filename carries no token (the operator-correction-wins path).
  2. manual_phase_lookup ``before`` value suppresses enhancement even when
     the filename contains an "after"/"术后" token (the operator-veto path).
  3. With no manual_phase_lookup the scanner falls back to the existing
     filename token rule, preserving the pre-patch behaviour for unlabelled
     cases (regression guard).

The final test is an end-to-end integration that drives the full
``RenderQueue._execute_render`` flow with a real sqlite DB and the real
staging-symlink machinery; it asserts the callsite correctly translates
``case_image_overrides.manual_phase`` (keyed by primary-case filename) into
``manual_phase_lookup`` (keyed by staging link-name).

We isolate the external AI subprocess via the same monkeypatch pattern used
elsewhere in the suite (see test_image_overrides_basic.py:1372 etc.) — this
patches the adapter boundary, not test data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import ai_generation_adapter, render_queue


def _make_staging(tmp_path: Path, filenames: list[str]) -> Path:
    staging = tmp_path / ".case-workbench-bound-render" / "job-1"
    staging.mkdir(parents=True)
    for name in filenames:
        (staging / name).write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal placeholder bytes
    return staging


@pytest.fixture
def capture_enhancement_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch the external AI subprocess and capture invocations."""
    calls: list[dict] = []

    def _fake(image_path, brand, focus_targets=None):
        calls.append({
            "name": Path(image_path).name,
            "brand": brand,
            "focus_targets": list(focus_targets or []),
        })
        return image_path  # signal "no enhanced output" so replace path is skipped

    monkeypatch.setattr(
        ai_generation_adapter,
        "run_direct_clinical_enhancement",
        _fake,
    )
    return calls


# --- Test 1: manual_phase 'after' wins over missing filename token ---------


def test_manual_phase_after_triggers_enhancement_for_untagged_filename(
    tmp_path: Path,
    capture_enhancement_calls: list[dict],
) -> None:
    staging = _make_staging(tmp_path, ["IMG_5678.jpg", "neutral.jpeg"])
    queue = render_queue.RenderQueue()

    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["苹果肌"]',
        manual_phase_lookup={"IMG_5678.jpg": "after"},
    )

    assert [c["name"] for c in capture_enhancement_calls] == ["IMG_5678.jpg"]
    assert capture_enhancement_calls[0]["brand"] == "md_ai"
    assert capture_enhancement_calls[0]["focus_targets"] == ["苹果肌"]


# --- Test 2: manual_phase 'before' vetoes filename token 'after' ----------


def test_manual_phase_before_suppresses_enhancement_for_after_token(
    tmp_path: Path,
    capture_enhancement_calls: list[dict],
) -> None:
    staging = _make_staging(tmp_path, ["术后-mislabeled.jpg", "after-ok.jpg"])
    queue = render_queue.RenderQueue()

    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["下颌线"]',
        manual_phase_lookup={"术后-mislabeled.jpg": "before"},
    )

    names = sorted(c["name"] for c in capture_enhancement_calls)
    assert names == ["after-ok.jpg"]


# --- Test 3: no manual override → fallback to filename token (regression) -


def test_filename_token_fallback_when_no_manual_override(
    tmp_path: Path,
    capture_enhancement_calls: list[dict],
) -> None:
    staging = _make_staging(
        tmp_path,
        ["术前-1.jpg", "术后-1.jpg", "after-2.jpg", "IMG_9999.jpg"],
    )
    queue = render_queue.RenderQueue()

    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="meiji_ai",
        case_tags_json='["苹果肌", "下颌线"]',
        manual_phase_lookup=None,
    )

    names = sorted(c["name"] for c in capture_enhancement_calls)
    assert names == ["after-2.jpg", "术后-1.jpg"]
    # focus_targets should reflect both tag matches, deduped
    assert capture_enhancement_calls[0]["focus_targets"] == ["苹果肌", "下颌线"]


# --- Test 4: brand guard — non-MD brand never invokes enhancement ---------


def test_non_md_brand_is_a_noop(
    tmp_path: Path,
    capture_enhancement_calls: list[dict],
) -> None:
    staging = _make_staging(tmp_path, ["术后-1.jpg", "after-2.jpg"])
    queue = render_queue.RenderQueue()

    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="fumei",
        case_tags_json='["苹果肌"]',
        manual_phase_lookup={"术后-1.jpg": "after"},
    )

    assert capture_enhancement_calls == []


# --- Test 5: empty manual_phase_lookup dict still falls back cleanly ------


def test_empty_manual_phase_lookup_falls_back_to_filename(
    tmp_path: Path,
    capture_enhancement_calls: list[dict],
) -> None:
    staging = _make_staging(tmp_path, ["术后-1.jpg"])
    queue = render_queue.RenderQueue()

    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json=None,
        manual_phase_lookup={},
    )

    assert [c["name"] for c in capture_enhancement_calls] == ["术后-1.jpg"]
    # No tags → no focus_targets
    assert capture_enhancement_calls[0]["focus_targets"] == []


# --- Observability: silent adapter swallow is logged loudly ---


def test_silent_adapter_swallow_emits_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The adapter's silent-failure return (enhanced_path == input) must log a warning.

    Without this guard, bulk runs of 74+ unrendered cases would have no
    audit trail when AI calls fail — the failure looks identical to a
    no-op success because run_direct_clinical_enhancement returns the
    same path in both cases.
    """
    import logging

    staging = _make_staging(tmp_path, ["术后-A.jpg"])
    queue = render_queue.RenderQueue()

    def _silent_swallow(image_path, brand, focus_targets=None):
        return image_path  # mimic adapter's swallow path

    import pytest as _pt  # alias to avoid shadowing
    monkeypatch = _pt.MonkeyPatch()
    try:
        monkeypatch.setattr(
            ai_generation_adapter,
            "run_direct_clinical_enhancement",
            _silent_swallow,
        )
        with caplog.at_level(logging.WARNING, logger="backend.render_queue"):
            queue._automate_md_ai_clinical_enhancements(
                render_case_dir=str(staging),
                brand="md_ai",
                case_tags_json=None,
                manual_phase_lookup=None,
            )
    finally:
        monkeypatch.undo()

    warnings = [
        rec.message for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "MD-AI enhancement returned original path" in rec.message
    ]
    assert len(warnings) == 1, (
        f"expected one 'returned original path' warning, got: "
        f"{[rec.message for rec in caplog.records]}"
    )
    assert "术后-A.jpg" in warnings[0]


def test_missing_enhanced_file_emits_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When adapter claims a non-original path that doesn't exist, log loudly."""
    import logging

    staging = _make_staging(tmp_path, ["术后-A.jpg"])
    queue = render_queue.RenderQueue()
    bogus_path = tmp_path / "nonexistent" / "vanished.jpg"

    def _broken_contract(image_path, brand, focus_targets=None):
        return bogus_path  # non-original but missing on disk

    import pytest as _pt
    monkeypatch = _pt.MonkeyPatch()
    try:
        monkeypatch.setattr(
            ai_generation_adapter,
            "run_direct_clinical_enhancement",
            _broken_contract,
        )
        with caplog.at_level(logging.WARNING, logger="backend.render_queue"):
            queue._automate_md_ai_clinical_enhancements(
                render_case_dir=str(staging),
                brand="md_ai",
                case_tags_json=None,
                manual_phase_lookup=None,
            )
    finally:
        monkeypatch.undo()

    warnings = [
        rec.message for rec in caplog.records
        if rec.levelno == logging.WARNING
        and "file is missing" in rec.message
    ]
    assert len(warnings) == 1
    assert "vanished.jpg" in warnings[0]


# --- Integration: full _execute_render flow translates manual_phase to staging ----


def test_execute_render_translates_manual_phase_into_staging_lookup(
    seed_case,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: _execute_render → callsite builds manual_phase_lookup → scanner fires.

    Scenario: md_ai case with two images
      - IMG_5678.jpg  (no phase token; operator marked manual_phase='after')
      - 术后-A.jpg   (filename token already 'after'; no override)
      - 术前-B.jpg   (filename token 'before'; should never be enhanced)

    Both 'after' images should be enhanced. The interesting one is IMG_5678
    — without the patch, it would be silently skipped.
    """
    from backend import db, render_executor

    case_dir = tmp_path / "case-md-ai"
    case_dir.mkdir()
    image_files = ["IMG_5678.jpg", "术后-A.jpg", "术前-B.jpg"]
    for name in image_files:
        (case_dir / name).write_bytes(b"\x89PNG\r\n\x1a\n")

    case_id = seed_case(abs_path=str(case_dir), customer_raw="客户A", category="A")

    now = datetime.now(timezone.utc).isoformat()
    case_meta = {"image_files": image_files}
    # Pre-fill skill_image_metadata so _classification_blocking_preflight passes
    # for ALL three files. We then rely on case_image_overrides only for the
    # ENHANCEMENT decision (the patched code path under test).
    #
    # IMG_5678.jpg's metadata says phase=after / view=front so preflight is happy;
    # but the *enhancement scanner* doesn't read skill_image_metadata — it reads
    # case_image_overrides.manual_phase (the patched path) and the filename
    # token. To prove the override path is what fires enhancement for IMG_5678,
    # we make sure its FILENAME has no token (it doesn't — "IMG_5678" matches
    # neither BEFORE_TOKENS nor AFTER_TOKENS).
    skill_metadata = [
        {
            "filename": "IMG_5678.jpg",
            "relative_path": "IMG_5678.jpg",
            "phase": "after",
            "view": "front",
            "view_bucket": "front",
            "angle_confidence": 0.96,
            "sharpness_score": 88,
            "pose": {"yaw": 0, "pitch": 0, "roll": 0},
            "identity_embedding": [0.5, 0.5, 0.5, 0.5],
            "brightness": 0.55,
            "crop_margin": 0.18,
        },
        {
            "filename": "术后-A.jpg",
            "relative_path": "术后-A.jpg",
            "phase": "after",
            "view": "oblique",
            "view_bucket": "oblique",
            "angle_confidence": 0.96,
            "sharpness_score": 88,
            "pose": {"yaw": 35, "pitch": 0, "roll": 0},
            "identity_embedding": [0.5, 0.5, 0.5, 0.5],
            "brightness": 0.55,
            "crop_margin": 0.18,
        },
        {
            "filename": "术前-B.jpg",
            "relative_path": "术前-B.jpg",
            "phase": "before",
            "view": "front",
            "view_bucket": "front",
            "angle_confidence": 0.96,
            "sharpness_score": 88,
            "pose": {"yaw": 0, "pitch": 0, "roll": 0},
            "identity_embedding": [0.5, 0.5, 0.5, 0.5],
            "brightness": 0.55,
            "crop_margin": 0.18,
        },
    ]
    with db.connect() as conn:
        conn.execute(
            """UPDATE cases SET meta_json = ?, tags_json = ?,
                                skill_image_metadata_json = ?
               WHERE id = ?""",
            (
                json.dumps(case_meta, ensure_ascii=False),
                json.dumps(["苹果肌"]),
                json.dumps(skill_metadata, ensure_ascii=False),
                case_id,
            ),
        )
        # Operator-corrected phase for unlabeled image — this is the patched path.
        # Without the patch, the enhancement scanner would ignore this row entirely.
        conn.execute(
            """
            INSERT INTO case_image_overrides
                (case_id, filename, manual_phase, manual_view, updated_at)
            VALUES (?, 'IMG_5678.jpg', 'after', 'front', ?)
            """,
            (case_id, now),
        )
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
                (case_id, brand, template, status, enqueued_at, semantic_judge)
            VALUES (?, 'md_ai', 'single-compare', 'queued', ?, 'off')
            """,
            (case_id, now),
        ).lastrowid

    # Capture enhancement calls
    enhancement_calls: list[dict] = []

    def _fake_enhance(image_path, brand, focus_targets=None):
        enhancement_calls.append({
            "path": str(image_path),
            "name": Path(image_path).name,
            "brand": brand,
            "focus_targets": list(focus_targets or []),
        })
        return image_path  # signal no replacement

    monkeypatch.setattr(
        ai_generation_adapter,
        "run_direct_clinical_enhancement",
        _fake_enhance,
    )

    # Stub render_executor.run_render — the heavy renderer isn't under test
    final_board = tmp_path / "final-board.jpg"
    manifest = tmp_path / "manifest.final.json"

    def _fake_run_render(case_dir_arg, **kwargs):
        # By the time run_render is invoked, _automate_md_ai_clinical_enhancements
        # has already run on the staging dir. Capture the staging path so the
        # assertions below can verify the manual_phase_lookup translation.
        staging_path_seen["dir"] = case_dir_arg
        final_board.write_bytes(b"fake")
        manifest.write_text(json.dumps({"groups": []}), encoding="utf-8")
        return {
            "output_path": str(final_board),
            "manifest_path": str(manifest),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "face",
            "effective_templates": ["single-compare"],
            "ai_usage": {},
            "blocking_issues": [],
            "warnings": [],
            "composition_alerts": [],
        }

    staging_path_seen: dict[str, str] = {}
    monkeypatch.setattr(render_executor, "run_render", _fake_run_render)
    monkeypatch.setattr(render_queue.render_executor, "run_render", _fake_run_render)

    # Drive the full pipeline
    render_queue.RenderQueue()._execute_render(int(job_id))

    # If the test fails, dump job state to make root cause obvious.
    if not enhancement_calls:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT status, error_message FROM render_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        pytest.fail(
            f"No enhancement calls fired. Job status={row['status']!r} "
            f"error={row['error_message']!r}. Staging seen={staging_path_seen}"
        )

    # Assertion 1: enhancement was invoked on both 'after' images, NOT on the before image
    enhanced_names = sorted(c["name"] for c in enhancement_calls)
    expected = {"IMG_5678.jpg", "术后-A.jpg"}

    # Names in staging are produced by _safe_link_name(case_id, case_path, filename).
    # That function preserves Chinese chars + alphanum + `._-` and rewrites
    # other chars. For our filenames (plain ASCII + 中文 + .jpg) the original
    # tokens survive intact inside the link name, so substring checks suffice.
    assert len(enhancement_calls) == 2, (
        f"expected 2 enhancement calls (after images only), got: {enhanced_names}"
    )
    seen_5678 = any("IMG_5678" in name for name in enhanced_names)
    seen_zhsh = any("术后-A" in name for name in enhanced_names)
    seen_before = any("术前-B" in name for name in enhanced_names)
    assert seen_5678, (
        "manual_phase='after' override did NOT cause IMG_5678.jpg enhancement"
        f" (calls: {enhanced_names})"
    )
    assert seen_zhsh, (
        "filename-token-based phase detection broke for 术后-A.jpg"
        f" (calls: {enhanced_names})"
    )
    assert not seen_before, (
        f"术前-B.jpg should never be enhanced (calls: {enhanced_names})"
    )

    # Assertion 2: brand and focus_targets propagated correctly
    for call in enhancement_calls:
        assert call["brand"] == "md_ai"
        assert call["focus_targets"] == ["苹果肌"]

    # Assertion 3: enhancement happened inside the staging dir (non-destructive)
    assert staging_path_seen.get("dir"), "run_render was not invoked"
    staging_dir = Path(staging_path_seen["dir"])
    assert ".case-workbench-bound-render" in str(staging_dir), (
        f"render_case_dir was not staging: {staging_dir}"
    )
    # And every enhancement call hit a file under that staging dir,
    # NOT the original case_dir (proves non-destructive behaviour)
    for call in enhancement_calls:
        assert str(staging_dir) in call["path"], (
            f"enhancement touched non-staging path: {call['path']}"
        )
        assert str(case_dir) not in call["path"] or str(staging_dir) in call["path"]

    # Assertion 4: job ended in 'done' status
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM render_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row["status"] == "done"
