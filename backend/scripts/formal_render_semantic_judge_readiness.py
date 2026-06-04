"""Build a T59.4 semantic judge readiness report from real probe and matrix evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
DEFAULT_MATRIX = ROOT / "tasks" / "t594_hard_quality_targeted_matrix.json"
DEFAULT_JSON_OUTPUT = ROOT / "tasks" / "t594_semantic_judge_readiness.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "tasks" / "t594_semantic_judge_readiness.md"
UNVERIFIED = "未验证/无法获取"
SECRET_ENV_NAMES = (
    "VISION_API_KEY",
    "GEMINI_FLASH_API_KEY",
    "GEMINI_TUZI_API_KEY",
    "CASE_WORKBENCH_VLM_JUDGE_API_KEY",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _source_hash(path: Path | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _record_texts(record: dict[str, Any]) -> list[str]:
    quality = record.get("quality") if isinstance(record.get("quality"), dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    texts = []
    for key in ("warnings", "display_warnings", "audit_warnings", "blocking_issues"):
        texts.extend(str(item or "") for item in _as_list(metrics.get(key)))
    texts.extend(str(item or "") for item in _as_list(record.get("hard_blockers")))
    return [item for item in texts if item]


def _semantic_warning_kind(text: str) -> str | None:
    lowered = text.lower()
    if "视觉补判仅供参考" not in text and "semantic" not in lowered:
        return None
    if "insufficient_user_quota" in text or "预扣费额度失败" in text or "quota" in lowered:
        return "quota"
    if "api 403" in lowered or "403" in text:
        return "api_403"
    if "api" in lowered or "gpt-5.4" in lowered:
        return "provider_error"
    return "semantic_review_warning"


def _semantic_issues(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for record in _as_list(matrix.get("records")):
        if not isinstance(record, dict):
            continue
        for text in _record_texts(record):
            kind = _semantic_warning_kind(text)
            if not kind:
                continue
            issues.append({
                "case_id": int(record.get("case_id") or 0),
                "job_id": int(record.get("job_id") or 0),
                "kind": kind,
                "sample": text[:220],
            })
    return issues


def _env_presence() -> dict[str, bool]:
    return {name: bool(os.environ.get(name)) for name in SECRET_ENV_NAMES}


def build_report(
    *,
    matrix_path: Path,
    provider: str,
    model: str,
    probe_status: str,
    probe_detail: str = "",
    available_models: list[str] | None = None,
) -> dict[str, Any]:
    if not matrix_path.exists():
        return {
            "generated_at": _now(),
            "run_status": "blocked_missing_real_matrix",
            "used_mock_data": False,
            "decision": f"{UNVERIFIED}：缺少真实 matrix report，不能证明 semantic judge readiness。",
            "missing_paths": [str(matrix_path)],
            "secret_policy": {"stores_secret_values": False},
        }

    matrix = _load_json(matrix_path)
    issues = _semantic_issues(matrix)
    issue_kinds: dict[str, int] = {}
    for issue in issues:
        issue_kinds[issue["kind"]] = issue_kinds.get(issue["kind"], 0) + 1

    probe_verified = probe_status == "verified"
    if probe_verified and not issues:
        run_status = "semantic_judge_verified_on_current_matrix"
        decision = "当前真实 matrix 未发现 semantic judge quota/API 阻断，备用 judge 可作为本轮正式出图补判证据。"
    elif probe_verified:
        run_status = "backup_judge_verified_pending_matrix_rerun"
        decision = "备用 semantic judge 已真实探针通过；当前 matrix 仍是旧 provider/quota 阻断结果，必须用备用 provider 复跑后才可消除 warning。"
    else:
        run_status = "blocked_semantic_judge_unverified"
        decision = f"{UNVERIFIED}：semantic judge 未恢复，保持 fail-closed；不能把 done_with_issues 放行为可交付。"

    return {
        "generated_at": _now(),
        "run_status": run_status,
        "used_mock_data": False,
        "decision": decision,
        "policy": {
            "source_matrix_path": str(matrix_path),
            "source_matrix_sha256": _source_hash(matrix_path),
            "does_not_modify_db": True,
            "does_not_enqueue_render": True,
            "stores_secret_values": False,
            "done_with_issues_is_not_publishable": True,
        },
        "provider_probe": {
            "provider": provider,
            "model": model,
            "status": probe_status,
            "detail": probe_detail[:240],
            "available_models": available_models or [],
            "env_presence": _env_presence(),
        },
        "summary": {
            "record_count": len(_as_list(matrix.get("records"))),
            "semantic_issue_count": len(issues),
            "semantic_issue_kind_counts": issue_kinds,
            "quota_or_403_count": sum(1 for item in issues if item["kind"] in {"quota", "api_403", "provider_error"}),
            "backup_judge_ready_for_rerun": probe_verified,
            "ready_for_publish_gate": probe_verified and not issues,
        },
        "semantic_issue_samples": issues[:12],
    }


def write_markdown(report: dict[str, Any], output_path: Path) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    probe = report.get("provider_probe") if isinstance(report.get("provider_probe"), dict) else {}
    lines = [
        "# T59.4 Semantic Judge Readiness",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- decision: {report.get('decision')}",
        f"- provider: `{probe.get('provider')}`",
        f"- model: `{probe.get('model')}`",
        f"- probe_status: `{probe.get('status')}`",
        f"- semantic_issue_count: `{summary.get('semantic_issue_count')}`",
        f"- quota_or_403_count: `{summary.get('quota_or_403_count')}`",
        f"- ready_for_publish_gate: `{summary.get('ready_for_publish_gate')}`",
        "",
        "## Issue Samples",
        "",
    ]
    for item in _as_list(report.get("semantic_issue_samples")):
        lines.append(
            f"- case `{item.get('case_id')}` job `{item.get('job_id')}` kind `{item.get('kind')}`: {item.get('sample')}"
        )
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument("--provider", default="未验证")
    parser.add_argument("--model", default="未验证")
    parser.add_argument("--probe-status", choices=["verified", "blocked", "not_run"], default="not_run")
    parser.add_argument("--probe-detail", default="")
    parser.add_argument("--available-models", default="")
    args = parser.parse_args()

    report = build_report(
        matrix_path=args.matrix,
        provider=args.provider,
        model=args.model,
        probe_status=args.probe_status,
        probe_detail=args.probe_detail,
        available_models=_split_csv(args.available_models),
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report, args.markdown_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
