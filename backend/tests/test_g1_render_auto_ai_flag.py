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


def test_h1_helper_happy_path_returns_generated_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H-1: _run_comfyui_workflow returns generated_path → helper returns that Path."""
    image_path = tmp_path / "after.jpg"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
    generated = tmp_path / "enhanced.png"
    generated.write_bytes(b"\x89PNG\r\n\x1a\nGEN")

    def _fake_workflow(input_path, *, output_dir, workflow_name, **kwargs):
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

    assert result == generated
    assert result.exists()


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
    """S-4: flag true + P2 raises (e.g. ComfyUI down) → render continues, LOGGER.warning, file unchanged.

    Per user G1.A decision: 'render done + ComfyUI fail audit only'.
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
    # Warning logged (either by enhancer itself or by render_queue wrap)
    assert any("comfyui" in rec.message.lower() or "enhancement" in rec.message.lower()
               for rec in caplog.records), f"no warning logged: {[r.message for r in caplog.records]}"
