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

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

# 默认从 PANEL_ENV_FILE 取（dev 机 export 一次即可）；不硬编码个人绝对路径。
# 生产/CI 必须显式传 --env-file 或设 PANEL_ENV_FILE。
DEFAULT_ENV_FILE = os.environ.get("PANEL_ENV_FILE", "")
DEFAULT_TIMEOUT_MS = 120_000

logger = logging.getLogger(__name__)


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
    # 请求分辨率降级链（owner 2026-06-11 拍板 4K 优先）：依次尝试，被上游尺寸校验拒则降档，
    # 末档恒为「不传 size」=旧行为。空 tuple = 该 provider 不传 size（模型自定分辨率）。
    # rsta 实测边界：最长边 ≤3840 且总像素 ≤~8.3MP；方形输入安全最大 2560x2560（2880² 中转掐死）。
    sizes: tuple[str, ...] = ()
    quality: str = ""   # gpt-image 系 quality 参数（如 "high"）；空=不传，随 sizes 档一起发
    # stream=true（SSE）破 rsta 反代 60s read timeout（2026-06-12 探针实锤：同步模式生成期
    # 零字节 → 60.2-60.3s 整被掐；stream 模式首事件 12.9s、keepalive 每 10-30s 重置反代计时，
    # 122s 拿到完整 2048² b64）。不进 AI cache key——只改传输方式，不影响产物语义。
    stream: bool = False

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
    sizes = tuple(s.strip() for s in env.get(f"{prefix}_SIZES", "").split(",") if s.strip())
    return ImageProvider(
        name=name,
        base_url=_normalize_base(env.get(f"{prefix}_BASE_URL", "")),
        api_key=env.get(f"{prefix}_API_KEY", ""),
        model=(env.get(f"{prefix}_MODELS") or env.get(f"{prefix}_MODEL") or "").split(",")[0].strip(),
        api_format=(env.get(f"{prefix}_API_FORMAT", "images_edit") or "images_edit").strip().lower(),
        timeout_ms=int(timeout) if timeout and timeout.isdigit() else DEFAULT_TIMEOUT_MS,
        sizes=sizes,
        quality=env.get(f"{prefix}_QUALITY", "").strip(),
        stream=env.get(f"{prefix}_STREAM", "").strip().lower() in ("1", "true", "yes"),
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
                *, mime: str = "image/jpeg", size_override: str | None = None) -> bytes:
    """单次生图调用，返回生成图字节。失败抛异常。按 api_format 分派。

    size_override：每图动态尺寸（自适应 4K 用）。给定时忽略 provider.sizes，只发该尺寸
    （仍带 provider.quality）+ 末档无 size 兜底。默认 None = 沿用 provider.sizes 旧行为。
    """
    if provider.api_format == "ai_studio_generate_content":
        return ai_studio_generate_content(provider, image_bytes, prompt, mime=mime)
    if provider.api_format == "vertex_generate_content":
        return vertex_generate_content(provider, image_bytes, prompt, mime=mime)

    import requests

    # img2img 生图慢（gpt-image-2 实测 ~70-120s）→ 给 ≥180s，盖过 .env 里偏小的 chat 超时
    timeout_s = max(provider.timeout_ms, 180_000) / 1000.0
    session = requests.Session()
    session.trust_env = False
    last_size_err: RuntimeError | None = None
    for extra in _size_rungs(provider, size_override):
        files = {"image": ("input.jpg", image_bytes, mime)}
        data = {"prompt": prompt, "model": provider.model, "n": "1", **extra}
        if provider.stream:
            data["stream"] = "true"
        r = session.post(
            f"{provider.base_url}/images/edits",
            headers={"Authorization": f"Bearer {provider.api_key}"},
            files=files, data=data, timeout=timeout_s, stream=provider.stream,
        )
        if r.status_code != 200:
            err = RuntimeError(f"{provider.name} images/edits HTTP {r.status_code}: {r.text[:300]}")
            # 尺寸被上游校验拒（image_generation_user_error）→ 降一档重试；
            # 其它错误（鉴权/超时/5xx）直接抛给 generate_with_fallback 换 provider。
            if extra and _is_size_rejection(r.status_code, r.text):
                last_size_err = err
                continue
            raise err
        if provider.stream and "text/event-stream" in (r.headers.get("content-type") or ""):
            # 墙钟护栏：keepalive 每 10-30s 重置 read timeout → 仅靠 requests timeout 永不触发，
            # 上游挂死时单槽可阻塞整夜（2026-06-12 首槽 648s 实锤）。墙钟必须独立兜底。
            b64 = _sse_collect_b64(r.iter_lines(),
                                   deadline=time.monotonic() + _STREAM_WALL_CAP_S)
            if b64:
                import base64
                return base64.b64decode(b64)
            raise RuntimeError(f"{provider.name} images/edits stream ended without b64_json")
        # stream 开但上游忽略参数返 JSON（或 stream 关）→ 走原同步解析
        item = (r.json().get("data") or [{}])[0]
        if item.get("b64_json"):
            import base64
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            dl = session.get(item["url"], timeout=60)
            dl.raise_for_status()
            return dl.content
        raise RuntimeError(f"{provider.name} returned neither b64_json nor url")
    raise last_size_err or RuntimeError(f"{provider.name} images/edits: no size rung succeeded")


