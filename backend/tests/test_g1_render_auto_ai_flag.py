"""G1.A.i Regression — render flow ComfyUI inline enhance swap (env flag controlled).

User goal G1.A.i (from 2026-05-28 design doc):
  Add `run_comfyui_inline_enhance(image_path, ...)` helper to ai_generation_adapter
  that calls ComfyUI HTTP API (via _run_comfyui_workflow) without watermark/qa_scores/
  audit overhead. Wire it into _automate_md_ai_clinical_enhancements behind two-tier
  gate: env flag RENDER_AUTO_AI_ENABLED + should_promote(case_id).

These tests pin G1.A.i contract:
  H-1: helper unit — happy path returns Path from _run_comfyui_workflow
  H-2: helper unit — _run_comfyui_workflow raises → return input image_path (silent)
  H-3: helper unit — no generated_path in run_result → return input image_path
  H-4: helper unit — LOGGER.warning emitted on fail
  S-1: swap — flag false (default) → calls run_direct_clinical_enhancement (BC)
  S-2: swap — flag true + should_promote=True → calls run_comfyui_inline_enhance
  S-3: swap — flag true + should_promote=False → falls back to run_direct_clinical_enhancement
  S-4: swap — flag true + ComfyUI exception → render done + LOGGER.warning (audit-only fail)
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from backend import ai_generation_adapter, render_queue


# --- Helper unit tests (run_comfyui_inline_enhance) ----------------------------


def test_h1_helper_happy_path_returns_stable_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-1: _run_comfyui_workflow returns generated_path → helper returns a stable Path
    with the same bytes (K-1 hardening: copies out of soon-to-be-rmtree'd tempdir).
    """
    image_path = tmp_path / "after.jpg"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    # _run_comfyui_workflow writes its output INSIDE output_dir (real production behavior)
    enhanced_bytes = b"\x89PNG\r\n\x1a\nGEN"

    def _fake_workflow(input_path, *, output_dir, workflow_name, **kwargs):
        # Mirror real production: write generated.png inside output_dir
        generated = Path(output_dir) / "comfyui-generated.png"
        generated.write_bytes(enhanced_bytes)
        return {
            "generated_path": str(generated),
            "prompt_id": "test-prompt-1",
            "workflow_name": workflow_name,
        }

    monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _fake_workflow)

    result = ai_generation_adapter.run_comfyui_inline_enhance(
        image_path,
        focus_targets=["面部"],
        brand="md_ai",
        case_id=129,
    )

    assert result != image_path, "helper should return a different path on success"
    assert result.exists(), f"stable copy {result} should exist after K-1 cleanup"
    assert result.read_bytes() == enhanced_bytes, "bytes should match generated"
    # K-1: tempdir cleaned up (no .comfyui-inline-* dirs left under image_path.parent)
    residue = [d for d in image_path.parent.iterdir() if d.name.startswith(".comfyui-inline-")]
    assert residue == [], f"K-1 violation: tempdir leaked: {residue}"


def test_h2_helper_workflow_raises_returns_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """H-2: _run_comfyui_workflow raises → helper returns input image_path (silent)."""
    image_path = tmp_path / "after.jpg"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    def _raising(input_path, *, output_dir, workflow_name, **kwargs):
        raise RuntimeError("ComfyUI MPS allocation failed")

    monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _raising)

    with caplog.at_level(logging.WARNING):
        result = ai_generation_adapter.run_comfyui_inline_enhance(
            image_path,
            focus_targets=["面部"],
            brand="md_ai",
            case_id=129,
        )

    assert result == image_path
    assert any("inline enhance failed" in rec.message.lower() for rec in caplog.records)


def test_h3_helper_no_generated_path_returns_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """H-3: run_result missing generated_path → helper returns input image_path."""
    image_path = tmp_path / "after.jpg"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    def _no_output(input_path, *, output_dir, workflow_name, **kwargs):
        return {"prompt_id": "test", "workflow_name": workflow_name}  # missing generated_path

    monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _no_output)

    with caplog.at_level(logging.WARNING):
        result = ai_generation_adapter.run_comfyui_inline_enhance(
            image_path,
            focus_targets=["面部"],
            brand="md_ai",
            case_id=129,
        )

    assert result == image_path
    assert any("no generated_path" in rec.message.lower() for rec in caplog.records)


def test_h4_helper_generated_path_missing_file_returns_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-4: generated_path string returned but file doesn't exist → return input."""
    image_path = tmp_path / "after.jpg"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    def _phantom_output(input_path, *, output_dir, workflow_name, **kwargs):
        return {"generated_path": str(tmp_path / "does-not-exist.png")}

    monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _phantom_output)

    result = ai_generation_adapter.run_comfyui_inline_enhance(
        image_path,
        focus_targets=["面部"],
        brand="md_ai",
        case_id=129,
    )

    assert result == image_path
    # K-1: tempdir cleaned up even on no-op fail path
    residue = [d for d in image_path.parent.iterdir() if d.name.startswith(".comfyui-inline-")]
    assert residue == [], f"K-1 violation: tempdir leaked on fail path: {residue}"


