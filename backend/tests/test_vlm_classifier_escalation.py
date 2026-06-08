"""Tiered escalation: low-confidence local VLM results auto-upgrade to cloud."""
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
    """Configurable fake VLM provider that returns different results per call."""

    def __init__(
        self,
        parsed: dict[str, Any] | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        self.parsed = parsed or {
            "phase": "after",
            "view": "45deg",
            "body_part": "face",
            "confidence": 0.70,
            "reasoning": "low confidence local",
        }
        self.env = env or {}
        self.calls: list[dict[str, Any]] = []

    def call_vision(
        self,
        prompt: str,
        images: list[Path],
        *,
        timeout: float = 30.0,
        purpose: str | None = None,
        max_dimension: int | None = None,
    ):
        from backend.services.vlm_provider import VLMResponse

        self.calls.append({"prompt": prompt, "images": images, "timeout": timeout, "purpose": purpose})
        return VLMResponse(
            text=json.dumps(self.parsed),
            parsed=self.parsed,
            provider="fake_local",
            model="gemma4:12b",
            latency_ms=50,
            input_tokens=100,
            output_tokens=20,
            usage_raw={"input_tokens": 100, "output_tokens": 20},
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


# ---------------------------------------------------------------------------
# Unit tests for _make_cloud_provider
# ---------------------------------------------------------------------------

def test_make_cloud_provider_flash_configured() -> None:
    from backend.services.vlm_source_classifier import _make_cloud_provider

    env = {
        "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
        "VLM_CLOUD_FLASH_API_KEY": "sk-flash",
        "VLM_CLOUD_MODEL": "gemini-3.1-pro-preview",
    }
    provider = _make_cloud_provider(env, "flash")
    assert provider is not None
    config = provider.configure(purpose="classifier")
    assert config.ready
    assert config.model == "gemini-3.1-pro-preview"
    assert "flashapi" in config.endpoint


def test_make_cloud_provider_tuzi_configured() -> None:
    from backend.services.vlm_source_classifier import _make_cloud_provider

    env = {
        "VLM_CLOUD_TUZI_BASE_URL": "https://api.tu-zi.com/v1",
        "VLM_CLOUD_TUZI_API_KEY": "sk-tuzi",
        "VLM_CLOUD_MODEL": "gemini-3.1-pro-preview",
    }
    provider = _make_cloud_provider(env, "tuzi")
    assert provider is not None
    config = provider.configure(purpose="classifier")
    assert config.ready
    assert "tu-zi" in config.endpoint


def test_make_cloud_provider_missing_key_returns_none() -> None:
    from backend.services.vlm_source_classifier import _make_cloud_provider

    env = {"VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1"}
    assert _make_cloud_provider(env, "flash") is None


def test_make_cloud_provider_unknown_tier_returns_none() -> None:
    from backend.services.vlm_source_classifier import _make_cloud_provider

    assert _make_cloud_provider({}, "unknown_tier") is None


def test_make_cloud_provider_default_model() -> None:
    from backend.services.vlm_source_classifier import _make_cloud_provider

    env = {
        "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
        "VLM_CLOUD_FLASH_API_KEY": "sk-flash",
    }
    provider = _make_cloud_provider(env, "flash")
    assert provider is not None
    config = provider.configure(purpose="classifier")
    assert config.model == "gemini-3.1-pro-preview"


# ---------------------------------------------------------------------------
# Unit tests for classify_with_escalation
# ---------------------------------------------------------------------------

def test_escalation_upgrades_low_confidence_items(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import (
        ClassificationResult,
        classify_with_escalation,
        _make_cloud_provider,
    )

    img1 = tmp_path / "high.png"
    img2 = tmp_path / "low.png"
    _write_tiny_png(img1)
    _write_tiny_png(img2)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.70, "reasoning": "local"},
        env={
            "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
            "VLM_CLOUD_FLASH_API_KEY": "sk-test",
            "VLM_CLOUD_MODEL": "gemini-3.1-pro-preview",
        },
    )

    cloud_parsed = {"phase": "after", "view": "45deg", "body_part": "face", "confidence": 0.93, "reasoning": "cloud upgrade"}

    _original_make = _make_cloud_provider.__wrapped__ if hasattr(_make_cloud_provider, "__wrapped__") else None

    cloud_calls: list[dict] = []

    from backend.services import vlm_source_classifier as mod
    original_make = mod._make_cloud_provider

    def fake_make_cloud(env, tier, **kwargs):
        if tier == "flash":
            return FakeProvider(parsed=cloud_parsed, env=env)
        return None

    mod._make_cloud_provider = fake_make_cloud
    try:
        results, tier_stats = classify_with_escalation(
            [img1, img2], local_provider, concurrency=1, timeout=5.0,
        )
    finally:
        mod._make_cloud_provider = original_make

    assert len(results) == 2
    assert all(isinstance(r, ClassificationResult) for r in results)
    assert results[0].confidence == 0.93
    assert results[0].phase == "after"
    assert results[1].confidence == 0.93

    assert len(tier_stats) == 1
    assert tier_stats[0].tier == "flash"
    assert tier_stats[0].attempted == 2
    assert tier_stats[0].upgraded == 2
    assert tier_stats[0].failed == 0


def test_escalation_skips_when_all_high_confidence(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import (
        ClassificationResult,
        classify_with_escalation,
    )

    img = tmp_path / "high.png"
    _write_tiny_png(img)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.92, "reasoning": "confident"},
        env={
            "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
            "VLM_CLOUD_FLASH_API_KEY": "sk-test",
        },
    )

    results, tier_stats = classify_with_escalation(
        [img], local_provider, concurrency=1, timeout=5.0,
    )

    assert len(results) == 1
    assert results[0].confidence == 0.92
    assert tier_stats == []