def _size_rungs(provider: ImageProvider, size_override: str | None = None) -> list[dict[str, str]]:
    """请求参数降级链（纯函数可单测）：每个显式 size 一档（带 quality），末档空 dict = 不传
    size/quality 的旧行为，保证任何 sizes 配置下最终都能回到模型自定分辨率。

    size_override 给定时只用该尺寸（忽略 provider.sizes），用于自适应 4K 每图动态尺寸。
    """
    sizes = (size_override,) if size_override else provider.sizes
    rungs: list[dict[str, str]] = []
    for s in sizes:
        extra = {"size": s}
        if provider.quality:
            extra["quality"] = provider.quality
        rungs.append(extra)
    rungs.append({})
    return rungs


def _is_size_rejection(status_code: int, body: str) -> bool:
    """上游尺寸校验拒（rsta 实测 400 + image_generation_user_error / Invalid size / pixel budget）。"""
    if status_code != 400:
        return False
    t = (body or "").lower()
    return "invalid size" in t or "pixel budget" in t or "image_generation_user_error" in t


# stream 单次调用墙钟上限：2× 最长成功观测（2048² stream 134s）。超限抛 TimeoutError
# → generate_with_fallback 正常换 attempt/provider，不让单槽拖死批量。
_STREAM_WALL_CAP_S = 300.0


def _sse_collect_b64(lines, *, deadline: float | None = None) -> str | None:
    """从 SSE 字节行流收集最终图 b64（纯函数可单测）。

    rsta stream=true 实测形态（2026-06-12 探针）：keepalive 注释行 b":" 维持连接；data 行
    兼容两形态——顶层 b64_json（partial/final chat 风格）或 type=*completed 事件。
    completed / [DONE] 提前收口；流自然结束取最后一次见到的 b64。无图返回 None。
    deadline（time.monotonic() 刻度）超限抛 TimeoutError——keepalive 重置 read timeout，
    墙钟是唯一能兜住「连接活着但永远不出图」的闸。
    """
    import json

    final_b64: str | None = None
    for line in lines:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(
                f"SSE stream wall-clock cap exceeded ({_STREAM_WALL_CAP_S:.0f}s), no final image")
        if not line or not line.startswith(b"data:"):
            continue  # 空行 / b":" keepalive / event: 行
        payload = line[len(b"data:"):].strip()
        if payload == b"[DONE]":
            break
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("error") or obj.get("type") == "error":
            # rsta 实测会把上游错误以 SSE error 事件透传（2026-06-12 探针：server_error）
            # → 浮出错误详情抛给 fallback 链，而非静默 None 丢失诊断信息。
            raise RuntimeError(f"SSE error event: {json.dumps(obj.get('error') or obj)[:300]}")
        if obj.get("b64_json"):
            final_b64 = obj["b64_json"]
        if str(obj.get("type", "")).endswith("completed"):
            final_b64 = obj.get("b64_json") or final_b64
            break
    return final_b64


class ProviderOutageError(RuntimeError):
    """所有 provider 都因 outage-class 错误（HTTP 5xx / SSE server_error / 墙钟 cap timeout）
    失败 → 上浮给执行器/前端展示「服务商暂不可用」清晰提示，而非泛化「all providers failed」。"""


# provider outage 短路守卫（2026-06-14 F3）：rsta 拥塞高峰时单 provider 每槽 attempt×2 各 ~150s
# 空跑，一批 N 槽 = N×~300s 死等。模块级计数（单进程 run 内有效，跨 generate_with_fallback 调用
# 累积）：某 provider 连续 outage-class 失败达阈值 → 后续槽直接跳过它走 fallback；成功即清零；
# 最近失败超出窗口自动失效（不毒化后续健康期）。零行为改变 = provider 健康时与旧逻辑完全一致。
_PROVIDER_OUTAGE: dict[str, dict[str, float]] = {}
_OUTAGE_FAIL_THRESHOLD = 2      # 连续 outage 失败次数达此值 → 短路跳过
_OUTAGE_WINDOW_S = 120.0        # 最近一次 outage 失败须在此窗口内才算「仍 outage」


