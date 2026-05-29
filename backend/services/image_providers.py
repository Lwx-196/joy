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

DEFAULT_ENV_FILE = "/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env"
DEFAULT_TIMEOUT_MS = 120_000


@dataclass(frozen=True)
class ImageProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    api_format: str = "images_edit"
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


def load_env_file(path: str | None = None) -> dict[str, str]:
    """读 .env 成 dict（KEY=VALUE，跳过注释/空行）。文件不存在返回空 dict。"""
    path = path or DEFAULT_ENV_FILE
    out: dict[str, str] = {}
    if not os.path.isfile(path):
        return out
    for line in open(path, encoding="utf-8"):
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


def images_edit(provider: ImageProvider, image_bytes: bytes, prompt: str,
                *, mime: str = "image/jpeg") -> bytes:
    """单次 OpenAI 兼容 images/edits 调用，返回生成图字节。失败抛异常。"""
    import requests

    files = {"image": ("input.jpg", image_bytes, mime)}
    data = {"prompt": prompt, "model": provider.model, "n": "1"}
    # img2img 生图慢（gpt-image-2 实测 ~70-120s）→ 给 ≥180s，盖过 .env 里偏小的 chat 超时
    timeout_s = max(provider.timeout_ms, 180_000) / 1000.0
    r = requests.post(
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
        dl = requests.get(item["url"], timeout=60)
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
    "images_edit", "generate_with_fallback", "DEFAULT_ENV_FILE",
]
