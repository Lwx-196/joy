"""Tests for case_material_coverage — soft angle classify + degrade-not-reject routing (pure)."""
from __future__ import annotations

from backend.services import case_material_coverage as cov


def pv(view, yaw, certain, has_face=True, path="p.jpg"):
    return cov.PhotoView(path=path, has_face=has_face, yaw=yaw, view=view, certain=certain)


def test_classify_angle_soft_bands():
    assert cov.classify_angle(1, True) == ("front", True)
    assert cov.classify_angle(15, True) == ("oblique", False)    # front↔oblique 模糊
    assert cov.classify_angle(33, True) == ("oblique", True)
    assert cov.classify_angle(55, True) == ("profile", False)    # oblique↔profile 模糊
    assert cov.classify_angle(75, True) == ("profile", True)
    # 不硬分：检不到脸 → unknown + 不确定（不强判侧面）
    assert cov.classify_angle(None, False) == (cov.VIEW_UNKNOWN, False)


def test_covered_when_certain_required_view_present():
    # 下颌线 需 oblique/profile；有确定 oblique → covered
    photos = [pv("front", 1, True), pv("oblique", 33, True, path="ob.jpg")]
    rc = cov.route_region("下颌线", photos)
    assert rc.status == cov.STATUS_COVERED
    assert rc.chosen.path == "ob.jpg"


def test_uncertain_boundary_degrades_not_rejects():
    # 只有不确定 oblique（边界）→ degraded（仍可用），不 missing
    rc = cov.route_region("下颌线", [pv("oblique", 50, False)])
    assert rc.status == cov.STATUS_DEGRADED
    assert rc.chosen is not None


def test_wrong_angle_degrades_to_best_available():
    # 下颌线 需 oblique/profile，但只有确定正面 → 不丢弃，降级用现有脸
    rc = cov.route_region("下颌线", [pv("front", 2, True)])
    assert rc.status == cov.STATUS_DEGRADED
    assert rc.chosen is not None


def test_noface_profile_region_degrades_to_2d():
    # 鼻尖 需 front/profile；只有 no-face（侧面无 landmark）→ 走 2D 降级
    rc = cov.route_region("鼻尖", [pv(cov.VIEW_UNKNOWN, None, False, has_face=False)])
    assert rc.status == cov.STATUS_DEGRADED
    assert "2D" in rc.note


def test_missing_only_when_no_usable_material():
    # 泪沟 只需 front，且只有 no-face 照（profile 不在需求）→ 真 missing
    rc = cov.route_region("泪沟", [pv(cov.VIEW_UNKNOWN, None, False, has_face=False)])
    assert rc.status == cov.STATUS_MISSING


def test_best_for_view_picks_closest_angle():
    # oblique 取最接近 33°
    photos = [pv("oblique", 20, True, path="a.jpg"), pv("oblique", 34, True, path="b.jpg")]
    rc = cov.route_region("面颊", photos)   # 面颊 oblique-first
    assert rc.chosen.path == "b.jpg"


def test_analyze_multi_region():
    photos = [pv("front", 1, True), pv("oblique", 33, True)]
    cc = cov.analyze("玻尿酸注射面颊，下巴", photos)
    regions = {r.region: r.status for r in cc.regions}
    assert regions["面颊"] == cov.STATUS_COVERED      # oblique 确定
    assert regions["下巴"] == cov.STATUS_COVERED      # 下巴 front-first，有确定 front
