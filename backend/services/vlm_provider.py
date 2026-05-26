"""Unified fail-closed VLM provider helpers."""
from __future__ import annotations

import base64
import email.utils
import io
import json
import mimetypes
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

OPENAI_PROVIDERS = {"openai", "openai_responses", "openai-compatible", "openai_compatible", "flashapi"}
GEMINI_PROVIDERS = {"gemini", "gemini_generate_content"}
VERTEX_PROVIDERS = {"vertex", "vertex-ai", "vertex_generate_content_adc", "google-vertex"}
VALID_PROVIDERS = OPENAI_PROVIDERS | GEMINI_PROVIDERS | VERTEX_PROVIDERS
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

PostJson = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]
TokenProvider = Callable[[], str | None]
Sleep = Callable[[float], None]
Jitter = Callable[[float], float]


class VLMRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str = ""
    endpoint: str = ""
    ready: bool = False
    status: str = "not_configured"
    decision: str = ""
    api_key: str | None = None
    project: str | None = None
    location: str | None = None
    billing_mode: str = "paygo"


@dataclass(frozen=True)
class VLMRequest:
    prompt: str
    images: list[Path]
    timeout: float = 30.0
    purpose: str = "judge"
    case_id: int | None = None
    max_dimension: int | None = None


@dataclass(frozen=True)
class VLMResponse:
    text: str
    parsed: dict[str, Any]
    provider: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    usage_raw: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None


@dataclass(frozen=True)
class _PreparedImage:
    path: Path
    mime: str
    data: bytes
    original_byte_size: int
    resized: bool = False


def _parse_retry_after(value: str | None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        retry_at = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured provider endpoint.
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        raise VLMRequestError(
            detail,
            status_code=int(exc.code),
            retry_after_seconds=_parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None),
        ) from exc
    except urllib.error.URLError as exc:
        raise VLMRequestError(str(exc), status_code=None) from exc
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _gcloud_adc_token() -> str | None:
    import subprocess

    try:
        result = subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _data_url(image: _PreparedImage) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime};base64,{encoded}"


def _inline_data(image: _PreparedImage) -> dict[str, str]:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {"mime_type": image.mime, "data": encoded}


def _vertex_inline_data(image: _PreparedImage) -> dict[str, str]:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {"mimeType": image.mime, "data": encoded}


def _extract_openai_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for output in response.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
    for choice in response.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _extract_gemini_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in response.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.lower().startswith("json"):
            value = value[4:].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            return {}
        parsed = json.loads(value[start : end + 1])
    return parsed if isinstance(parsed, dict) else {}


def _openai_responses_endpoint(env: dict[str, str], endpoint: str | None = None) -> str:
    base = (
        endpoint
        or env.get("VLM_ENDPOINT")
        or env.get("CASE_WORKBENCH_VLM_JUDGE_ENDPOINT")
        or env.get("CASE_WORKBENCH_VLM_JUDGE_BASE_URL")
        or env.get("OPENAI_BASE_URL")
        or env.get("VISION_API_BASE")
        or ""
    ).strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1/responses"
    if base.endswith("/responses"):
        return base
    return f"{base}/responses"


def _openai_chat_completions_endpoint(env: dict[str, str], endpoint: str | None = None) -> str:
    base = (
        endpoint
        or env.get("VLM_ENDPOINT")
        or env.get("CASE_WORKBENCH_VLM_JUDGE_ENDPOINT")
        or env.get("CASE_WORKBENCH_VLM_JUDGE_BASE_URL")
        or env.get("OPENAI_BASE_URL")
        or env.get("VISION_API_BASE")
        or ""
    ).strip().rstrip("/")
    if not base:
        return "https://api.openai.com/v1/chat/completions"
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def _base_looks_like_tuzi(base: str) -> bool:
    return "tu-zi" in str(base or "").lower() or "tuzi" in str(base or "").lower()


def _base_looks_like_flashapi(base: str) -> bool:
    return "flashapi" in str(base or "").lower()


