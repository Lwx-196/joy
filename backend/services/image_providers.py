"""OpenAI 兼容 images/edits 多 provider 生图层（tuzi / flash / 77code …）.

三家都是 OpenAI 兼容 `POST {base}/images/edits`（multipart: image+prompt+model，Bearer 鉴权，
返回 data[0].b64_json|url）→ 一个 client 通吃，provider 用 env 配，按优先级 fallback。

provider 来源（env）：
- `tuzi`        ← 复用 `TUZI_IMAGE_PRIMARY_*`（现 = flashapi.top / gpt-image-2）
- `tuzi_legacy` ← 复用 `TUZI_IMAGE_LEGACY_*`（api.tu-zi.com）
- 新增（如 flash/code77）：`PANEL_IMG_<NAME>_BASE_URL` / `_API_KEY` / `_MODEL` / `_FORMAT`
- 优先级：`PANEL_IMAGE_PROVIDERS` CSV（默认 "tuzi"）

key 只在内存，不打印。网络调用 lazy import requests（registry 构建是纯函数，可单测）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 默认从 PANEL_ENV_FILE 取（dev 机 export 一次即可）；不硬编码个人绝对路径。
# 生产/CI 必须显式传 --env-file 或设 PANEL_ENV_FILE。
DEFAULT_ENV_FILE = os.environ.get("PANEL_ENV_FILE", "")
DEFAULT_TIMEOUT_MS = 120_000


@dataclass(frozen=True)
class ImageProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    api_format: str = "images_edit"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    project: str = ""   # vertex_generate_content 专用
    location: str = ""  # vertex_generate_content 专用

    @property
    def ready(self) -> bool:
        if self.api_format == "vertex_generate_content":
            return bool(self.project and self.location and self.model)
        if self.api_format == "ai_studio_generate_content":
            return bool(self.api_key and self.model)
        return bool(self.base_url and self.api_key and self.model)


def load_env_file(path: str | None = None) -> dict[str, str]:
    """读 .env 成 dict（KEY=VALUE，跳过注释/空行）。文件不存在返回空 dict。"""
    path = path or DEFAULT_ENV_FILE
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _normalize_base(raw: str) -> str:
    text = (raw or "").strip().rstrip("/")
    if text.endswith("/chat/completions"):
        text = text[: -len("/chat/completions")]
    return text


def _seed_from_prefix(env: dict[str, str], name: str, prefix: str) -> ImageProvider:
    timeout = env.get(f"{prefix}_TIMEOUT_MS")
    return ImageProvider(
        name=name,
        base_url=_normalize_base(env.get(f"{prefix}_BASE_URL", "")),
        api_key=env.get(f"{prefix}_API_KEY", ""),
        model=(env.get(f"{prefix}_MODELS") or env.get(f"{prefix}_MODEL") or "").split(",")[0].strip(),
        api_format=(env.get(f"{prefix}_API_FORMAT", "images_edit") or "images_edit").strip().lower(),
        timeout_ms=int(timeout) if timeout and timeout.isdigit() else DEFAULT_TIMEOUT_MS,
    )


def build_registry(env: dict[str, str]) -> dict[str, ImageProvider]:
    """从 env 构建 provider 注册表（含未就绪的，ready 标记是否可用）。"""
    reg: dict[str, ImageProvider] = {}
    # 复用现有 tuzi 配置
    reg["tuzi"] = _seed_from_prefix(env, "tuzi", "TUZI_IMAGE_PRIMARY")
    reg["tuzi_legacy"] = _seed_from_prefix(env, "tuzi_legacy", "TUZI_IMAGE_LEGACY")
    # 通用扩展：PANEL_IMG_<NAME>_*  （NAME 取小写）
    names: set[str] = set()
    for k in env:
        if k.startswith("PANEL_IMG_") and k.endswith("_BASE_URL"):
            names.add(k[len("PANEL_IMG_"):-len("_BASE_URL")])
    for upper in names:
        reg[upper.lower()] = _seed_from_prefix(env, upper.lower(), f"PANEL_IMG_{upper}")
    # Google AI Studio 出图（gemini image via generativelanguage API key 认证）
    ai_studio_key = (env.get("GOOGLE_GENAI_API_KEY") or env.get("GEMINI_API_KEY") or "").strip()
    ai_studio_model = (env.get("AI_STUDIO_IMAGE_MODEL") or "gemini-3-pro-image").strip()
    if ai_studio_key:
        reg["ai_studio"] = ImageProvider(
            name="ai_studio", base_url="", api_key=ai_studio_key, model=ai_studio_model,
            api_format="ai_studio_generate_content",
        )
    # Vertex ADC 出图（gemini image via generateContent）——GCP 受限时暂停。
    vertex_projects_csv = env.get("VERTEX_IMAGE_PROJECTS", "")
    vertex_project = (env.get("CASE_WORKBENCH_VERTEX_PROJECT") or env.get("VLM_PROJECT") or "").strip()
    if vertex_projects_csv.strip() or vertex_project:
        first_project = vertex_projects_csv.split(",")[0].strip() if vertex_projects_csv.strip() else vertex_project
        vertex_location = (env.get("CASE_WORKBENCH_VERTEX_IMAGE_LOCATION") or env.get("CASE_WORKBENCH_VERTEX_LOCATION") or env.get("VLM_LOCATION") or "global").strip()
        vertex_model = (env.get("CASE_WORKBENCH_VERTEX_IMAGE_MODEL") or "gemini-3-pro-image-preview").strip()
        reg["vertex"] = ImageProvider(
            name="vertex", base_url="", api_key="", model=vertex_model,
            api_format="vertex_generate_content",
            project=first_project, location=vertex_location,
        )
    return reg


def resolve_chain(env: dict[str, str], explicit: list[str] | None = None) -> list[ImageProvider]:
    """按优先级返回**就绪**的 provider 列表。explicit > PANEL_IMAGE_PROVIDERS > ['tuzi']。"""
    reg = build_registry(env)
    if explicit:
        order = explicit
    else:
        csv = env.get("PANEL_IMAGE_PROVIDERS", "tuzi")
        order = [x.strip() for x in csv.split(",") if x.strip()]
    chain = [reg[n] for n in order if n in reg and reg[n].ready]
    return chain


_ADC_TOKEN_CACHE: dict[str, object] = {}
_ADC_TOKEN_TTL_S = 2400.0  # 40 分钟（ADC token 有效期 ~1h，留余量）


def _adc_token(*, force_refresh: bool = False) -> str:
    """In-process ADC access token via google-auth（不打印）。进程内缓存 40min + 瞬时失败重试 3 次。

    取代 gcloud 子进程（spawn + 30s 超时 + 偶发刷新失败 + 并发锁竞争）：用 google-auth 在进程内
    解析 ADC 凭证一次并缓存，token 由库内 refresh 签发。契约不变（force_refresh / 缓存 / 抛错）。
    """
    import time

    cached = _ADC_TOKEN_CACHE.get("token")
    if cached and not force_refresh and (time.monotonic() - float(_ADC_TOKEN_CACHE.get("ts", 0.0))) < _ADC_TOKEN_TTL_S:
        return str(cached)

    last_err: Exception | None = None
    for _attempt in (1, 2, 3):
        try:
            import google.auth
            from google.auth.transport.requests import Request

            creds = _ADC_TOKEN_CACHE.get("creds")
            if creds is None:
                creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                _ADC_TOKEN_CACHE["creds"] = creds
            creds.refresh(Request())
            token = creds.token
            if token:
                _ADC_TOKEN_CACHE["token"] = token
                _ADC_TOKEN_CACHE["ts"] = time.monotonic()
                return str(token)
            last_err = RuntimeError("google-auth 返回空 token")
        except Exception as exc:  # noqa: BLE001 — 凭证/刷新瞬时失败，丢弃缓存凭证重试再换
            last_err = exc
            _ADC_TOKEN_CACHE.pop("creds", None)
        time.sleep(1.5)
    raise RuntimeError(f"vertex: 无法获取 ADC token（重试 3 次后）: {last_err}")


def vertex_generate_content(provider: ImageProvider, image_bytes: bytes, prompt: str,
                            *, mime: str = "image/png") -> bytes:
    """Vertex ADC gemini 出图（img2img via generateContent）。返回生成图字节，失败抛异常。

    aiplatform 端实测 ~1/3 概率瞬时掉线（SSL EOF / RemoteDisconnected）→ 内置退避
    重试 4 次（2/4/8s），把单次 ~67% 成功率拉到 ~99%。401 刷 token、5xx/429 退避重试、
    4xx 直接抛（真错误如 model 不存在）。
    """
    import base64
    import time

    import requests

    token = _adc_token()
    host = ("aiplatform.googleapis.com" if provider.location == "global"
            else f"{provider.location}-aiplatform.googleapis.com")
    url = (f"https://{host}/v1beta1/projects/{provider.project}/locations/{provider.location}"
           f"/publishers/google/models/{provider.model}:generateContent")
    payload = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(image_bytes).decode("ascii")}},
            {"text": prompt},
        ]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    timeout_s = max(provider.timeout_ms, 180_000) / 1000.0
    last_err: Exception | None = None
    for attempt in range(1, 5):
        session = requests.Session()
        session.trust_env = False
        try:
            r = session.post(url, headers={"Authorization": f"Bearer {token}"},
                             json=payload, timeout=timeout_s)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.SSLError) as exc:  # 瞬时掉线 → 退避重试
            last_err = exc
            time.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code == 401:  # token 过期 → 刷新（不计退避）
            token = _adc_token(force_refresh=True)
            last_err = RuntimeError("401 token expired")
            continue
        if r.status_code in (408, 429, 500, 502, 503, 504):  # 瞬时服务端 → 退避重试
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            time.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{provider.name} generateContent HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        for cand in data.get("candidates", []):
            for part in (cand.get("content", {}).get("parts") or []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        last_err = RuntimeError(f"无 image part（偶发 text-only）: {str(data)[:160]}")
        time.sleep(2)
    raise RuntimeError(f"{provider.name} generateContent 重试 4 次仍失败: {last_err}")


def ai_studio_generate_content(provider: ImageProvider, image_bytes: bytes, prompt: str,
                               *, mime: str = "image/png") -> bytes:
    """Google AI Studio gemini 出图（API key 认证，generativelanguage 端点）。"""
    import base64
    import time

    import requests

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{provider.model}:generateContent?key={provider.api_key}")
    payload = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(image_bytes).decode("ascii")}},
            {"text": prompt},
        ]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    timeout_s = max(provider.timeout_ms, 180_000) / 1000.0
    last_err: Exception | None = None
    for attempt in range(1, 5):
        session = requests.Session()
        session.trust_env = False
        try:
            r = session.post(url, json=payload, timeout=timeout_s)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.SSLError) as exc:
            last_err = exc
            time.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code in (408, 429, 500, 502, 503, 504):
            last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
            time.sleep(min(2 ** attempt, 10))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"ai_studio generateContent HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        for cand in data.get("candidates", []):
            for part in (cand.get("content", {}).get("parts") or []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"])
        last_err = RuntimeError(f"无 image part: {str(data)[:160]}")
        time.sleep(2)
    raise RuntimeError(f"ai_studio generateContent 重试 4 次仍失败: {last_err}")


def images_edit(provider: ImageProvider, image_bytes: bytes, prompt: str,
                *, mime: str = "image/jpeg") -> bytes:
    """单次生图调用，返回生成图字节。失败抛异常。按 api_format 分派。"""
    if provider.api_format == "ai_studio_generate_content":
        return ai_studio_generate_content(provider, image_bytes, prompt, mime=mime)
    if provider.api_format == "vertex_generate_content":
        return vertex_generate_content(provider, image_bytes, prompt, mime=mime)

    import requests

    files = {"image": ("input.jpg", image_bytes, mime)}
    data = {"prompt": prompt, "model": provider.model, "n": "1"}
    # img2img 生图慢（gpt-image-2 实测 ~70-120s）→ 给 ≥180s，盖过 .env 里偏小的 chat 超时
    timeout_s = max(provider.timeout_ms, 180_000) / 1000.0
    session = requests.Session()
    session.trust_env = False
    r = session.post(
        f"{provider.base_url}/images/edits",
        headers={"Authorization": f"Bearer {provider.api_key}"},
        files=files, data=data, timeout=timeout_s,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{provider.name} images/edits HTTP {r.status_code}: {r.text[:300]}")
    item = (r.json().get("data") or [{}])[0]
    if item.get("b64_json"):
        import base64
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        dl = session.get(item["url"], timeout=60)
        dl.raise_for_status()
        return dl.content
    raise RuntimeError(f"{provider.name} returned neither b64_json nor url")


def generate_with_fallback(providers: list[ImageProvider], image_bytes: bytes, prompt: str,
                           *, mime: str = "image/jpeg") -> tuple[bytes, str]:
    """按链 fallback；返回 (图字节, provider 名)。全失败抛最后异常。"""
    if not providers:
        raise RuntimeError("no ready image provider (check env: TUZI_IMAGE_PRIMARY_* / PANEL_IMG_*)")
    last: Exception | None = None
    for p in providers:
        for attempt in (1, 2):   # 瞬时网络/代理错误重试一次再换 provider
            try:
                return images_edit(p, image_bytes, prompt, mime=mime), p.name
            except Exception as e:  # noqa: BLE001 — 逐家试，记下最后一个
                last = e
    raise RuntimeError(f"all providers failed; last={last}")


__all__ = [
    "ImageProvider", "load_env_file", "build_registry", "resolve_chain",
    "images_edit", "vertex_generate_content", "generate_with_fallback", "DEFAULT_ENV_FILE",
]
