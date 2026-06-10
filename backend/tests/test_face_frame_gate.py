"""Tests for the face frame truncation gate (出框源图实时 gate).

纯数学用例的几何构造说明（与标定公式一致）：
eye_d=100, face_h=250 → box_w = max(310, 312.5) = 312.5, box_h = max(295, 300) = 300。
"""
from __future__ import annotations

import pytest

from backend import face_frame_gate as ffg
from backend.face_frame_gate import (
    FACE_FRAME_TRUNCATION_THRESHOLD,
    evaluate_face_frame,
    protection_box_truncation,
)


@pytest.fixture(autouse=True)
def _reset_gate_state(monkeypatch):
    """每测试清空 (path, mtime) 缓存，避免跨测试串味。"""
    monkeypatch.setattr(ffg, "_VERDICT_CACHE", {})


def _face_info(eye_center, eye_distance=100.0, face_height=250.0):
    return {
        "eye_center": eye_center,
        "eye_distance": eye_distance,
        "face_height": face_height,
    }


class TestProtectionBoxTruncation:
    def test_centered_face_no_truncation(self):
        trunc, edges = protection_box_truncation(_face_info((500.0, 400.0)), (1000, 1000))
        assert trunc == 0.0
        assert edges == {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}

    def test_half_face_out_left(self):
        # eye_center 贴左边缘 → x1 = -box_w/2，左侧越界恰一半
        trunc, edges = protection_box_truncation(_face_info((0.0, 400.0)), (1000, 1000))
        assert trunc == pytest.approx(0.5)
        assert edges["left"] == pytest.approx(0.5)
        assert edges["right"] == 0.0

    def test_boundary_truncation_at_threshold(self):
        # 左越界 37.5px / box_w 312.5 = 0.12，恰为标定阈值
        eye_x = 312.5 / 2 - 37.5
        trunc, edges = protection_box_truncation(_face_info((eye_x, 400.0)), (1000, 1000))
        assert trunc == pytest.approx(FACE_FRAME_TRUNCATION_THRESHOLD)
        assert edges["left"] == pytest.approx(0.12)

    def test_clearly_below_threshold(self):
        # 左越界 15px / 312.5 = 0.048 < 0.12（好 case max 0.071 同档）
        eye_x = 312.5 / 2 - 15.0
        trunc, _ = protection_box_truncation(_face_info((eye_x, 400.0)), (1000, 1000))
        assert trunc == pytest.approx(0.048)
        assert trunc < FACE_FRAME_TRUNCATION_THRESHOLD

    def test_clearly_above_threshold(self):
        # 左越界 100px / 312.5 = 0.32（黄靖榕坏照 0.322 同档）
        eye_x = 312.5 / 2 - 100.0
        trunc, _ = protection_box_truncation(_face_info((eye_x, 400.0)), (1000, 1000))
        assert trunc == pytest.approx(0.32)
        assert trunc >= FACE_FRAME_TRUNCATION_THRESHOLD

    def test_top_overflow(self):
        # y1 = eye_y - 300*0.36 = eye_y - 108；eye_y=50 → 上越界 58px/300
        trunc, edges = protection_box_truncation(_face_info((500.0, 50.0)), (1000, 1000))
        assert edges["top"] == pytest.approx(58.0 / 300.0)
        assert edges["bottom"] == 0.0
        assert trunc == pytest.approx(58.0 / 300.0)

    def test_face_larger_than_image_overflows_all_edges(self):
        # 小图大脸：保护框 312.5x300 套在 200x200 图上
        trunc, edges = protection_box_truncation(_face_info((100.0, 100.0)), (200, 200))
        assert trunc > 0.5
        assert all(edges[edge] > 0.0 for edge in ("left", "top", "right", "bottom"))

    def test_degenerate_face_info_fails_open(self):
        trunc, edges = protection_box_truncation(
            _face_info((500.0, 400.0), eye_distance=0.0, face_height=0.0), (1000, 1000)
        )
        assert trunc == 0.0
        assert edges == {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}


class TestEvaluateFaceFrame:
    def test_missing_file_fails_open(self):
        verdict = evaluate_face_frame("/nonexistent/path/img.jpg")
        assert verdict["status"] == "unavailable"
        assert verdict["exceeded"] is False

    def test_face_align_unavailable_fails_open(self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"fake")
        monkeypatch.setattr(ffg, "_FA_MODULE", False)
        verdict = evaluate_face_frame(img)
        assert verdict["status"] == "unavailable"
        assert verdict["exceeded"] is False

    def test_no_face_fails_open(self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"fake")
        monkeypatch.setattr(ffg, "_load_face_align", lambda: object())
        monkeypatch.setattr(
            ffg, "_detect_face_geometry", lambda path, fa: (_ for _ in ()).throw(ValueError("no face"))
        )
        verdict = evaluate_face_frame(img)
        assert verdict["status"] == "no_face"
        assert verdict["exceeded"] is False

    def test_detection_error_fails_open(self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"fake")
        monkeypatch.setattr(ffg, "_load_face_align", lambda: object())
        monkeypatch.setattr(
            ffg, "_detect_face_geometry", lambda path, fa: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        verdict = evaluate_face_frame(img)
        assert verdict["status"] == "unavailable"
        assert verdict["exceeded"] is False

    def test_evaluated_above_threshold(self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"fake")
        # 左越界 100px / 312.5 = 0.32 ≥ 0.12
        face_info = {**_face_info((312.5 / 2 - 100.0, 400.0)), "size": (1000, 1000)}
        monkeypatch.setattr(ffg, "_load_face_align", lambda: object())
        monkeypatch.setattr(ffg, "_detect_face_geometry", lambda path, fa: face_info)
        verdict = evaluate_face_frame(img)
        assert verdict["status"] == "evaluated"
        assert verdict["truncation"] == pytest.approx(0.32)
        assert verdict["exceeded"] is True
        assert verdict["edge_overflow"]["left"] == pytest.approx(0.32)

    def test_evaluated_below_threshold(self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"fake")
        face_info = {**_face_info((500.0, 400.0)), "size": (1000, 1000)}
        monkeypatch.setattr(ffg, "_load_face_align", lambda: object())
        monkeypatch.setattr(ffg, "_detect_face_geometry", lambda path, fa: face_info)
        verdict = evaluate_face_frame(img)
        assert verdict["status"] == "evaluated"
        assert verdict["truncation"] == 0.0
        assert verdict["exceeded"] is False

    def test_verdict_cached_by_path_and_mtime(self, tmp_path, monkeypatch):
        img = tmp_path / "a.jpg"
        img.write_bytes(b"fake")
        calls = []

        def fake_evaluate(path):
            calls.append(path)
            return {"status": "evaluated", "truncation": 0.0, "edge_overflow": {}, "exceeded": False}

        monkeypatch.setattr(ffg, "_evaluate", fake_evaluate)
        evaluate_face_frame(img)
        evaluate_face_frame(img)
        assert len(calls) == 1
        # mtime 变化 → 缓存失效重测
        import os

        stat = img.stat()
        os.utime(img, (stat.st_atime, stat.st_mtime + 10))
        evaluate_face_frame(img)
        assert len(calls) == 2
