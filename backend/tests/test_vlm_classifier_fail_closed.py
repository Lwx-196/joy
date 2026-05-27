"""P0.3-b — vlm_source_classifier apply 决策点 fail-closed 守门。

模型坍缩（单类 ≥90% + p50 conf ≥0.9）时，apply 模式强制降级为 live-no-apply：
不写 image_observations，但仍写 vlm_usage_log 留存证据。
"""
from __future__ import annotations

import json
from pathlib import Path

from backend.services import vlm_source_classifier


def _write_tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _seed_observation(conn, *, case_id: int, root_path: str, image_path: str,
                      phase: str = "unknown", view: str = "unknown") -> int:
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
            (group_id, case_id, image_path, phase, "face", view, "{}", 0.25,
             "rules", "[]", now, now),
        ).lastrowid
    )


class _CollapseProvider:
    """11/12 phase=before + 1 after = 91.7% 主导 + conf 0.95 → 坍缩。
    （detect_distribution_collapse 跳过单值维度，所以混 1 张让 phase 有 2 类。）"""

    name = "collapse-classifier"
    model = "collapse-model"

    def __init__(self) -> None:
        self._i = 0

    def call_vision_batch(self, requests, *, concurrency: int = 1, return_exceptions: bool = False):
        from backend.services.vlm_provider import VLMResponse

        out = []
        for _ in requests:
            # 最后一张让 phase=after / view=side 给一些噪音，让维度非单值
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
                provider="collapse-classifier",
                model="collapse-model",
                latency_ms=5,
                input_tokens=5,
                output_tokens=5,
                usage_raw={},
            ))
        return out


class _DiverseProvider:
    """返回交替 phase / view，保证分布健康。"""

    name = "diverse-classifier"
    model = "diverse-model"

    def __init__(self) -> None:
        self._i = 0

    def call_vision_batch(self, requests, *, concurrency: int = 1, return_exceptions: bool = False):
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
                provider="diverse-classifier",
                model="diverse-model",
                latency_ms=5,
                input_tokens=5,
                output_tokens=5,
                usage_raw={},
            ))
        return out


def test_run_classification_fail_closed_on_collapse(temp_db, tmp_path: Path) -> None:
    """apply 模式 + 坍缩分布 → 自动降级 live-no-apply，0 写入 image_observations，
    但仍写 vlm_usage_log 留证。"""
    from backend import db

    obs_ids: list[int] = []
    with db.connect() as conn:
        for i in range(12):
            png = tmp_path / f"p{i}.png"
            _write_tiny_png(png)
            obs_ids.append(_seed_observation(
                conn, case_id=126, root_path=str(tmp_path), image_path=f"p{i}.png"
            ))
        report = vlm_source_classifier.run_classification(
            conn,
            provider=_CollapseProvider(),
            case_id=126,
            mode="apply",
        )
        applied = conn.execute(
            "SELECT COUNT(*) FROM image_observations WHERE source = 'vlm_classifier'",
        ).fetchone()[0]
        usage_count = conn.execute(
            "SELECT COUNT(*) FROM vlm_usage_log WHERE purpose = 'classifier'",
        ).fetchone()[0]

    assert report.get("calibration_status") == "uncalibrated", (
        f"坍缩分布应触发 calibration_status=uncalibrated，实测 {report.get('calibration_status')}"
    )
    assert report.get("fail_closed") is True, "report 应标记 fail_closed=True"
    assert applied == 0, "fail-closed 应阻止任何 image_observations 写入"
    assert usage_count >= 12, "vlm_usage_log 仍应记录所有调用（留证）"


def test_run_classification_apply_path_unchanged_when_healthy(temp_db, tmp_path: Path) -> None:
    """健康分布 → 正常 apply 写入 image_observations，fail_closed=False。"""
    from backend import db

    with db.connect() as conn:
        for i in range(9):  # 9 / 3 = 3 each phase × 3 views
            png = tmp_path / f"p{i}.png"
            _write_tiny_png(png)
            _seed_observation(
                conn, case_id=127, root_path=str(tmp_path), image_path=f"p{i}.png"
            )
        report = vlm_source_classifier.run_classification(
            conn,
            provider=_DiverseProvider(),
            case_id=127,
            mode="apply",
        )
        applied = conn.execute(
            "SELECT COUNT(*) FROM image_observations WHERE source = 'vlm_classifier'",
        ).fetchone()[0]

    assert report.get("calibration_status") == "ok"
    assert report.get("fail_closed") is False
    assert applied == 9


def test_live_no_apply_mode_unaffected_by_collapse(temp_db, tmp_path: Path) -> None:
    """live-no-apply 模式本就不写，calibration 状态不影响 mode 决策。"""
    from backend import db

    with db.connect() as conn:
        for i in range(12):
            png = tmp_path / f"p{i}.png"
            _write_tiny_png(png)
            _seed_observation(
                conn, case_id=128, root_path=str(tmp_path), image_path=f"p{i}.png"
            )
        report = vlm_source_classifier.run_classification(
            conn,
            provider=_CollapseProvider(),
            case_id=128,
            mode="live-no-apply",
        )
        applied = conn.execute(
            "SELECT COUNT(*) FROM image_observations WHERE source = 'vlm_classifier'",
        ).fetchone()[0]

    # 坍缩信号仍然被报告，但 live-no-apply 没有 apply 可降级
    assert report.get("calibration_status") == "uncalibrated"
    assert applied == 0
    # fail_closed 仅在原 mode=apply 被迫降级时为 True
    assert report.get("fail_closed") is False
