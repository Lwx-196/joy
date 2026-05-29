"""Tests for board_annotator — 成品 board 内部 QA 标注（纯逻辑，mediapipe lazy）."""
from __future__ import annotations

import json

import numpy as np

from backend.services import board_annotator as ba


def _pts_at(cx: float, cy: float = 500.0) -> np.ndarray:
    """造一团围绕 (cx,cy) 的 landmark 点（478,2）用于 before/after 列判定。"""
    rng = np.linspace(-40, 40, 478)
    return np.stack([cx + rng, cy + rng[::-1]], axis=1).astype(np.float32)


def test_is_before_left_column():
    board_w = 2000
    assert ba._is_before(_pts_at(500), board_w) is True       # 左半 = 术前
    assert ba._is_before(_pts_at(1500), board_w) is False     # 右半 = 术后


def test_focus_from_manifest_prefers_targets():
    out_root = ba.Path("/x/y")
    assert ba._focus_from_manifest({"focus_targets": ["下颌线", "面颊"]}, out_root) == "下颌线 面颊"
    # 无 focus_targets → case_dir 目录名（含整段术式）
    m = {"focus_targets": [], "case_dir": "/a/b/2025.9.11保妥适下颌线、咬肌"}
    assert ba._focus_from_manifest(m, out_root) == "2025.9.11保妥适下颌线、咬肌"


def test_annotate_board_no_known_region_returns_original():
    # 术式抽不出已知部位 → 原图返回 + 空明细（不跑 facemesh，无需 model）
    board = np.zeros((100, 200, 3), np.uint8)
    out, detail = ba.annotate_board(board, "童颜针全脸提升", model_path="/nonexistent.task")
    assert detail == []
    assert out is board


def test_annotate_render_output_skips_when_no_board(tmp_path):
    res = ba.annotate_render_output(tmp_path)
    assert res["status"] == "skipped" and "final-board" in res["reason"]


def test_annotate_render_output_skips_when_no_model(tmp_path, monkeypatch):
    (tmp_path / "final-board.jpg").write_bytes(b"\xff\xd8\xff")  # 占位
    monkeypatch.delenv(ba.DEFAULT_MODEL_ENV, raising=False)
    monkeypatch.setattr(ba, "_DEV_MODEL_FALLBACK", "/definitely/missing.task")
    res = ba.annotate_render_output(tmp_path)
    assert res["status"] == "skipped" and "model" in res["reason"]


def test_resolve_model_path_prefers_explicit(tmp_path, monkeypatch):
    real = tmp_path / "m.task"
    real.write_bytes(b"x")
    assert ba.resolve_model_path(str(real)) == str(real)
    monkeypatch.setattr(ba, "_DEV_MODEL_FALLBACK", "/missing.task")
    monkeypatch.delenv(ba.DEFAULT_MODEL_ENV, raising=False)
    assert ba.resolve_model_path(None) is None


def test_manifest_focus_roundtrip_with_real_fixture_shape():
    # focus_targets 缺省 → 退 case_dir 名（真实 manifest 的常见形态）
    m = json.loads('{"focus_targets": [], "case_dir": "/p/q/隆鼻山根下巴"}')
    assert "隆鼻" in ba._focus_from_manifest(m, ba.Path("/p/q"))
