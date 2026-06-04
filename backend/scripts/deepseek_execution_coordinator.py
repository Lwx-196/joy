"""Use DeepSeek official API to coordinate text-only execution reports.

The coordinator is advisory only. It never changes DB state, production gates,
or ComfyUI/VLM promotion decisions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.deepseek_client import DeepSeekClient, DeepSeekRequestError

DEFAULT_REPORT_PATHS = [
    Path("tasks/vlm_preflight_report.json"),
    Path("tasks/vlm_production_batch_report.json"),
    Path("tasks/vlm_production_batch_calibration_report.json"),
    Path("tasks/live_service_status_report.json"),
    Path("tasks/vlm_production_classification_dry_run.json"),
    Path("tasks/vlm_production_classification_live_write.json"),
    Path("tasks/comfyui_local_region_retest_report.json"),
    Path("tasks/comfyui_local_region_retest_report.md"),
    Path("tasks/comfyui_local_region_candidate30_stability_report.json"),
]

ADVISORY_SYSTEM_PROMPT = (
    "You are an execution coordinator for Case Workbench. You only analyze the real local "
    "reports provided in the evidence bundle. You must not invent missing data. If evidence "
    "is absent, write '未验证/无法获取'. DeepSeek is text-only here: it must not replace VLM "
    "image judgment, human review, DeliveryGate, or production promotion gates. Return one "
    "strict JSON object with keys: production_decision, blockers, efficiency_actions, "
    "validation_plan, unverified_items. When reports conflict, prefer the newest generated_at "
    "snapshot for live service status while keeping historical gate failures fail-closed. "
    "production_decision must include keep_baseline_default, "
    "can_promote_comfyui, can_enable_vlm_autopass, and rationale."
)

SENSITIVE_KEY_PARTS = ("key", "token", "secret", "authorization", "credential", "password")
SUMMARY_FIELD_NAMES = {
    "run_status",
    "calibration_status",
    "production_ready",
    "promote_to_default",
    "reason_code",
    "candidate_count",
    "classified_count",
    "updated_count",
    "skipped_count",
    "error_count",
    "judge_item_count",
    "attempted_count",
    "successful_judgment_count",
    "manual_review_count",
    "failed_judgment_count",
    "accepted_judgment_count",
    "agreement_rate",
    "false_candidate_promotion_count",
    "candidate_win_count",
    "baseline_win_count",
    "hard_defect_codes",
    "record_count",
    "real_record_count",
    "combined_comparable_pair_count",
    "combined_failed_record_count",
    "failure_category_counts",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists() or path.suffix.lower() != ".json":
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _build_current_state(report_paths: list[Path]) -> dict[str, Any]:
    """Summarize current evidence deterministically before asking DeepSeek.

    The raw bundle can include older reports for audit continuity. This compact
    state prevents advisory-only analysis from treating stale port checks or
    skipped-but-replaced sample rows as current blockers.
    """
    state: dict[str, Any] = {
        "interpretation_rules": [
            "When port reports conflict, live_service_status_report.json is the current service snapshot.",
            "Skipped pair-candidate rows are data hygiene, not a current stability blocker, if candidate30 real_record_count >= 30 and failed_count == 0.",
            "Production promotion remains fail-closed unless production_gate.production_ready and promote_to_default are both true.",
        ]
    }
    by_name = {path.name: path for path in report_paths}

    live = _load_json_dict(by_name.get("live_service_status_report.json", Path("__missing__")))
    if live:
        state["live_services"] = {
            "generated_at": live.get("generated_at"),
            "summary": live.get("summary"),
            "all_ok": all(bool(check.get("ok")) for check in live.get("checks") or [] if isinstance(check, dict)),
        }

    candidate30 = _load_json_dict(
        by_name.get("comfyui_local_region_candidate30_stability_report.json", Path("__missing__"))
    )
    if candidate30:
        real_count = int(candidate30.get("real_record_count") or 0)
        failed_count = int(candidate30.get("failed_count") or 0)
        state["local_region_candidate30_stability"] = {
            "real_record_count": real_count,
            "ok_count": candidate30.get("ok_count"),
            "failed_count": failed_count,
            "mps_error_count": candidate30.get("mps_error_count"),
            "max_halo_score": candidate30.get("max_halo_score"),
            "max_non_target_change_score": candidate30.get("max_non_target_change_score"),
            "stability_blocker": not (real_count >= 30 and failed_count == 0),
            "skipped_pair_candidate_count": candidate30.get("skipped_pair_candidate_count"),
            "skipped_rows_block_current_30_run": not (real_count >= 30 and failed_count == 0),
        }

    gate = _load_json_dict(by_name.get("comfyui_local_region_retest_report.json", Path("__missing__")))
    if isinstance(gate.get("production_gate"), dict):
        state["comfyui_production_gate"] = gate.get("production_gate")
    if isinstance(gate.get("vlm_guardrail"), dict):
        state["vlm_guardrail"] = gate.get("vlm_guardrail")

    calibration = _load_json_dict(by_name.get("vlm_production_batch_calibration_report.json", Path("__missing__")))
    if calibration:
        state["vlm_calibration"] = {
            "calibration_status": calibration.get("calibration_status"),
            "accepted_judgment_count": calibration.get("accepted_judgment_count"),
            "agreement_rate": calibration.get("agreement_rate"),
            "false_candidate_promotion_count": calibration.get("false_candidate_promotion_count"),
        }
    return state


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in SENSITIVE_KEY_PARTS):
                redacted[str(key)] = "***REDACTED***"
            else:
                redacted[str(key)] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_report(path: Path, *, per_report_max_chars: int) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "status": "missing",
    }
    if not path.exists():
        return report
    raw = path.read_text(encoding="utf-8", errors="replace")
    report.update(
        {
            "status": "read",
            "byte_size": len(raw.encode("utf-8")),
            "sha256": _sha256_text(raw),
        }
    )
    if path.suffix.lower() == ".json":
        try:
            content: Any = _redact(json.loads(raw))
        except json.JSONDecodeError as exc:
            report.update({"status": "invalid_json", "error": str(exc)})
            return report
        if isinstance(content, dict):
            report["summary_fields"] = {
                key: content.get(key)
                for key in sorted(SUMMARY_FIELD_NAMES)
                if key in content
            }
        content_text = json.dumps(content, ensure_ascii=False, sort_keys=True)
    else:
        content_text = raw

    if len(content_text) > per_report_max_chars:
        report["truncated"] = True
        content_text = content_text[:per_report_max_chars] + "\n...[truncated]"
    else:
        report["truncated"] = False
    report["content_text"] = content_text
    return report


def build_evidence_bundle(
    report_paths: list[Path] | None = None,
    *,
    per_report_max_chars: int = 6000,
) -> dict[str, Any]:
    paths = report_paths or DEFAULT_REPORT_PATHS
    reports = [_read_report(path, per_report_max_chars=per_report_max_chars) for path in paths]
    return {
        "generated_at": _now(),
        "source": "local_case_workbench_reports",
        "advisory_only": True,
        "computed_current_state": _build_current_state(paths),
        "policy": {
            "deepseek_role": "text_report_coordination_only",
            "must_keep_fail_closed": True,
            "must_not_promote_without_gate": True,
            "must_not_replace_vlm_or_human_review": True,
        },
        "reports": reports,
        "read_report_count": sum(1 for report in reports if report.get("status") == "read"),
        "missing_report_count": sum(1 for report in reports if report.get("status") == "missing"),
    }


def _build_user_prompt(evidence_bundle: dict[str, Any]) -> str:
    bundle_text = json.dumps(evidence_bundle, ensure_ascii=False, indent=2)
    return (
        "Analyze this evidence bundle and produce an execution coordination JSON object. "
        "Prioritize blockers, safe parallel work, and validation steps. Keep all production "
        "promotion decisions fail-closed unless the evidence explicitly proves gates passed.\n\n"
        f"{bundle_text}"
    )


def _normalize_advisory(parsed: dict[str, Any]) -> dict[str, Any]:
    if not parsed:
        return {}
    decision = parsed.get("production_decision") if isinstance(parsed.get("production_decision"), dict) else {}
    return {
        "production_decision": {
            "keep_baseline_default": bool(decision.get("keep_baseline_default", True)),
            "can_promote_comfyui": bool(decision.get("can_promote_comfyui", False)),
            "can_enable_vlm_autopass": bool(decision.get("can_enable_vlm_autopass", False)),
            "rationale": str(decision.get("rationale") or "未验证/无法获取"),
        },
        "blockers": parsed.get("blockers") if isinstance(parsed.get("blockers"), list) else [],
        "efficiency_actions": (
            parsed.get("efficiency_actions") if isinstance(parsed.get("efficiency_actions"), list) else []
        ),
        "validation_plan": parsed.get("validation_plan") if isinstance(parsed.get("validation_plan"), list) else [],
        "unverified_items": (
            parsed.get("unverified_items") if isinstance(parsed.get("unverified_items"), list) else []
        ),
    }


def _evidence_requires_fail_closed(evidence_bundle: dict[str, Any]) -> bool:
    markers = [
        '"production_ready": false',
        '"promote_to_default": false',
        '"calibration_status": "not_calibrated',
        '"run_status": "blocked_',
        '"reason_code": "hard_defects_present"',
        "candidate_failed_or_blank",
        "未验证/无法获取",
    ]
    for report in evidence_bundle.get("reports") or []:
        if not isinstance(report, dict):
            continue
        text = str(report.get("content_text") or "").lower()
        if any(marker.lower() in text for marker in markers):
            return True
    return False


def _apply_fail_closed_overrides(advisory: dict[str, Any], evidence_bundle: dict[str, Any]) -> dict[str, Any]:
    if not advisory or not _evidence_requires_fail_closed(evidence_bundle):
        return advisory
    decision = advisory.setdefault("production_decision", {})
    decision["keep_baseline_default"] = True
    decision["can_promote_comfyui"] = False
    decision["can_enable_vlm_autopass"] = False
    rationale = str(decision.get("rationale") or "")
    guardrail = "DeepSeek advisory clamped fail-closed because evidence still contains blocked/false gate markers."
    decision["rationale"] = f"{rationale} {guardrail}".strip()
    return advisory


def run_coordination(
    *,
    report_paths: list[Path] | None = None,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    client: DeepSeekClient | None = None,
    timeout_seconds: float = 60.0,
    per_report_max_chars: int = 6000,
) -> dict[str, Any]:
    evidence_bundle = build_evidence_bundle(report_paths, per_report_max_chars=per_report_max_chars)
    deepseek = client or DeepSeekClient(env=dict(os.environ if env is None else env))
    config = deepseek.configure()
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _now(),
        "run_status": "dry_run_evidence_bundle_only" if dry_run else "started",
        "advisory_only": True,
        "deepseek_api": {
            "provider": "deepseek_official_api",
            "model": config.model,
            "endpoint": config.endpoint,
            "ready": config.ready,
            "status": config.status,
        },
        "evidence_bundle": evidence_bundle,
        "deepseek_advisory": None,
        "deepseek_text": "",
    }
    if dry_run:
        return report
    if not config.ready:
        report["run_status"] = config.status
        report["unverified_items"] = ["DeepSeek official API call 未验证/无法获取：缺少可用配置。"]
        return report

    try:
        response = deepseek.complete_json(
            system_prompt=ADVISORY_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(evidence_bundle),
            timeout=timeout_seconds,
        )
    except DeepSeekRequestError as exc:
        report["run_status"] = "blocked_deepseek_api_error"
        report["error"] = {
            "message": str(exc),
            "status_code": exc.status_code,
            "retry_after_seconds": exc.retry_after_seconds,
        }
        report["unverified_items"] = ["DeepSeek official API call 未验证/无法获取：API 请求失败。"]
        return report

    advisory = _apply_fail_closed_overrides(_normalize_advisory(response.parsed), evidence_bundle)
    report["deepseek_api"].update(
        {
            "latency_ms": response.latency_ms,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "response_id": response.response_id,
        }
    )
    report["deepseek_text"] = response.text
    report["deepseek_advisory"] = advisory or None
    report["run_status"] = "completed" if advisory else "blocked_invalid_deepseek_json"
    if not advisory:
        report["unverified_items"] = ["DeepSeek 返回内容不是可用 JSON，协同建议未验证/无法获取。"]
    return report


def render_markdown(report: dict[str, Any]) -> str:
    api = report.get("deepseek_api") if isinstance(report.get("deepseek_api"), dict) else {}
    evidence = report.get("evidence_bundle") if isinstance(report.get("evidence_bundle"), dict) else {}
    advisory = report.get("deepseek_advisory") if isinstance(report.get("deepseek_advisory"), dict) else {}
    decision = advisory.get("production_decision") if isinstance(advisory.get("production_decision"), dict) else {}
    lines = [
        "# DeepSeek Execution Coordination Report",
        "",
        f"- run_status: `{report.get('run_status')}`",
        f"- advisory_only: `{report.get('advisory_only')}`",
        f"- api_status: `{api.get('status')}`",
        f"- api_ready: `{api.get('ready')}`",
        f"- model: `{api.get('model')}`",
        f"- read_report_count: `{evidence.get('read_report_count')}`",
        f"- missing_report_count: `{evidence.get('missing_report_count')}`",
        "",
        "## Production Decision",
        "",
        f"- keep_baseline_default: `{decision.get('keep_baseline_default', True)}`",
        f"- can_promote_comfyui: `{decision.get('can_promote_comfyui', False)}`",
        f"- can_enable_vlm_autopass: `{decision.get('can_enable_vlm_autopass', False)}`",
        f"- rationale: {decision.get('rationale') or '未验证/无法获取'}",
    ]
    blockers = advisory.get("blockers") if isinstance(advisory.get("blockers"), list) else []
    lines.extend(["", "## Blockers", ""])
    if blockers:
        for blocker in blockers[:10]:
            if isinstance(blocker, dict):
                label = blocker.get("id") or blocker.get("title") or blocker.get("evidence") or "blocker"
                lines.append(f"- {label}: {blocker.get('next_action') or blocker.get('evidence') or ''}")
            else:
                lines.append(f"- {blocker}")
    else:
        lines.append("- 未验证/无法获取")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", type=Path, help="Report path to include. Defaults to key tasks reports.")
    parser.add_argument("--dry-run", action="store_true", help="Only build evidence bundle; do not call DeepSeek.")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--per-report-max-chars", type=int, default=6000)
    parser.add_argument("--output-json", type=Path, default=Path("tasks/deepseek_execution_coordination_report.json"))
    parser.add_argument("--output-md", type=Path, default=Path("tasks/deepseek_execution_coordination_report.md"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_coordination(
        report_paths=args.report,
        dry_run=bool(args.dry_run),
        timeout_seconds=args.timeout_seconds,
        per_report_max_chars=args.per_report_max_chars,
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"run_status": report.get("run_status"), "output_json": str(args.output_json)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
