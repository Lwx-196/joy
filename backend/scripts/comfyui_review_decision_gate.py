"""Validate human review decisions before importing ComfyUI A/B winners."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.scripts import comfyui_ab_report

VALID_WINNER_ROLES = {"baseline", "candidate"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("decisions") or payload.get("review_decisions") or payload.get("items") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _unit_id(decision: dict[str, Any]) -> str:
    return str(decision.get("ab_unit_id") or decision.get("unit_id") or decision.get("case_id") or "").strip()


def _winner_role(decision: dict[str, Any]) -> str:
    return str(decision.get("winner_role") or decision.get("preferred_role") or "").strip().lower()


def _winner_variant(decision: dict[str, Any]) -> str:
    return str(
        decision.get("winner_variant")
        or decision.get("winner")
        or decision.get("ab_winner")
        or decision.get("preferred_variant")
        or ""
    ).strip()


def _reviewer(decision: dict[str, Any]) -> str:
    return str(decision.get("reviewer") or "").strip()


def _manifest_units(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    units: dict[str, dict[str, Any]] = {}
    for unit in manifest.get("review_units") or []:
        if isinstance(unit, dict):
            unit_id = str(unit.get("ab_unit_id") or unit.get("unit_id") or unit.get("case_id") or "").strip()
            if unit_id:
                units[unit_id] = unit
    return units


def _variants_by_role(unit: dict[str, Any]) -> dict[str, set[str]]:
    by_role: dict[str, set[str]] = {"baseline": set(), "candidate": set()}
    for asset in unit.get("packet_assets") or []:
        if not isinstance(asset, dict):
            continue
        role = str(asset.get("role") or "").strip().lower()
        variant = str(asset.get("variant") or "").strip()
        if role in by_role and variant:
            by_role[role].add(variant)
    return by_role


def _reject(decision: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    return {
        "ab_unit_id": _unit_id(decision),
        "reason_code": code,
        "reason": reason,
        "reviewer": _reviewer(decision) or None,
        "winner_role": _winner_role(decision) or None,
        "winner_variant": _winner_variant(decision) or None,
    }


def _sanitize(decision: dict[str, Any], *, unit: dict[str, Any], winner_role: str, winner_variant: str) -> dict[str, Any]:
    return {
        "ab_unit_id": _unit_id(decision),
        "case_id": decision.get("case_id") or unit.get("case_id"),
        "view": decision.get("view") or unit.get("view"),
        "workflow": decision.get("workflow") or unit.get("workflow"),
        "winner_role": winner_role,
        "winner_variant": winner_variant,
        "reviewer": _reviewer(decision),
        "review_note": decision.get("review_note"),
    }


def validate_review_decisions(
    decision_payload: dict[str, Any],
    manifest: dict[str, Any],
    *,
    min_pairs: int = 20,
) -> dict[str, Any]:
    units = _manifest_units(manifest)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    candidate_win_count = 0
    baseline_win_count = 0

    for decision in _decisions(decision_payload):
        unit_id = _unit_id(decision)
        if not unit_id:
            rejected.append(_reject(decision, "missing_ab_unit", "缺少 ab_unit_id，无法导入人工决策。"))
            continue
        if unit_id in seen_units:
            rejected.append(_reject(decision, "duplicate_ab_unit", "同一个 ab_unit_id 只能导入一条人工决策。"))
            continue
        unit = units.get(unit_id)
        if not unit:
            rejected.append(_reject(decision, "unknown_ab_unit", "ab_unit_id 不在当前 review packet manifest 中。"))
            continue
        reviewer = _reviewer(decision)
        if not reviewer:
            rejected.append(_reject(decision, "missing_reviewer", "缺少 reviewer，不能计入 winner evidence。"))
            continue
        winner_role = _winner_role(decision)
        winner_variant = _winner_variant(decision)
        if winner_role not in VALID_WINNER_ROLES:
            rejected.append(_reject(decision, "invalid_winner_role", "winner_role 必须是 baseline 或 candidate。"))
            continue
        variants_by_role = _variants_by_role(unit)
        role_variants = variants_by_role.get(winner_role) or set()
        if not winner_variant and len(role_variants) == 1:
            winner_variant = next(iter(role_variants))
        if not winner_variant:
            rejected.append(_reject(decision, "missing_winner_variant", "缺少 winner_variant，且无法从 role 唯一推导。"))
            continue
        if winner_variant not in role_variants:
            rejected.append(_reject(decision, "winner_variant_not_in_role", "winner_variant 必须匹配对应 role 的真实 packet asset variant。"))
            continue

        seen_units.add(unit_id)
        accepted_decision = _sanitize(decision, unit=unit, winner_role=winner_role, winner_variant=winner_variant)
        accepted.append(accepted_decision)
        if winner_role == "candidate":
            candidate_win_count += 1
        else:
            baseline_win_count += 1

    accepted_count = len(accepted)
    if accepted_count == 0:
        status = "unverified_missing_human_decisions"
        decision = "未验证/无法获取：当前没有可导入的真实人工 winner 决策。"
    elif accepted_count < min_pairs:
        status = "unverified_insufficient_human_decisions"
        decision = f"未验证：可导入人工 winner 决策 {accepted_count} < {min_pairs}。"
    elif candidate_win_count < min_pairs:
        status = "unverified_candidate_wins_below_threshold"
        decision = f"未验证：candidate 人工胜出 {candidate_win_count} < {min_pairs}。"
    else:
        status = "ready_for_report"
        decision = "人工 winner 决策已达到报告导入门槛；仍不会自动 promote default。"

    sanitized = {
        "generated_at": _now(),
        "scope": "t47_sanitized_comfyui_review_decisions_v1",
        "source_scope": decision_payload.get("scope"),
        "decisions": accepted,
    }
    return {
        "generated_at": _now(),
        "scope": "t47_comfyui_review_decision_validation_v1",
        "validation_status": status,
        "ready_for_report": status == "ready_for_report",
        "decision": decision,
        "required_human_decisions_min": min_pairs,
        "manifest_review_unit_count": len(units),
        "submitted_decision_count": len(_decisions(decision_payload)),
        "accepted_decision_count": accepted_count,
        "rejected_decision_count": len(rejected),
        "candidate_win_count": candidate_win_count,
        "baseline_win_count": baseline_win_count,
        "accepted_decisions": accepted,
        "rejected_decisions": rejected,
        "sanitized_decisions": sanitized,
    }


def build_ab_report_from_validation(
    records: list[dict[str, Any]],
    validation: dict[str, Any],
    *,
    min_pairs: int = 20,
    target_pairs: int = 30,
    promotion_approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = comfyui_ab_report.summarize_ab_records(
        records,
        review_decisions=validation.get("accepted_decisions") if isinstance(validation.get("accepted_decisions"), list) else [],
        promotion_approval=promotion_approval,
        min_pairs=min_pairs,
        target_pairs=target_pairs,
    )
    report["review_decision_validation"] = {
        "validation_status": validation.get("validation_status"),
        "accepted_decision_count": validation.get("accepted_decision_count"),
        "rejected_decision_count": validation.get("rejected_decision_count"),
        "candidate_win_count": validation.get("candidate_win_count"),
        "baseline_win_count": validation.get("baseline_win_count"),
    }
    return report


def _load_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        records.extend(comfyui_ab_report.load_jsonl(path))
    return records


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate ComfyUI A/B human decisions and optionally rebuild the report.")
    parser.add_argument("--manifest-json", type=Path, required=True)
    parser.add_argument("--review-decisions-json", type=Path, required=True)
    parser.add_argument("--records-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--validation-output", type=Path, required=True)
    parser.add_argument("--sanitized-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path)
    parser.add_argument("--min-pairs", type=int, default=20)
    parser.add_argument("--target-pairs", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    validation = validate_review_decisions(
        _load_json(args.review_decisions_json),
        _load_json(args.manifest_json),
        min_pairs=int(args.min_pairs),
    )
    _write_json(args.validation_output, validation)
    _write_json(args.sanitized_output, validation["sanitized_decisions"])
    if args.report_output:
        report = build_ab_report_from_validation(
            _load_records(args.records_jsonl),
            validation,
            min_pairs=int(args.min_pairs),
            target_pairs=int(args.target_pairs),
        )
        _write_json(args.report_output, report)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
