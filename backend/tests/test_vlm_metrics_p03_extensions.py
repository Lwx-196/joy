"""P0.3-a — vlm_usage_metrics.summarize_classifier_outputs 扩展。

新增字段（plan §100）：
- confidence_buckets_calibrated：4 段 P0.3 校准用刻度（0.0-0.7 / 0.7-0.85 / 0.85-0.95 / ≥0.95）
- bias_alerts：list[{dimension, value, threshold, severity}]，由 detect_distribution_collapse 派生
"""
from __future__ import annotations

from pathlib import Path



def _seed_observation(
    conn,
    *,
    image_path: str,
    confidence: float = 0.7,
    phase: str = "unknown",
    view: str = "unknown",
    body_part: str = "face",
    source: str = "vlm_classifier",
) -> int:
    now = "2026-05-27T00:00:00+00:00"
    import json as _json
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    case_id = conn.execute(
        "INSERT INTO cases (scan_id, abs_path, category, last_modified, indexed_at) VALUES (?, ?, ?, ?, ?)",
        (scan_id, f"/tmp/{image_path}", "standard_face", now, now),
    ).lastrowid
    group_id = conn.execute(
        """
        INSERT INTO case_groups (
          group_key, primary_case_id, title, root_path, case_ids_json,
          status, diagnosis_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"g-{image_path}",
            case_id,
            "unit",
            f"/tmp/g-{image_path}",
            _json.dumps([case_id]),
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
            (group_id, case_id, image_path, phase, body_part, view, "{}", confidence,
             source, "[]", now, now),
        ).lastrowid
    )


def test_summarize_classifier_outputs_emits_calibrated_buckets(temp_db: Path) -> None:
    """P0.3a 新 4 段刻度 (0.0-0.7 / 0.7-0.85 / 0.85-0.95 / ≥0.95) 必须存在。"""
    from backend import db
    from backend.services.vlm_usage_metrics import summarize_classifier_outputs

    with db.connect() as conn:
        _seed_observation(conn, image_path="a.jpg", confidence=0.5)
        _seed_observation(conn, image_path="b.jpg", confidence=0.7)
        _seed_observation(conn, image_path="c.jpg", confidence=0.85)
        _seed_observation(conn, image_path="d.jpg", confidence=0.97)
        result = summarize_classifier_outputs(conn)

    assert "confidence_buckets_calibrated" in result, "P0.3a 新 calibrated bucket 字段缺失"
    buckets = result["confidence_buckets_calibrated"]
    expected_keys = {"0.0-0.7", "0.7-0.85", "0.85-0.95", ">=0.95"}
    assert set(buckets.keys()) == expected_keys, f"calibrated bucket keys 不符: {buckets.keys()}"
    assert buckets["0.0-0.7"] == 1  # 0.5
    assert buckets["0.7-0.85"] == 1  # 0.7
    assert buckets["0.85-0.95"] == 1  # 0.85
    assert buckets[">=0.95"] == 1  # 0.97


def test_summarize_classifier_outputs_emits_bias_alerts_on_collapse(temp_db: Path) -> None:
    """单维度 > 90% 主导 + p50 conf > 0.9 → bias_alerts 中含 uncalibrated 严重度记录。"""
    from backend import db
    from backend.services.vlm_usage_metrics import summarize_classifier_outputs

    with db.connect() as conn:
        # 制造坍缩：phase 11 全 before, 1 after, 全高 conf
        for i in range(11):
            _seed_observation(conn, image_path=f"c{i}.jpg", confidence=0.95,
                              phase="before", view="front")
        _seed_observation(conn, image_path="z.jpg", confidence=0.95,
                          phase="after", view="front")
        result = summarize_classifier_outputs(conn)

    assert "bias_alerts" in result, "P0.3a bias_alerts 字段缺失"
    alerts = result["bias_alerts"]
    assert isinstance(alerts, list)
    phase_alerts = [a for a in alerts if a.get("dimension") == "phase"]
    assert phase_alerts, "phase 维度坍缩应触 alert"
    pa = phase_alerts[0]
    for key in ("dimension", "value", "threshold", "severity"):
        assert key in pa, f"alert 缺 key: {key}"
    assert pa["severity"] in {"warn", "uncalibrated"}
    assert pa["value"] >= 0.9  # 11/12 ≈ 0.917


def test_summarize_classifier_outputs_no_alerts_when_healthy(temp_db: Path) -> None:
    """各维度均衡时 bias_alerts 应为空 list。"""
    from backend import db
    from backend.services.vlm_usage_metrics import summarize_classifier_outputs

    with db.connect() as conn:
        for i in range(3):
            _seed_observation(conn, image_path=f"b{i}.jpg", confidence=0.7,
                              phase="before", view="front")
            _seed_observation(conn, image_path=f"a{i}.jpg", confidence=0.7,
                              phase="after", view="front")
            _seed_observation(conn, image_path=f"d{i}.jpg", confidence=0.7,
                              phase="during", view="side")
        result = summarize_classifier_outputs(conn)

    assert result["bias_alerts"] == [], (
        f"健康分布不应触发 alert，实测 {result['bias_alerts']}"
    )


def test_summarize_classifier_outputs_back_compat_keeps_old_buckets(temp_db: Path) -> None:
    """旧 confidence_buckets 字段必须保留（已有 CLI / 报告依赖）。"""
    from backend import db
    from backend.services.vlm_usage_metrics import summarize_classifier_outputs

    with db.connect() as conn:
        _seed_observation(conn, image_path="x.jpg", confidence=0.95)
        result = summarize_classifier_outputs(conn)

    assert "confidence_buckets" in result, "旧 confidence_buckets 字段必须保留"
    old = result["confidence_buckets"]
    assert "0.9-1.0" in old, f"旧 bucket 命名变了: {old.keys()}"