def _openai_compatible_api_format(env: dict[str, str], endpoint: str) -> str:
    raw = (
        env.get("VLM_OPENAI_COMPATIBLE_API")
        or env.get("CASE_WORKBENCH_VLM_JUDGE_API_FORMAT")
        or env.get("VISION_API_FORMAT")
        or ""
    ).strip().lower()
    if raw in {"chat", "chat_completions", "chat/completions"}:
        return "chat_completions"
    if raw in {"responses", "responses_api"}:
        return "responses"
    if str(endpoint or "").rstrip("/").endswith("/chat/completions") or _base_looks_like_tuzi(endpoint):
        return "chat_completions"
    return "responses"


def _openai_compatible_endpoint(env: dict[str, str], endpoint: str | None = None) -> tuple[str, str]:
    probe_endpoint = endpoint or env.get("VLM_ENDPOINT") or env.get("CASE_WORKBENCH_VLM_JUDGE_ENDPOINT") or env.get("VISION_API_BASE") or ""
    api_format = _openai_compatible_api_format(env, probe_endpoint)
    if api_format == "chat_completions":
        return _openai_chat_completions_endpoint(env, endpoint), api_format
    return _openai_responses_endpoint(env, endpoint), api_format


def _openai_compatible_api_key(env: dict[str, str], endpoint: str) -> str:
    if _base_looks_like_tuzi(endpoint):
        return (
            env.get("VISION_PRIMARY_API_KEY")
            or env.get("GEMINI_TUZI_API_KEY")
            or env.get("TUZI_API_KEY")
            or env.get("VLM_API_KEY")
            or env.get("OPENAI_API_KEY")
            or env.get("CASE_WORKBENCH_VLM_JUDGE_API_KEY")
            or env.get("VISION_API_KEY")
            or ""
        ).strip()
    if _base_looks_like_flashapi(endpoint):
        return (
            env.get("VLM_API_KEY")
            or env.get("OPENAI_API_KEY")
            or env.get("CASE_WORKBENCH_VLM_JUDGE_API_KEY")
            or env.get("VISION_API_KEY")
            or env.get("FLASHAPI_API_KEY")
            or env.get("GEMINI_TUZI_API_KEY")
            or env.get("TUZI_API_KEY")
            or ""
        ).strip()
    return (
        env.get("VLM_API_KEY")
        or env.get("OPENAI_API_KEY")
        or env.get("CASE_WORKBENCH_VLM_JUDGE_API_KEY")
        or env.get("VISION_API_KEY")
        or ""
    ).strip()


