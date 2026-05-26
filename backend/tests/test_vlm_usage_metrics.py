"""PLAN P2 wave-1: tests for vlm_usage_metrics summaries."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.vlm_usage_metrics import (
    _percentile,
    summarize_classifier_outputs,
    summarize_usage,
)


def _seed_usage_row(
    conn,
    *,
    purpose: str = "classifier",
    provider: str = "openai_chat_completions",
    model: str = "mlx-community/Qwen3-VL-4B-Instruct-4bit",
    latency_ms: int = 5000,
    input_tokens: int = 100,
    output_tokens: int = 30,
    cost_usd: float = 0.0,
    status: str = "success",
    created_at: str = "2026-05-26T12:00:00+00:00",
) -> int:
    from backend.services.vlm_usage import record_vlm_usage

    return record_vlm_usage(
        conn,
        purpose=purpose,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        status=status,
        created_at=created_at,
    )


def _seed_observation(
    conn,
    *,
    case_id: int = 100,
    image_path: str = "img.jpg",
    phase: str = "before",
    view: str = "front",
    body_part: str = "face",
    confidence: float = 0.95,
    source: str = "vlm_classifier",
) -> int:
    import json as _json

    now = "2026-05-26T12:00:00+00:00"
    group_id = int(
        conn.execute(
            """
            INSERT INTO case_groups (
              group_key, primary_case_id, title, root_path, case_ids_json,
              status, diagnosis_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"unit-{image_path}-{case_id}",
                case_id,
                "unit-metrics",
                "/tmp",
                _json.dumps([case_id]),
                "needs_review",
                "{}",
                now,
                now,
            ),
        ).lastrowid
    )
    return int(
        conn.execute(
            """
            INSERT INTO image_observations
                (group_id, case_id, image_path, phase, body_part, view,
                 quality_json, confidence, source, reasons_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, '[]', ?, ?)
            """,
            (group_id, case_id, image_path, phase, body_part, view, confidence, source, now, now),
        ).lastrowid
    )


def test_percentile_helper_handles_edge_cases() -> None:
    assert _percentile([], 0.5) == 0.0
    assert _percentile([5], 0.95) == 5.0
    # interpolated p50 of [10, 20, 30, 40] = 25
    assert _percentile([10, 20, 30, 40], 0.5) == 25.0


def test_summarize_usage_returns_zeros_when_empty(temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        summary = summarize_usage(conn)

    assert summary["total_calls"] == 0
    assert summary["latency_ms"]["p50"] == 0.0
    assert summary["cost_usd"]["total"] == 0.0
    assert summary["per_provider"] == {}


def test_summarize_usage_computes_percentiles_and_breakdowns(temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        for lat in [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]:
            _seed_usage_row(conn, latency_ms=lat)
        _seed_usage_row(conn, provider="vertex_generate_content_adc", latency_ms=15000, cost_usd=0.002)
        _seed_usage_row(conn, status="error", latency_ms=500)

        summary = summarize_usage(conn)

    assert summary["total_calls"] == 12
    assert summary["status_breakdown"]["success"] == 11
    assert summary["status_breakdown"]["error"] == 1
    assert summary["provider_breakdown"]["openai_chat_completions"] == 11
    assert summary["provider_breakdown"]["vertex_generate_content_adc"] == 1
    # latency p50 is interpolated; with these 12 values it should fall between
    # 5000 and 6000 (and be > 4500, < 7000) — exact spec covered in helper test
    assert 4500 <= summary["latency_ms"]["p50"] <= 7000
    assert summary["latency_ms"]["max"] == 15000
    assert summary["cost_usd"]["total"] == pytest.approx(0.002, abs=1e-9)
    assert "openai_chat_completions" in summary["per_provider"]
    assert "vertex_generate_content_adc" in summary["per_provider"]
    vertex = summary["per_provider"]["vertex_generate_content_adc"]
    assert vertex["count"] == 1
    assert vertex["cost_usd_total"] == pytest.approx(0.002, abs=1e-9)


def test_summarize_usage_respects_filters(temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        _seed_usage_row(conn, purpose="classifier", latency_ms=1000)
        _seed_usage_row(conn, purpose="judge", latency_ms=8000)
        _seed_usage_row(conn, purpose="classifier", latency_ms=2000, status="error")

        only_classifier = summarize_usage(conn, purpose="classifier")
        only_success = summarize_usage(conn, status="success")

    assert only_classifier["total_calls"] == 2
    assert "judge" not in only_classifier["purpose_breakdown"]
    assert only_success["total_calls"] == 2
    assert only_success["status_breakdown"] == {"success": 2}


def test_summarize_classifier_outputs_buckets_confidence(temp_db: Path) -> None:
    from backend import db

    with db.connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")  # metric helper queries image_observations only
        _seed_observation(conn, image_path="a.jpg", confidence=0.30, phase="unknown", view="unknown")
        _seed_observation(conn, image_path="b.jpg", confidence=0.65, phase="before", view="front")
        _seed_observation(conn, image_path="c.jpg", confidence=0.85, phase="before", view="side")
        _seed_observation(conn, image_path="d.jpg", confidence=0.95, phase="after", view="side")
        _seed_observation(conn, image_path="e.jpg", confidence=0.99, phase="after", view="side", source="rules")

        result = summarize_classifier_outputs(conn)

    # 'rules' source should be excluded by default filter
    assert result["total"] == 4
    assert result["phase"] == {"unknown": 1, "before": 2, "after": 1}
    assert result["view"] == {"unknown": 1, "front": 1, "side": 2}
    assert result["confidence_buckets"] == {
        "0.0-0.5": 1,
        "0.5-0.8": 1,
        "0.8-0.9": 1,
        "0.9-1.0": 1,
    }
    assert 0.6 < result["confidence_mean"] < 0.8
