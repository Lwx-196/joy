"""face_h-anchor（DCS-gated）单测：脸大小配对修复。

背景（2026-06-14 信号可行性探针三证 + STEP1 DCS pose-invariant CV2-3%）：
side/oblique 近侧位下 eye_distance/face_height/protection_box 全被前缩腐蚀，
保护框锚把真实相等的头渲成 1.125（郭璟琳 side blocker）。修复 = side/oblique 改用
pixel face_height 做 after-follows-before 配平锚，DCS（=face_h×|t_z|/w，pose-invariant）
做 gate 确认真实头≈等大才拉等（不掩盖真实差异）。front / DCS缺失 / 真差 → 回退保护框。
"""
import numpy as np

from scripts.render_brand_clean import (
    _FACESIZE_DCS_GATE,
    _depth_corrected_size,
    compute_facesize_match,
)


def _face(face_h, width, tz, with_matrix=True):
    f = {"face_height": face_h, "size": (width, 1.0)}
    if with_matrix:
        m = np.eye(4)
        m[2][3] = tz  # 平移 z（深度），_depth_corrected_size 取 |m[2][3]|
        f["transform_matrix"] = m
    return f


# ---- _depth_corrected_size ----

def test_dcs_basic():
    # DCS = face_h × |tz| / width
    assert _depth_corrected_size(_face(2000, 4000, -33)) == 2000 * 33 / 4000


def test_dcs_pose_invariant_same_person():
    # 同一人不同 pose：近(小 fh，小|tz|) 与 远(大 fh? 不) —— 关键是 DCS 抵消深度近似相等。
    near = _depth_corrected_size(_face(2400, 4000, -27.5))
    far = _depth_corrected_size(_face(2000, 4000, -33.0))
    assert abs(near - far) / far < 0.02  # 跨 pose <2%（与实测 CV 同量级）


def test_dcs_missing_matrix_returns_none():
    assert _depth_corrected_size(_face(2000, 4000, -33, with_matrix=False)) is None


def test_dcs_bad_fields_return_none():
    assert _depth_corrected_size({"face_height": 0, "size": (4000, 1), "transform_matrix": np.eye(4)}) is None
    assert _depth_corrected_size({"face_height": 2000, "size": (0, 1), "transform_matrix": np.eye(4)}) is None
    assert _depth_corrected_size({}) is None  # 异常 fail-open


# ---- compute_facesize_match ----

def test_front_keeps_protection_box():
    # front 不进 side/oblique 分支 → 保护框高度原样（零回归）
    bm, am, dbg = compute_facesize_match(_face(2000, 4000, -33), _face(2400, 4000, -27.5),
                                         "front", 1500.0, 1700.0)
    assert (bm, am) == (1500.0, 1700.0)
    assert dbg["anchor"] == "protection_box"


def test_side_real_equal_uses_face_height():
    # side + DCS≈相等（同人）→ face_height 锚
    before = _face(2000, 4000, -33.0)
    after = _face(2400, 4000, -27.5)  # DCS 比 ≈1.0
    bm, am, dbg = compute_facesize_match(before, after, "side", 1500.0, 1700.0)
    assert (bm, am) == (2000.0, 2400.0)  # 返回 face_height 而非保护框
    assert dbg["anchor"] == "face_height"
    assert abs(dbg["dcs_ratio"] - 1.0) <= _FACESIZE_DCS_GATE


def test_oblique_real_equal_uses_face_height():
    bm, am, dbg = compute_facesize_match(_face(1850, 3600, -45), _face(2062, 3600, -40.4),
                                         "oblique", 1400.0, 1550.0)
    assert dbg["anchor"] == "face_height"
    assert (bm, am) == (1850.0, 2062.0)


def test_side_real_size_diff_falls_back_to_protection_box():
    # DCS 比超 gate（真实头真差 / 错人）→ 不拉等，回退保护框 + 留诊断
    before = _face(2000, 4000, -33.0)   # DCS 16.5
    after = _face(2400, 4000, -18.0)    # DCS 10.8 → 比 0.654 < 0.80 超 0.20 gate
    bm, am, dbg = compute_facesize_match(before, after, "side", 1500.0, 1700.0)
    assert (bm, am) == (1500.0, 1700.0)
    assert dbg["anchor"] == "protection_box"
    assert dbg["gate"] == "dcs_real_size_diff"


def test_side_missing_matrix_fails_open_to_protection_box():
    # DCS 不可得（fallback face）→ fail-open 保护框（零风险回退）
    before = _face(2000, 4000, -33.0, with_matrix=False)
    after = _face(2400, 4000, -27.5, with_matrix=False)
    bm, am, dbg = compute_facesize_match(before, after, "side", 1500.0, 1700.0)
    assert (bm, am) == (1500.0, 1700.0)
    assert dbg["anchor"] == "protection_box"


def test_side_missing_face_height_falls_back():
    before = {"size": (4000, 1), "transform_matrix": np.eye(4)}  # 无 face_height
    after = _face(2400, 4000, -27.5)
    bm, am, dbg = compute_facesize_match(before, after, "side", 1500.0, 1700.0)
    assert (bm, am) == (1500.0, 1700.0)
    assert dbg["anchor"] == "protection_box"