def test_escalation_skips_unconfigured_tiers(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import classify_with_escalation

    img = tmp_path / "low.png"
    _write_tiny_png(img)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.60, "reasoning": "low"},
        env={},
    )

    results, tier_stats = classify_with_escalation(
        [img], local_provider, concurrency=1, timeout=5.0,
    )

    assert len(results) == 1
    assert results[0].confidence == 0.60
    assert tier_stats == []


def test_escalation_keeps_local_when_cloud_lower_confidence(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import (
        ClassificationResult,
        classify_with_escalation,
    )
    from backend.services import vlm_source_classifier as mod

    img = tmp_path / "img.png"
    _write_tiny_png(img)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.75, "reasoning": "local mid"},
        env={
            "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
            "VLM_CLOUD_FLASH_API_KEY": "sk-test",
        },
    )

    cloud_lower = {"phase": "after", "view": "side", "body_part": "face", "confidence": 0.50, "reasoning": "cloud worse"}

    original_make = mod._make_cloud_provider

    def fake_make_cloud(env, tier, **kwargs):
        if tier == "flash":
            return FakeProvider(parsed=cloud_lower, env=env)
        return None

    mod._make_cloud_provider = fake_make_cloud
    try:
        results, tier_stats = classify_with_escalation(
            [img], local_provider, concurrency=1, timeout=5.0,
        )
    finally:
        mod._make_cloud_provider = original_make

    assert results[0].confidence == 0.75
    assert results[0].phase == "before"
    assert tier_stats[0].upgraded == 0


def test_escalation_flash_fail_falls_through_to_tuzi(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import (
        ClassificationResult,
        classify_with_escalation,
    )
    from backend.services import vlm_source_classifier as mod

    img = tmp_path / "img.png"
    _write_tiny_png(img)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.60, "reasoning": "low local"},
        env={
            "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
            "VLM_CLOUD_FLASH_API_KEY": "sk-flash",
            "VLM_CLOUD_TUZI_BASE_URL": "https://api.tu-zi.com/v1",
            "VLM_CLOUD_TUZI_API_KEY": "sk-tuzi",
            "VLM_CLOUD_MODEL": "gemini-3.1-pro-preview",
        },
    )

    flash_bad = {"phase": "after", "view": "front", "body_part": "face", "confidence": 0.55, "reasoning": "flash worse"}
    tuzi_good = {"phase": "after", "view": "45deg", "body_part": "face", "confidence": 0.91, "reasoning": "tuzi upgrade"}

    original_make = mod._make_cloud_provider

    def fake_make_cloud(env, tier, **kwargs):
        if tier == "flash":
            return FakeProvider(parsed=flash_bad, env=env)
        if tier == "tuzi":
            return FakeProvider(parsed=tuzi_good, env=env)
        return None

    mod._make_cloud_provider = fake_make_cloud
    try:
        results, tier_stats = classify_with_escalation(
            [img], local_provider, concurrency=1, timeout=5.0,
        )
    finally:
        mod._make_cloud_provider = original_make

    assert results[0].confidence == 0.91
    assert results[0].provider == "fake_local"  # from FakeProvider
    assert len(tier_stats) == 2
    assert tier_stats[0].tier == "flash"
    assert tier_stats[0].upgraded == 0
    assert tier_stats[1].tier == "tuzi"
    assert tier_stats[1].upgraded == 1


