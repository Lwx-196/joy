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

from backend import render_executor, render_queue, render_quality


# ----------------------------------------------------------------------
# _parse_ai_board_result
# ----------------------------------------------------------------------


def test_parse_ai_board_result_extracts_path():
    stdout = "chatter line\n  AI_BOARD_RESULT: /tmp/out/board.jpg  \ntrailing"
    assert render_executor._parse_ai_board_result(stdout) == "/tmp/out/board.jpg"


def test_parse_ai_board_result_none_when_absent():
    assert render_executor._parse_ai_board_result("no marker here\nfoo") is None


# ----------------------------------------------------------------------
# _parse_ai_board_held (WP2 aligned-render-pipeline)
# ----------------------------------------------------------------------


def test_parse_ai_board_held_pair_with_board():
    stdout = (
        "chatter\n"
        '  AI_BOARD_HELD: {"gate": "pair", "reason": "front eye_ratio=1.5", "board": "/tmp/o/b.jpg"}  \n'
        "trailing"
    )
    held = render_executor._parse_ai_board_held(stdout)
    assert held == {"gate": "pair", "reason": "front eye_ratio=1.5", "board": "/tmp/o/b.jpg"}


def test_parse_ai_board_held_angle_board_none():
    stdout = 'AI_BOARD_HELD: {"gate": "angle", "reason": "印堂需正面|斜侧", "board": null}'
    held = render_executor._parse_ai_board_held(stdout)
    assert held["gate"] == "angle"
    assert held["board"] is None
    # reason 含 '|' 字符也不影响（JSON 编码，非分隔符方案）
    assert "|" in held["reason"]


def test_parse_ai_board_held_none_when_absent():
    assert render_executor._parse_ai_board_held("no marker\nfoo") is None


def test_parse_ai_board_held_none_when_malformed_json():
    assert render_executor._parse_ai_board_held("AI_BOARD_HELD: {not json}") is None


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

    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        # WP1 (aligned-render-pipeline): 接通 non-native 增强路。
        assert "--case-dir" in args
        assert "--enhance-direction" in args and "heal" in args
        assert "--enhance-model" in args and "gemini-3-pro-image-preview" in args
        # 去 native 局部重绘 + 去 no-cache（复用全局内容寻址 cache）。
        assert "--native-enhance" not in args
        assert "--no-cache" not in args
        # 前端 per-click 不烧 judge 钱。
        assert "--no-board-qa" in args
        captured["extra_env"] = extra_env
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
    # 默认透传自适应 4K（跟 4K 批量对齐），子进程 env 干净不继承父进程。
    assert captured["extra_env"]["CASE_WORKBENCH_ADAPTIVE_4K"] == "1"


