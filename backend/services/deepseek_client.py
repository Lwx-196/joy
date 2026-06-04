"""DeepSeek official API client for text-only execution coordination."""
from __future__ import annotations

import email.utils
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

PostJson = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]
Sleep = Callable[[float], None]
Jitter = Callable[[float], float]


class DeepSeekRequestError(RuntimeError):
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
class DeepSeekConfig:
    model: str = ""
    endpoint: str = ""
    ready: bool = False
    status: str = "not_configured"
    api_key: str | None = None


@dataclass(frozen=True)
class DeepSeekResponse:
    text: str
    parsed: dict[str, Any]
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    usage_raw: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None


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
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured API endpoint.
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        raise DeepSeekRequestError(
            detail,
            status_code=int(exc.code),
            retry_after_seconds=_parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None),
        ) from exc
    except urllib.error.URLError as exc:
        raise DeepSeekRequestError(str(exc), status_code=None) from exc
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


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


def _extract_chat_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class DeepSeekClient:
    """Small OpenAI-compatible DeepSeek client with bounded retry."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        post_json: PostJson = _post_json,
        sleep: Sleep = time.sleep,
        jitter: Jitter | None = None,
    ) -> None:
        import os

        self.env = dict(os.environ if env is None else env)
        self._post_json = post_json
        self._sleep = sleep
        self._jitter = jitter or (lambda delay: delay + random.uniform(0.0, min(1.0, delay * 0.25)))

    def configure(self, *, model: str | None = None, base_url: str | None = None) -> DeepSeekConfig:
        selected_model = (model or self.env.get("DEEPSEEK_MODEL") or DEFAULT_MODEL).strip()
        selected_base = (base_url or self.env.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
        endpoint = f"{selected_base}/chat/completions"
        api_key = (self.env.get("DEEPSEEK_API_KEY") or "").strip()
        if not selected_model:
            return DeepSeekConfig(model="", endpoint=endpoint, status="blocked_missing_deepseek_model")
        if not api_key:
            return DeepSeekConfig(model=selected_model, endpoint=endpoint, status="blocked_missing_deepseek_api_key")
        return DeepSeekConfig(model=selected_model, endpoint=endpoint, ready=True, status="ready", api_key=api_key)

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> DeepSeekResponse:
        config = self.configure(model=model, base_url=base_url)
        if not config.ready or not config.api_key:
            raise DeepSeekRequestError(config.status)

        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        raw = self._call_with_retry(config.endpoint, headers, payload, timeout)
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = _extract_chat_text(raw)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        return DeepSeekResponse(
            text=text,
            parsed=_parse_json_object(text),
            model=config.model,
            latency_ms=latency_ms,
            input_tokens=_int_or_zero(usage.get("prompt_tokens")),
            output_tokens=_int_or_zero(usage.get("completion_tokens")),
            usage_raw=usage,
            response_id=str(raw.get("id")) if raw.get("id") else None,
        )

    def _call_with_retry(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        attempts = _int_or_zero(self.env.get("DEEPSEEK_RETRY_MAX_ATTEMPTS") or 3) or 1
        base_seconds = float(self.env.get("DEEPSEEK_RETRY_BASE_SECONDS") or 1.0)
        max_seconds = float(self.env.get("DEEPSEEK_RETRY_MAX_SECONDS") or 30.0)
        last_error: DeepSeekRequestError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._post_json(url, headers, payload, timeout)
            except DeepSeekRequestError as exc:
                last_error = exc
                if exc.status_code not in TRANSIENT_STATUS_CODES or attempt >= attempts:
                    raise
                retry_after = exc.retry_after_seconds
                if retry_after is None:
                    retry_after = min(max_seconds, base_seconds * (2 ** (attempt - 1)))
                self._sleep(self._jitter(min(max_seconds, retry_after)))
        raise last_error or DeepSeekRequestError("deepseek_request_failed")
