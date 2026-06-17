"""B1+B2 gate 误报修复单测（2026-06-16，owner 拍板）。

B1：identity 缺 embedding 证据假阴（status=not_verified 且无具体 mismatch code）→ blocks_render=False
    （仍 blocks_publish=True 不自动发布）；真 mismatch 保持 block。
B2：sharpness 0.0/<=0 视为计算失败 flaky 不假拦；真低清 0<x<=8 仍拦。
证据：5-case 探针坐实 identity ticket 全是 status='not_verified'/code=None=缺 ArcFace embedding 假阴。
"""

from backend.services.pre_render_gate import _component_ticket, _pair_tickets


# ---------------------------------------------------------------- B1 identity


def test_identity_missing_evidence_downgraded_to_warn() -> None:
    """假阴（not_verified 无 code）→ 不拦出图但仍不自动发布。"""
    t = _component_ticket(
        view="front",
        component_name="identity",
        component={"status": "not_verified"},
        before={},
        after={},
    )
    assert t is not None
    assert t["blocks_render"] is False  # B1 降 warn
    assert t["blocks_publish"] is True  # 仍须人工身份确认才发布
    assert t["reason_code"] == "identity_not_verified"
    assert t["evidence"]["missing_evidence"] is True


def test_identity_real_mismatch_still_blocks() -> None:
    """真 mismatch（带具体 code）保持 block。"""
    t = _component_ticket(
        view="front",
        component_name="identity",
        component={"status": "block", "code": "identity_embedding_mismatch"},
        before={},
        after={},
    )
    assert t is not None
    assert t["blocks_render"] is True
    assert t["reason_code"] == "identity_embedding_mismatch"
    assert t["evidence"]["missing_evidence"] is False


def test_identity_block_status_without_code_still_blocks() -> None:
    """status=block 但非 not_verified（无 code）→ 仍 block（只有 not_verified 假阴才降）。"""
    t = _component_ticket(
        view="oblique",
        component_name="identity",
        component={"status": "block"},
        before={},
        after={},
    )
    assert t is not None
    assert t["blocks_render"] is True


def test_identity_ok_no_ticket() -> None:
    t = _component_ticket(
        view="side",
        component_name="identity",
        component={"status": "ok"},
        before={},
        after={},
    )
    assert t is None


# ---------------------------------------------------------------- B2 sharpness


def _plan_with_sharpness(before_sharp, after_sharp=200.0):
    return {
        "slots": {
            "front": {
                "before": {"filename": "b.jpg", "sharpness_score": before_sharp},
                "after": {"filename": "a.jpg", "sharpness_score": after_sharp},
                "pair_quality": {"metrics": {"primary_judge": {}}},
            }
        }
    }


def _blur_tickets(plan):
    return [t for t in _pair_tickets(plan) if t["reason_code"] == "blur_or_low_sharpness"]


def test_sharpness_zero_is_flaky_not_blocked() -> None:
    """0.0 视为计算失败 flaky，不产 blur ticket。"""
    assert _blur_tickets(_plan_with_sharpness(0.0)) == []


def test_sharpness_negative_not_blocked() -> None:
    assert _blur_tickets(_plan_with_sharpness(-1.0)) == []


def test_sharpness_low_still_blocks() -> None:
    """真低清 0<x<=8 仍拦。"""
    tickets = _blur_tickets(_plan_with_sharpness(5.0))
    assert len(tickets) == 1
    assert tickets[0]["blocks_render"] is True


def test_sharpness_sharp_no_ticket() -> None:
    assert _blur_tickets(_plan_with_sharpness(50.0)) == []
