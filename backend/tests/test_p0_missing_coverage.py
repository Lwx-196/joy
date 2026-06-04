"""P0 follow-up: 5 missing coverage tests (eval-auditor 建议).

集中在已存在 P0 实施代码的边界 / 防御路径上：
1. classify_batch concurrent per-image failure (concurrency=3 半失败)
2. audit_json 字段是 malformed JSON 时端点不崩
3. detect_distribution_collapse 在 ratio == 0.90 / p50 == 0.90 边界（含等号）
4. fail_closed_reason 未被设置时 report 的防御形状（None / missing key）
5. pre_render_gate._apply_accepted_warnings 空数组 / 全非 dict 列表 防御
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.services import (
    pre_render_gate,
    vlm_calibration,
    vlm_source_classifier,
)


# ---------------------------------------------------------------------------
# Tiny PNG fixture
# ---------------------------------------------------------------------------


def _tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _seed_observation(
    conn,
    *,
    case_id: int,
    root_path: str,
    image_path: str,
    phase: str = "unknown",
    view: str = "unknown",
) -> int:
    now = "2026-05-27T00:00:00+00:00"
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO cases (id, scan_id, abs_path, category, last_modified, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
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
            (group_id, case_id, image_path, phase, "face", view, "{}", 0.25,
             "rules", "[]", now, now),
        ).lastrowid
    )


# ---------------------------------------------------------------------------
# 1. classify_batch concurrent per-image failure
# ---------------------------------------------------------------------------


class _ConcurrentMixedProvider:
    """Concurrency=3 下让部分 path 抛错，部分成功；用 lock 保证 stub 行为可预测。"""

    name = "concurrent-mixed"
    model = "concurrent-model"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def call_vision(self, prompt, images, *, timeout=30.0, max_dimension=None, purpose=None):
        from backend.services.vlm_provider import VLMResponse

        path = str(images[0]) if images else ""
        with self._lock:
            self.calls.append(path)
        if "fail" in path:
            raise RuntimeError(f"simulated provider failure for {Path(path).name}")
        parsed = {
            "phase": "after",
            "view": "front",
            "body_part": "face",
            "confidence": 0.95,
            "reasoning": "stub",
        }
        return VLMResponse(
            text=json.dumps(parsed),
            parsed=parsed,
            provider="concurrent-mixed",
            model="concurrent-model",
            latency_ms=5,
            input_tokens=5,
            output_tokens=5,
            usage_raw={},
        )

    def call_vision_batch(self, items, *, concurrency=3, return_exceptions=False):
        # Reuse the real ThreadPool-backed implementation from the base provider class
        # by mimicking its return_exceptions semantics. We don't actually need
        # ThreadPool here for correctness — sequential is enough to exercise the
        # per-item exception collection path.
        out = []
        for item in items:
            try:
                out.append(self.call_vision(item.prompt, item.images, timeout=item.timeout,
                                            purpose=getattr(item, "purpose", None)))
            except BaseException as exc:  # noqa: BLE001
                if not return_exceptions:
                    raise
                out.append(exc)
        return out


def test_classify_batch_concurrent_per_image_failure(tmp_path: Path) -> None:
    """classify_batch(concurrency=3, return_exceptions=True) — 半失败时按位置返回
    Exception 占位，不让单点失败拖垮整批。"""
    good_a = tmp_path / "good_a.png"
    bad = tmp_path / "fail_b.png"
    good_c = tmp_path / "good_c.png"
    for p in (good_a, bad, good_c):
        _tiny_png(p)

    provider = _ConcurrentMixedProvider()
    results = vlm_source_classifier.classify_batch(
        [good_a, bad, good_c],
        provider,
        concurrency=3,
        return_exceptions=True,
    )

    assert len(results) == 3, "结果数必须等于输入数（按位置占位）"
    assert not isinstance(results[0], BaseException), "good_a 应成功"
    assert isinstance(results[1], BaseException), "fail_b 应返回 Exception"
    assert isinstance(results[1], RuntimeError)
    assert "fail_b" in str(results[1])
    assert not isinstance(results[2], BaseException), "good_c 应成功"
    # 成功项必须是 ClassificationResult
    assert results[0].phase == "after"
    assert results[2].phase == "after"


def test_classify_batch_concurrent_default_raises_without_return_exceptions(
    tmp_path: Path,
) -> None:
    """return_exceptions=False（默认）时单点失败立刻向上抛，不静默吞掉。"""
    good = tmp_path / "good.png"
    bad = tmp_path / "fail.png"
    _tiny_png(good)
    _tiny_png(bad)

    provider = _ConcurrentMixedProvider()
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        vlm_source_classifier.classify_batch(
            [good, bad],
            provider,
            concurrency=2,
            return_exceptions=False,
        )


# ---------------------------------------------------------------------------
# 2. audit_json malformed JSON — endpoint defense
# ---------------------------------------------------------------------------


def _seed_simulation_job_raw(
    conn,
    *,
    status: str,
    audit_json_raw: str,
    error_message: str | None = "boom",
    created_at: datetime | None = None,
) -> int:
    when = (created_at or datetime.now(timezone.utc)).isoformat()
    cur = conn.execute(
        """
        INSERT INTO simulation_jobs (
          status, focus_targets_json, policy_json, model_plan_json,
          input_refs_json, output_refs_json, watermarked, audit_json,
          error_message, can_publish, created_at, updated_at
        )
        VALUES (?, '[]', '{}', '{}', '[]', '[]', 0, ?, ?, 0, ?, ?)
        """,
        (status, audit_json_raw, error_message, when, when),
    )
    return int(cur.lastrowid)


def test_failures_recent_tolerates_malformed_audit_json(client, temp_db: Path) -> None:
    """audit_json 损坏（非合法 JSON）时端点不应 500，而应把那条记到 unknown 桶。"""
    from backend import db

    with db.connect() as conn:
        # 1 条合法 + 1 条损坏 + 1 条空字符串
        _seed_simulation_job_raw(
            conn,
            status="failed",
            audit_json_raw=json.dumps({"failure": {"failure_stage": "provider_call"}}),
        )
        _seed_simulation_job_raw(
            conn,
            status="failed",
            audit_json_raw="{not valid json at all",
        )
        _seed_simulation_job_raw(
            conn,
            status="failed",
            audit_json_raw="",
        )

    resp = client.get("/api/render/jobs/failures/recent?days=7&group_by=stage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_failed"] == 3
    by_stage = {row["key"]: row["count"] for row in body["groups"]}
    assert by_stage.get("provider_call") == 1
    assert by_stage.get("unknown") == 2, (
        f"malformed/empty audit_json 应记到 unknown 桶，实测 by_stage={by_stage}"
    )


def test_failure_trace_tolerates_malformed_audit_json(client, temp_db: Path) -> None:
    """单 job failure-trace 在 audit_json 损坏时返回 failure=null，不 500。"""
    from backend import db

    with db.connect() as conn:
        job_id = _seed_simulation_job_raw(
            conn,
            status="failed",
            audit_json_raw="{still not json",
            error_message="boom-with-broken-audit",
        )

    resp = client.get(f"/api/render/jobs/{job_id}/failure-trace")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simulation_job_id"] == job_id
    assert body["status"] == "failed"
    assert body["failure"] is None
    assert body["error_message"] == "boom-with-broken-audit"


def test_ops_vlm_comfyui_status_tolerates_malformed_audit_json(
    client, temp_db: Path
) -> None:
    """ops 聚合 endpoint 也不能因为一行 audit_json 损坏而 500。"""
    from backend import db

    with db.connect() as conn:
        _seed_simulation_job_raw(
            conn,
            status="failed",
            audit_json_raw="not json at all",
        )

    resp = client.get("/api/render/ops/vlm-comfyui/status?days=7")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "comfyui" in body
    assert body["comfyui"]["simulation_jobs_7d"]["failed"] >= 1


# ---------------------------------------------------------------------------
# 3. detect_distribution_collapse — exactly-90% boundary (inclusive)
# ---------------------------------------------------------------------------


def _calib_record(phase: str, view: str, body_part: str, confidence: float) -> dict:
    return {
        "phase": phase,
        "view": view,
        "body_part": body_part,
        "confidence": confidence,
    }


def test_calibration_exactly_90_percent_ratio_and_conf_is_uncalibrated() -> None:
    """边界包含：ratio == 0.90 且 p50 conf == 0.90 必须触发 uncalibrated（>=）。

    9 before + 1 after 共 10 条 → 9/10 = 0.90，全部 conf 0.90 → p50 = 0.90。
    """
    records = (
        [_calib_record("before", "front", "face", 0.90)] * 9
        + [_calib_record("after", "side", "face", 0.90)] * 1
    )
    status = vlm_calibration.detect_distribution_collapse(records)
    assert status.status == "uncalibrated"
    phase_alerts = [a for a in status.evidence if a.dimension == "phase"]
    assert phase_alerts and phase_alerts[0].severity == "uncalibrated"
    assert phase_alerts[0].dominant_ratio == 0.9
    assert phase_alerts[0].confidence_p50 == 0.9


def test_calibration_just_under_90_percent_ratio_is_warn_not_uncalibrated() -> None:
    """89% < 90%（严格 <）→ 不应 uncalibrated；ratio 仍 >= warn 0.80 → warn。"""
    # 89 before + 11 after = 89/100 = 0.89；p50 conf 仍高 → warn (不到 collapse)
    records = (
        [_calib_record("before", "front", "face", 0.95)] * 89
        + [_calib_record("after", "side", "face", 0.95)] * 11
    )
    status = vlm_calibration.detect_distribution_collapse(records)
    assert status.status == "warn", (
        f"89% ratio 在 0.90 阈值下不应 collapse, 实测 {status.status}"
    )


def test_calibration_exactly_90_ratio_conf_below_boundary_is_warn() -> None:
    """ratio==0.90 但 p50 conf == 0.89（<0.90）→ 落 warn 桶（仍 >= 0.85）。"""
    records = (
        [_calib_record("before", "front", "face", 0.89)] * 9
        + [_calib_record("after", "side", "face", 0.89)] * 1
    )
    status = vlm_calibration.detect_distribution_collapse(records)
    assert status.status == "warn"
    phase_alerts = [a for a in status.evidence if a.dimension == "phase"]
    assert phase_alerts and phase_alerts[0].severity == "warn"


# ---------------------------------------------------------------------------
# 4. fail_closed_reason None 防御路径
# ---------------------------------------------------------------------------


class _HealthyProvider:
    name = "healthy"
    model = "healthy-model"

    def __init__(self) -> None:
        self._i = 0

    def call_vision_batch(self, requests, *, concurrency=1, return_exceptions=False):
        from backend.services.vlm_provider import VLMResponse

        phases = ["before", "after", "during"]
        views = ["front", "side", "oblique"]
        out = []
        for _ in requests:
            parsed = {
                "phase": phases[self._i % 3],
                "view": views[self._i % 3],
                "body_part": "face",
                "confidence": 0.95,
                "reasoning": "stub",
            }
            self._i += 1
            out.append(VLMResponse(
                text=json.dumps(parsed),
                parsed=parsed,
                provider="healthy",
                model="healthy-model",
                latency_ms=5,
                input_tokens=5,
                output_tokens=5,
                usage_raw={},
            ))
        return out


def test_run_classification_healthy_apply_omits_fail_closed_reason(
    temp_db, tmp_path: Path
) -> None:
    """健康 + apply → fail_closed=False, 且 fail_closed_reason 必须缺失 / None（防御契约）。

    避免下游 consumer 拿到 stale "fail_closed_reason" 字段误判系统状态。
    """
    from backend import db

    with db.connect() as conn:
        for i in range(9):
            img = tmp_path / f"good_{i}.png"
            _tiny_png(img)
            _seed_observation(
                conn,
                case_id=4001,
                root_path=str(tmp_path),
                image_path=f"good_{i}.png",
            )
        report = vlm_source_classifier.run_classification(
            conn,
            provider=_HealthyProvider(),
            case_id=4001,
            mode="apply",
        )

    assert report["fail_closed"] is False
    assert report.get("fail_closed_reason") is None, (
        f"健康路径不能携带 fail_closed_reason, 实测={report.get('fail_closed_reason')!r}"
    )
    # 同时验证 consumer 侧 .get() 防御能拿到 None 不崩
    reason = report.get("fail_closed_reason")
    assert (reason or "no degradation") == "no degradation"


def test_run_classification_live_no_apply_collapse_does_not_set_fail_closed_reason(
    temp_db, tmp_path: Path
) -> None:
    """live-no-apply mode 即便 calibration uncalibrated 也 NOT 设 fail_closed / reason，
    因为不存在 apply→live-no-apply 的降级动作可言。"""
    from backend import db

    class _SplitProvider:
        name = "split"
        model = "split-model"

        def __init__(self) -> None:
            self._i = 0

        def call_vision_batch(self, requests, *, concurrency=1, return_exceptions=False):
            from backend.services.vlm_provider import VLMResponse

            out = []
            for _ in requests:
                if self._i == 11:
                    parsed = {"phase": "after", "view": "side", "body_part": "face",
                              "confidence": 0.95, "reasoning": "stub"}
                else:
                    parsed = {"phase": "before", "view": "front", "body_part": "face",
                              "confidence": 0.95, "reasoning": "stub"}
                self._i += 1
                out.append(VLMResponse(
                    text=json.dumps(parsed),
                    parsed=parsed,
                    provider="split",
                    model="split-model",
                    latency_ms=5,
                    input_tokens=5,
                    output_tokens=5,
                    usage_raw={},
                ))
            return out

    with db.connect() as conn:
        for i in range(12):
            img = tmp_path / f"img_{i}.png"
            _tiny_png(img)
            _seed_observation(
                conn,
                case_id=4002,
                root_path=str(tmp_path),
                image_path=f"img_{i}.png",
            )
        report = vlm_source_classifier.run_classification(
            conn,
            provider=_SplitProvider(),
            case_id=4002,
            mode="live-no-apply",
        )

    assert report["calibration_status"] == "uncalibrated"
    assert report["fail_closed"] is False
    assert report.get("fail_closed_reason") is None


# ---------------------------------------------------------------------------
# 5. pre_render_gate._apply_accepted_warnings — empty / malformed accepted list
# ---------------------------------------------------------------------------


def _gate_ticket() -> dict[str, Any]:
    return {
        "ticket_type": "source_quality_review",
        "reason_code": "crop_touches_frame",
        "slot": "front",
        "message": "front crop touches frame",
        "blocks_render": True,
        "evidence": {
            "slot": "front",
            "component": {
                "slot": "front",
                "selected_files": ["a.jpg"],
                "message_contains": "touches frame",
            },
        },
    }


def test_apply_accepted_warnings_empty_list_is_passthrough() -> None:
    """accepted_warnings=[] 必须等价于 None：不做过滤，原样返回 tickets。"""
    tickets = [_gate_ticket()]
    out = pre_render_gate._apply_accepted_warnings(tickets, [])
    assert out == tickets


def test_apply_accepted_warnings_all_non_dict_entries_is_passthrough() -> None:
    """全 None / 字符串 / 数字 → 等价于 None：完全没有可匹配的 accepted，跳过过滤。"""
    tickets = [_gate_ticket()]
    out = pre_render_gate._apply_accepted_warnings(tickets, [None, "x", 5, 0.1])
    assert out == tickets


def test_apply_accepted_warnings_mixed_non_dict_still_filters_real_entries() -> None:
    """混进 None / str 不应让 dict accepted 失效：dict 仍精确匹配过滤。"""
    tickets = [_gate_ticket()]
    accepted = [
        None,
        "not a dict",
        {"slot": "front", "code": "crop_touches_frame", "selected_files": ["a.jpg"]},
    ]
    out = pre_render_gate._apply_accepted_warnings(tickets, accepted)
    assert out == []


def test_evaluate_pre_render_gate_accepts_empty_accepted_warnings_kwarg(
    temp_db, tmp_path: Path
) -> None:
    """evaluate_pre_render_gate(accepted_warnings=[]) 不应崩溃，也不应误丢 ticket。"""
    from backend import db

    case_dir = tmp_path / "case-gate-empty"
    case_dir.mkdir()

    now = "2026-05-27T00:00:00+00:00"
    with db.connect() as conn:
        scan_id = conn.execute(
            "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
            (now, "[]", "unit"),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO cases (
              id, scan_id, abs_path, category, meta_json, last_modified, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                5001,
                scan_id,
                str(case_dir),
                "standard_face",
                json.dumps({"image_files": []}, ensure_ascii=False),
                now,
                now,
            ),
        )
        result = pre_render_gate.evaluate_pre_render_gate(
            case_id=5001,
            template="tri-compare",
            persist_tickets=False,
            accepted_warnings=[],
            conn=conn,
        )

    assert "gate" in result
    assert "tickets" in result
    assert result["gate"]["case_id"] == 5001
