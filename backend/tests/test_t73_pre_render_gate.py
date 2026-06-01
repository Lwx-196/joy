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
import json
import sqlite3
from pathlib import Path

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


def _seed_pre_render_case(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    case_id: int,
    reason_code: str = "crop_touches_frame",
    decision: str = "source_quality_review",
) -> int:
    case_dir = tmp_path / f"case-{case_id}"
    case_dir.mkdir()
    image_files = ["before-front.jpg", "after-front.jpg"]
    for filename in image_files:
        (case_dir / filename).write_bytes(b"\xff\xd8\xff\xd9")

    is_crop_case = reason_code == "crop_touches_frame"
    meta = {
        "image_files": image_files,
        "source_group_selection": {
            "ticket_decisions": [
                {
                    "decision": decision,
                    "reason_code": reason_code,
                    "slot": "front",
                    "evidence": {
                        "slot": "front",
                        "before": {"filename": image_files[0]},
                        "after": {"filename": image_files[1]},
                    },
                }
            ]
        },
    }
    skill_metadata = [
        {
            "filename": image_files[0],
            "phase": "before",
            "view_bucket": "front",
            "angle_confidence": 0.96,
            "mean_luma": 0.5,
            "same_person_similarity": 0.9 if is_crop_case else 0.4,
            "crop_touches_frame": is_crop_case,
            "face_crop_touches_frame": False,
            "crop_margin": 0.0 if is_crop_case else 0.2,
        },
        {
            "filename": image_files[1],
            "phase": "after",
            "view_bucket": "front",
            "angle_confidence": 0.95,
            "mean_luma": 0.52,
            "same_person_similarity": 0.9 if is_crop_case else 0.4,
            "crop_touches_frame": False,
            "face_crop_touches_frame": False,
            "crop_margin": 0.2,
        },
    ]
    now = "2026-05-31T00:00:00+00:00"
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO cases (
          id, scan_id, abs_path, category, meta_json, skill_image_metadata_json,
          tags_json, manual_blocking_issues_json, template_tier, last_modified, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            scan_id,
            str(case_dir),
            "standard_face",
            json.dumps(meta, ensure_ascii=False),
            json.dumps(skill_metadata, ensure_ascii=False),
            "[]",
            "[]",
            "standard",
            now,
            now,
        ),
    )
    return case_id


def _create_board_delivery_qa_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE board_delivery_qa (
            content_hash TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            case_id INTEGER,
            job_id INTEGER,
            verdict TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            assessed_at TEXT NOT NULL,
            PRIMARY KEY (content_hash, prompt_version)
        )
        """
    )


def _insert_d6_verdict(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    verdict: str,
    review_status: str = "pending",
    assessed_at: str = "2026-05-31T01:00:00+00:00",
) -> None:
    job_id = conn.execute(
        """
        INSERT INTO render_jobs (case_id, brand, template, status, enqueued_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, "test", "single-compare", "done", assessed_at, assessed_at),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO board_delivery_qa (
          content_hash, prompt_version, case_id, job_id, verdict, review_status, assessed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (f"hash-{case_id}", "test-v1", case_id, job_id, verdict, review_status, assessed_at),
    )


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

    src = Path("backend/services/pre_render_gate.py").read_text(encoding="utf-8")
    # 旧逻辑硬把 crop_touches_frame 排除在 source_quality_review ticket 之外
    assert not re.search(
        r"code not in \{[^}]*['\"]crop_touches_frame['\"][^}]*\}",
        src,
    ), "硬排除 crop_touches_frame 的代码仍在；P0.2-B 未真正解锁 warning path"


def test_unverified_crop_override_no_longer_blanket_passes_gate(temp_db, tmp_path: Path) -> None:
    """R1a V4: crop_touches_frame override without D6 clean must still block render."""
    from backend import db

    with db.connect() as conn:
        case_id = _seed_pre_render_case(conn, tmp_path, case_id=7301)
        _create_board_delivery_qa_table(conn)

        result = pre_render_gate.evaluate_pre_render_gate(
            case_id,
            template="single-compare",
            persist_tickets=False,
            conn=conn,
        )

    assert result["gate"]["passed"] is False
    assert any(t["reason_code"] == "crop_touches_frame" for t in result["tickets"])


def test_crop_override_with_d6_clean_still_passes_gate(temp_db, tmp_path: Path) -> None:
    """R1a V5: a downstream-clean crop override keeps the existing pass behavior."""
    from backend import db

    with db.connect() as conn:
        case_id = _seed_pre_render_case(conn, tmp_path, case_id=7302)
        _create_board_delivery_qa_table(conn)
        _insert_d6_verdict(conn, case_id=case_id, verdict="clean")

        result = pre_render_gate.evaluate_pre_render_gate(
            case_id,
            template="single-compare",
            persist_tickets=False,
            conn=conn,
        )

    assert result["gate"]["passed"] is True
    assert not any(t["reason_code"] == "crop_touches_frame" for t in result["tickets"])


def test_non_crop_override_behavior_is_unchanged_without_d6_table(temp_db, tmp_path: Path) -> None:
    """R1a V6: non-crop ticket decisions must not require any D6 lookup."""
    from backend import db

    with db.connect() as conn:
        case_id = _seed_pre_render_case(
            conn,
            tmp_path,
            case_id=7303,
            reason_code="identity_embedding_mismatch",
            decision="identity_review",
        )

        result = pre_render_gate.evaluate_pre_render_gate(
            case_id,
            template="single-compare",
            persist_tickets=False,
            conn=conn,
        )

    assert result["gate"]["passed"] is True
    assert not any(t["reason_code"] == "identity_embedding_mismatch" for t in result["tickets"])
