"""Tests for pose_backend — 可插拔姿态后端 + 影子模式路由（纯逻辑，重 CV 全 lazy，无需 numpy）。"""
from __future__ import annotations

import json
import logging

import pytest

from backend.services import pose_backend as pb


def _fake(name, yaw, has_face=True, **kw):
    return pb.PoseResult(has_face=has_face, yaw=yaw, source=name, **kw)


# --------------------------- mode flag 解析 ---------------------------

def test_mode_default_facemesh(monkeypatch):
    monkeypatch.delenv(pb.ENV_BACKEND, raising=False)
    assert pb.pose_backend_mode() == pb.MODE_FACEMESH


def test_mode_explicit_and_normalized(monkeypatch):
    for raw, exp in [("facemesh", pb.MODE_FACEMESH), ("sixdrep", pb.MODE_SIXDREP),
                     ("shadow", pb.MODE_SHADOW), ("SHADOW", pb.MODE_SHADOW),
                     ("  Sixdrep  ", pb.MODE_SIXDREP), ("hybrid", pb.MODE_HYBRID),
                     ("HYBRID", pb.MODE_HYBRID)]:
        monkeypatch.setenv(pb.ENV_BACKEND, raw)
        assert pb.pose_backend_mode() == exp


def test_mode_unknown_falls_back_facemesh(monkeypatch):
    monkeypatch.setenv(pb.ENV_BACKEND, "bogus-backend")
    assert pb.pose_backend_mode() == pb.MODE_FACEMESH


# --------------------------- PoseSession 路由（fake backends）---------------------------

class _FakeFM:
    def __init__(self, *a, **k):
        self.calls = 0

    def estimate(self, _img):
        self.calls += 1
        return _fake("facemesh", 40.0)          # 40° → classify_angle 判 oblique


class _FakeSD:
    def __init__(self, *a, **k):
        self.calls = 0

    def estimate(self, _img):
        self.calls += 1
        return _fake("sixdrep", 55.0, score=0.9, certain=True)   # 55° → profile


@pytest.fixture
def patched(monkeypatch):
    fm, sd = _FakeFM(), _FakeSD()
    monkeypatch.setattr(pb, "FaceMeshPoseBackend", lambda *a, **k: fm)
    monkeypatch.setattr(pb, "SixDRepPoseBackend", lambda *a, **k: sd)
    return fm, sd


def test_session_facemesh_mode_only_runs_facemesh(monkeypatch, patched):
    fm, sd = patched
    monkeypatch.setenv(pb.ENV_BACKEND, "facemesh")
    r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "facemesh" and r.yaw == 40.0
    assert sd.calls == 0                         # sixdrep 完全不跑


def test_session_sixdrep_mode_only_runs_sixdrep(monkeypatch, patched):
    fm, sd = patched
    monkeypatch.setenv(pb.ENV_BACKEND, "sixdrep")
    r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "sixdrep" and r.yaw == 55.0
    assert fm.calls == 0


def test_session_shadow_returns_facemesh_but_runs_both(monkeypatch, patched, caplog):
    fm, sd = patched
    monkeypatch.setenv(pb.ENV_BACKEND, "shadow")
    with caplog.at_level(logging.INFO, logger="pose_shadow_compare"):
        r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "facemesh"                # 影子模式生产值恒用 facemesh（字节不变）
    assert fm.calls == 1 and sd.calls == 1       # 两 backend 都跑
    rec = json.loads(next(m for m in reversed(caplog.messages) if m.startswith("{")))
    assert rec["facemesh"]["yaw"] == 40.0 and rec["sixdrep"]["yaw"] == 55.0
    assert rec["fm_view"] == "oblique" and rec["sd_view"] == "profile"
    assert rec["view_diff"] is True              # 侧脸纠偏信号：40°判斜 vs 55°判侧


def test_session_shadow_survives_sixdrep_failure(monkeypatch, caplog):
    fm = _FakeFM()

    class _Boom:
        def estimate(self, _img):
            raise RuntimeError("onnx model missing")

    monkeypatch.setattr(pb, "FaceMeshPoseBackend", lambda *a, **k: fm)
    monkeypatch.setattr(pb, "SixDRepPoseBackend", lambda *a, **k: _Boom())
    monkeypatch.setenv(pb.ENV_BACKEND, "shadow")
    with caplog.at_level(logging.WARNING, logger="pose_shadow_compare"):
        r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "facemesh" and r.yaw == 40.0   # facemesh 生产值不受 6D 失败影响
    assert any("shadow arm failed" in m for m in caplog.messages)


def test_session_unknown_flag_uses_facemesh(monkeypatch, patched):
    fm, sd = patched
    monkeypatch.setenv(pb.ENV_BACKEND, "bogus")
    r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "facemesh" and sd.calls == 0


# --------------------------- hybrid 模式（Phase 5：6D 高置信 → 6D，否则回退 facemesh）---------------------------

def test_session_hybrid_uses_sixdrep_when_face(monkeypatch, patched):
    fm, sd = patched                                  # _FakeSD: has_face=True yaw=55 profile
    monkeypatch.setenv(pb.ENV_BACKEND, "hybrid")
    r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "sixdrep" and r.yaw == 55.0    # 6D 高置信侧脸 → 用 6D 更准读数
    assert sd.calls == 1 and fm.calls == 0            # facemesh 不跑


def test_session_hybrid_falls_back_to_facemesh_when_gate_rejects(monkeypatch):
    fm = _FakeFM()                                    # facemesh: has_face=True yaw=40 oblique

    class _RejectSD:                                  # 门控拒绝：score<thr → has_face=False
        def __init__(self):
            self.calls = 0

        def estimate(self, _img):
            self.calls += 1
            return _fake("sixdrep", None, has_face=False, score=0.33)

    sd = _RejectSD()
    monkeypatch.setattr(pb, "FaceMeshPoseBackend", lambda *a, **k: fm)
    monkeypatch.setattr(pb, "SixDRepPoseBackend", lambda *a, **k: sd)
    monkeypatch.setenv(pb.ENV_BACKEND, "hybrid")
    r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "facemesh" and r.yaw == 40.0   # 门控误杀的真脸 → 回退 FaceMesh 现状
    assert sd.calls == 1 and fm.calls == 1            # 两臂都跑过（6D 先，回退 FaceMesh）


def test_session_hybrid_falls_back_on_sixdrep_exception(monkeypatch, caplog):
    fm = _FakeFM()

    class _Boom:
        def estimate(self, _img):
            raise RuntimeError("onnxruntime not installed")

    monkeypatch.setattr(pb, "FaceMeshPoseBackend", lambda *a, **k: fm)
    monkeypatch.setattr(pb, "SixDRepPoseBackend", lambda *a, **k: _Boom())
    monkeypatch.setenv(pb.ENV_BACKEND, "hybrid")
    with caplog.at_level(logging.WARNING, logger="pose_shadow_compare"):
        r = pb.PoseSession("m.task").estimate(object())
    assert r.source == "facemesh" and r.yaw == 40.0   # 6D 依赖缺失 → 回退 facemesh，不崩
    assert fm.calls == 1
    assert any("fallback facemesh" in m for m in caplog.messages)


# --------------------------- PoseResult 形状 ---------------------------

def test_pose_result_defaults():
    r = pb.PoseResult(has_face=True, yaw=30.0)
    assert r.pitch is None and r.roll is None
    assert r.certain is False and r.source == "" and r.score is None


def test_pose_result_no_face():
    r = pb.PoseResult(has_face=False, yaw=None, source="sixdrep", score=0.2)
    assert r.has_face is False and r.yaw is None and r.score == 0.2