def _estimate_input_tokens(prompt: str, images: list[_PreparedImage]) -> int:
    byte_count = len(prompt.encode("utf-8"))
    byte_count += sum(len(image.data) for image in images)
    return max(1, byte_count // 4)


def _image_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _prepare_image(path: Path, *, max_dimension: int) -> _PreparedImage:
    original_data = path.read_bytes()
    original_mime = _image_mime(path)
    if max_dimension <= 0:
        return _PreparedImage(path=path, mime=original_mime, data=original_data, original_byte_size=len(original_data))

    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        return _PreparedImage(path=path, mime=original_mime, data=original_data, original_byte_size=len(original_data))

    try:
        with Image.open(path) as opened:
            original_format = (opened.format or "").upper()
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            long_edge = max(width, height)
            if long_edge <= max_dimension:
                return _PreparedImage(path=path, mime=original_mime, data=original_data, original_byte_size=len(original_data))
            scale = float(max_dimension) / float(long_edge)
            target_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            resampling = getattr(Image, "Resampling", None)
            resample = resampling.LANCZOS if resampling is not None else Image.LANCZOS
            resized = image.resize(target_size, resample)
            output = io.BytesIO()
            if original_format == "PNG":
                save_image = resized
                if save_image.mode in {"RGBA", "LA"}:
                    background = Image.new("RGB", save_image.size, (255, 255, 255))
                    alpha = save_image.getchannel("A")
                    background.paste(save_image.convert("RGB"), mask=alpha)
                    save_image = background
                elif save_image.mode not in {"RGB", "L"}:
                    save_image = save_image.convert("RGB")
                save_image.save(output, format="JPEG", quality=60, optimize=True)
                mime = "image/jpeg"
            elif original_format == "WEBP":
                save_image = resized.convert("RGB") if resized.mode not in {"RGB", "L"} else resized
                save_image.save(output, format="WEBP", quality=85, method=6)
                mime = "image/webp"
            else:
                save_image = resized.convert("RGB") if resized.mode not in {"RGB", "L"} else resized
                save_image.save(output, format="JPEG", quality=85, optimize=True)
                mime = "image/jpeg"
            return _PreparedImage(
                path=path,
                mime=mime,
                data=output.getvalue(),
                original_byte_size=len(original_data),
                resized=True,
            )
    except (OSError, UnidentifiedImageError):
        return _PreparedImage(path=path, mime=original_mime, data=original_data, original_byte_size=len(original_data))


def _extract_usage_raw(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage")
    if isinstance(usage, dict):
        return usage
    usage_metadata = response.get("usageMetadata")
    return usage_metadata if isinstance(usage_metadata, dict) else {}


def _usage_int(usage: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = usage.get(key)
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


class VLMProvider:
    def __init__(
        self,
        env: dict[str, str] | None = None,
        *,
        post_json: PostJson = _post_json,
        token_provider: TokenProvider = _gcloud_adc_token,
        sleep: Sleep = time.sleep,
        jitter: Jitter | None = None,
    ) -> None:
        self.env = dict(env or {})
        self._post_json = post_json
        self._token_provider = token_provider
        self._sleep = sleep
        self._jitter = jitter if jitter is not None else self._default_jitter
        self._rate_lock = threading.Lock()
        self._next_allowed_request_at = 0.0

    def configure(
        self,
        provider: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> ProviderConfig:
        env = self.env
        selected_provider = (
            provider
            or env.get("VLM_PROVIDER")
            or env.get("CASE_WORKBENCH_VLM_JUDGE_PROVIDER")
            or env.get("VISION_PROVIDER")
            or ""
        ).strip().lower()
        selected_model = (
            model
            or env.get("VLM_MODEL")
            or env.get("CASE_WORKBENCH_VLM_JUDGE_MODEL")
            or env.get("VISION_API_MODEL")
            or ""
        ).strip()
        selected_endpoint = (endpoint or env.get("VLM_ENDPOINT") or env.get("CASE_WORKBENCH_VLM_JUDGE_ENDPOINT") or "").strip()

        if not selected_provider:
            return ProviderConfig(provider="", model=selected_model, status="blocked_missing_vlm_provider_config")
        if selected_provider not in VALID_PROVIDERS:
            return ProviderConfig(provider=selected_provider, model=selected_model, status="blocked_unsupported_vlm_provider")
        if not selected_model:
            return ProviderConfig(provider=selected_provider, status="blocked_missing_vlm_model_config")

        if selected_provider in VERTEX_PROVIDERS:
            project = (
                env.get("VLM_PROJECT")
                or env.get("CASE_WORKBENCH_VERTEX_PROJECT")
                or env.get("CASE_LAYOUT_VERTEX_PROJECT")
                or env.get("GOOGLE_CLOUD_PROJECT")
                or env.get("GCLOUD_PROJECT")
                or ""
            ).strip()
            location = (
                env.get("VLM_LOCATION")
                or env.get("CASE_WORKBENCH_VERTEX_LOCATION")
                or env.get("CASE_LAYOUT_VERTEX_LOCATION")
                or env.get("GOOGLE_CLOUD_LOCATION")
                or ""
            ).strip()
            request_type = (
                env.get("VLM_REQUEST_TYPE")
                or env.get("CASE_WORKBENCH_VERTEX_REQUEST_TYPE")
                or env.get("CASE_LAYOUT_VERTEX_REQUEST_TYPE")
                or env.get("VERTEX_AI_LLM_REQUEST_TYPE")
                or ""
            ).strip().lower()
            billing_mode = "provisioned_throughput" if request_type in {"dedicated", "provisioned_throughput", "pt"} else "paygo"
            if not project:
                return ProviderConfig(
                    provider="vertex_generate_content_adc",
                    model=selected_model,
                    billing_mode=billing_mode,
                    status="blocked_missing_vertex_project_config",
                )
            if not location:
                return ProviderConfig(
                    provider="vertex_generate_content_adc",
                    model=selected_model,
                    project=project,
                    billing_mode=billing_mode,
                    status="blocked_missing_vertex_location_config",
                )
            resolved_endpoint = selected_endpoint or (
                f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
                f"/publishers/google/models/{selected_model}:generateContent"
            )
            return ProviderConfig(
                provider="vertex_generate_content_adc",
                model=selected_model,
                endpoint=resolved_endpoint,
                ready=True,
                status="ready",
                project=project,
                location=location,
                billing_mode=billing_mode,
            )

        if selected_provider in GEMINI_PROVIDERS:
            api_key = (
                env.get("VLM_API_KEY")
                or env.get("CASE_WORKBENCH_VLM_JUDGE_API_KEY")
                or env.get("GEMINI_API_KEY")
                or env.get("GOOGLE_API_KEY")
                or env.get("GOOGLE_GENAI_API_KEY")
                or ""
            ).strip()
            if not api_key:
                return ProviderConfig(provider="gemini_generate_content", model=selected_model, status="blocked_missing_gemini_api_key")
            resolved_endpoint = selected_endpoint or f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent"
            return ProviderConfig(
                provider="gemini_generate_content",
                model=selected_model,
                endpoint=resolved_endpoint,
                ready=True,
                status="ready",
                api_key=api_key,
            )

        resolved_endpoint, api_format = _openai_compatible_endpoint(env, selected_endpoint or None)
        api_key = _openai_compatible_api_key(env, resolved_endpoint)
        if not api_key:
            return ProviderConfig(provider="openai_responses", model=selected_model, status="blocked_missing_openai_api_key")
        return ProviderConfig(
            provider="openai_chat_completions" if api_format == "chat_completions" else "openai_responses",
            model=selected_model,
            endpoint=resolved_endpoint,
            ready=True,
            status="ready",
            api_key=api_key,
        )

    def call_vision(
        self,
        prompt: str,
        images: list[Path],
        *,
        timeout: float = 30.0,
        max_dimension: int | None = None,
    ) -> VLMResponse:
        config = self.configure()
        if not config.ready:
            raise VLMRequestError(config.status)
        image_paths = [Path(path) for path in images]
        prepared_images = [_prepare_image(path, max_dimension=self._max_image_dimension(max_dimension)) for path in image_paths]
        payload = self._payload(config, prompt, prepared_images)
        headers = self._headers(config)
        start = time.perf_counter()
        raw = self._post_with_retry(config.endpoint, headers, payload, float(timeout))
        latency_ms = int((time.perf_counter() - start) * 1000)
        text = _extract_gemini_text(raw) if config.provider in {"gemini_generate_content", "vertex_generate_content_adc"} else _extract_openai_text(raw)
        usage_raw = _extract_usage_raw(raw)
        estimated_input_tokens = _estimate_input_tokens(prompt, prepared_images)
        input_tokens = _usage_int(
            usage_raw,
            ("input_tokens", "inputTokenCount", "prompt_tokens", "promptTokenCount", "prompt_token_count"),
        )
        output_tokens = _usage_int(
            usage_raw,
            ("output_tokens", "outputTokenCount", "completion_tokens", "candidatesTokenCount", "response_token_count"),
        )
        return VLMResponse(
            text=text,
            parsed=_parse_json_object(text),
            provider=config.provider,
            model=config.model,
            latency_ms=max(0, latency_ms),
            input_tokens=input_tokens if input_tokens is not None else estimated_input_tokens,
            output_tokens=output_tokens if output_tokens is not None else max(0, len(text.encode("utf-8")) // 4),
            usage_raw=usage_raw,
            response_id=str(raw.get("id") or raw.get("responseId") or "") or None,
        )

    def call_vision_batch(
        self,
        items: list[VLMRequest],
        *,
        concurrency: int = 3,
        return_exceptions: bool = False,
    ) -> list[VLMResponse] | list[VLMResponse | BaseException]:
        max_workers = self._effective_concurrency(concurrency)
        responses: list[VLMResponse | BaseException | None] = [None] * len(items)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self.call_vision,
                    item.prompt,
                    item.images,
                    timeout=item.timeout,
                    max_dimension=item.max_dimension,
                ): index
                for index, item in enumerate(items)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    responses[index] = future.result()
                except BaseException as exc:
                    if not return_exceptions:
                        raise
                    responses[index] = exc
        return [response for response in responses if response is not None]

    def _env_int(self, name: str, default: int) -> int:
        try:
            return int(str(self.env.get(name) or "").strip() or default)
        except ValueError:
            return default

    def _env_float(self, name: str, default: float) -> float:
        try:
            return float(str(self.env.get(name) or "").strip() or default)
        except ValueError:
            return default

    def _effective_concurrency(self, requested: int | None) -> int:
        value = max(1, int(requested or 1))
        env_cap = self._env_int("VLM_MAX_CONCURRENCY", 0)
        if env_cap > 0:
            value = min(value, env_cap)
        return max(1, value)

    def _requests_per_minute(self) -> float:
        return max(0.0, self._env_float("VLM_REQUESTS_PER_MINUTE", 0.0))

    def _throttle_rate_limit(self) -> None:
        rpm = self._requests_per_minute()
        if rpm <= 0:
            return
        interval = 60.0 / rpm
        with self._rate_lock:
            now = time.monotonic()
            if self._next_allowed_request_at > now:
                self._sleep(self._next_allowed_request_at - now)
                now = self._next_allowed_request_at
            self._next_allowed_request_at = max(now, self._next_allowed_request_at) + interval

    def _max_image_dimension(self, override: int | None = None) -> int:
        if override is not None:
            return max(0, int(override))
        raw = (
            self.env.get("VLM_MAX_IMAGE_DIMENSION")
            or self.env.get("VLM_IMAGE_MAX_DIMENSION")
            or self.env.get("CASE_WORKBENCH_VLM_MAX_IMAGE_DIMENSION")
            or ""
        ).strip()
        if not raw:
            return 1024
        try:
            return max(0, int(raw))
        except ValueError:
            return 1024

    def _headers(self, config: ProviderConfig) -> dict[str, str]:
        if config.provider == "vertex_generate_content_adc":
            token = self._token_provider()
            if not token:
                raise VLMRequestError("blocked_missing_vertex_adc_token")
            return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        if config.provider == "gemini_generate_content":
            return {"Content-Type": "application/json", "x-goog-api-key": str(config.api_key)}
        return {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"}

    def _payload(self, config: ProviderConfig, prompt: str, images: list[_PreparedImage]) -> dict[str, Any]:
        if config.provider == "vertex_generate_content_adc":
            parts: list[dict[str, Any]] = [{"text": prompt}]
            parts.extend({"inlineData": _vertex_inline_data(image)} for image in images)
            return {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        if config.provider == "gemini_generate_content":
            parts = [{"text": prompt}]
            parts.extend({"inline_data": _inline_data(image)} for image in images)
            return {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        if config.provider == "openai_chat_completions":
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            content.extend({"type": "image_url", "image_url": {"url": _data_url(image)}} for image in images)
            return {
                "model": config.model,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 800,
                "stream": False,
            }
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        content.extend({"type": "input_image", "image_url": _data_url(image)} for image in images)
        return {
            "model": config.model,
            "store": False,
            "max_output_tokens": 800,
            "text": {"format": {"type": "json_object"}},
            "input": [{"role": "user", "content": content}],
        }

    def _post_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        *,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        attempts = max(1, int(max_attempts or self._env_int("VLM_RETRY_MAX_ATTEMPTS", 3)))
        last_error: VLMRequestError | None = None
        for attempt in range(attempts):
            try:
                self._throttle_rate_limit()
                return self._post_json(url, headers, payload, timeout)
            except VLMRequestError as exc:
                last_error = exc
                if exc.status_code not in TRANSIENT_STATUS_CODES or attempt >= attempts - 1:
                    raise
                self._sleep(self._retry_delay_seconds(exc, attempt))
        if last_error:
            raise last_error
        raise VLMRequestError("vlm request failed")

    def _retry_delay_seconds(self, exc: VLMRequestError, attempt: int) -> float:
        if exc.retry_after_seconds is not None:
            return max(0.0, float(exc.retry_after_seconds))
        base = max(0.0, self._env_float("VLM_RETRY_BASE_SECONDS", 1.0))
        cap = max(base, self._env_float("VLM_RETRY_MAX_SECONDS", 30.0))
        delay = min(cap, base * (2**attempt))
        return max(0.0, delay + self._jitter(delay))

    @staticmethod
    def _default_jitter(delay: float) -> float:
        return random.uniform(0.0, min(max(0.0, delay) * 0.1, 1.0))
