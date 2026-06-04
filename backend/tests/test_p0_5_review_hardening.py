"""P0.5 review-fix hardening — H-1 + H-2 + interface C-1。

来自 4-subagent review：
- H-1 (nyquist): batch_records 空时 calibration_status="ok" 误报。
- H-2 (nyquist): traceback.format_exc() 无界 → DB 行膨胀。
- C-1 (interface-auditor): selected_files producer vs writer 文件名格式漂移。
"""
from __future__ import annotations

import json
from pathlib import Path


# ---------- H-1: empty batch_records → insufficient_data + fail-closed ----------


class _AllFailProvider:
    """每次 call_vision_batch 都返回 exception；模拟全批次失败。"""

    name = "all-fail"
    model = "all-fail"

    def call_vision_batch(self, requests, *, concurrency: int = 1, return_exceptions: bool = False):
        return [ValueError("simulated total failure") for _ in requests]


def _write_tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _seed_obs(conn, *, case_id: int, root_path: str, image_path: str) -> int:
    now = "2026-05-27T00:00:00+00:00"
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO cases (id, scan_id, abs_path, category, last_modified, indexed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (case_id, scan_id, root_path, "standard_face", now, now),
    )
    group_id = conn.execute(
        """
        INSERT INTO case_groups (
          group_key, primary_case_id, title, root_path, case_ids_json,
          status, diagnosis_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{root_path}-{image_path}",
            case_id,
            "unit",
            root_path,
            json.dumps([case_id]),
            "needs_review",
            "{}",
            now,
            now,
        ),
    ).lastrowid
    return int(
        conn.execute(
            """
            INSERT INTO image_observations (
              group_id, case_id, image_path, phase, body_part, view,
              quality_json, confidence, source, reasons_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (group_id, case_id, image_path, "unknown", "face", "unknown", "{}", 0.25,
             "rules", "[]", now, now),
        ).lastrowid
    )


def test_empty_batch_records_marks_insufficient_data(temp_db, tmp_path: Path) -> None:
    """H-1: 全批次失败 → batch_records 空 → calibration_status='insufficient_data'
    + apply 强制降级 live-no-apply（不误报 ok）。"""
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    for i in range(3):
        _write_tiny_png(tmp_path / f"p{i}.png")
    with db.connect() as conn:
        for i in range(3):
            _seed_obs(conn, case_id=126, root_path=str(tmp_path), image_path=f"p{i}.png")
        report = run_classification(
            conn,
            provider=_AllFailProvider(),
            case_id=126,
            mode="apply",
        )

    assert report["calibration_status"] == "insufficient_data", (
        f"全失败批次必须标 insufficient_data 不能是 'ok'；实测 {report['calibration_status']}"
    )
    assert report["fail_closed"] is True, "全失败批次 + apply mode → 必须 fail_closed"
    assert report["mode"] == "live-no-apply"
    assert report["error_count"] == 3


# ---------- H-2: traceback 截 16KB ----------


def test_traceback_truncated_to_16kb_in_classifier_error_json(temp_db, tmp_path: Path) -> None:
    """H-2: 大 traceback (>16KB) → 写入 vlm_usage_log.error_json 前应被截断。"""
    from backend.services.vlm_source_classifier import (
        TRACEBACK_MAX_CHARS,
        _truncate_tb,
    )

    huge = "X" * (TRACEBACK_MAX_CHARS * 3)
    truncated = _truncate_tb(huge)
    assert len(truncated) <= TRACEBACK_MAX_CHARS + 100, (
        f"截断后必须 ≤ {TRACEBACK_MAX_CHARS}+padding；实测 {len(truncated)}"
    )
    assert "[truncated" in truncated, "截断标记必须出现，便于排查时识别"

    small = "x" * 100
    assert _truncate_tb(small) == small, "短 traceback 不应被改动"


def test_traceback_truncation_constant_exists() -> None:
    """H-2: TRACEBACK_MAX_CHARS 必须在模块层暴露，便于运维调参。"""
    from backend.services import vlm_source_classifier

    assert hasattr(vlm_source_classifier, "TRACEBACK_MAX_CHARS")
    assert vlm_source_classifier.TRACEBACK_MAX_CHARS >= 4096
    assert vlm_source_classifier.TRACEBACK_MAX_CHARS <= 65536


# ---------- interface C-1: selected_files basename canonicalization ----------


def test_accepted_warnings_match_via_basename_normalization() -> None:
    """C-1: accepted_files=['front/术后正面.jpg']（writer 格式） + ticket selected_files=
    ['术后正面.jpg']（producer 格式）→ basename 归一化后必须匹配。"""
    from backend.services import pre_render_gate

    ticket = {
        "ticket_type": "source_quality_review",
        "reason_code": "crop_touches_frame",
        "slot": "front",
        "message": "正式正面图存在面部/主体裁切贴边",
        "blocks_render": True,
        "evidence": {
            "slot": "front",
            "component": {
                "slot": "front",
                "selected_files": ["术后正面.jpg"],  # basename only
                "message_contains": "裁切贴边",
            },
        },
    }
    # Writer 端往往携带 group_relative_path 前缀
    accepted = [{
        "slot": "front",
        "code": "crop_touches_frame",
        "selected_files": ["front/术后正面.jpg"],  # has prefix
    }]
    out = pre_render_gate._apply_accepted_warnings([ticket], accepted)
    assert out == [], (
        "basename 归一化前两侧不相交，归一化后必须匹配 → ticket 被过滤为空"
    )


def test_basename_canonicalization_preserves_unique_files() -> None:
    """C-1 edge: 同名文件 / 多 path 段都应正确归一化为 basename。"""
    from backend.services.pre_render_gate import _basename

    assert _basename("front/术后正面.jpg") == "术后正面.jpg"
    assert _basename("术后正面.jpg") == "术后正面.jpg"
    assert _basename("/abs/path/to/a.png") == "a.png"
    assert _basename("") == ""
    assert _basename(None) == ""  # type: ignore[arg-type]
