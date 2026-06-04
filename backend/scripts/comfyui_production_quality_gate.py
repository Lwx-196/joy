"""ComfyUI production-candidate quality gate.

This gate does not promote ComfyUI. It converts real A/B, human review, and
VLM guardrail evidence into a fail-closed workflow-level action plan.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNVERIFIED = "未验证/无法获取"
BASELINE_VARIANT = "ps_model_router@default"
DEFAULT_ALLOWED_RETEST_WORKFLOWS = ["local_region_enhance_v1@conservative"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _workflow(value: dict[str, Any]) -> str:
    return str(value.get("workflow") or value.get("workflow_name") or "unknown").strip() or "unknown"


def _unit_id(value: dict[str, Any]) -> str:
    return str(value.get("ab_unit_id") or value.get("unit_id") or value.get("case_id") or "").strip()


def _role(value: dict[str, Any]) -> str:
    return str(value.get("variant_role") or value.get("role") or "").strip().lower()


def _winner_role(value: dict[str, Any]) -> str:
    return str(value.get("winner_role") or value.get("preferred_role") or "").strip().lower()


def _is_candidate_record(record: dict[str, Any]) -> bool:
    return _role(record) == "candidate" or str(record.get("variant") or "").startswith("comfyui_local:")


def _is_real_record(record: dict[str, Any]) -> bool:
    return bool(_unit_id(record)) and not bool(record.get("dry_run"))


def _latest_real_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    superseded = 0
    for record in records:
        if not _is_real_record(record):
            continue
        role = _role(record)
        if not role:
            role = "candidate" if _is_candidate_record(record) else str(record.get("variant") or "unknown")
        key = (_unit_id(record), role)
        if key in latest:
            superseded += 1
        latest[key] = record
    return list(latest.values()), superseded


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_blank_image(path: Path) -> bool:
    from PIL import Image

    with Image.open(path) as image_raw:
        image = image_raw.convert("RGB")
        extrema = image.getextrema()
    return all(low == high for low, high in extrema)


def _candidate_output_ref_defects(record: dict[str, Any]) -> set[str]:
    raw_refs = record.get("output_refs")
    if raw_refs is None:
        return set()
    output_refs = raw_refs if isinstance(raw_refs, list) else []
    if not output_refs:
        return {"candidate_failed_or_blank"}
    defects: set[str] = set()
    image_ref_seen = False
    for ref in output_refs:
        if not isinstance(ref, dict):
            continue
        path_raw = str(ref.get("path") or "").strip()
        if not path_raw:
            continue
        path = Path(path_raw)
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        image_ref_seen = True
        if not path.is_file():
            defects.add("candidate_failed_or_blank")
            continue
        try:
            if path.stat().st_size <= 0 or _looks_blank_image(path):
                defects.add("candidate_failed_or_blank")
        except Exception:  # noqa: BLE001 - unreadable candidate output must fail closed.
            defects.add("candidate_failed_or_blank")
    if not image_ref_seen:
        defects.add("candidate_failed_or_blank")
    return defects


def _qa_dimension_defects(qa: dict[str, Any]) -> set[str]:
    output_width = _as_int(qa.get("output_width"))
    output_height = _as_int(qa.get("output_height"))
    canvas_width = _as_int(qa.get("canvas_width"))
    canvas_height = _as_int(qa.get("canvas_height"))
    if None in {output_width, output_height, canvas_width, canvas_height}:
        return set()
    if output_width != canvas_width or output_height != canvas_height:
        return {"dimension_mismatch"}
    return set()


def _candidate_record_defects(record: dict[str, Any]) -> set[str]:
    defects: set[str] = set()
    if not _is_candidate_record(record):
        return defects
    status = str(record.get("status") or "").strip().lower()
    error_text = " ".join(str(record.get(key) or "") for key in ("error", "error_message")).strip().lower()
    output_refs = record.get("output_refs") if isinstance(record.get("output_refs"), list) else []
    if record.get("ok") is False or status == "failed" or error_text:
        defects.add("candidate_failed_or_blank")
    if (status == "failed" or record.get("ok") is False) and not output_refs:
        defects.add("candidate_failed_or_blank")
    if bool(record.get("fallback_used")):
        defects.add("candidate_fallback_used")
    defects.update(_candidate_output_ref_defects(record))

    qa = record.get("qa_scores") if isinstance(record.get("qa_scores"), dict) else {}
    if qa:
        defects.update(_qa_dimension_defects(qa))
        if qa.get("dimension_match") is False:
            defects.add("dimension_mismatch")
        if _as_float(qa.get("halo_score")) >= 8:
            defects.add("halo_or_edge_artifact")
        if _as_float(qa.get("mask_outside_delta")) >= 8:
            defects.add("mask_outside_delta")
        if _as_float(qa.get("subject_scale_delta")) > 0.08:
            defects.add("subject_scale_delta")
        if _as_float(qa.get("slot_center_delta")) > 0.08:
            defects.add("slot_center_delta")
        if _as_float(qa.get("color_cast_delta")) >= 6:
            defects.add("tone_color_shift")
        if _as_float(qa.get("masked_luma_delta")) <= -7:
            defects.add("tone_color_shift")
        if _as_float(qa.get("texture_detail_delta")) <= -6:
            defects.add("over_smoothing")
        shadow_contrast_delta = _as_float(qa.get("masked_shadow_contrast_delta"))
        shadow_p10_delta = _as_float(qa.get("masked_shadow_p10_delta"))
        if shadow_contrast_delta >= 16 and shadow_p10_delta <= 0:
            defects.add("over_contoured_shadow")
        highlight_p95_delta = _as_float(qa.get("masked_highlight_p95_delta"))
        highlight_p99_delta = _as_float(qa.get("masked_highlight_p99_delta"))
        specular_ratio_delta = _as_float(qa.get("masked_specular_ratio_delta"))
        texture_detail_delta = _as_float(qa.get("texture_detail_delta"))
        if (
            (highlight_p95_delta >= 10 or highlight_p99_delta >= 18)
            and (specular_ratio_delta >= 0.03 or texture_detail_delta <= 0)
        ):
            defects.add("over_waxy_highlight")
        if highlight_p99_delta >= 12 and (highlight_p95_delta < 10 or texture_detail_delta <= 0):
            defects.add("local_highlight_artifact")
        face_luma_delta = _as_float(qa.get("face_luma_delta"))
        face_background_contrast_delta = _as_float(qa.get("face_background_contrast_delta"))
        if face_luma_delta <= -4 or face_background_contrast_delta <= -4:
            defects.add("face_luma_contrast_insufficient")

    diff = record.get("difference_analysis") if isinstance(record.get("difference_analysis"), dict) else {}
    if diff:
        target_change = _as_float(diff.get("target_region_change_score"))
        non_target_change = _as_float(diff.get("non_target_change_score"))
        if 0 < target_change < 1.0 and non_target_change < 1.0:
            defects.add("candidate_weak_visible_improvement")

    if "blank" in error_text or "empty" in error_text or "空图" in error_text:
        defects.add("candidate_failed_or_blank")
    if "watermark" in error_text or "ai simulation" in error_text:
        defects.add("watermark_or_gray_border")
    if "halo" in error_text or "光晕" in error_text or "脏边" in error_text:
        defects.add("halo_or_edge_artifact")
    return defects


def _guardrail_items(vlm_guardrail: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(vlm_guardrail, dict):
        return []
    guardrail = vlm_guardrail.get("candidate_promotion_guardrail")
    if isinstance(guardrail, dict) and isinstance(guardrail.get("items"), list):
        return [item for item in guardrail["items"] if isinstance(item, dict)]
    if isinstance(vlm_guardrail.get("items"), list):
        return [item for item in vlm_guardrail["items"] if isinstance(item, dict)]
    return []


def _guardrail_status(vlm_guardrail: dict[str, Any] | None) -> str:
    if not isinstance(vlm_guardrail, dict):
        return ""
    guardrail = vlm_guardrail.get("candidate_promotion_guardrail")
    if isinstance(guardrail, dict):
        return str(guardrail.get("guardrail_status") or "").strip()
    return ""


def _text_defect_codes(text: str) -> set[str]:
    lowered = text.lower()
    defects: set[str] = set()
    if "watermark" in lowered or "ai simulation" in lowered or "gray border" in lowered or "grey border" in lowered or "灰边" in lowered:
        defects.add("watermark_or_gray_border")
    if "blank" in lowered or "empty" in lowered or "空图" in lowered:
        defects.add("candidate_failed_or_blank")
    if "halo" in lowered or "artifact" in lowered or "伪影" in lowered or "脏边" in lowered:
        defects.add("halo_or_edge_artifact")
    if "over_smoothing" in lowered or "waxy" in lowered or "过度磨皮" in lowered:
        defects.add("over_smoothing")
    if (
        "color_shift" in lowered
        or "red_cast" in lowered
        or "red cast" in lowered
        or "magenta" in lowered
        or "cyan" in lowered
        or "色偏" in lowered
        or "红偏" in lowered
    ):
        defects.add("tone_color_shift")
    if (
        "facial_feature_drift" in lowered
        or "identity_drift" in lowered
        or "lip alteration" in lowered
        or "lips" in lowered
        or "mouth" in lowered
        or "nose" in lowered
        or "eye" in lowered
        or "五官" in lowered
        or "唇" in lowered
    ):
        defects.add("identity_or_feature_drift")
    return defects


def _vlm_unit_defects(vlm_guardrail: dict[str, Any] | None) -> dict[str, set[str]]:
    defects_by_unit: dict[str, set[str]] = defaultdict(set)
    false_candidate_count = int(_as_float((vlm_guardrail or {}).get("false_candidate_promotion_count"), 0))
    for item in _guardrail_items(vlm_guardrail):
        unit_id = _unit_id(item)
        if not unit_id:
            continue
        action = str(item.get("action") or "").strip()
        if action == "hard_veto":
            defects_by_unit[unit_id].add("vlm_hard_veto")
        risk_parts = [str(flag) for flag in item.get("risk_flags") or []]
        hard_veto_reason = str(item.get("hard_veto_reason") or "").strip()
        if hard_veto_reason:
            risk_parts.append(hard_veto_reason)
        if action == "hard_veto":
            risk_parts.extend([str(item.get("reason") or ""), str(item.get("rationale") or "")])
        risk_text = " ".join(risk_parts)
        defects_by_unit[unit_id].update(_text_defect_codes(risk_text))
        if false_candidate_count > 0 and str(item.get("judge_winner_role") or "").strip() in {"", "candidate"}:
            defects_by_unit[unit_id].add("vlm_false_candidate_promotion")
    if false_candidate_count > 0 and not defects_by_unit:
        defects_by_unit["*"].add("vlm_false_candidate_promotion")
    if isinstance(vlm_guardrail, dict):
        for judgment in vlm_guardrail.get("accepted_judgments") or []:
            if not isinstance(judgment, dict) or not bool(judgment.get("false_baseline_rejection")):
                continue
            unit_id = _unit_id(judgment)
            if not unit_id:
                continue
            defects_by_unit[unit_id].add("vlm_false_baseline_rejection")
            risk_parts = [str(flag) for flag in judgment.get("risk_flags") or []]
            risk_parts.extend(
                [
                    str(judgment.get("hard_veto_reason") or ""),
                    str(judgment.get("visual_evidence_summary") or ""),
                    str(judgment.get("rationale") or ""),
                ]
            )
            defects_by_unit[unit_id].update(_text_defect_codes(" ".join(risk_parts)))
    return defects_by_unit


def _usable_decisions(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in decisions
        if isinstance(item, dict) and _unit_id(item) and _winner_role(item) in {"baseline", "candidate"}
    ]


def _recommended_action(
    *,
    workflow: str,
    allowed: bool,
    candidate_wins: int,
    hard_defects: set[str],
) -> str:
    if allowed:
        return "repair_then_retest" if hard_defects or candidate_wins > 0 else "hold_until_candidate_evidence"
    if hard_defects:
        return "disable_candidate_hard_defects"
    if candidate_wins <= 0:
        return "disable_candidate_keep_baseline"
    return "disable_candidate_outside_allowed_scope"


def build_gate_report(
    records: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    vlm_guardrail: dict[str, Any] | None = None,
    allowed_retest_workflows: list[str] | None = None,
    min_candidate_wins: int = 20,
    target_pairs: int = 30,
) -> dict[str, Any]:
    allowed = list(dict.fromkeys(allowed_retest_workflows or DEFAULT_ALLOWED_RETEST_WORKFLOWS))
    usable = _usable_decisions(decisions)
    decisions_by_unit = {_unit_id(item): item for item in usable}
    latest_records, superseded_record_count = _latest_real_records(records)
    records_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in latest_records:
        records_by_unit[_unit_id(record)].append(record)

    vlm_defects_by_unit = _vlm_unit_defects(vlm_guardrail)
    workflows = sorted(
        {
            _workflow(item)
            for item in [*latest_records, *usable]
            if _workflow(item) != "unknown"
        }
    )

    workflow_summary: list[dict[str, Any]] = []
    total_candidate_wins = 0
    total_baseline_wins = 0
    all_hard_defects: set[str] = set()
    for workflow in workflows:
        workflow_units = {
            _unit_id(item)
            for item in [*latest_records, *usable]
            if _workflow(item) == workflow and _unit_id(item)
        }
        candidate_wins = 0
        baseline_wins = 0
        hard_defects: set[str] = set()
        candidate_record_count = 0
        comparable_unit_count = 0
        for unit_id in workflow_units:
            decision = decisions_by_unit.get(unit_id)
            winner = _winner_role(decision or {})
            if winner == "candidate":
                candidate_wins += 1
            elif winner == "baseline":
                baseline_wins += 1

            unit_records = records_by_unit.get(unit_id, [])
            roles = {_role(record) for record in unit_records}
            if {"baseline", "candidate"}.issubset(roles):
                comparable_unit_count += 1
            for record in unit_records:
                if _is_candidate_record(record):
                    candidate_record_count += 1
                    hard_defects.update(_candidate_record_defects(record))
            hard_defects.update(vlm_defects_by_unit.get(unit_id, set()))
        if "*" in vlm_defects_by_unit and workflow in allowed:
            hard_defects.update(vlm_defects_by_unit["*"])

        total_candidate_wins += candidate_wins
        total_baseline_wins += baseline_wins
        all_hard_defects.update(hard_defects)
        is_allowed = workflow in allowed
        workflow_summary.append(
            {
                "workflow": workflow,
                "production_candidate_scope": "allowed_repair_retest" if is_allowed else "disabled",
                "candidate_win_count": candidate_wins,
                "baseline_win_count": baseline_wins,
                "comparable_unit_count": comparable_unit_count,
                "candidate_record_count": candidate_record_count,
                "hard_defect_count": len(hard_defects),
                "hard_defect_codes": sorted(hard_defects),
                "recommended_action": _recommended_action(
                    workflow=workflow,
                    allowed=is_allowed,
                    candidate_wins=candidate_wins,
                    hard_defects=hard_defects,
                ),
            }
        )

    false_candidate_count = int(_as_float((vlm_guardrail or {}).get("false_candidate_promotion_count"), 0))
    guardrail_status = _guardrail_status(vlm_guardrail)
    calibration_status = str((vlm_guardrail or {}).get("calibration_status") or "")
    vlm_not_calibrated = bool(calibration_status) and calibration_status != "calibrated_for_fail_closed_review"
    vlm_fail_closed = false_candidate_count > 0 or guardrail_status == "hard_veto" or vlm_not_calibrated
    if vlm_fail_closed:
        reason_code = "vlm_guardrail_fail_closed"
        if false_candidate_count > 0:
            reason = "VLM guardrail 仍有 false candidate promotion，保持 fail-closed。"
        elif vlm_not_calibrated:
            reason = "VLM judge 缺少真实可导入结果或未达到校准门槛，保持 fail-closed。"
        else:
            reason = "VLM guardrail 仍有 hard veto，保持 fail-closed。"
    elif all_hard_defects:
        reason_code = "hard_defects_present"
        reason = f"candidate 仍存在硬缺陷：{', '.join(sorted(all_hard_defects))}。"
    elif total_candidate_wins < min_candidate_wins:
        reason_code = "candidate_wins_below_threshold"
        reason = f"candidate 人工胜出 {total_candidate_wins} < {min_candidate_wins}，不能进入正式生产。"
    else:
        reason_code = "promotion_approval_required"
        reason = "质量门暂可进入人工 promotion approval，但本脚本不会自动默认启用。"

    production_ready = False
    return {
        "generated_at": _now(),
        "scope": "t90_comfyui_production_candidate_gate_v1",
        "production_gate": {
            "production_ready": production_ready,
            "promote_to_default": False,
            "reason_code": reason_code,
            "reason": reason,
            "required_candidate_wins_min": min_candidate_wins,
            "target_real_ab_pairs": target_pairs,
            "candidate_win_count": total_candidate_wins,
            "baseline_win_count": total_baseline_wins,
            "hard_defect_codes": sorted(all_hard_defects),
        },
        "formal_chain_policy": {
            "integration_mode": "candidate_layer_only_with_baseline_fallback",
            "default_provider": BASELINE_VARIANT,
            "candidate_provider": "comfyui_local",
            "publish_gate": "formal render_quality and delivery_gate remain authoritative",
        },
        "vlm_guardrail": {
            "calibration_status": (vlm_guardrail or {}).get("calibration_status"),
            "guardrail_status": _guardrail_status(vlm_guardrail) or None,
            "false_candidate_promotion_count": false_candidate_count,
            "false_baseline_rejection_count": int(_as_float((vlm_guardrail or {}).get("false_baseline_rejection_count"), 0)),
        },
        "next_retest_plan": {
            "allowed_workflows": allowed,
            "blocked_workflows": [
                item["workflow"]
                for item in workflow_summary
                if item["workflow"] not in allowed
            ],
            "execution_status": UNVERIFIED,
            "execution_reason": "只有 5291/8188 均真实可用并生成新 A/B records 后，才能更新质量结论。",
        },
        "record_policy": {
            "latest_record_per_unit_role": True,
            "input_record_count": len(records),
            "effective_record_count": len(latest_records),
            "superseded_record_count": superseded_record_count,
        },
        "workflow_summary": workflow_summary,
    }


def build_retest_plan(plan: dict[str, Any], allowed_workflows: list[str]) -> dict[str, Any]:
    allowed = set(allowed_workflows)
    out = deepcopy(plan)
    units = [unit for unit in plan.get("units") or [] if isinstance(unit, dict) and str(unit.get("workflow") or "") in allowed]
    out["generated_at"] = _now()
    out["scope"] = "t90_comfyui_local_region_retest_plan_v1"
    out["source_scope"] = plan.get("scope")
    out["allowed_workflows"] = list(dict.fromkeys(allowed_workflows))
    out["units"] = units
    out["planned_pair_count"] = len(units)
    out["retry_run_count"] = sum(len(unit.get("runs") or []) for unit in units if isinstance(unit, dict))
    return out


def _load_json(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_jsonl(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            item = json.loads(raw)
            if isinstance(item, dict):
                records.append(item)
    return records


def _load_decisions(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    raw = data.get("decisions")
    if not isinstance(raw, list):
        sanitized = data.get("sanitized_decisions") if isinstance(data.get("sanitized_decisions"), dict) else {}
        raw = sanitized.get("decisions")
    if not isinstance(raw, list):
        raw = data.get("accepted_decisions")
    return [item for item in (raw or []) if isinstance(item, dict)]


def render_markdown(report: dict[str, Any]) -> str:
    gate = report["production_gate"]
    lines = [
        "# T90 ComfyUI Production Candidate Gate",
        "",
        f"- production_ready: `{gate['production_ready']}`",
        f"- promote_to_default: `{gate['promote_to_default']}`",
        f"- reason_code: `{gate['reason_code']}`",
        f"- reason: {gate['reason']}",
        f"- next allowed workflows: `{', '.join(report['next_retest_plan']['allowed_workflows'])}`",
        "",
        "## Workflow Summary",
        "",
        "| workflow | candidate wins | baseline wins | defects | action |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for item in report.get("workflow_summary") or []:
        lines.append(
            "| {workflow} | {candidate_win_count} | {baseline_win_count} | {defects} | {recommended_action} |".format(
                workflow=item["workflow"],
                candidate_win_count=item["candidate_win_count"],
                baseline_win_count=item["baseline_win_count"],
                defects=", ".join(item["hard_defect_codes"]) or "-",
                recommended_action=item["recommended_action"],
            )
        )
    lines.extend(
        [
            "",
            "## Policy",
            "",
            "- ComfyUI remains a candidate layer with baseline fallback.",
            "- Formal render_quality and delivery_gate remain authoritative.",
            "- No workflow is promoted by this report.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build T90 ComfyUI production-candidate gate report.")
    parser.add_argument("--records-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--decisions-json", type=Path, required=True)
    parser.add_argument("--vlm-guardrail-json", type=Path)
    parser.add_argument("--plan-json", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--output-plan-json", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = build_gate_report(
        _load_jsonl(args.records_jsonl),
        _load_decisions(args.decisions_json),
        vlm_guardrail=_load_json(args.vlm_guardrail_json),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(report), encoding="utf-8")
    if args.plan_json and args.output_plan_json:
        plan = build_retest_plan(_load_json(args.plan_json), report["next_retest_plan"]["allowed_workflows"])
        args.output_plan_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_plan_json.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["production_gate"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