def _is_outage_error(exc: Exception) -> bool:
    """outage-class 错误（provider 侧不可用）：HTTP 5xx / SSE server_error 事件 / 墙钟 cap
    timeout。区别于尺寸校验拒（4xx，images_edit 内部换档不外抛）与瞬时网络抖动（值得重试一次）。"""
    if isinstance(exc, TimeoutError):
        return True  # SSE 墙钟 cap = 连接活着但上游永不出图
    s = str(exc).lower()
    return ("server_error" in s
            or "http 500" in s or "http 502" in s or "http 503" in s or "http 504" in s
            or "wall-clock cap" in s)


def _mark_outage(name: str, now: float) -> None:
    rec = _PROVIDER_OUTAGE.get(name)
    if rec and now - rec["last_ts"] <= _OUTAGE_WINDOW_S:
        rec["count"] += 1
        rec["last_ts"] = now
    else:
        _PROVIDER_OUTAGE[name] = {"count": 1.0, "last_ts": now}


def _is_outaged(name: str, now: float) -> bool:
    rec = _PROVIDER_OUTAGE.get(name)
    return bool(rec and rec["count"] >= _OUTAGE_FAIL_THRESHOLD
               and now - rec["last_ts"] <= _OUTAGE_WINDOW_S)


def _clear_outage(name: str) -> None:
    _PROVIDER_OUTAGE.pop(name, None)


def generate_with_fallback(providers: list[ImageProvider], image_bytes: bytes, prompt: str,
                           *, mime: str = "image/jpeg", size_override: str | None = None,
                           progress_callback: Callable[[dict[str, object]], None] | None = None) -> tuple[bytes, str]:
    """按链 fallback；返回 (图字节, provider 名)。全失败抛最后异常。

    size_override 透传给 images_edit（自适应 4K 每图动态尺寸）。默认 None = 旧行为。
    outage 短路（F3）：近期连续 outage-class 失败的 provider 整段跳过；全 outage 抛 ProviderOutageError。
    """
    if not providers:
        raise RuntimeError("no ready image provider (check env: TUZI_IMAGE_PRIMARY_* / PANEL_IMG_*)")
    last: Exception | None = None
    saw_outage = False
    for p in providers:
        if _is_outaged(p.name, time.time()):
            # circuit-breaker：近窗口内已连续 outage → 不再每槽空跑 ~300s，直接换 provider。
            logger.warning("images_edit %s 跳过（outage 短路：近 %.0fs 内连续 ≥%d 次 outage 失败）",
                           p.name, _OUTAGE_WINDOW_S, _OUTAGE_FAIL_THRESHOLD)
            last = last or ProviderOutageError(f"{p.name} skipped (outage circuit-breaker)")
            saw_outage = True
            if progress_callback:
                progress_callback({
                    "event": "provider_skipped",
                    "provider": p.name,
                    "reason": "outage_circuit_breaker",
                })
            continue
        for attempt in (1, 2):   # 瞬时网络/代理错误重试一次再换 provider
            t0 = time.time()
            try:
                if progress_callback:
                    progress_callback({
                        "event": "provider_attempt_start",
                        "provider": p.name,
                        "attempt": attempt,
                        "api_format": p.api_format,
                        "stream": bool(p.stream),
                        "size_override": size_override,
                    })
                result = images_edit(p, image_bytes, prompt, mime=mime, size_override=size_override), p.name
                _clear_outage(p.name)   # 成功 = provider 恢复，清零毒化计数
                if progress_callback:
                    progress_callback({
                        "event": "provider_attempt_success",
                        "provider": p.name,
                        "attempt": attempt,
                        "elapsed_s": round(time.time() - t0, 3),
                    })
                return result
            except Exception as e:  # noqa: BLE001 — 逐家试，记下最后一个；
                # 失败必留痕（2026-06-12：刘亦卿槽 648s 静默降级 flashapi 低清，无日志无法归因）
                last = e
                logger.warning("images_edit %s attempt %d 失败 (%.1fs): %s",
                               p.name, attempt, time.time() - t0, str(e)[:200])
                if progress_callback:
                    progress_callback({
                        "event": "provider_attempt_failed",
                        "provider": p.name,
                        "attempt": attempt,
                        "elapsed_s": round(time.time() - t0, 3),
                        "error": str(e)[:500],
                        "error_type": type(e).__name__,
                    })
                if _is_outage_error(e):
                    # outage 类（5xx/server_error/墙钟）重试 2nd attempt 也是死等 ~150s → 记账后立刻换 provider。
                    saw_outage = True
                    _mark_outage(p.name, time.time())
                    break
    if saw_outage:
        raise ProviderOutageError(f"all providers unavailable (outage); last={last}")
    raise RuntimeError(f"all providers failed; last={last}")


__all__ = [
    "ImageProvider", "load_env_file", "build_registry", "resolve_chain",
    "images_edit", "vertex_generate_content", "generate_with_fallback", "DEFAULT_ENV_FILE",
    "ProviderOutageError",
]
