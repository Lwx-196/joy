"""Build ComfyUI A/B validation reports from real run records.

This script does not synthesize winners. If the input records do not contain
enough completed real A/B pairs, the report explicitly stays unverified.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROMOTION_APPROVAL_SCOPE = "comfyui_default_promotion_v1"
PROMOTION_APPROVAL_DECISION = "approve_default_promotion"
DEFAULT_APPROVED_WORKFLOWS = ["local_region_enhance_v1@conservative"]
REQUIRED_APPROVAL_EVIDENCE_HASH_KEYS = ("vlm_guardrail_report", "production_gate_report")


def _variant(record: dict[str, Any]) -> str:
    for key in ("variant", "model_name", "workflow_profile_name", "workflow_name", "provider"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _unit_id(record: dict[str, Any]) -> str:
    value = str(record.get("ab_unit_id") or "").strip()
    if value:
        return value
    return str(record.get("case_id") or "").strip()


def _variant_role(record: dict[str, Any]) -> str:
    return str(record.get("variant_role") or record.get("role") or "").strip().lower()


def _is_real_success(record: dict[str, Any]) -> bool:
    if bool(record.get("dry_run")):
        return False
    if record.get("ok") is False:
        return False
    if record.get("error"):
        return False
    return bool(record.get("case_id")) and _variant(record) != "unknown"


def _explicit_winner(record: dict[str, Any]) -> str | None:
    for key in ("winner", "ab_winner", "preferred_variant"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    result = record.get("ab_result")
    if isinstance(result, dict):
        value = str(result.get("winner") or "").strip()
        if value:
            return value
    return None


def _decision_unit_id(decision: dict[str, Any]) -> str:
    return str(decision.get("ab_unit_id") or decision.get("unit_id") or decision.get("case_id") or "").strip()


def _decision_winner_variant(decision: dict[str, Any]) -> str | None:
    for key in ("winner_variant", "winner", "ab_winner", "preferred_variant"):
        value = str(decision.get(key) or "").strip()
        if value:
            return value
    result = decision.get("ab_result")
    if isinstance(result, dict):
        value = str(result.get("winner") or "").strip()
        if value:
            return value
    return None


def _decision_winner_role(decision: dict[str, Any]) -> str:
    return str(decision.get("winner_role") or decision.get("preferred_role") or "").strip().lower()


def _is_failed_record(record: dict[str, Any]) -> bool:
    if bool(record.get("dry_run")):
        return False
    if record.get("ok") is False:
        return True
    if record.get("error"):
        return True
    return str(record.get("status") or "").strip().lower() == "failed"


def _is_usable_review_decision(decision: dict[str, Any]) -> bool:
    reviewer = str(decision.get("reviewer") or "").strip()
    if not reviewer:
        return False
    winner_role = _decision_winner_role(decision)
    winner_variant = _decision_winner_variant(decision)
    if winner_role not in {"candidate", "baseline"} and not winner_variant:
        return False
    return bool(_decision_unit_id(decision))


def evaluate_promotion_approval(
    approval: dict[str, Any] | None,
    *,
    report_ready: bool,
    candidate_win_count: int,
    required_candidate_wins: int,
) -> dict[str, Any]:
    if not approval:
        return {
            "status": "missing_approval",
            "approved": False,
            "required_scope": PROMOTION_APPROVAL_SCOPE,
            "required_decision": PROMOTION_APPROVAL_DECISION,
            "reason": "默认启用需要单独的人工批准文件；A/B 达标不会自动 promote。",
        }
    if not report_ready:
        return {
            "status": "blocked_report_not_ready",
            "approved": False,
            "required_scope": PROMOTION_APPROVAL_SCOPE,
            "required_decision": PROMOTION_APPROVAL_DECISION,
            "reason": "A/B report 未达到 ready_for_human_review，不能批准默认启用。",
        }
    scope = str(approval.get("scope") or "").strip()
    decision = str(approval.get("decision") or "").strip()
    approver = str(approval.get("approver") or approval.get("approved_by") or "").strip()
    raw_workflows = approval.get("approved_workflows") or approval.get("workflow") or approval.get("approved_workflow")
    if isinstance(raw_workflows, str):
        approved_workflows = [item.strip() for item in raw_workflows.replace("\n", ",").split(",") if item.strip()]
    elif isinstance(raw_workflows, list):
        approved_workflows = [str(item).strip() for item in raw_workflows if str(item).strip()]
    else:
        approved_workflows = []
    evidence_hashes = approval.get("approved_evidence_sha256") if isinstance(approval.get("approved_evidence_sha256"), dict) else {}
    missing_evidence_hash = any(
        not str(evidence_hashes.get(key) or "").startswith("sha256:")
        for key in REQUIRED_APPROVAL_EVIDENCE_HASH_KEYS
    )
    if (
        scope != PROMOTION_APPROVAL_SCOPE
        or decision != PROMOTION_APPROVAL_DECISION
        or not approver
        or not approved_workflows
        or missing_evidence_hash
        or candidate_win_count < required_candidate_wins
    ):
        return {
            "status": "invalid_approval",
            "approved": False,
            "required_scope": PROMOTION_APPROVAL_SCOPE,
            "required_decision": PROMOTION_APPROVAL_DECISION,
            "approver": approver or None,
            "approved_workflows": approved_workflows,
            "required_evidence_hash_keys": list(REQUIRED_APPROVAL_EVIDENCE_HASH_KEYS),
            "reason": "批准文件必须包含正确 scope、decision、approver、workflow scope、证据 hash，且 candidate wins 达到门槛。",
        }
    return {
        "status": "approved",
        "approved": True,
        "scope": scope,
        "decision": decision,
        "approver": approver,
        "approved_workflows": approved_workflows,
        "approved_evidence_sha256": {
            key: str(evidence_hashes.get(key))
            for key in REQUIRED_APPROVAL_EVIDENCE_HASH_KEYS
        },
        "reason": str(approval.get("reason") or "").strip() or None,
    }


def summarize_ab_records(
    records: list[dict[str, Any]],
    *,
    review_decisions: list[dict[str, Any]] | None = None,
    promotion_approval: dict[str, Any] | None = None,
    min_pairs: int = 20,
    target_pairs: int = 30,
) -> dict[str, Any]:
    dry_run_count = sum(1 for record in records if bool(record.get("dry_run")))
    failed_record_count = sum(1 for record in records if _is_failed_record(record))
    real_records = [record for record in records if _is_real_success(record)]
    variants = sorted({_variant(record) for record in records if _variant(record) != "unknown"})
    by_case: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for record in real_records:
        unit_id = _unit_id(record)
        if unit_id:
            by_case[unit_id][_variant(record)].append(record)

    comparable_cases = {
        case_id: variant_records
        for case_id, variant_records in by_case.items()
        if len(variant_records) >= 2
    }
    candidate_variants_by_unit: dict[str, set[str]] = {}
    baseline_variants_by_unit: dict[str, set[str]] = {}
    for unit_id, variant_records in comparable_cases.items():
        candidate_variants: set[str] = set()
        baseline_variants: set[str] = set()
        for variant, records_for_variant in variant_records.items():
            roles = {_variant_role(record) for record in records_for_variant}
            if "candidate" in roles or str(variant).startswith("comfyui_local:"):
                candidate_variants.add(variant)
            if "baseline" in roles or str(variant).startswith("ps_model_router"):
                baseline_variants.add(variant)
        candidate_variants_by_unit[unit_id] = candidate_variants
        baseline_variants_by_unit[unit_id] = baseline_variants

    wins_by_variant: dict[str, int] = defaultdict(int)
    explicit_winners_by_unit: dict[str, str] = {}
    for unit_id, variant_records in comparable_cases.items():
        winners = {
            winner
            for records_for_variant in variant_records.values()
            for record in records_for_variant
            for winner in [_explicit_winner(record)]
            if winner
        }
        if len(winners) == 1:
            explicit_winners_by_unit[unit_id] = next(iter(winners))

    decisions_by_unit: dict[str, dict[str, Any]] = {}
    ignored_review_decision_count = 0
    for decision in review_decisions or []:
        if not isinstance(decision, dict):
            ignored_review_decision_count += 1
            continue
        if not _is_usable_review_decision(decision):
            ignored_review_decision_count += 1
            continue
        unit_id = _decision_unit_id(decision)
        if unit_id:
            decisions_by_unit[unit_id] = decision

    winner_evidence_count = 0
    candidate_win_count = 0
    baseline_win_count = 0
    for unit_id in comparable_cases:
        winner_variant = explicit_winners_by_unit.get(unit_id)
        winner_role = ""
        decision = decisions_by_unit.get(unit_id)
        if decision:
            winner_variant = _decision_winner_variant(decision) or winner_variant
            winner_role = _decision_winner_role(decision)
        if not winner_variant and winner_role == "candidate":
            candidates = sorted(candidate_variants_by_unit.get(unit_id) or [])
            winner_variant = candidates[0] if len(candidates) == 1 else "candidate"
        if not winner_variant and winner_role == "baseline":
            baselines = sorted(baseline_variants_by_unit.get(unit_id) or [])
            winner_variant = baselines[0] if len(baselines) == 1 else "baseline"
        if not winner_variant:
            continue
        wins_by_variant[winner_variant] += 1
        winner_evidence_count += 1
        if winner_role == "candidate" or winner_variant in candidate_variants_by_unit.get(unit_id, set()):
            candidate_win_count += 1
        elif winner_role == "baseline" or winner_variant in baseline_variants_by_unit.get(unit_id, set()):
            baseline_win_count += 1

    comparable_pair_count = len(comparable_cases)
    comparable_case_ids = sorted(
        {
            str(record.get("case_id"))
            for variant_records in comparable_cases.values()
            for records_for_variant in variant_records.values()
            for record in records_for_variant
            if record.get("case_id") is not None
        },
        key=str,
    )
    if comparable_pair_count == 0 and dry_run_count:
        status = "dry_run_only"
        decision = "无法获取 20-30 组真实 A/B 胜出；当前只有 dry-run/待提交计划，不能升级为默认。"
    elif comparable_pair_count < min_pairs:
        status = "unverified_insufficient_real_ab"
        decision = (
            f"未验证：真实可比 A/B 组数 {comparable_pair_count} < {min_pairs}，"
            "不能视为 20-30 组真实胜出。"
        )
    elif winner_evidence_count < min_pairs:
        status = "unverified_missing_winner_evidence"
        decision = (
            f"未验证：已有真实可比 A/B 组数 {comparable_pair_count}，"
            f"但明确胜出证据只有 {winner_evidence_count} 组。"
        )
    elif candidate_win_count < min_pairs:
        status = "unverified_candidate_wins_below_threshold"
        decision = (
            f"未验证：已有明确胜出证据 {winner_evidence_count} 组，"
            f"但 ComfyUI candidate 胜出只有 {candidate_win_count} 组 < {min_pairs}。"
        )
    else:
        status = "ready_for_human_review"
        decision = "已达到最低真实 A/B 样本门槛；仍需人工复核胜出质量后再考虑默认启用。"

    ready_for_human_review = status == "ready_for_human_review"
    promotion = evaluate_promotion_approval(
        promotion_approval,
        report_ready=ready_for_human_review,
        candidate_win_count=candidate_win_count,
        required_candidate_wins=min_pairs,
    )
    promote_to_default = bool(promotion.get("approved")) and ready_for_human_review

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "comfyui_real_ab_validation_v1",
        "validation_status": status,
        "ready_for_human_review": ready_for_human_review,
        "decision": decision,
        "promote_to_default": promote_to_default,
        "promotion_approval": promotion,
        "required_real_ab_pairs_min": min_pairs,
        "target_real_ab_pairs": target_pairs,
        "total_record_count": len(records),
        "dry_run_record_count": dry_run_count,
        "failed_record_count": failed_record_count,
        "real_success_record_count": len(real_records),
        "comparable_pair_count": comparable_pair_count,
        "winner_evidence_count": winner_evidence_count,
        "ignored_review_decision_count": ignored_review_decision_count,
        "candidate_win_count": candidate_win_count,
        "baseline_win_count": baseline_win_count,
        "variants": variants,
        "wins_by_variant": dict(sorted(wins_by_variant.items())),
        "case_ids_with_comparable_pairs": comparable_case_ids,
        "comparable_unit_ids": sorted(comparable_cases.keys(), key=str),
    }


def build_review_decisions_template(records: list[dict[str, Any]]) -> dict[str, Any]:
    real_records = [record for record in records if _is_real_success(record)]
    by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in real_records:
        unit_id = _unit_id(record)
        if unit_id:
            by_unit[unit_id].append(record)
    items: list[dict[str, Any]] = []
    for unit_id in sorted(by_unit, key=str):
        unit_records = by_unit[unit_id]
        roles = {_variant_role(record) for record in unit_records}
        if not {"baseline", "candidate"}.issubset(roles):
            continue
        review_assets = []
        for record in sorted(unit_records, key=lambda item: _variant_role(item)):
            review_assets.append(
                {
                    "role": _variant_role(record),
                    "variant": _variant(record),
                    "status": record.get("status"),
                    "simulation_job_id": record.get("simulation_job_id"),
                    "output_refs": record.get("output_refs") if isinstance(record.get("output_refs"), list) else [],
                }
            )
        items.append(
            {
                "ab_unit_id": unit_id,
                "case_id": unit_records[0].get("case_id"),
                "view": unit_records[0].get("view"),
                "workflow": unit_records[0].get("workflow"),
                "variants": sorted({_variant(record) for record in unit_records}),
                "review_assets": review_assets,
                "winner_role": None,
                "winner_variant": None,
                "reviewer": None,
                "review_note": None,
                "decision_required": True,
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "t45_comfyui_ab_review_decisions_template_v1",
        "instructions": "人工 reviewer 必须填写 reviewer、winner_role 或 winner_variant；空模板不会计入 winner evidence。",
        "decisions": items,
    }


def build_promotion_approval_template(report: dict[str, Any]) -> dict[str, Any]:
    report_ready = bool(report.get("ready_for_human_review"))
    candidate_win_count = int(report.get("candidate_win_count") or 0)
    required_candidate_wins = int(report.get("required_real_ab_pairs_min") or 20)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": PROMOTION_APPROVAL_SCOPE,
        "decision": PROMOTION_APPROVAL_DECISION,
        "approver": None,
        "reason": None,
        "approved_workflows": list(DEFAULT_APPROVED_WORKFLOWS),
        "approved_evidence_sha256": {
            key: None
            for key in REQUIRED_APPROVAL_EVIDENCE_HASH_KEYS
        },
        "report_validation_status": report.get("validation_status"),
        "ready_for_human_review": report_ready,
        "comparable_pair_count": report.get("comparable_pair_count"),
        "winner_evidence_count": report.get("winner_evidence_count"),
        "candidate_win_count": candidate_win_count,
        "can_approve_now": False,
        "ab_prerequisites_met": report_ready and candidate_win_count >= required_candidate_wins,
        "approval_prerequisites_met": False,
        "approval_blockers": [
            "missing_approver",
            "missing_evidence_hashes",
            "requires_runtime_vlm_and_production_gate_check",
        ],
        "instructions": "只有真实负责人填写 approver、确认 workflow scope，并填入最新 VLM guardrail 与 production gate 报告 sha256 后才可作为默认启用批准；不要用空模板或自动脚本代替人工批准。",
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            records.append(item)
    return records


def load_review_decisions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        raw = data.get("decisions") or data.get("review_decisions") or data.get("items") or []
    else:
        raw = data
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def load_promotion_approval(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize real ComfyUI A/B validation evidence.")
    parser.add_argument("--records-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--dry-run-json", type=Path, action="append", default=[])
    parser.add_argument("--review-decisions-json", type=Path, action="append", default=[])
    parser.add_argument("--review-template-output", type=Path)
    parser.add_argument("--promotion-approval-json", type=Path)
    parser.add_argument("--promotion-template-output", type=Path)
    parser.add_argument("--min-pairs", type=int, default=20)
    parser.add_argument("--target-pairs", type=int, default=30)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    records: list[dict[str, Any]] = []
    for path in args.records_jsonl:
        records.extend(load_jsonl(path))
    for path in args.dry_run_json:
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data.get("jobs") if isinstance(data, dict) else []
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict):
                    request = job.get("request") if isinstance(job.get("request"), dict) else {}
                    records.append(
                        {
                            "case_id": job.get("case_id"),
                            "variant": request.get("model_name") or request.get("provider"),
                            "dry_run": True,
                        }
                    )
        units = data.get("units") if isinstance(data, dict) else []
        if isinstance(units, list):
            for unit in units:
                if not isinstance(unit, dict):
                    continue
                for run in unit.get("runs") or []:
                    if not isinstance(run, dict):
                        continue
                    records.append(
                        {
                            "ab_unit_id": unit.get("ab_unit_id"),
                            "case_id": unit.get("case_id"),
                            "variant": run.get("variant"),
                            "variant_role": run.get("role"),
                            "dry_run": True,
                        }
                    )
    review_decisions: list[dict[str, Any]] = []
    for path in args.review_decisions_json:
        review_decisions.extend(load_review_decisions(path))
    promotion_approval = load_promotion_approval(args.promotion_approval_json) if args.promotion_approval_json else None
    report = summarize_ab_records(
        records,
        review_decisions=review_decisions,
        promotion_approval=promotion_approval,
        min_pairs=int(args.min_pairs),
        target_pairs=int(args.target_pairs),
    )
    if args.review_template_output:
        template = build_review_decisions_template(records)
        args.review_template_output.parent.mkdir(parents=True, exist_ok=True)
        args.review_template_output.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.promotion_template_output:
        template = build_promotion_approval_template(report)
        args.promotion_template_output.parent.mkdir(parents=True, exist_ok=True)
        args.promotion_template_output.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["validation_status"] == "ready_for_human_review" else 2


if __name__ == "__main__":
    raise SystemExit(main())
