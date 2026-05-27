"""P0.2-B — pre_render_gate 消费 cases.meta_json.source_group_selection.accepted_warnings。

完整测试矩阵覆盖 plan §87 的 6 个用例。

匹配维度（与 P0.2-A source_selection warning 对齐）：
- slot 必匹配
- code 必匹配
- (selected_files 交集非空) OR (message_contains 是 ticket.message 的子串)
- accepted_warning 若未给 selected_files 也未给 message_contains，仅靠 slot+code 广义接受
"""
from __future__ import annotations

import inspect

from backend.services import pre_render_gate


def _ticket(*, code: str, slot: str, message: str = "", selected_files: list[str] | None = None,
            message_contains: str | None = None, blocks_render: bool = True) -> dict:
    evidence = {"slot": slot}
    if selected_files is not None or message_contains is not None:
        component = {"slot": slot}
        if selected_files is not None:
            component["selected_files"] = selected_files
        if message_contains is not None:
            component["message_contains"] = message_contains
        evidence["component"] = component
    return {
        "ticket_type": "source_quality_review",
        "reason_code": code,
        "slot": slot,
        "message": message,
        "blocks_render": blocks_render,
        "evidence": evidence,
    }


# ---------- Helper presence ----------


def test_apply_accepted_warnings_helper_exists() -> None:
    """P0.2-B 必须暴露 _apply_accepted_warnings helper（内部 API 也行，下游测试依赖）。"""
    assert hasattr(pre_render_gate, "_apply_accepted_warnings"), (
        "_apply_accepted_warnings helper missing — P0.2-B 未实施"
    )


def test_evaluate_pre_render_gate_accepts_accepted_warnings_kwarg() -> None:
    """P0.2-B 新增 accepted_warnings 入参，默认 None。"""
    sig = inspect.signature(pre_render_gate.evaluate_pre_render_gate)
    assert "accepted_warnings" in sig.parameters, "evaluate_pre_render_gate must accept accepted_warnings"
    assert sig.parameters["accepted_warnings"].default is None


# ---------- Matrix tests (plan §87) ----------


def test_accept_drops_ticket_when_slot_code_and_files_all_match() -> None:
    """场景 1: crop_touches_frame front + accepted（slot + code + selected_files 全匹配）→ 通过。"""
    tickets = [
        _ticket(code="crop_touches_frame", slot="front", selected_files=["a.jpg", "b.jpg"],
                message="正式正面图存在面部/主体裁切贴边"),
    ]
    accepted = [{"slot": "front", "code": "crop_touches_frame", "selected_files": ["a.jpg"]}]
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert out == []


def test_no_acceptance_keeps_ticket() -> None:
    """场景 2: crop_touches_frame front + 未 accept → 仍 block。"""
    tickets = [_ticket(code="crop_touches_frame", slot="front", selected_files=["a.jpg"])]
    out = pre_render_gate._apply_accepted_warnings(tickets, None)
    assert len(out) == 1
    assert out[0]["reason_code"] == "crop_touches_frame"


def test_slot_mismatch_keeps_ticket() -> None:
    """场景 3: accepted slot=side 但 warning slot=front → 仍 block。"""
    tickets = [_ticket(code="crop_touches_frame", slot="front", selected_files=["a.jpg"])]
    accepted = [{"slot": "side", "code": "crop_touches_frame", "selected_files": ["a.jpg"]}]
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert len(out) == 1


def test_selected_files_no_intersection_and_no_message_match_keeps_ticket() -> None:
    """场景 4: accepted 但 selected_files 不相交 且 message_contains 不匹配 → 仍 block。"""
    tickets = [_ticket(code="crop_touches_frame", slot="front",
                       selected_files=["a.jpg"], message="某种消息")]
    accepted = [{
        "slot": "front",
        "code": "crop_touches_frame",
        "selected_files": ["zz.jpg"],  # 不相交
        "message_contains": "完全不一样的字符串",  # 不匹配
    }]
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert len(out) == 1


def test_message_contains_substring_match_drops_ticket() -> None:
    """场景 5: accepted message_contains 是 ticket.message 子串 → 即便 files 不相交也通过。"""
    tickets = [_ticket(code="crop_touches_frame", slot="front",
                       selected_files=["x.jpg"], message="正式正面图存在面部/主体裁切贴边")]
    accepted = [{
        "slot": "front",
        "code": "crop_touches_frame",
        "selected_files": ["zz.jpg"],  # 不相交
        "message_contains": "裁切贴边",  # 子串 match
    }]
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert out == []


def test_partial_acceptance_filters_only_matched_tickets() -> None:
    """场景 6: 多 ticket 部分 accept → 只过滤被 accept 的，其他保留。"""
    tickets = [
        _ticket(code="crop_touches_frame", slot="front", selected_files=["a.jpg"]),
        _ticket(code="crop_touches_frame", slot="side", selected_files=["b.jpg"]),
        _ticket(code="identity_embedding_mismatch", slot="front", selected_files=["a.jpg"]),
    ]
    accepted = [{"slot": "front", "code": "crop_touches_frame", "selected_files": ["a.jpg"]}]
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert {(t["reason_code"], t["slot"]) for t in out} == {
        ("crop_touches_frame", "side"),
        ("identity_embedding_mismatch", "front"),
    }


def test_broad_acceptance_code_slot_only_drops_ticket() -> None:
    """edge: accepted 仅给 code+slot，无 selected_files/message_contains → 仍广义接受（slot+code 严匹配）。"""
    tickets = [_ticket(code="crop_touches_frame", slot="front", selected_files=["a.jpg"])]
    accepted = [{"slot": "front", "code": "crop_touches_frame"}]  # 无 files / no message_contains
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert out == []


def test_warning_path_ticket_carries_dimensions_via_evidence_warning() -> None:
    """Warning-source tickets put dims under evidence.warning (not evidence.component)."""
    ticket = {
        "ticket_type": "source_quality_review",
        "reason_code": "crop_touches_frame",
        "slot": "front",
        "message": "正式正面图存在面部/主体裁切贴边",
        "blocks_render": True,
        "evidence": {
            "slot": "front",
            "warning": {
                "code": "crop_touches_frame",
                "slot": "front",
                "selected_files": ["w.jpg"],
                "message_contains": "裁切贴边",
            },
        },
    }
    accepted = [{"slot": "front", "code": "crop_touches_frame", "selected_files": ["w.jpg"]}]
    out = pre_render_gate._apply_accepted_warnings([ticket], accepted)
    assert out == []


def test_hardcoded_exclusion_for_crop_touches_frame_removed() -> None:
    """P0.2-B 移除 line 286 对 crop_touches_frame 的硬排除（让 warning path 也能产 ticket）。"""
    import re
    from pathlib import Path

    src = Path("backend/services/pre_render_gate.py").read_text(encoding="utf-8")
    # 旧逻辑硬把 crop_touches_frame 排除在 source_quality_review ticket 之外
    assert not re.search(
        r"code not in \{[^}]*['\"]crop_touches_frame['\"][^}]*\}",
        src,
    ), "硬排除 crop_touches_frame 的代码仍在；P0.2-B 未真正解锁 warning path"
