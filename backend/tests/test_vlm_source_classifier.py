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

    def call_vision_batch(self, items: list[Any], *, concurrency: int = 3):
        return [
            self.call_vision(item.prompt, item.images, timeout=item.timeout, purpose=getattr(item, "purpose", None))
            for item in items
        ]


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
