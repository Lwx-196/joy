"""VLM source image classification."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any



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
    conn.execute(
        """
        INSERT OR IGNORE INTO cases (
          id, scan_id, abs_path, category, last_modified, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, scan_id, root_path, "standard_face", now, now),
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
            f"{root_path}-{image_path}",
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
            "view": "45deg",
            "body_part": "face",
            "confidence": 0.91,
            "reasoning": "visible post-treatment image",
        }
        self.calls: list[dict[str, Any]] = []

    def call_vision(
        self,
        prompt: str,
        images: list[Path],
        *,
        timeout: float = 30.0,
        purpose: str | None = None,
    ):
        from backend.services.vlm_provider import VLMResponse

        self.calls.append({"prompt": prompt, "images": images, "timeout": timeout, "purpose": purpose})
        return VLMResponse(
            text=json.dumps(self.parsed),
            parsed=self.parsed,
            provider="unit_provider",
            model="unit_model",
            latency_ms=7,
            input_tokens=11,
            output_tokens=5,
            usage_raw={"input_tokens": 11, "output_tokens": 5},
        )

    def call_vision_batch(
        self,
        items: list[Any],
        *,
        concurrency: int = 3,
        return_exceptions: bool = False,
    ):
        results: list[Any] = []
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


def test_classify_image_normalizes_schema_and_prompt(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import classify_image

    image = tmp_path / "after-45.png"
    _write_tiny_png(image)
    provider = FakeProvider()

    result = classify_image(image, provider)

    assert result.phase == "after"
    assert result.view == "oblique"
    assert result.body_part == "face"
    assert result.confidence == 0.91
    assert provider.calls
    assert "strict JSON" in provider.calls[0]["prompt"]


def test_low_confidence_queue_skips_manual_override_and_high_confidence(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services.vlm_source_classifier import fetch_classification_queue

    _write_tiny_png(tmp_path / "unknown.png")
    _write_tiny_png(tmp_path / "manual.png")
    _write_tiny_png(tmp_path / "high.png")
    with db.connect() as conn:
        wanted = _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")
        _seed_observation(conn, root_path=str(tmp_path), image_path="manual.png", source="rules")
        _seed_observation(conn, root_path=str(tmp_path), image_path="high.png", phase="before", view="front", confidence=0.96)
        conn.execute(
            """
            INSERT INTO case_image_overrides (case_id, filename, manual_phase, manual_view, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (126, "manual.png", "before", "front", "2026-05-18T00:00:00+00:00"),
        )
        rows = fetch_classification_queue(conn, case_id=126, max_items=20)

    assert [row.observation_id for row in rows] == [wanted]
    assert rows[0].image_abs_path == tmp_path / "unknown.png"


def test_run_classification_dry_run_does_not_call_provider(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")
        report = run_classification(conn, provider=None, case_id=126, dry_run=True)
        source_count = conn.execute("SELECT COUNT(*) AS n FROM image_observations WHERE source = 'vlm_classifier'").fetchone()["n"]

    assert report["run_status"] == "dry_run"
    assert report["candidate_count"] == 1
    assert report["classified_count"] == 0
    assert source_count == 0


def test_run_classification_real_updates_observation_and_usage_log(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        observation_id = _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")
        report = run_classification(conn, provider=FakeProvider(), case_id=126, dry_run=False)
        row = conn.execute("SELECT * FROM image_observations WHERE id = ?", (observation_id,)).fetchone()
        usage = conn.execute("SELECT * FROM vlm_usage_log WHERE purpose = 'classifier'").fetchone()

    assert report["run_status"] == "completed_vlm_classification"
    assert report["classified_count"] == 1
    assert row["phase"] == "after"
    assert row["view"] == "oblique"
    assert row["source"] == "vlm_classifier"
    assert row["confidence"] == 0.91
    assert usage is not None
    assert usage["provider"] == "unit_provider"
    assert usage["model"] == "unit_model"


def test_run_classification_live_no_apply_records_usage_without_writes(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        observation_id = _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")
        report = run_classification(conn, provider=FakeProvider(), case_id=126, mode="live-no-apply")
        row = conn.execute("SELECT * FROM image_observations WHERE id = ?", (observation_id,)).fetchone()
        usage_rows = conn.execute("SELECT * FROM vlm_usage_log WHERE purpose = 'classifier'").fetchall()

    assert report["run_status"] == "completed_vlm_classification_live_no_apply"
    assert report["mode"] == "live-no-apply"
    assert report["classified_count"] == 0
    assert report["would_apply_count"] == 1
    assert len(report["previews"]) == 1
    preview = report["previews"][0]
    assert preview["predicted_phase"] == "after"
    assert preview["predicted_view"] == "oblique"
    assert preview["predicted_confidence"] == 0.91
    assert preview["would_apply"] is True
    assert row["source"] != "vlm_classifier"
    assert len(usage_rows) == 1
    assert usage_rows[0]["status"] == "live_no_apply"


def test_run_classification_apply_mode_writes_observations(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        observation_id = _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")
        report = run_classification(conn, provider=FakeProvider(), case_id=126, mode="apply")
        row = conn.execute("SELECT * FROM image_observations WHERE id = ?", (observation_id,)).fetchone()

    assert report["mode"] == "apply"
    assert report["run_status"] == "completed_vlm_classification"
    assert report["classified_count"] == 1
    assert row["source"] == "vlm_classifier"


def test_run_classification_invalid_mode_raises(temp_db: Path, tmp_path: Path) -> None:
    import pytest as _pytest
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    with db.connect() as conn:
        with _pytest.raises(ValueError, match="invalid mode"):
            run_classification(conn, provider=None, case_id=126, mode="ship-it-yolo")


class _PartialFailureProvider(FakeProvider):
    """Raises on images whose filename contains ``fail`` (e.g. a HEIC stand-in)."""

    def call_vision(
        self,
        prompt: str,
        images: list[Path],
        *,
        timeout: float = 30.0,
        purpose: str | None = None,
    ):
        if any("fail" in str(p) for p in images):
            raise ValueError("simulated HEIC decode failure")
        return super().call_vision(prompt, images, timeout=timeout, purpose=purpose)


def test_classify_batch_returns_exceptions_per_item(tmp_path: Path) -> None:
    """PLAN P1 task #7: 单 image 失败不应 abort 整批，return_exceptions=True 模式下保留位置。"""
    from backend.services.vlm_source_classifier import classify_batch

    good_path = tmp_path / "good.png"
    bad_path = tmp_path / "fail-heic.png"
    _write_tiny_png(good_path)
    _write_tiny_png(bad_path)
    provider = _PartialFailureProvider()

    results = classify_batch([good_path, bad_path], provider, return_exceptions=True)

    assert len(results) == 2
    assert results[0].phase == "after"
    assert isinstance(results[1], BaseException)
    assert "HEIC" in str(results[1])


def test_run_classification_apply_continues_on_partial_failures(
    temp_db: Path, tmp_path: Path
) -> None:
    """PLAN P1 task #7: HEIC 等单图失败不再让整批 abort（90 分钟 0 写入的根因修复）。"""
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    good_path = tmp_path / "good.png"
    bad_path = tmp_path / "fail-heic.png"
    _write_tiny_png(good_path)
    _write_tiny_png(bad_path)
    with db.connect() as conn:
        good_obs_id = _seed_observation(
            conn, root_path=str(tmp_path), image_path="good.png"
        )
        bad_obs_id = _seed_observation(
            conn, root_path=str(tmp_path), image_path="fail-heic.png"
        )
        report = run_classification(
            conn,
            provider=_PartialFailureProvider(),
            case_id=126,
            mode="apply",
        )
        good_row = conn.execute(
            "SELECT source FROM image_observations WHERE id = ?",
            (good_obs_id,),
        ).fetchone()
        bad_row = conn.execute(
            "SELECT source FROM image_observations WHERE id = ?",
            (bad_obs_id,),
        ).fetchone()

    assert report["run_status"] == "completed_vlm_classification"
    assert report["classified_count"] == 1
    assert report["error_count"] == 1
    assert len(report["errors"]) == 1
    assert report["errors"][0]["image_path"] == "fail-heic.png"
    assert "ValueError" in report["errors"][0]["reason"]
    assert good_row["source"] == "vlm_classifier"
    assert bad_row["source"] != "vlm_classifier"


def test_vlm_provider_registers_heif_opener_when_available() -> None:
    """PLAN P1 task #8: pillow-heif 装好时 PIL 应能识别 .heic 扩展名。"""
    import importlib

    from backend.services import vlm_provider as _vlm_provider

    importlib.reload(_vlm_provider)
    if not _vlm_provider._HEIF_AVAILABLE:
        import pytest as _pytest

        _pytest.skip("pillow-heif not installed in this env")
    from PIL import Image

    assert ".heic" in Image.registered_extensions()


def test_classify_images_api_dry_run(client, tmp_path: Path) -> None:
    from backend import db

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")

    response = client.post("/api/cases/126/classify-images", json={"dry_run": True, "max_items": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["run_status"] == "dry_run"
    assert body["candidate_count"] == 1


def test_classify_images_api_mode_dry_run(client, tmp_path: Path) -> None:
    """PLAN P2: API mode='dry-run' 等同于 legacy dry_run=True 入口。"""
    from backend import db

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")

    response = client.post(
        "/api/cases/126/classify-images",
        json={"mode": "dry-run", "max_items": 10},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_status"] == "dry_run"
    assert body["mode"] == "dry-run"
    assert body["candidate_count"] == 1


def test_classify_images_api_mode_invalid_returns_400(client, tmp_path: Path) -> None:
    """PLAN P2: API 拒绝非三态枚举的 mode 值。"""
    response = client.post(
        "/api/cases/126/classify-images",
        json={"mode": "ship-it-yolo"},
    )
    assert response.status_code == 400
    assert "invalid mode" in response.json()["detail"]


def test_classify_images_api_legacy_dry_run_false_maps_to_apply(client, tmp_path: Path) -> None:
    """PLAN P2: 向后兼容 dry_run=False 应映射到 mode=apply。"""
    from backend import db

    _write_tiny_png(tmp_path / "unknown.png")
    with db.connect() as conn:
        _seed_observation(conn, root_path=str(tmp_path), image_path="unknown.png")

    response = client.post(
        "/api/cases/126/classify-images",
        json={"dry_run": False, "max_items": 1},
    )
    # Provider env may not be configured in test env; either way the resolved
    # mode should be 'apply' (not legacy dry_run path).
    assert response.status_code == 200
    body = response.json()
    # P0.5 H-1 后：requested_mode 保留用户请求的 apply（即便 fail-closed 把
    # 实际 mode 降到 live-no-apply）。本测试只验证 legacy dry_run=False → apply
    # 映射契约，不强求 final mode == apply。
    assert body["requested_mode"] == "apply"
    assert body["mode"] in {"apply", "live-no-apply"}


def test_source_group_uses_vlm_classifier_observation_without_manual_override(client, tmp_path: Path) -> None:
    from backend import db

    image = tmp_path / "raw.png"
    _write_tiny_png(image)
    now = "2026-05-18T00:00:00+00:00"
    with db.connect() as conn:
        scan_id = conn.execute(
            "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
            (now, "[]", "unit"),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO cases (
              id, scan_id, abs_path, category, meta_json, last_modified, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                127,
                scan_id,
                str(tmp_path),
                "standard_face",
                json.dumps({"image_files": ["raw.png"]}, ensure_ascii=False),
                now,
                now,
            ),
        )
        _seed_observation(
            conn,
            case_id=127,
            root_path=str(tmp_path),
            image_path="raw.png",
            phase="after",
            view="front",
            confidence=0.92,
            source="vlm_classifier",
        )

    response = client.get("/api/cases/127/source-group")

    assert response.status_code == 200
    image_payload = response.json()["sources"][0]["images"][0]
    assert image_payload["phase"] == "after"
    assert image_payload["phase_source"] == "vlm_classifier"
    assert image_payload["view"] == "front"
    assert image_payload["view_source"] == "vlm_classifier"
    assert image_payload["angle_confidence"] == 0.92


def test_render_selection_context_uses_vlm_classifier_observations(temp_db: Path, tmp_path: Path) -> None:
    """P0 bridge: image_observations.vlm_classifier rows enrich render selection plan."""
    from backend import db, render_queue

    _write_tiny_png(tmp_path / "before.png")
    _write_tiny_png(tmp_path / "after.png")
    now = "2026-05-18T00:00:00+00:00"
    with db.connect() as conn:
        scan_id = conn.execute(
            "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
            (now, "[]", "unit"),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO cases (
              id, scan_id, abs_path, category, meta_json, last_modified, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                128,
                scan_id,
                str(tmp_path),
                "standard_face",
                json.dumps({"image_files": ["before.png", "after.png"]}, ensure_ascii=False),
                now,
                now,
            ),
        )
        _seed_observation(
            conn,
            case_id=128,
            root_path=str(tmp_path),
            image_path="before.png",
            phase="before",
            view="front",
            confidence=0.93,
            source="vlm_classifier",
        )
        _seed_observation(
            conn,
            case_id=128,
            root_path=str(tmp_path),
            image_path="after.png",
            phase="after",
            view="front",
            confidence=0.94,
            source="vlm_classifier",
        )
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (128,)).fetchone()
        context = render_queue._build_render_selection_context(conn, [dict(row)])

    front = context["plan"]["slots"]["front"]
    assert front["before"]["filename"] == "before.png"
    assert front["before"]["classification_source"] == "vlm_classifier"
    assert front["after"]["filename"] == "after.png"
    assert front["after"]["classification_source"] == "vlm_classifier"