# --- Swap integration tests (_automate_md_ai_clinical_enhancements) -----------


def _make_staging(tmp_path: Path, filenames: list[str]) -> Path:
    staging = tmp_path / ".case-workbench-bound-render" / "job-1"
    staging.mkdir(parents=True)
    for name in filenames:
        (staging / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    return staging


@pytest.fixture
def capture_p1_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture run_direct_clinical_enhancement (P1 Node.js PS path) invocations."""
    calls: list[dict] = []

    def _fake_p1(image_path, brand, focus_targets=None):
        calls.append({"path": "p1", "name": Path(image_path).name, "brand": brand})
        return image_path  # signal no-op

    monkeypatch.setattr(ai_generation_adapter, "run_direct_clinical_enhancement", _fake_p1)
    return calls


@pytest.fixture
def capture_p2_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture run_comfyui_inline_enhance (P2 ComfyUI inline path) invocations."""
    calls: list[dict] = []

    def _fake_p2(image_path, *, focus_targets=None, brand="fumei", case_id=None, **kwargs):
        calls.append({
            "path": "p2",
            "name": Path(image_path).name,
            "brand": brand,
            "case_id": case_id,
        })
        return image_path  # signal no-op

    monkeypatch.setattr(ai_generation_adapter, "run_comfyui_inline_enhance", _fake_p2)
    return calls


def test_s1_flag_false_uses_p1_node_js_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_p1_calls: list[dict],
    capture_p2_calls: list[dict],
) -> None:
    """S-1: flag default false → calls P1 (run_direct_clinical_enhancement), not P2."""
    monkeypatch.delenv("RENDER_AUTO_AI_ENABLED", raising=False)
    # Reload the module-level constant via patch on render_queue
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", False)

    staging = _make_staging(tmp_path, ["术后-正面.jpg"])
    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["面部"]',
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
    )

    assert len(capture_p1_calls) == 1
    assert capture_p1_calls[0]["path"] == "p1"
    assert len(capture_p2_calls) == 0


def test_s2_flag_true_promoted_uses_p2_comfyui_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_p1_calls: list[dict],
    capture_p2_calls: list[dict],
) -> None:
    """S-2: flag true + should_promote=True → calls P2 (run_comfyui_inline_enhance)."""
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", True)
    monkeypatch.setattr(render_queue, "should_promote", lambda case_id: True)

    staging = _make_staging(tmp_path, ["术后-正面.jpg"])
    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["面部"]',
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
    )

    assert len(capture_p2_calls) == 1, f"expected P2 call, got {capture_p1_calls=} {capture_p2_calls=}"
    assert capture_p2_calls[0]["path"] == "p2"
    assert capture_p2_calls[0]["case_id"] == 129
    assert capture_p2_calls[0]["brand"] == "md_ai"
    assert len(capture_p1_calls) == 0


def test_s3_flag_true_shadow_falls_back_to_p1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_p1_calls: list[dict],
    capture_p2_calls: list[dict],
) -> None:
    """S-3: flag true + should_promote=False (shadow) → fall back to P1 (BC)."""
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", True)
    monkeypatch.setattr(render_queue, "should_promote", lambda case_id: False)  # shadow

    staging = _make_staging(tmp_path, ["术后-正面.jpg"])
    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["面部"]',
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
    )

    assert len(capture_p1_calls) == 1
    assert len(capture_p2_calls) == 0


