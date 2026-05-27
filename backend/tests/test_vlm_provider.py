"""Unified VLM provider behavior."""
from __future__ import annotations

import base64
import io
import json
import threading
import time
from pathlib import Path


def _write_tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def test_provider_prefers_new_vlm_env_and_normalizes_vertex() -> None:
    from backend.services.vlm_provider import VLMProvider

    provider = VLMProvider(
        env={
            "VLM_PROVIDER": "vertex",
            "VLM_MODEL": "gemini-2.5-flash",
            "GOOGLE_CLOUD_PROJECT": "project-id",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "CASE_WORKBENCH_VLM_JUDGE_PROVIDER": "flashapi",
            "CASE_WORKBENCH_VLM_JUDGE_MODEL": "legacy-model",
            "VISION_API_KEY": "legacy-key",
        }
    )

    config = provider.configure()

    assert config.ready is True
    assert config.provider == "vertex_generate_content_adc"
    assert config.model == "gemini-2.5-flash"
    assert config.project == "project-id"
    assert config.location == "us-central1"
    assert config.billing_mode == "paygo"
    assert "publishers/google/models/gemini-2.5-flash:generateContent" in config.endpoint


def test_provider_reuses_legacy_flashapi_as_openai_responses() -> None:
    from backend.services.vlm_provider import VLMProvider

    config = VLMProvider(
        env={
            "VISION_PROVIDER": "flashapi",
            "VISION_API_BASE": "https://api.tu-zi.com/v1",
            "VISION_API_KEY": "vision-key",
            "VISION_API_MODEL": "gpt-5.4-mini",
        }
    ).configure()

    assert config.ready is True
    assert config.provider == "openai_responses"
    assert config.model == "gpt-5.4-mini"
    assert config.endpoint == "https://api.tu-zi.com/v1/responses"
    assert config.api_key == "vision-key"


