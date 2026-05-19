"""Run a real independent VLM judge over the T51 blind packet.

No fallback judgments are generated. If provider credentials are missing or a
provider call fails, the output remains blocked/partial instead of inventing
quality decisions.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.services.vlm_provider import VLMProvider, VLMRequest, VLMRequestError, VLMResponse

UNVERIFIED = "未验证/无法获取"
VALID_WINNER_ROLES = {"baseline", "candidate"}
MANUAL_REVIEW_WINNER_ROLES = {"tie", "manual_review"}
PostJson = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]
TokenProvider = Callable[[], str | None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_env_files(base_env: dict[str, str], paths: list[Path]) -> dict[str, str]:
    env = dict(base_env)
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in env:
                env[key] = value
    return env


def _unit_id(value: dict[str, Any]) -> str:
    return str(value.get("ab_unit_id") or value.get("unit_id") or value.get("case_id") or "").strip()


def _asset_path(asset: dict[str, Any], packet_root: Path) -> Path:
    for key in ("packet_path", "source_path"):
        raw = str(asset.get(key) or "").strip()
        if raw:
            path = Path(raw)
            if path.is_file():
                return path
    rel = str(asset.get("packet_relative_path") or "").strip()
    if rel:
        path = packet_root / rel
        if path.is_file():
            return path
    raise FileNotFoundError(f"无法获取真实 packet image: {asset.get('packet_relative_path') or asset.get('packet_path')}")


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _inline_data(path: Path) -> dict[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"mime_type": mime, "data": encoded}


def _vertex_inline_data(path: Path) -> dict[str, str]:
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"mimeType": mime, "data": encoded}


def _judge_prompt(item: dict[str, Any]) -> str:
    criteria = item.get("criteria") if isinstance(item.get("criteria"), list) else []
    criteria_lines = "\n".join(f"- {criterion}" for criterion in criteria)
    return (
        "You are an independent medical-aesthetic image delivery quality judge.\n"
        "Compare exactly two images. The first image is baseline; the second image is candidate.\n"
        "Use only visual evidence from the images.\n"
        "Do not assume either image is better because of file names, variants, prior reports, or generation system.\n"
        "Score each criterion from 1-5 for baseline and candidate where 5 is best.\n"
        "Return only one JSON object. Do not include markdown or long hidden reasoning.\n"
        "Required JSON keys: ab_unit_id, winner_role, confidence, criterion_scores, visual_evidence_summary, rationale, risk_flags, hard_veto_reason.\n"
        "criterion_scores must be an object keyed by criterion, each value shaped as {\"baseline\": 1-5, \"candidate\": 1-5}.\n"
        "winner_role may be baseline, candidate, tie, or manual_review.\n"
        "Use tie when the visual difference is not meaningful; use manual_review when evidence is ambiguous, safety-relevant, or confidence is below 0.75.\n"
        "Importable judgments must use winner_role baseline or candidate; tie/manual_review are auxiliary review results only.\n"
        "visual_evidence_summary must be brief and auditable, based only on visible image evidence.\n\n"
        f"ab_unit_id: {_unit_id(item)}\n"
        f"case_id: {item.get('case_id')}\n"
        f"view: {item.get('view')}\n"
        f"workflow: {item.get('workflow')}\n"
        "Criteria:\n"
        f"{criteria_lines}\n"
    )


def build_openai_responses_payload(item: dict[str, Any], *, model: str, packet_root: Path) -> dict[str, Any]:
    baseline_path = _asset_path(item.get("baseline") if isinstance(item.get("baseline"), dict) else {}, packet_root)
    candidate_path = _asset_path(item.get("candidate") if isinstance(item.get("candidate"), dict) else {}, packet_root)
    return {
        "model": model,
        "store": False,
        "max_output_tokens": 800,
        "text": {"format": {"type": "json_object"}},
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _judge_prompt(item)},
                    {"type": "input_text", "text": "Image A is baseline."},
                    {"type": "input_image", "image_url": _data_url(baseline_path)},
                    {"type": "input_text", "text": "Image B is candidate."},
                    {"type": "input_image", "image_url": _data_url(candidate_path)},
                ],
            }
        ],
    }


def build_gemini_generate_content_payload(item: dict[str, Any], *, packet_root: Path) -> dict[str, Any]:
    baseline_path = _asset_path(item.get("baseline") if isinstance(item.get("baseline"), dict) else {}, packet_root)
    candidate_path = _asset_path(item.get("candidate") if isinstance(item.get("candidate"), dict) else {}, packet_root)
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _judge_prompt(item)},
                    {"text": "Image A is baseline."},
                    {"inline_data": _inline_data(baseline_path)},
                    {"text": "Image B is candidate."},
                    {"inline_data": _inline_data(candidate_path)},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }


def build_vertex_generate_content_payload(item: dict[str, Any], *, packet_root: Path) -> dict[str, Any]:
    baseline_path = _asset_path(item.get("baseline") if isinstance(item.get("baseline"), dict) else {}, packet_root)
    candidate_path = _asset_path(item.get("candidate") if isinstance(item.get("candidate"), dict) else {}, packet_root)
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": _judge_prompt(item)},
                    {"text": "Image A is baseline."},
                    {"inlineData": _vertex_inline_data(baseline_path)},
                    {"text": "Image B is candidate."},
                    {"inlineData": _vertex_inline_data(candidate_path)},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _extract_output_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    chunks: list[str] = []
    for output in response.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
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
            raise
        parsed = json.loads(value[start : end + 1])
    return parsed if isinstance(parsed, dict) else {}


def _judgment_from_parsed(
    parsed: dict[str, Any],
    *,
    item: dict[str, Any],
    provider: str,
    model: str,
    provider_response_id: str | None = None,
) -> dict[str, Any]:
    unit_id = str(parsed.get("ab_unit_id") or _unit_id(item)).strip()
    winner_role = str(parsed.get("winner_role") or "").strip().lower()
    risk_flags = parsed.get("risk_flags") if isinstance(parsed.get("risk_flags"), list) else []
    criterion_scores = parsed.get("criterion_scores") if isinstance(parsed.get("criterion_scores"), dict) else {}
    return {
        "ab_unit_id": unit_id,
        "winner_role": winner_role,
        "confidence": parsed.get("confidence"),
        "criterion_scores": criterion_scores,
        "visual_evidence_summary": parsed.get("visual_evidence_summary"),
        "rationale": parsed.get("rationale"),
        "risk_flags": risk_flags,
        "hard_veto_reason": parsed.get("hard_veto_reason"),
        "judge_provider": provider,
        "judge_model": model,
        "provider_response_id": provider_response_id,
    }


def parse_openai_responses_judgment(
    response: dict[str, Any],
    *,
    item: dict[str, Any],
    provider: str,
    model: str,
) -> dict[str, Any]:
    parsed = _parse_json_object(_extract_output_text(response))
    return _judgment_from_parsed(parsed, item=item, provider=provider, model=model, provider_response_id=response.get("id"))


def parse_gemini_generate_content_judgment(
    response: dict[str, Any],
    *,
    item: dict[str, Any],
    provider: str,
    model: str,
) -> dict[str, Any]:
    parsed = _parse_json_object(_extract_gemini_text(response))
    return _judgment_from_parsed(parsed, item=item, provider=provider, model=model, provider_response_id=response.get("responseId"))


def parse_vertex_generate_content_judgment(
    response: dict[str, Any],
    *,
    item: dict[str, Any],
    provider: str,
    model: str,
) -> dict[str, Any]:
    parsed = _parse_json_object(_extract_gemini_text(response))
    return _judgment_from_parsed(parsed, item=item, provider=provider, model=model, provider_response_id=response.get("responseId"))


def parse_vlm_provider_judgment(response: VLMResponse, *, item: dict[str, Any]) -> dict[str, Any]:
    return _judgment_from_parsed(
        response.parsed,
        item=item,
        provider=response.provider,
        model=response.model,
        provider_response_id=response.response_id,
    )


def _manual_review_from_provider_error(
    item: dict[str, Any],
    *,
    provider: str,
    model: str,
    error: BaseException,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ab_unit_id": _unit_id(item),
        "winner_role": "manual_review",
        "confidence": 0,
        "criterion_scores": {},
        "visual_evidence_summary": UNVERIFIED,
        "rationale": "VLM provider call failed; fail-closed to manual review.",
        "risk_flags": ["vlm_provider_error"],
        "hard_veto_reason": "vlm_provider_error_fail_closed",
        "judge_provider": provider,
        "judge_model": model,
        "fail_closed_reason": str(error)[:1000],
    }
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        out["provider_status_code"] = status_code
    return out


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
    token = result.stdout.strip()
    return token or None


def _blocked_decision(status: str, provider: str | None = None) -> str:
    if status == "blocked_missing_vlm_provider_config":
        return f"{UNVERIFIED}：未配置真实独立 VLM judge provider。"
    if status == "blocked_unsupported_vlm_provider":
        return f"{UNVERIFIED}：不支持的 VLM judge provider: {provider or ''}。"
    if status == "blocked_missing_vlm_model_config":
        return f"{UNVERIFIED}：未配置 VLM_MODEL/CASE_WORKBENCH_VLM_JUDGE_MODEL。"
    if status == "blocked_missing_gemini_api_key":
        return f"{UNVERIFIED}：未配置 VLM_API_KEY/CASE_WORKBENCH_VLM_JUDGE_API_KEY/GEMINI_API_KEY/GOOGLE_API_KEY。"
    if status == "blocked_missing_vertex_project_config":
        return f"{UNVERIFIED}：未配置 VLM_PROJECT/CASE_WORKBENCH_VERTEX_PROJECT/GOOGLE_CLOUD_PROJECT。"
    if status == "blocked_missing_vertex_location_config":
        return f"{UNVERIFIED}：未配置 VLM_LOCATION/CASE_WORKBENCH_VERTEX_LOCATION/GOOGLE_CLOUD_LOCATION。"
    if status == "blocked_missing_openai_api_key":
        return f"{UNVERIFIED}：未配置 VLM_API_KEY、OPENAI_API_KEY、CASE_WORKBENCH_VLM_JUDGE_API_KEY 或 VISION_API_KEY。"
    return f"{UNVERIFIED}：真实 VLM provider 未就绪: {status}。"


def _provider_config(
    *,
    env: dict[str, str],
    provider: str | None,
    model: str | None,
    endpoint: str | None,
) -> dict[str, Any]:
    config = VLMProvider(env=env).configure(provider=provider, model=model, endpoint=endpoint)
    result: dict[str, Any] = {
        "ready": config.ready,
        "provider": config.provider,
        "model": config.model,
        "endpoint": config.endpoint,
        "project": config.project,
        "location": config.location,
        "billing_mode": config.billing_mode,
        "api_key": config.api_key,
        "status": config.status,
    }
    if not config.ready:
        result["decision"] = _blocked_decision(config.status, config.provider)
    return {key: value for key, value in result.items() if value is not None and value != ""}


def _blocked_results(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    results = {
        "generated_at": _now(),
        "scope": "t52_independent_vlm_judge_results_v1",
        "real_vlm_judge": False,
        "provider": config.get("provider"),
        "model": config.get("model"),
        "project": config.get("project"),
        "location": config.get("location"),
        "billing_mode": config.get("billing_mode"),
        "run_status": config["status"],
        "decision": config["decision"],
        "judgments": [],
    }
    report = {
        "generated_at": _now(),
        "scope": "t52_vlm_judge_run_report_v1",
        "run_status": config["status"],
        "decision": config["decision"],
        "real_vlm_judge": False,
        "provider": config.get("provider"),
        "model": config.get("model"),
        "project": config.get("project"),
        "location": config.get("location"),
        "billing_mode": config.get("billing_mode"),
        "attempted_count": 0,
        "successful_judgment_count": 0,
        "failed_judgment_count": 0,
        "errors": [],
    }
    return results, report


def _runtime_env(
    base_env: dict[str, str],
    *,
    provider: str | None,
    model: str | None,
    endpoint: str | None,
) -> dict[str, str]:
    env = dict(base_env)
    if provider:
        env["VLM_PROVIDER"] = provider
    if model:
        env["VLM_MODEL"] = model
    if endpoint:
        env["VLM_ENDPOINT"] = endpoint
    return env


def _case_id_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        return int(text) if text.isdigit() else None
    except (TypeError, ValueError):
        return None


def _judge_request(item: dict[str, Any], *, packet_root: Path, timeout_seconds: float) -> VLMRequest:
    baseline_path = _asset_path(item.get("baseline") if isinstance(item.get("baseline"), dict) else {}, packet_root)
    candidate_path = _asset_path(item.get("candidate") if isinstance(item.get("candidate"), dict) else {}, packet_root)
    case_id = item.get("case_id")
    return VLMRequest(
        prompt=_judge_prompt(item),
        images=[baseline_path, candidate_path],
        timeout=float(timeout_seconds),
        purpose="judge",
        case_id=_case_id_int(case_id),
    )


def run_vlm_judge(
    packet: dict[str, Any],
    *,
    packet_root: Path,
    env: dict[str, str] | None = None,
    provider: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    max_items: int | None = None,
    timeout_seconds: float = 120.0,
    sleep_seconds: float = 0.0,
    concurrency: int = 3,
    post_json: PostJson = _post_json,
    token_provider: TokenProvider = _gcloud_adc_token,
) -> tuple[dict[str, Any], dict[str, Any]]:
    runtime_env = _runtime_env(
        dict(os.environ if env is None else env),
        provider=provider,
        model=model,
        endpoint=endpoint,
    )
    config = _provider_config(
        env=runtime_env,
        provider=None,
        model=None,
        endpoint=None,
    )
    if not config.get("ready"):
        return _blocked_results(config)

    items = [item for item in packet.get("judge_items") or [] if isinstance(item, dict)]
    if max_items is not None:
        items = items[: max(0, int(max_items))]

    judgments: list[dict[str, Any]] = []
    manual_review_judgments: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    provider_name = str(config["provider"])
    model_name = str(config["model"])
    project = config.get("project")
    location = config.get("location")
    billing_mode = config.get("billing_mode")
    runtime_token_provider = token_provider
    if provider_name == "vertex_generate_content_adc":
        cached_token = token_provider()
        if not cached_token:
            return _blocked_results(
                {
                    **config,
                    "status": "blocked_missing_vertex_adc_token",
                    "decision": f"{UNVERIFIED}：无法通过 ADC 获取 Vertex AI bearer token。",
                }
            )
        runtime_token_provider = lambda: cached_token

    requests: list[VLMRequest] = []
    request_items: list[dict[str, Any]] = []
    for item in items:
        unit_id = _unit_id(item)
        try:
            requests.append(_judge_request(item, packet_root=packet_root, timeout_seconds=timeout_seconds))
            request_items.append(item)
        except OSError as exc:
            errors.append({"ab_unit_id": unit_id, "reason": str(exc)})

    provider_client = VLMProvider(env=runtime_env, post_json=post_json, token_provider=runtime_token_provider)
    batch_size = max(1, int(concurrency or 1))
    for batch_start in range(0, len(requests), batch_size):
        batch_requests = requests[batch_start : batch_start + batch_size]
        batch_items = request_items[batch_start : batch_start + batch_size]
        try:
            responses = provider_client.call_vision_batch(
                batch_requests,
                concurrency=batch_size,
                return_exceptions=True,
            )
        except (VLMRequestError, OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
            for item in batch_items:
                errors.append({"ab_unit_id": _unit_id(item), "reason": str(exc)})
        else:
            for item, response in zip(batch_items, responses, strict=False):
                unit_id = _unit_id(item)
                if isinstance(response, BaseException):
                    manual_review_judgments.append(
                        _manual_review_from_provider_error(
                            item,
                            provider=provider_name,
                            model=model_name,
                            error=response,
                        )
                    )
                    continue
                try:
                    judgment = parse_vlm_provider_judgment(response, item=item)
                    winner_role = judgment.get("winner_role")
                    if winner_role in VALID_WINNER_ROLES:
                        judgments.append(judgment)
                    elif winner_role in MANUAL_REVIEW_WINNER_ROLES:
                        manual_review_judgments.append(judgment)
                    else:
                        raise ValueError("VLM response winner_role must be baseline, candidate, tie, or manual_review")
                except (ValueError, json.JSONDecodeError) as exc:
                    errors.append({"ab_unit_id": unit_id, "reason": str(exc)})
        if sleep_seconds and batch_start + batch_size < len(requests):
            time.sleep(float(sleep_seconds))

    status = (
        "completed_real_vlm_judge"
        if judgments and not errors and not manual_review_judgments
        else "partial_real_vlm_judge"
        if judgments
        else "blocked_no_successful_vlm_judgments"
    )
    decision = (
        f"真实独立 VLM judge 已生成 {len(judgments)} 条可导入 judgments。"
        if judgments
        else f"{UNVERIFIED}：真实 VLM provider 未返回任何可导入 judgment。"
    )
    results = {
        "generated_at": _now(),
        "scope": "t52_independent_vlm_judge_results_v1",
        "real_vlm_judge": bool(judgments),
        "provider": provider_name,
        "model": model_name,
        "project": project,
        "location": location,
        "billing_mode": billing_mode,
        "run_status": status,
        "decision": decision,
        "judgments": judgments,
        "manual_review_judgments": manual_review_judgments,
    }
    report = {
        "generated_at": _now(),
        "scope": "t52_vlm_judge_run_report_v1",
        "run_status": status,
        "decision": decision,
        "real_vlm_judge": bool(judgments),
        "provider": provider_name,
        "model": model_name,
        "project": project,
        "location": location,
        "billing_mode": billing_mode,
        "judge_item_count": int(packet.get("judge_item_count") or len(packet.get("judge_items") or [])),
        "attempted_count": len(items),
        "successful_judgment_count": len(judgments),
        "manual_review_count": len(manual_review_judgments),
        "failed_judgment_count": len(errors),
        "errors": errors,
    }
    return results, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a real independent VLM judge over T51 blind judge items.")
    parser.add_argument("--packet-json", type=Path, required=True)
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--results-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--endpoint")
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--concurrency", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    packet_path = args.packet_json.resolve()
    packet_root = args.packet_root.resolve() if args.packet_root else packet_path.parent
    env = load_env_files(dict(os.environ), [path.resolve() for path in args.env_file])
    results, report = run_vlm_judge(
        _load_json(packet_path),
        packet_root=packet_root,
        env=env,
        provider=args.provider,
        model=args.model,
        endpoint=args.endpoint,
        max_items=args.max_items,
        timeout_seconds=float(args.timeout_seconds),
        sleep_seconds=float(args.sleep_seconds),
        concurrency=int(args.concurrency),
    )
    _write_json(args.results_output, results)
    _write_json(args.report_output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