def test_s4_flag_true_p2_raises_render_continues_audit_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_p1_calls: list[dict],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S-4: flag true + P2 wrapper raises (defense-in-depth) → render continues + LOGGER.warning + file unchanged.

    NOTE: helper has its own internal silent-fail (run_comfyui_inline_enhance catches Exception
    and returns input path). This test deliberately mocks helper to RAISE — verifying the
    render_queue caller layer's defense-in-depth try/except (line 1690-1700 fallback).
    See S-5 for the real-production silent-swallow path (helper internal catch).
    """
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", True)
    monkeypatch.setattr(render_queue, "should_promote", lambda case_id: True)

    def _p2_raise(image_path, **kwargs):
        raise ConnectionError("ComfyUI 127.0.0.1:8188 not reachable")

    monkeypatch.setattr(ai_generation_adapter, "run_comfyui_inline_enhance", _p2_raise)

    staging = _make_staging(tmp_path, ["术后-正面.jpg"])
    entry = staging / "术后-正面.jpg"
    original_bytes = entry.read_bytes()

    queue = render_queue.RenderQueue()
    with caplog.at_level(logging.WARNING):
        # Should NOT raise — fail-safe contract
        queue._automate_md_ai_clinical_enhancements(
            render_case_dir=str(staging),
            brand="md_ai",
            case_tags_json='["面部"]',
            manual_phase_lookup={},
            render_job_id=999,
            case_id=129,
        )

    # File unchanged (no replacement happened)
    assert entry.read_bytes() == original_bytes
    # K-4 hardening: tighten assertion to "raised" or "ConnectionError"
    # (previously was OR("comfyui","enhancement") which collides with no-op warning)
    assert any(
        "raised" in rec.message.lower() or "connectionerror" in rec.message.lower()
        for rec in caplog.records
    ), f"expected 'raised' or 'ConnectionError' in warnings: {[r.message for r in caplog.records]}"


def test_s5_flag_true_helper_internal_silent_fail_real_production_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S-5 (K-3 hardening): mocks _run_comfyui_workflow (not the helper) — verifies the
    actual silent-fail path that production hits when ComfyUI service is down.

    Real-production fail mode:
      ComfyUI 127.0.0.1:8188 down → _run_comfyui_workflow raises ConnectionError
      → helper internal `except Exception` catches → returns input image_path
      → render_queue sees enhanced_path == entry → logs "silent adapter swallow" warning
      → render continues (md_ai brand layout uses original staging after image)

    This is THE path real production failures take. S-4 covers the wrapper-layer
    defense-in-depth (helper itself raises) which doesn't happen in production
    because the helper internally catches all Exception (silent-fail contract).
    """
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", True)
    monkeypatch.setattr(render_queue, "should_promote", lambda case_id: True)

    def _workflow_raises(input_path, *, output_dir, workflow_name, **kwargs):
        raise ConnectionError("ComfyUI 127.0.0.1:8188 connection refused")

    # Mock the lower-level _run_comfyui_workflow so helper's internal try/except runs.
    monkeypatch.setattr(ai_generation_adapter, "_run_comfyui_workflow", _workflow_raises)

    staging = _make_staging(tmp_path, ["术后-正面.jpg"])
    entry = staging / "术后-正面.jpg"
    original_bytes = entry.read_bytes()

    queue = render_queue.RenderQueue()
    with caplog.at_level(logging.WARNING):
        queue._automate_md_ai_clinical_enhancements(
            render_case_dir=str(staging),
            brand="md_ai",
            case_tags_json='["面部"]',
            manual_phase_lookup={},
            render_job_id=999,
            case_id=129,
        )

    # File unchanged — helper returned image_path (silent fail), so enhanced_path == entry
    # → render_queue takes "silent adapter swallow" branch, no os.replace called.
    assert entry.read_bytes() == original_bytes
    # Helper's own warning "inline enhance failed for ... ConnectionError" should fire.
    helper_warnings = [r for r in caplog.records if "inline enhance failed" in r.message.lower()]
    assert len(helper_warnings) == 1, (
        f"expected helper to log 'inline enhance failed' for ConnectionError, "
        f"got: {[r.message for r in caplog.records]}"
    )
    # Also assert tempdir was cleaned (K-1 hardening)
    residue = [d for d in staging.iterdir() if d.name.startswith(".comfyui-inline-")]
    assert residue == [], f"K-1 violation: tempdir not cleaned up: {residue}"


def test_s6_iterdir_skips_dotfile_and_enhanced_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_p1_calls: list[dict],
) -> None:
    """S-6 (K-2 hardening): residue files from prior crashed renders are not double-enhanced.

    Simulates a crash recovery scenario where staging dir contains:
      - .comfyui-inline-<ts>-<pid>/ — tempdir residue from earlier crash (dir, ignored by is_file)
      - .comfyui-out-<ts>-<pid>-术后-正面.jpg.png — dotfile residue (skipped by name)
      - enhanced_术后-正面.jpg — copy artifact from before os.replace crash (skipped)
      - 术后-正面.jpg — legitimate after image (enhanced exactly once)
    """
    monkeypatch.setattr(render_queue, "_RENDER_AUTO_AI_ENABLED", False)  # P1 BC path

    staging = tmp_path / ".case-workbench-bound-render" / "job-1"
    staging.mkdir(parents=True)
    # legitimate after image
    legit = staging / "术后-正面.jpg"
    legit.write_bytes(b"\x89PNG\r\n\x1a\n")
    # residue (3 forms)
    (staging / ".comfyui-out-stale.jpg.png").write_bytes(b"\x89PNG\r\n\x1a\n.residue")
    (staging / "enhanced_术后-正面.jpg").write_bytes(b"\x89PNG\r\n\x1a\nenh-residue")
    (staging / ".comfyui-inline-stale").mkdir()  # tempdir residue (dir, skipped by is_file)

    queue = render_queue.RenderQueue()
    queue._automate_md_ai_clinical_enhancements(
        render_case_dir=str(staging),
        brand="md_ai",
        case_tags_json='["面部"]',
        manual_phase_lookup={},
        render_job_id=999,
        case_id=129,
    )

    # Only legitimate after image was enhanced; residue files were skipped.
    enhanced_names = sorted(c["name"] for c in capture_p1_calls)
    assert enhanced_names == ["术后-正面.jpg"], (
        f"K-2 violation: residue files were double-enhanced: {enhanced_names}"
    )