def test_call_vision_retries_transient_errors_and_parses_json(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMProvider, VLMRequestError

    image = tmp_path / "probe.png"
    _write_tiny_png(image)
    calls: list[dict] = []

    def post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        if len(calls) < 3:
            raise VLMRequestError("rate limited", status_code=429)
        return {"output_text": json.dumps({"ok": True, "attempts": len(calls)})}

    provider = VLMProvider(
        env={"VLM_PROVIDER": "openai", "VLM_MODEL": "gpt-4.1-mini", "VLM_API_KEY": "unit-key"},
        post_json=post_json,
        sleep=lambda _seconds: None,
    )

    response = provider.call_vision("return strict json", [image], timeout=12.5)

    assert len(calls) == 3
    assert response.parsed == {"ok": True, "attempts": 3}
    assert response.provider == "openai_responses"
    assert response.model == "gpt-4.1-mini"
    assert response.latency_ms >= 0
    assert response.input_tokens > 0


def test_call_vision_honors_retry_after_and_retry_env(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMProvider, VLMRequestError

    image = tmp_path / "probe.png"
    _write_tiny_png(image)
    calls = 0
    sleeps: list[float] = []

    def post_json(_url: str, _headers: dict[str, str], _payload: dict, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise VLMRequestError("quota", status_code=429, retry_after_seconds=1.25)
        return {"output_text": json.dumps({"ok": True, "calls": calls})}

    provider = VLMProvider(
        env={
            "VLM_PROVIDER": "openai",
            "VLM_MODEL": "gpt-4.1-mini",
            "VLM_API_KEY": "unit-key",
            "VLM_RETRY_MAX_ATTEMPTS": "3",
            "VLM_RETRY_BASE_SECONDS": "0.5",
            "VLM_RETRY_MAX_SECONDS": "5",
        },
        post_json=post_json,
        sleep=sleeps.append,
        jitter=lambda _delay: 0.0,
    )

    response = provider.call_vision("return strict json", [image])

    assert response.parsed == {"ok": True, "calls": 3}
    assert sleeps == [1.25, 1.25]


def test_call_vision_uses_capped_exponential_backoff_with_jitter(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMProvider, VLMRequestError

    image = tmp_path / "probe.png"
    _write_tiny_png(image)
    calls = 0
    sleeps: list[float] = []

    def post_json(_url: str, _headers: dict[str, str], _payload: dict, _timeout: float) -> dict:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise VLMRequestError("server busy", status_code=503)
        return {"output_text": json.dumps({"ok": True})}

    provider = VLMProvider(
        env={
            "VLM_PROVIDER": "openai",
            "VLM_MODEL": "gpt-4.1-mini",
            "VLM_API_KEY": "unit-key",
            "VLM_RETRY_MAX_ATTEMPTS": "4",
            "VLM_RETRY_BASE_SECONDS": "0.5",
            "VLM_RETRY_MAX_SECONDS": "0.75",
        },
        post_json=post_json,
        sleep=sleeps.append,
        jitter=lambda delay: round(delay * 0.1, 3),
    )

    provider.call_vision("return strict json", [image])

    assert sleeps == [0.55, 0.825, 0.825]


def test_call_vision_batch_limits_concurrency_and_preserves_order(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMProvider, VLMRequest

    image = tmp_path / "probe.png"
    _write_tiny_png(image)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def post_json(_url: str, _headers: dict[str, str], payload: dict, _timeout: float) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        prompt = payload["input"][0]["content"][0]["text"]
        return {"output_text": json.dumps({"prompt": prompt})}

    provider = VLMProvider(
        env={"VLM_PROVIDER": "openai", "VLM_MODEL": "gpt-4.1-mini", "VLM_API_KEY": "unit-key"},
        post_json=post_json,
    )
    requests = [VLMRequest(prompt=f"item-{index}", images=[image]) for index in range(5)]

    responses = provider.call_vision_batch(requests, concurrency=2)

    assert [response.parsed["prompt"] for response in responses] == [f"item-{index}" for index in range(5)]
    assert max_active <= 2


def test_call_vision_batch_caps_concurrency_from_env(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMProvider, VLMRequest

    image = tmp_path / "probe.png"
    _write_tiny_png(image)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def post_json(_url: str, _headers: dict[str, str], payload: dict, _timeout: float) -> dict:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        prompt = payload["input"][0]["content"][0]["text"]
        return {"output_text": json.dumps({"prompt": prompt})}

    provider = VLMProvider(
        env={
            "VLM_PROVIDER": "openai",
            "VLM_MODEL": "gpt-4.1-mini",
            "VLM_API_KEY": "unit-key",
            "VLM_MAX_CONCURRENCY": "1",
        },
        post_json=post_json,
    )
    requests = [VLMRequest(prompt=f"item-{index}", images=[image]) for index in range(4)]

    responses = provider.call_vision_batch(requests, concurrency=4)

    assert [response.parsed["prompt"] for response in responses] == [f"item-{index}" for index in range(4)]
    assert max_active == 1


def test_call_vision_batch_applies_requests_per_minute_limit(tmp_path: Path) -> None:
    from backend.services.vlm_provider import VLMProvider, VLMRequest

    image = tmp_path / "probe.png"
    _write_tiny_png(image)
    sleeps: list[float] = []

    def post_json(_url: str, _headers: dict[str, str], payload: dict, _timeout: float) -> dict:
        prompt = payload["input"][0]["content"][0]["text"]
        return {"output_text": json.dumps({"prompt": prompt})}

    provider = VLMProvider(
        env={
            "VLM_PROVIDER": "openai",
            "VLM_MODEL": "gpt-4.1-mini",
            "VLM_API_KEY": "unit-key",
            "VLM_REQUESTS_PER_MINUTE": "60",
        },
        post_json=post_json,
        sleep=sleeps.append,
    )

    provider.call_vision_batch([VLMRequest(prompt=f"item-{index}", images=[image]) for index in range(3)], concurrency=1)

    assert len(sleeps) >= 2
    assert all(seconds >= 0.9 for seconds in sleeps[:2])


def test_call_vision_resizes_large_images_before_encoding_without_touching_original(tmp_path: Path) -> None:
    from PIL import Image

    from backend.services.vlm_provider import VLMProvider

    image = tmp_path / "large.jpg"
    Image.new("RGB", (2048, 512), color=(120, 90, 60)).save(image, format="JPEG", quality=95)
    original_size = image.stat().st_size
    calls: list[dict] = []

    def post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> dict:
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
        return {"output_text": json.dumps({"ok": True})}

    provider = VLMProvider(
        env={"VLM_PROVIDER": "openai", "VLM_MODEL": "gpt-4.1-mini", "VLM_API_KEY": "unit-key"},
        post_json=post_json,
    )

    response = provider.call_vision("return strict json", [image], timeout=12.5)

    image_url = calls[0]["payload"]["input"][0]["content"][1]["image_url"]
    encoded = image_url.split(",", 1)[1]
    encoded_image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert max(encoded_image.size) == 1024
    assert encoded_image.size == (1024, 256)
    assert Image.open(image).size == (2048, 512)
    assert image.stat().st_size == original_size
    assert response.input_tokens < original_size // 4


def test_call_vision_can_disable_resize_for_cost_comparison(tmp_path: Path) -> None:
    from PIL import Image

    from backend.services.vlm_provider import VLMProvider

    image = tmp_path / "large.jpg"
    Image.new("RGB", (1200, 800), color=(10, 40, 90)).save(image, format="JPEG", quality=95)
    calls: list[dict] = []

    def post_json(_url: str, _headers: dict[str, str], payload: dict, _timeout: float) -> dict:
        calls.append(payload)
        return {"output_text": json.dumps({"ok": True})}

    provider = VLMProvider(
        env={
            "VLM_PROVIDER": "openai",
            "VLM_MODEL": "gpt-4.1-mini",
            "VLM_API_KEY": "unit-key",
            "VLM_MAX_IMAGE_DIMENSION": "0",
        },
        post_json=post_json,
    )

    provider.call_vision("return strict json", [image])

    image_url = calls[0]["input"][0]["content"][1]["image_url"]
    encoded = image_url.split(",", 1)[1]
    encoded_image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert encoded_image.size == (1200, 800)
