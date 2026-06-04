"""Build a readiness report for formal semantic judge on Vertex AI Gemini.

This report is deliberately fail-closed. Provisioned Throughput is a paid cloud
resource, so this script only verifies local configuration/readiness and never
creates Google Cloud resources.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

UNVERIFIED = "未验证/无法获取"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = Path("/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env")
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t61_vertex_semantic_judge_readiness.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t61_vertex_semantic_judge_readiness.md"
DEFAULT_LIVE_PROBE_IMAGE = (
    ROOT
    / "tasks"
    / "t46_human_review_packet"
    / "assets"
    / "case88-front-background-cleanup-v1-conservative"
    / "baseline.jpg"
)
VERTEX_PROVIDERS = {"vertex", "vertex-ai", "vertex_generate_content_adc", "google-vertex"}
TokenProbe = Callable[[], str | None]
LiveProbe = Callable[..., dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_env_file(path: Path, env: dict[str, str]) -> dict[str, str]:
    out = dict(env)
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in out:
            out[key] = value
    return out


def load_env(paths: list[Path] | None = None, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    for path in paths or [DEFAULT_ENV_FILE]:
        env = _load_env_file(path, env)
    return env


def _gcloud_adc_token() -> str | None:
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


def _selected_model(env: dict[str, str]) -> str:
    explicit = (
        env.get("CASE_LAYOUT_VERTEX_MODEL")
        or env.get("CASE_LAYOUT_SEMANTIC_MODEL")
        or env.get("VERTEX_AI_MODEL")
        or ""
    ).strip()
    if explicit:
        return explicit
    vision_model = (env.get("VISION_API_MODEL") or "").strip()
    if vision_model.lower().startswith(("gemini", "claude")):
        return vision_model
    return "gemini-2.5-flash"


def _provider_config(env: dict[str, str]) -> dict[str, Any]:
    selected_provider = (env.get("VISION_PROVIDER") or env.get("GPT54_PROVIDER") or "").strip().lower()
    model = _selected_model(env)
    project = (
        env.get("CASE_LAYOUT_VERTEX_PROJECT")
        or env.get("CASE_WORKBENCH_VERTEX_PROJECT")
        or env.get("GOOGLE_CLOUD_PROJECT")
        or env.get("GCLOUD_PROJECT")
        or ""
    ).strip()
    location = (
        env.get("CASE_LAYOUT_VERTEX_LOCATION")
        or env.get("CASE_WORKBENCH_VERTEX_LOCATION")
        or env.get("GOOGLE_CLOUD_LOCATION")
        or ""
    ).strip()
    endpoint = (env.get("CASE_LAYOUT_VERTEX_ENDPOINT") or env.get("VERTEX_GENERATE_CONTENT_ENDPOINT") or "").strip()
    request_type = (env.get("CASE_LAYOUT_VERTEX_REQUEST_TYPE") or env.get("VERTEX_AI_LLM_REQUEST_TYPE") or "").strip()
    pt_declared = bool(endpoint or request_type or (env.get("CASE_LAYOUT_VERTEX_PROVISIONED_THROUGHPUT") or "").lower() in {"1", "true", "yes"})
    if project and location and not endpoint:
        endpoint = (
            f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
            f"/publishers/google/models/{model}:generateContent"
        )
    return {
        "selected_provider": selected_provider or "unset",
        "is_vertex_provider": selected_provider in VERTEX_PROVIDERS,
        "provider": "vertex_generate_content_adc",
        "model": model,
        "project_configured": bool(project),
        "location_configured": bool(location),
        "project": project or None,
        "location": location or None,
        "endpoint": endpoint or None,
        "request_type": request_type or None,
        "provisioned_throughput_declared": pt_declared,
        "billing_mode": "provisioned_throughput" if pt_declared else "paygo",
    }


def _not_run_live_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"ok": False, "status": "not_run", "reason": f"{UNVERIFIED}：未执行真实 Vertex live probe。"}


def _extract_vertex_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in response.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def _vertex_live_probe_result_from_response(
    data: dict[str, Any],
    provider: dict[str, Any],
    *,
    image_path: Path,
) -> dict[str, Any]:
    candidates = data.get("candidates") or []
    text = _extract_vertex_text(data)
    base = {
        "model": provider.get("model"),
        "endpoint": provider.get("endpoint"),
        "image_path": str(image_path),
        "response_id": data.get("responseId"),
        "candidate_count": len(candidates),
        "text_excerpt": text[:300],
    }
    if not candidates or not text:
        return {
            "ok": False,
            "status": "vertex_api_empty_candidate",
            "reason": f"{UNVERIFIED}：Vertex live probe 返回空候选或空文本。",
            **base,
        }
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "vertex_api_non_json_judge_response",
            "reason": f"{UNVERIFIED}：Vertex live probe 未返回严格机器 JSON。",
            **base,
        }
    if not isinstance(parsed, dict) or not isinstance(parsed.get("publishable"), bool):
        return {
            "ok": False,
            "status": "vertex_api_invalid_judge_schema",
            "reason": f"{UNVERIFIED}：Vertex live probe JSON 缺少 publishable 布尔字段。",
            "parsed_json": parsed if isinstance(parsed, dict) else None,
            **base,
        }
    return {
        "ok": True,
        "status": "ok",
        "parsed_json": parsed,
        **base,
    }


def _vertex_live_probe(provider: dict[str, Any], env: dict[str, str], *, image_path: Path = DEFAULT_LIVE_PROBE_IMAGE) -> dict[str, Any]:
    endpoint = provider.get("endpoint")
    model = provider.get("model")
    if not endpoint:
        return {"ok": False, "status": "blocked_missing_vertex_endpoint", "reason": f"{UNVERIFIED}：缺少 Vertex generateContent endpoint。"}
    if not image_path.is_file():
        return {
            "ok": False,
            "status": "blocked_missing_real_probe_image",
            "reason": f"{UNVERIFIED}：真实 probe 图片不存在。",
            "image_path": str(image_path),
        }
    token = _gcloud_adc_token()
    if not token:
        return {"ok": False, "status": "blocked_missing_vertex_adc_token", "reason": f"{UNVERIFIED}：无法通过 ADC 获取 Vertex AI bearer token。"}

    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    prompt = (
        "你是医美案例正式出图质量审查员。只输出一个严格 JSON object，禁止解释、禁止 Markdown、禁止前后缀。"
        'JSON schema: {"publishable": boolean, "blocking_reasons": string[], "quality_notes": string[]}. '
        '本次是连通性探针，请输出 {"publishable":false,"blocking_reasons":[],"quality_notes":["live_probe"]}'
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime_type, "data": image_data}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 256,
            "responseMimeType": "application/json",
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if provider.get("request_type"):
        headers["X-Vertex-AI-LLM-Request-Type"] = str(provider["request_type"])
    try:
        req = urllib.request.Request(
            str(endpoint),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=int(env.get("CASE_LAYOUT_VERTEX_LIVE_PROBE_TIMEOUT_SECONDS", "60"))) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": "vertex_api_http_error",
            "http_status": exc.code,
            "reason": f"{UNVERIFIED}：Vertex live probe HTTP {exc.code}。",
            "body_excerpt": body[:500],
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": "vertex_api_request_error",
            "reason": f"{UNVERIFIED}：Vertex live probe 请求失败。",
            "error": str(exc)[:500],
        }
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "status": "vertex_api_invalid_json_response",
            "reason": f"{UNVERIFIED}：Vertex live probe 返回非 JSON。",
            "body_excerpt": raw[:500],
        }
    return _vertex_live_probe_result_from_response(data, {"model": model, "endpoint": endpoint}, image_path=image_path)


def build_report(
    *,
    env: dict[str, str],
    token_probe: TokenProbe = _gcloud_adc_token,
    live_probe: LiveProbe = _not_run_live_probe,
    scope_label: str = "vertex_semantic_judge_readiness_v2",
) -> dict[str, Any]:
    provider = _provider_config(env)
    token_available = bool(token_probe())
    live_result: dict[str, Any] = {"ok": False, "status": "not_attempted"}
    if not provider["is_vertex_provider"]:
        run_status = "formal_semantic_still_on_non_vertex_provider"
        decision = f"{UNVERIFIED}：正式 semantic judge 当前未切到 Vertex provider。"
    elif not provider["project_configured"]:
        run_status = "blocked_missing_vertex_project_config"
        decision = f"{UNVERIFIED}：未配置 CASE_LAYOUT_VERTEX_PROJECT/GOOGLE_CLOUD_PROJECT。"
    elif not provider["location_configured"]:
        run_status = "blocked_missing_vertex_location_config"
        decision = f"{UNVERIFIED}：未配置 CASE_LAYOUT_VERTEX_LOCATION/GOOGLE_CLOUD_LOCATION。"
    elif not token_available:
        run_status = "blocked_missing_vertex_adc_token"
        decision = f"{UNVERIFIED}：无法通过 ADC 获取 Vertex AI bearer token。"
    else:
        live_result = live_probe(provider, env)
        if live_result.get("ok"):
            if provider["billing_mode"] == "provisioned_throughput":
                run_status = "vertex_pt_semantic_judge_verified"
                decision = "Vertex AI Gemini Provisioned Throughput semantic judge 已通过真实 live probe，可作为正式主链路候选。"
            else:
                run_status = "vertex_paygo_semantic_judge_verified"
                decision = "Vertex AI Gemini PayGo semantic judge 已通过真实 live probe，可作为正式主链路候选。"
        else:
            run_status = "configured_pending_live_probe"
            decision = f"{UNVERIFIED}：Vertex {provider['billing_mode']} 配置存在，但尚未通过真实 live probe，保持 fail-closed。"
    ready = run_status in {"vertex_paygo_semantic_judge_verified", "vertex_pt_semantic_judge_verified"}
    return {
        "generated_at": _now(),
        "scope": scope_label,
        "run_status": run_status,
        "decision": decision,
        "ready_for_formal_semantic_primary": ready,
        "provider": provider,
        "adc_token_available": token_available,
        "live_probe": live_result,
        "notes": [
            "This script does not create or purchase Provisioned Throughput.",
            "PayGo can be the primary formal semantic judge only after a real Vertex live probe passes.",
            "If readiness is false, formal render semantic judge must remain on the current fallback/fail-closed path.",
        ],
    }


def write_report(report: dict[str, Any], json_output: Path, markdown_output: Path) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Vertex Semantic Judge Readiness",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- ready_for_formal_semantic_primary: `{report.get('ready_for_formal_semantic_primary')}`",
        f"- provider: `{(report.get('provider') or {}).get('provider')}`",
        f"- selected_provider: `{(report.get('provider') or {}).get('selected_provider')}`",
        f"- model: `{(report.get('provider') or {}).get('model')}`",
        f"- project_configured: `{(report.get('provider') or {}).get('project_configured')}`",
        f"- location_configured: `{(report.get('provider') or {}).get('location_configured')}`",
        f"- provisioned_throughput_declared: `{(report.get('provider') or {}).get('provisioned_throughput_declared')}`",
        f"- billing_mode: `{(report.get('provider') or {}).get('billing_mode')}`",
        f"- adc_token_available: `{report.get('adc_token_available')}`",
        f"- live_probe_status: `{(report.get('live_probe') or {}).get('status')}`",
    ]
    markdown_output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build T61 Vertex semantic judge readiness report.")
    parser.add_argument("--env-file", type=Path, action="append", default=[DEFAULT_ENV_FILE])
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--live-probe", action="store_true", help="Run one real Vertex generateContent probe. This can bill PayGo usage.")
    parser.add_argument("--live-probe-image", type=Path, default=DEFAULT_LIVE_PROBE_IMAGE)
    parser.add_argument("--scope-label", default="vertex_semantic_judge_readiness_v2")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    live_probe: LiveProbe
    if args.live_probe:
        def live_probe(provider: str, env: dict[str, str]) -> dict[str, Any]:
            return _vertex_live_probe(provider, env, image_path=args.live_probe_image)
    else:
        live_probe = _not_run_live_probe
    report = build_report(env=load_env(args.env_file), live_probe=live_probe, scope_label=args.scope_label)
    write_report(report, args.json_output, args.markdown_output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
