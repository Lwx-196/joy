"""Tests for render_executor._maybe_annotate_board seam（纯 backend venv，无需 cv2/numpy）.

这是「现有渲染管线 ↔ 标注子系统」的唯一集成接缝，承载 PR 核心安全契约：
  - 默认/关 → result 逐键不变（生产逐字节产物不受影响）
  - 开启 + 子系统抛异常 → 吞成 status=error，绝不向 run_render 传播
  - 开启 + 重 CV 依赖不在本解释器 → status=skipped（非 error）
  - 开启 + 成功 → 子系统返回的 dict 原样落 result['board_annotation']
用 monkeypatch 把 board_annotator 替成桩，脱离 numpy/cv2 即可在 CI 守护契约。
"""
from __future__ import annotations

import sys
import types

from backend import render_executor as rx


def _install_stub(monkeypatch, fn) -> None:
    """注入假 board_annotator（attr + sys.modules 双写，dev/CI 两 venv 都稳）。"""
    import backend.services as bsvc

    mod = types.ModuleType("backend.services.board_annotator")
    mod.annotate_render_output = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "backend.services.board_annotator", mod)
    monkeypatch.setattr(bsvc, "board_annotator", mod, raising=False)


def test_hook_off_leaves_result_untouched(monkeypatch):
    monkeypatch.delenv("CASE_WORKBENCH_ANNOTATE_BOARD", raising=False)
    result = {"final_board": "x", "status": "ok", "nested": {"a": 1}}
    snapshot = {"final_board": "x", "status": "ok", "nested": {"a": 1}}
    rx._maybe_annotate_board(rx.Path("/nonexistent"), result)
    assert result == snapshot
    assert "board_annotation" not in result


def test_hook_on_success_stores_dict_verbatim(monkeypatch):
    monkeypatch.setenv("CASE_WORKBENCH_ANNOTATE_BOARD", "1")
    payload = {"status": "ok", "annotated_path": "/x/final-board.annotated.jpg", "detail": [{"view": "front"}]}
    _install_stub(monkeypatch, lambda out_root: payload)
    result = {"status": "ok"}
    rx._maybe_annotate_board(rx.Path("/x"), result)
    assert result["board_annotation"] == payload


def test_hook_on_runtime_error_swallowed_as_error(monkeypatch):
    monkeypatch.setenv("CASE_WORKBENCH_ANNOTATE_BOARD", "1")

    def boom(out_root):
        raise RuntimeError("kaboom detail")

    _install_stub(monkeypatch, boom)
    result = {"status": "ok"}
    rx._maybe_annotate_board(rx.Path("/x"), result)  # 绝不 raise
    assert result["board_annotation"]["status"] == "error"
    assert "kaboom" in result["board_annotation"]["reason"]


def test_hook_on_missing_deps_skipped_not_error(monkeypatch):
    """重 CV 依赖不在 backend venv → import 抛 ImportError → status=skipped（非 error）。"""
    import backend.services as bsvc

    monkeypatch.setenv("CASE_WORKBENCH_ANNOTATE_BOARD", "1")
    # 双重保证 `from backend.services import board_annotator` 抛 ImportError：
    monkeypatch.delattr(bsvc, "board_annotator", raising=False)
    monkeypatch.setitem(sys.modules, "backend.services.board_annotator", None)
    result = {"status": "ok"}
    rx._maybe_annotate_board(rx.Path("/x"), result)  # 绝不 raise
    assert result["board_annotation"]["status"] == "skipped"
    assert "deps unavailable" in result["board_annotation"]["reason"]


def test_hook_env_truthy_and_falsy_variants(monkeypatch):
    payload = {"status": "ok"}
    _install_stub(monkeypatch, lambda out_root: payload)
    for val in ("1", "true", "TRUE", "yes", "Yes"):
        monkeypatch.setenv("CASE_WORKBENCH_ANNOTATE_BOARD", val)
        result: dict = {}
        rx._maybe_annotate_board(rx.Path("/x"), result)
        assert result.get("board_annotation") == payload, f"{val!r} should enable"
    for val in ("0", "", "no", "off"):
        monkeypatch.setenv("CASE_WORKBENCH_ANNOTATE_BOARD", val)
        result = {}
        rx._maybe_annotate_board(rx.Path("/x"), result)
        assert "board_annotation" not in result, f"{val!r} should stay off"