def test_escalation_cloud_error_keeps_local_result(tmp_path: Path) -> None:
    from backend.services.vlm_source_classifier import (
        ClassificationResult,
        classify_with_escalation,
    )
    from backend.services import vlm_source_classifier as mod

    img = tmp_path / "img.png"
    _write_tiny_png(img)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.60, "reasoning": "low local"},
        env={
            "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
            "VLM_CLOUD_FLASH_API_KEY": "sk-flash",
        },
    )

    class ErrorProvider(FakeProvider):
        def call_vision(self, *args, **kwargs):
            raise ConnectionError("cloud unreachable")

    original_make = mod._make_cloud_provider

    def fake_make_cloud(env, tier, **kwargs):
        if tier == "flash":
            return ErrorProvider(env=env)
        return None

    mod._make_cloud_provider = fake_make_cloud
    try:
        results, tier_stats = classify_with_escalation(
            [img], local_provider, concurrency=1, timeout=5.0,
        )
    finally:
        mod._make_cloud_provider = original_make

    assert results[0].confidence == 0.60
    assert results[0].phase == "before"
    assert tier_stats[0].failed == 1


# ---------------------------------------------------------------------------
# Integration test: run_classification with escalation
# ---------------------------------------------------------------------------

def test_run_classification_escalate_true_adds_escalation_stats(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services import vlm_source_classifier as mod
    from backend.services.vlm_source_classifier import run_classification

    _write_tiny_png(tmp_path / "img.png")
    with db.connect() as conn:
        _seed_observation(conn, root_path=str(tmp_path), image_path="img.png", confidence=0.25)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.70, "reasoning": "local"},
        env={
            "VLM_CLOUD_FLASH_BASE_URL": "https://ai.flashapi.top/v1",
            "VLM_CLOUD_FLASH_API_KEY": "sk-flash",
            "VLM_CLOUD_MODEL": "gemini-3.1-pro-preview",
        },
    )

    cloud_good = {"phase": "after", "view": "45deg", "body_part": "face", "confidence": 0.93, "reasoning": "cloud"}

    original_make = mod._make_cloud_provider

    def fake_make_cloud(env, tier, **kwargs):
        if tier == "flash":
            return FakeProvider(parsed=cloud_good, env=env)
        return None

    mod._make_cloud_provider = fake_make_cloud
    try:
        with db.connect() as conn:
            report = run_classification(
                conn,
                provider=local_provider,
                case_id=126,
                mode="apply",
                escalate=True,
            )
    finally:
        mod._make_cloud_provider = original_make

    assert report["classified_count"] == 1
    assert "escalation" in report
    assert report["escalation"][0]["tier"] == "flash"
    assert report["escalation"][0]["upgraded"] == 1


def test_run_classification_escalate_false_no_escalation_stats(temp_db: Path, tmp_path: Path) -> None:
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    _write_tiny_png(tmp_path / "img.png")
    with db.connect() as conn:
        _seed_observation(conn, root_path=str(tmp_path), image_path="img.png", confidence=0.25)

    local_provider = FakeProvider(
        parsed={"phase": "before", "view": "front", "body_part": "face", "confidence": 0.92, "reasoning": "good"},
        env={},
    )

    with db.connect() as conn:
        report = run_classification(
            conn,
            provider=local_provider,
            case_id=126,
            mode="apply",
            escalate=False,
        )

    assert report["classified_count"] == 1
    assert "escalation" not in report