def test_run_ai_enhanced_render_forwards_adaptive_4k_env_override(tmp_path, monkeypatch):
    """server env CASE_WORKBENCH_ADAPTIVE_4K=0 → 透传 0（运行时开关，owner 可翻档）。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "b.jpg"
    produced.write_bytes(b"x")
    monkeypatch.setenv("CASE_WORKBENCH_ADAPTIVE_4K", "0")
    captured = {}

    def fake_sub(args, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return _fake_proc(f"AI_BOARD_RESULT: {produced}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "o")
    render_executor.run_ai_enhanced_render(case_dir)
    assert captured["extra_env"]["CASE_WORKBENCH_ADAPTIVE_4K"] == "0"


def test_run_ai_enhanced_render_raises_on_nonzero(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(
        render_executor, "_run_render_subprocess", lambda a, t, extra_env=None: _fake_proc("boom", returncode=1)
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")
    with pytest.raises(RuntimeError, match="ai-enhance subprocess exit=1"):
        render_executor.run_ai_enhanced_render(case_dir)


def test_run_ai_enhanced_render_raises_when_no_marker(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    monkeypatch.setattr(
        render_executor, "_run_render_subprocess", lambda a, t, extra_env=None: _fake_proc("finished but no marker")
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: tmp_path / "outroot")
    with pytest.raises(RuntimeError, match="no board"):
        render_executor.run_ai_enhanced_render(case_dir)


def test_run_ai_enhanced_render_missing_case_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        render_executor.run_ai_enhanced_render(tmp_path / "does-not-exist")


# ----------------------------------------------------------------------
# run_ai_enhanced_render HELD → status="blocked" (WP2: gate HELD ≠ 渲染失败)
# ----------------------------------------------------------------------


def test_run_ai_enhanced_render_pair_held_blocks_with_diagnostic_board(tmp_path, monkeypatch):
    """G2 配对门 HELD：保留诊断板 → status='blocked' 不抛错，诊断板落 final-board。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    diag = tmp_path / "diag_board.jpg"
    diag.write_bytes(b"diagnostic-board-bytes")
    out_root = tmp_path / "outroot"

    held_line = json.dumps(
        {"gate": "pair", "reason": "front eye_ratio=1.46（允许 [0.78, 1.3]）", "board": str(diag)},
        ensure_ascii=False,
    )

    def fake_sub(args, timeout, extra_env=None):
        return _fake_proc(f"chatter\nAI_BOARD_HELD: {held_line}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)

    assert result["status"] == "blocked"
    assert result["held_gate"] == "pair"
    assert "eye_ratio" in result["held_reason"]
    assert result["render_error"] == result["held_reason"]
    assert result["blocking_issue_count"] == 1
    final = out_root / "final-board.jpg"
    assert result["output_path"] == str(final)
    assert final.read_bytes() == b"diagnostic-board-bytes"


def test_run_ai_enhanced_render_angle_held_blocks_without_board(tmp_path, monkeypatch):
    """G1 角度门 HELD：出板前短路无诊断板 → status='blocked'，output_path=None。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    out_root = tmp_path / "outroot"

    held_line = json.dumps(
        {"gate": "angle", "reason": "印堂需正面|斜侧（板上实有 ['front']）", "board": None},
        ensure_ascii=False,
    )

    def fake_sub(args, timeout, extra_env=None):
        return _fake_proc(f"AI_BOARD_HELD: {held_line}\n")

    monkeypatch.setattr(render_executor, "_run_render_subprocess", fake_sub)
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)

    assert result["status"] == "blocked"
    assert result["held_gate"] == "angle"
    assert result["output_path"] is None
    assert result["blocking_issue_count"] == 1
    # G1 短路无板 → 不创建 final-board
    assert not (out_root / "final-board.jpg").exists()


def test_evaluate_render_result_held_maps_to_blocked_even_with_board(tmp_path):
    """render_quality：held_gate 存在 → quality_status='blocked'，即使诊断板 output 存在（G2）。"""
    board = tmp_path / "final-board.jpg"
    board.write_bytes(b"x")
    quality = render_quality.evaluate_render_result(
        {
            "status": "blocked",
            "output_path": str(board),
            "blocking_issue_count": 1,
            "held_gate": "pair",
            "held_reason": "front eye_ratio=1.46",
            "render_error": "front eye_ratio=1.46",
        }
    )
    assert quality["quality_status"] == "blocked"
    assert quality["can_publish"] is False
    assert quality["metrics"]["held_gate"] == "pair"
    assert quality["metrics"]["held_reason"] == "front eye_ratio=1.46"


def test_evaluate_render_result_done_when_no_held(tmp_path):
    """无 held_gate + 干净出板 → 仍走原 done 逻辑（不回归）。"""
    board = tmp_path / "final-board.jpg"
    board.write_bytes(b"x")
    quality = render_quality.evaluate_render_result(
        {"status": "ok", "output_path": str(board), "blocking_issue_count": 0}
    )
    assert quality["quality_status"] in {"done", "done_with_issues"}
    assert quality["metrics"]["held_gate"] == ""


def test_run_ai_enhanced_render_board_result_wins_over_absent_held(tmp_path, monkeypatch):
    """成功路（有 AI_BOARD_RESULT）仍 status='done'，不被 HELD 逻辑干扰。"""
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    produced = tmp_path / "ok.jpg"
    produced.write_bytes(b"ok-bytes")
    out_root = tmp_path / "outroot"

    monkeypatch.setattr(
        render_executor,
        "_run_render_subprocess",
        lambda a, t, extra_env=None: _fake_proc(f"AI_BOARD_RESULT: {produced}\n"),
    )
    monkeypatch.setattr(render_executor.stress, "render_output_root", lambda *a, **k: out_root)

    result = render_executor.run_ai_enhanced_render(case_dir)
    assert result["status"] == "done"
    assert "held_gate" not in result


# ----------------------------------------------------------------------
# enqueue() defaults enhance_direction="heal" for render_mode="ai"
# ----------------------------------------------------------------------


def _enqueue_options(options: dict | None, render_mode: str = "ai") -> dict:
    """Simulate enqueue()'s options defaulting without touching the DB."""
    if render_mode == "ai":
        resolved = dict(options or {})
        resolved.setdefault("enhance_direction", "heal")
        return resolved
    return dict(options or {})


def test_enqueue_defaults_heal_when_no_options():
    assert _enqueue_options(None, render_mode="ai")["enhance_direction"] == "heal"


def test_enqueue_defaults_heal_when_options_omit_direction():
    opts = _enqueue_options({"enhance_model": "gemini"}, render_mode="ai")
    assert opts["enhance_direction"] == "heal"
    assert opts["enhance_model"] == "gemini"


def test_enqueue_best_pair_does_not_default_heal():
    assert _enqueue_options(None, render_mode="best-pair").get("enhance_direction") is None


def test_enqueue_explicit_direction_preserved():
    assert _enqueue_options({"enhance_direction": "sharpen"})["enhance_direction"] == "sharpen"


def test_enqueue_explicit_empty_direction_preserved():
    assert _enqueue_options({"enhance_direction": ""})["enhance_direction"] == ""
