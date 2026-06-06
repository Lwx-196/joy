"""Unit tests for the AI-enhanced board render integration (third render option).

Covers the file-system / dispatch logic without touching the real gemini/skill path:
  - render_executor.run_ai_enhanced_render — subprocess dispatch, AI_BOARD_RESULT
    parsing, board placement at the standard final-board.jpg, error paths.
  - render_executor._parse_ai_board_result / _ai_enhance_cli_python.
  - render_queue._parse_job_options — pulling enhance_direction/model from job meta_json.

The real gemini + skill render is exercised by a separate live end-to-end smoke.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from backend import render_executor, render_queue


# ----------------------------------------------------------------------
# _parse_ai_board_result
# ----------------------------------------------------------------------


def test_parse_ai_board_result_extracts_path():
    stdout = "chatter line\n  AI_BOARD_RESULT: /tmp/out/board.jpg  \ntrailing"
    assert render_executor._parse_ai_board_result(stdout) == "/tmp/out/board.jpg"


def test_parse_ai_board_result_none_when_absent():
    assert render_executor._parse_ai_board_result("no marker here\nfoo") is None


# ----------------------------------------------------------------------
# _ai_enhance_cli_python
# ----------------------------------------------------------------------


def test_ai_enhance_cli_python_env_override(monkeypatch):
    monkeypatch.setenv("CASE_WORKBENCH_AI_ENHANCE_PYTHON", "/custom/python")
    assert render_executor._ai_enhance_cli_python() == "/custom/python"


# ----------------------------------------------------------------------
# render_queue._parse_job_options
# ----------------------------------------------------------------------


def test_parse_job_options_pulls_enhance_fields():
    meta = json.dumps(
        {"options": {"enhance_direction": "heal", "enhance_model": "gemini-3-pro-image-preview"}}
    )
    opts = render_queue._parse_job_options(meta)
    assert opts["enhance_direction"] == "heal"
    assert opts["enhance_model"] == "gemini-3-pro-image-preview"


def test_parse_job_options_empty_when_no_options():
    assert render_queue._parse_job_options(json.dumps({"draft_preview": True})) == {}
    assert render_queue._parse_job_options(None) == {}
    assert render_queue._parse_job_options("not json at all") == {}


# ----------------------------------------------------------------------
# run_ai_enhanced_render (mocked subprocess)
# ----------------------------------------------------------------------


def _fake_proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_run_ai_enhanced_render_places_board_at_final(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "produced_board.jpg"
    produced.write_bytes(b"enhanced-board-bytes")
    out_root = tmp_path / "outroot"

    def fake_sub(args, timeout):
        # CLI invocation carries the expected flags.
        assert "--case-dir" in args and "--native-enhance" in args
        assert "--enhance-direction" in args and "heal" in args
        assert "--enhance-model" in args and "gemini-3-pro-image-preview" in args
        return _fake_proc(f"chatter\nAI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        enhance_direction="heal",
        enhance_model="gemini-3-pro-image-preview",
    )

    assert result["status"] == "done"
    assert result["case_mode"] == "ai_enhanced_board"
    assert result["enhance"] == {"direction": "heal", "model": "gemini-3-pro-image-preview"}
    final = out_root / "final-board.jpg"
    assert result["output_path"] == str(final)
    assert final.read_bytes() == b"enhanced-board-bytes"


def test_run_ai_enhanced_render_raises_on_nonzero(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(
        render_executor, "_run_render_subprocess", lambda a, t: _fake_proc("boom", returncode=1)
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")
    with pytest.raises(RuntimeError, match="ai-enhance subprocess exit=1"):
        render_executor.run_ai_enhanced_render(case_dir)


def test_run_ai_enhanced_render_raises_when_no_marker(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(
        render_executor, "_run_render_subprocess", lambda a, t: _fake_proc("finished but no marker")
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")
    with pytest.raises(RuntimeError, match="no board"):
        render_executor.run_ai_enhanced_render(case_dir)


def test_run_ai_enhanced_render_missing_case_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        render_executor.run_ai_enhanced_render(tmp_path / "does-not-exist")
