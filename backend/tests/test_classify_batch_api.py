"""Tests for POST /api/classification/batch + classify_batch_with_retry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _write_tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _seed_observation(
    conn,
    *,
    case_id: int = 126,
    root_path: str,
    image_path: str,
    phase: str = "unknown",
    view: str = "unknown",
    confidence: float = 0.25,
    source: str = "rules",
) -> int:
    now = "2026-05-18T00:00:00+00:00"
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    abs_path = f"{root_path}/case_{case_id}"
    conn.execute(
        """
        INSERT OR IGNORE INTO cases (
          id, scan_id, abs_path, category, last_modified, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, scan_id, abs_path, "standard_face", now, now),
    )
    group_id = conn.execute(
        """
        INSERT INTO case_groups (
          group_key, primary_case_id, title, root_path, case_ids_json,
          status, diagnosis_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{root_path}-{image_path}-{case_id}",
            case_id,
            "unit group",
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
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                case_id,
                image_path,
                phase,
                "face",
                view,
                "{}",
                confidence,
                source,
                "[]",
                now,
                now,
            ),
        ).lastrowid
    )


class FakeProvider:
    def __init__(self, parsed: dict[str, Any] | None = None) -> None:
        self.parsed = parsed or {
            "phase": "after",
            "view": "front",
            "body_part": "face",
            "confidence": 0.92,
            "reasoning": "post-treatment",
        }
        self.calls: list[dict[str, Any]] = []

    def call_vision(self, prompt, images, *, timeout=30.0, purpose=None, max_dimension=None):
        from backend.services.vlm_provider import VLMResponse

        self.calls.append({"images": images, "timeout": timeout})
        return VLMResponse(
            text=json.dumps(self.parsed),
            parsed=self.parsed,
            provider="unit",
            model="unit",
            latency_ms=5,
            input_tokens=10,
            output_tokens=5,
            usage_raw={},
        )

    def call_vision_batch(self, items, *, concurrency=3, return_exceptions=False):
        results = []
        for item in items:
            try:
                results.append(
                    self.call_vision(item.prompt, item.images, timeout=item.timeout, purpose=getattr(item, "purpose", None))
                )
            except BaseException as exc:
                if not return_exceptions:
                    raise
                results.append(exc)
        return results


# --- classify_batch_with_retry unit tests ---


def test_retry_recovers_timeout_errors(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMResponse
    from backend.services.vlm_source_classifier import classify_batch_with_retry

    img = tmp_path / "test.png"
    _write_tiny_png(img)

    call_count = 0

    class RetryProvider:
        name = "retry_test"

        def call_vision_batch(self, items, *, concurrency=3, return_exceptions=False):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [TimeoutError("socket timed out")]
            return [
                VLMResponse(
                    text=json.dumps({"phase": "before", "view": "front", "body_part": "face", "confidence": 0.9, "reasoning": "ok"}),
                    parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.9, "reasoning": "ok"},
                    provider="unit", model="unit", latency_ms=5, input_tokens=10, output_tokens=5, usage_raw={},
                )
            ]

    results = classify_batch_with_retry([img], RetryProvider(), max_retries=2)
    assert len(results) == 1
    assert not isinstance(results[0], BaseException)
    assert results[0].phase == "before"
    assert call_count == 2


def test_retry_does_not_retry_non_transient(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMRequestError
    from backend.services.vlm_source_classifier import classify_batch_with_retry

    img = tmp_path / "test.png"
    _write_tiny_png(img)

    class NonTransientProvider:
        name = "non_transient"

        def call_vision_batch(self, items, *, concurrency=3, return_exceptions=False):
            return [VLMRequestError("bad request", status_code=400)]

    results = classify_batch_with_retry([img], NonTransientProvider(), max_retries=3)
    assert len(results) == 1
    assert isinstance(results[0], BaseException)


def test_retry_partial_batch(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMResponse
    from backend.services.vlm_source_classifier import classify_batch_with_retry

    img1 = tmp_path / "ok.png"
    img2 = tmp_path / "fail.png"
    _write_tiny_png(img1)
    _write_tiny_png(img2)

    ok_response = VLMResponse(
        text=json.dumps({"phase": "after", "view": "front", "body_part": "face", "confidence": 0.9, "reasoning": "ok"}),
        parsed={"phase": "after", "view": "front", "body_part": "face", "confidence": 0.9, "reasoning": "ok"},
        provider="unit", model="unit", latency_ms=5, input_tokens=10, output_tokens=5, usage_raw={},
    )

    round_num = 0

    class PartialProvider:
        name = "partial"

        def call_vision_batch(self, items, *, concurrency=3, return_exceptions=False):
            nonlocal round_num
            round_num += 1
            if round_num == 1:
                return [ok_response, TimeoutError("timeout")]
            return [ok_response]

    results = classify_batch_with_retry([img1, img2], PartialProvider(), max_retries=1)
    assert len(results) == 2
    assert not isinstance(results[0], BaseException)
    assert not isinstance(results[1], BaseException)


# --- batch API endpoint tests ---


def test_batch_api_dry_run(client, temp_db, tmp_path: Path) -> None:
    from backend import db

    _write_tiny_png(tmp_path / "img.png")
    with db.connect() as conn:
        _seed_observation(conn, case_id=1, root_path=str(tmp_path), image_path="img.png")

    resp = client.post("/api/classification/batch", json={
        "case_ids": [1],
        "mode": "dry-run",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_status"] == "completed"
    assert data["mode"] == "dry-run"
    assert data["case_count"] == 1
    assert data["candidate_count"] >= 1


def test_batch_api_missing_case(client, temp_db) -> None:
    resp = client.post("/api/classification/batch", json={
        "case_ids": [99999],
        "mode": "dry-run",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["case_count"] == 1
    assert data["results"][0]["run_status"] == "case_not_found"


def test_batch_api_requires_case_ids_or_all(client, temp_db) -> None:
    resp = client.post("/api/classification/batch", json={"mode": "dry-run"})
    assert resp.status_code == 400


def test_batch_api_invalid_mode(client, temp_db) -> None:
    resp = client.post("/api/classification/batch", json={
        "case_ids": [1],
        "mode": "invalid",
    })
    assert resp.status_code == 400


def test_batch_api_all_low_confidence_dry_run(client, temp_db, tmp_path: Path) -> None:
    from backend import db

    _write_tiny_png(tmp_path / "a.png")
    _write_tiny_png(tmp_path / "b.png")
    with db.connect() as conn:
        _seed_observation(conn, case_id=10, root_path=str(tmp_path), image_path="a.png")
        _seed_observation(conn, case_id=11, root_path=str(tmp_path), image_path="b.png")

    resp = client.post("/api/classification/batch", json={
        "all_low_confidence": True,
        "mode": "dry-run",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_status"] == "completed"
    assert data["candidate_count"] >= 2


def test_batch_api_apply_with_mock_provider(client, temp_db, tmp_path: Path) -> None:
    from backend import db

    _write_tiny_png(tmp_path / "img.png")
    with db.connect() as conn:
        _seed_observation(conn, case_id=1, root_path=str(tmp_path), image_path="img.png")

    fake = FakeProvider()
    with patch("backend.routes.classification.VLMProvider", return_value=fake):
        resp = client.post("/api/classification/batch", json={
            "case_ids": [1],
            "mode": "apply",
            "max_retries": 0,
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_status"] == "completed"
    assert data["classified_count"] >= 1


def test_batch_api_multi_case(client, temp_db, tmp_path: Path) -> None:
    from backend import db

    _write_tiny_png(tmp_path / "c1.png")
    _write_tiny_png(tmp_path / "c2.png")
    with db.connect() as conn:
        _seed_observation(conn, case_id=20, root_path=str(tmp_path), image_path="c1.png")
        _seed_observation(conn, case_id=21, root_path=str(tmp_path), image_path="c2.png")

    resp = client.post("/api/classification/batch", json={
        "case_ids": [20, 21],
        "mode": "dry-run",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["case_count"] == 2
    assert data["candidate_count"] >= 2
