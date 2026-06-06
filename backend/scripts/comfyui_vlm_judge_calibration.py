"""Build and evaluate an independent VLM judge calibration packet.

This script never invents VLM judgments. Without a real external judge results
file, it writes a blocked report so ComfyUI cannot self-certify delivery quality.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNVERIFIED = "未验证/无法获取"
VALID_WINNER_ROLES = {"baseline", "candidate"}
DELIVERY_DEFECT_TERMS = (
    "ai simulation",
    "watermark",
    "grey border",
    "gray border",
    "blank",
    "placeholder",
    "severe artifact",
    "jagged",
    "halo",
    "dirty edge",
    "masking error",
    "over_smoothing",
    "waxy",
    "missing subject",
    "unsuitable for delivery",
    "水印",
    "灰边",
    "空图",
    "严重伪影",
    "锯齿",
    "光晕",
    "脏边",
)
WEAK_CANDIDATE_EVIDENCE_TERMS = (
    "slightly",
    "subtle",
    "more refined",
    "smoother",
    "equally good",
    "all other aspects",
    "without compromising",
    "略微",
    "稍微",
    "更顺滑",
    "基本一样",
)
CONCRETE_CANDIDATE_EVIDENCE_TERMS = (
    "baseline image is completely unsuitable",
    "baseline is completely unsuitable",
    "upside down",
    "correctly oriented",
    "reduces",
    "reduced",
    "removes",
    "blemish",
    "blemishes",
    "redness",
    "even skin tone",
    "evens out",
    "improved skin quality",
    "显著减少",
    "修复",
    "倒置",
    "方向正确",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_hard_veto(accepted: list[dict[str, Any]]) -> int:
    return sum(1 for item in accepted if item.get("hard_veto_reason"))


def _count_consensus_disagreement(accepted: list[dict[str, Any]]) -> int:
    return sum(
        1 for item in accepted
        if (item.get("pro_winner_role") and item.get("flash_winner_role"))
        and item["pro_winner_role"] != item["flash_winner_role"]
    )


def _count_below_cutoff(accepted: list[dict[str, Any]], *, cutoff: float) -> int:
    return sum(
        1 for item in accepted
        if isinstance(item.get("confidence"), (int, float)) and item["confidence"] < cutoff
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _unit_id(value: dict[str, Any]) -> str:
    return str(value.get("ab_unit_id") or value.get("unit_id") or value.get("case_id") or "").strip()


def _winner_role(value: dict[str, Any]) -> str:
    return str(value.get("winner_role") or value.get("preferred_role") or "").strip().lower()


def _winner_variant(value: dict[str, Any]) -> str:
    return str(value.get("winner_variant") or value.get("winner") or value.get("preferred_variant") or "").strip()


def _reviewer(value: dict[str, Any]) -> str:
    return str(value.get("reviewer") or "").strip()


def _decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("decisions") or payload.get("accepted_decisions") or payload.get("review_decisions") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _judgments(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = payload.get("judgments") or payload.get("vlm_judgments") or payload.get("items") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _asset_by_role(unit: dict[str, Any], role: str) -> dict[str, Any] | None:
    for asset in unit.get("packet_assets") or []:
        if isinstance(asset, dict) and str(asset.get("role") or "").strip().lower() == role:
            return asset
    return None


def _asset_ref(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "variant": str(asset.get("variant") or "").strip(),
        "packet_relative_path": asset.get("packet_relative_path"),
        "packet_path": asset.get("packet_path"),
        "source_path": asset.get("source_path"),
        "status": asset.get("status"),
        "kind": asset.get("kind"),
        "watermarked": asset.get("watermarked"),
        "simulation_job_id": asset.get("simulation_job_id"),
    }


def _human_label(decision: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "ab_unit_id": _unit_id(decision),
        "case_id": decision.get("case_id") or unit.get("case_id"),
        "view": decision.get("view") or unit.get("view"),
        "workflow": decision.get("workflow") or unit.get("workflow"),
        "winner_role": _winner_role(decision),
        "winner_variant": _winner_variant(decision),
        "reviewer": _reviewer(decision),
        "review_note": decision.get("review_note"),
    }


def build_vlm_judge_packet(manifest: dict[str, Any], human_decisions: dict[str, Any]) -> dict[str, Any]:
    units_by_id = {
        _unit_id(unit): unit
        for unit in manifest.get("review_units") or []
        if isinstance(unit, dict) and _unit_id(unit)
    }
    decisions_by_id = {
        _unit_id(decision): decision
        for decision in _decisions(human_decisions)
        if _unit_id(decision) and _winner_role(decision) in VALID_WINNER_ROLES
    }

    judge_items: list[dict[str, Any]] = []
    human_labels: list[dict[str, Any]] = []
    skipped_units: list[dict[str, Any]] = []

    for unit_id, unit in sorted(units_by_id.items(), key=lambda item: item[0]):
        baseline = _asset_by_role(unit, "baseline")
        candidate = _asset_by_role(unit, "candidate")
        if not baseline or not candidate:
            skipped_units.append(
                {
                    "ab_unit_id": unit_id,
                    "reason": "缺少 baseline 或 candidate packet asset，不能进入 VLM 盲评。",
                }
            )
            continue
        judge_items.append(
            {
                "ab_unit_id": unit_id,
                "case_id": unit.get("case_id"),
                "view": unit.get("view"),
                "workflow": unit.get("workflow"),
                "baseline": _asset_ref(baseline),
                "candidate": _asset_ref(candidate),
                "criteria": [
                    "医美交付质量：整体观感、自然度、是否适合交付给客户",
                    "主体保真：脸型、五官、轮廓、姿态不应异常变形",
                    "皮肤与纹理：不过度磨皮、不蜡像、不丢失关键细节",
                    "局部编辑质量：没有明显接缝、光晕、脏边、错误遮罩外溢",
                    "背景与服饰保持：不引入无关物体，不破坏衣服、头发、边缘",
                ],
                "required_output_schema": {
                    "ab_unit_id": "string",
                    "winner_role": "baseline|candidate",
                    "confidence": "number between 0 and 1",
                    "rationale": "brief visual reason from image evidence only",
                    "risk_flags": ["artifact", "identity_drift", "anatomy_error", "over_smoothing"],
                    "judge_provider": "independent VLM provider name",
                    "judge_model": "independent VLM model name",
                },
            }
        )
        decision = decisions_by_id.get(unit_id)
        if decision:
            human_labels.append(_human_label(decision, unit))

    return {
        "generated_at": _now(),
        "scope": "t51_independent_vlm_judge_packet_v1",
        "source_manifest_scope": manifest.get("scope"),
        "source_human_decisions_scope": human_decisions.get("scope"),
        "judge_item_count": len(judge_items),
        "human_label_count": len(human_labels),
        "skipped_unit_count": len(skipped_units),
        "judge_items": judge_items,
        "human_labels": human_labels,
        "skipped_units": skipped_units,
        "blind_review_requirement": (
            "Only send judge_items to the independent VLM. Do not send human_labels, "
            "winner_role, winner_variant, or prior T47/T50 conclusions to the judge."
        ),
        "notes": [
            "human_labels are kept only for post-hoc calibration against真实人工 A/B。judge item 本身不含人工 winner。",
            "This packet is not a VLM result and cannot unlock production readiness by itself.",
        ],
    }


def build_results_template(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": _now(),
        "scope": "t51_independent_vlm_judge_results_template_v1",
        "instructions": (
            "只填写真实独立 VLM 的输出；不得复制人工 winner，不得手填猜测值。"
            "若没有真实 VLM 输出，保持 null。"
        ),
        "judgments": [
            {
                "ab_unit_id": item.get("ab_unit_id"),
                "winner_role": None,
                "confidence": None,
                "rationale": None,
                "risk_flags": [],
                "judge_provider": None,
                "judge_model": None,
            }
            for item in packet.get("judge_items") or []
            if isinstance(item, dict)
        ],
    }


def _reject_judgment(judgment: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    return {
        "ab_unit_id": _unit_id(judgment),
        "reason_code": code,
        "reason": reason,
        "winner_role": _winner_role(judgment) or None,
        "judge_provider": judgment.get("judge_provider"),
        "judge_model": judgment.get("judge_model"),
    }


def _judge_identity(judgment: dict[str, Any]) -> tuple[str, str]:
    provider = str(judgment.get("judge_provider") or "").strip()
    model = str(judgment.get("judge_model") or "").strip()
    return provider, model


def _text_has_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


def _risk_text(item: dict[str, Any]) -> str:
    risk_flags = item.get("risk_flags") if isinstance(item.get("risk_flags"), list) else []
    return " ".join(str(flag) for flag in risk_flags)


def _candidate_guardrail_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("judge_winner_role") != "candidate":
        return None

    rationale = str(item.get("rationale") or "")
    risk_text = _risk_text(item)
    combined = f"{rationale} {risk_text}"
    has_delivery_defect = _text_has_any(risk_text, DELIVERY_DEFECT_TERMS) or _text_has_any(
        rationale,
        (
            "candidate image contains",
            "candidate contains",
            "candidate image has",
            "candidate has ai simulation",
            "candidate has a watermark",
            "candidate is a blank",
        ),
    )
    has_weak_evidence = _text_has_any(combined, WEAK_CANDIDATE_EVIDENCE_TERMS)
    has_concrete_evidence = _text_has_any(combined, CONCRETE_CANDIDATE_EVIDENCE_TERMS)

    if has_delivery_defect:
        action = "hard_veto"
        reason_code = "candidate_delivery_defect"
        reason = "VLM 选择 candidate 的同时报告了水印、灰边、空图或严重伪影等交付缺陷，不能计为质量放行。"
    elif not has_concrete_evidence:
        action = "requires_human_review"
        reason_code = (
            "weak_subjective_candidate_evidence" if has_weak_evidence else "missing_concrete_candidate_evidence"
        )
        reason = "VLM candidate 胜出理由缺少可核验的缺陷修复证据，只能进入人工复核。"
    else:
        action = "candidate_quality_evidence_supported"
        reason_code = "concrete_candidate_quality_evidence"
        reason = "VLM candidate 胜出理由包含明确缺陷修复或基线不可用证据；仍只作为 fail-closed 辅助信号。"

    return {
        "ab_unit_id": item.get("ab_unit_id"),
        "case_id": item.get("case_id"),
        "view": item.get("view"),
        "workflow": item.get("workflow"),
        "human_winner_role": item.get("human_winner_role"),
        "judge_winner_role": item.get("judge_winner_role"),
        "judge_provider": item.get("judge_provider"),
        "judge_model": item.get("judge_model"),
        "confidence": item.get("confidence"),
        "action": action,
        "reason_code": reason_code,
        "reason": reason,
        "rationale": item.get("rationale"),
        "risk_flags": item.get("risk_flags") if isinstance(item.get("risk_flags"), list) else [],
    }


def build_candidate_promotion_guardrail(accepted: list[dict[str, Any]]) -> dict[str, Any]:
    items = [
        guardrail_item
        for item in accepted
        if (guardrail_item := _candidate_guardrail_item(item)) is not None
    ]
    hard_veto_count = sum(1 for item in items if item.get("action") == "hard_veto")
    manual_review_required_count = sum(1 for item in items if item.get("action") == "requires_human_review")
    candidate_quality_clearance_count = sum(
        1 for item in items if item.get("action") == "candidate_quality_evidence_supported"
    )

    if hard_veto_count:
        status = "hard_veto"
    elif manual_review_required_count:
        status = "manual_review_required"
    elif items:
        status = "candidate_quality_evidence_supported"
    else:
        status = "no_candidate_wins"

    return {
        "guardrail_status": status,
        "candidate_win_count": len(items),
        "hard_veto_count": hard_veto_count,
        "manual_review_required_count": manual_review_required_count,
        "candidate_quality_clearance_count": candidate_quality_clearance_count,
        "rules": [
            {
                "reason_code": "candidate_delivery_defect",
                "action": "hard_veto",
                "description": "candidate 胜出但 VLM 自身报告水印、灰边、空图、严重伪影等交付缺陷时，不能进入质量放行。",
            },
            {
                "reason_code": "weak_subjective_candidate_evidence",
                "action": "requires_human_review",
                "description": "candidate 胜出理由只有略微更顺滑、主观更好或基本一样好，缺少明确缺陷修复证据时，必须人工复核。",
            },
            {
                "reason_code": "missing_concrete_candidate_evidence",
                "action": "requires_human_review",
                "description": "candidate 胜出理由未说明明确缺陷修复、基线不可用或可核验改善时，必须人工复核。",
            },
        ],
        "items": items,
    }


def evaluate_vlm_calibration(
    packet: dict[str, Any],
    *,
    judge_results: dict[str, Any] | None,
    min_judgments: int = 20,
    min_agreement: float = 0.8,
) -> dict[str, Any]:
    human_labels = {
        _unit_id(label): label
        for label in packet.get("human_labels") or []
        if isinstance(label, dict) and _unit_id(label) and _winner_role(label) in VALID_WINNER_ROLES
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for judgment in _judgments(judge_results):
        unit_id = _unit_id(judgment)
        if not unit_id:
            rejected.append(_reject_judgment(judgment, "missing_ab_unit", "缺少 ab_unit_id。"))
            continue
        if unit_id in seen:
            rejected.append(_reject_judgment(judgment, "duplicate_ab_unit", "同一个 ab_unit_id 只能导入一条 VLM 判断。"))
            continue
        label = human_labels.get(unit_id)
        if not label:
            rejected.append(_reject_judgment(judgment, "unknown_or_unlabeled_ab_unit", "ab_unit_id 不在校准人工标签中。"))
            continue
        judge_winner = _winner_role(judgment)
        if judge_winner not in VALID_WINNER_ROLES:
            rejected.append(_reject_judgment(judgment, "invalid_winner_role", "winner_role 必须是 baseline 或 candidate。"))
            continue
        provider, model = _judge_identity(judgment)
        if not provider or not model:
            rejected.append(_reject_judgment(judgment, "missing_judge_identity", "缺少 judge_provider 或 judge_model，不能计为真实独立 VLM 输出。"))
            continue
        seen.add(unit_id)
        human_winner = _winner_role(label)
        accepted.append(
            {
                "ab_unit_id": unit_id,
                "case_id": label.get("case_id"),
                "view": label.get("view"),
                "workflow": label.get("workflow"),
                "human_winner_role": human_winner,
                "human_winner_variant": label.get("winner_variant"),
                "judge_winner_role": judge_winner,
                "judge_provider": provider,
                "judge_model": model,
                "confidence": judgment.get("confidence"),
                "rationale": judgment.get("rationale"),
                "risk_flags": judgment.get("risk_flags") if isinstance(judgment.get("risk_flags"), list) else [],
                "agreement": judge_winner == human_winner,
                "false_candidate_promotion": judge_winner == "candidate" and human_winner == "baseline",
                "false_baseline_rejection": judge_winner == "baseline" and human_winner == "candidate",
            }
        )

    accepted_count = len(accepted)
    agreement_count = sum(1 for item in accepted if item["agreement"])
    false_candidate_promotion_count = sum(1 for item in accepted if item["false_candidate_promotion"])
    false_baseline_rejection_count = sum(1 for item in accepted if item["false_baseline_rejection"])
    agreement_rate = round(agreement_count / accepted_count, 4) if accepted_count else 0.0
    candidate_guardrail = build_candidate_promotion_guardrail(accepted)

    if not _judgments(judge_results):
        status = "blocked_missing_real_vlm_judgments"
        decision = f"{UNVERIFIED}：当前没有可导入的真实独立 VLM judge 输出。"
    elif accepted_count == 0:
        status = "blocked_no_accepted_real_vlm_judgments"
        decision = f"{UNVERIFIED}：提交的 VLM judge 输出全部被拒绝，不能校准。"
    elif accepted_count < min_judgments:
        status = "not_calibrated_fail_closed"
        decision = f"未校准：真实 VLM 判断 {accepted_count} < {min_judgments}，保持 fail-closed。"
    elif agreement_rate < min_agreement:
        status = "not_calibrated_fail_closed"
        decision = f"未校准：VLM 与人工一致率 {agreement_rate} < {min_agreement}，保持 fail-closed。"
    elif false_candidate_promotion_count > 0:
        status = "not_calibrated_fail_closed"
        decision = f"未校准：发现 {false_candidate_promotion_count} 条 false candidate promotion，保持 fail-closed。"
    else:
        status = "calibrated_for_fail_closed_review"
        decision = "VLM judge 已按人工 A/B 校准，可作为后续 fail-closed 辅助审核；仍不能自动 promote ComfyUI candidate。"

    judge_calibrated = status == "calibrated_for_fail_closed_review"
    return {
        "generated_at": _now(),
        "scope": "t51_vlm_judge_calibration_report_v1",
        "calibration_status": status,
        "judge_calibrated": judge_calibrated,
        "decision": decision,
        "required_judgment_count_min": int(min_judgments),
        "required_agreement_rate_min": float(min_agreement),
        "judge_item_count": int(packet.get("judge_item_count") or len(packet.get("judge_items") or [])),
        "human_label_count": len(human_labels),
        "submitted_judgment_count": len(_judgments(judge_results)),
        "accepted_judgment_count": accepted_count,
        "rejected_judgment_count": len(rejected),
        "agreement_count": agreement_count,
        "agreement_rate": agreement_rate,
        "human_candidate_count": sum(1 for item in human_labels.values() if _winner_role(item) == "candidate"),
        "human_baseline_count": sum(1 for item in human_labels.values() if _winner_role(item) == "baseline"),
        "judge_candidate_count": sum(1 for item in accepted if item["judge_winner_role"] == "candidate"),
        "judge_baseline_count": sum(1 for item in accepted if item["judge_winner_role"] == "baseline"),
        "false_candidate_promotion_count": false_candidate_promotion_count,
        "false_baseline_rejection_count": false_baseline_rejection_count,
        "hard_veto_count": _count_hard_veto(accepted),
        "consensus_disagreement_count": _count_consensus_disagreement(accepted),
        "confidence_below_cutoff_count": _count_below_cutoff(accepted, cutoff=0.85),
        "candidate_promotion_guardrail": candidate_guardrail,
        "accepted_judgments": accepted,
        "rejected_judgments": rejected,
        "production_gate": {
            "production_ready": False,
            "promote_to_default": False,
            "judge_calibrated": judge_calibrated,
            "candidate_promotion_guardrail_status": candidate_guardrail.get("guardrail_status"),
            "reason_code": status,
            "reason": (
                "独立 VLM judge 校准不是 ComfyUI candidate 生产放行；T47 人工 A/B 仍未证明 candidate 达标。"
                if judge_calibrated
                else decision
            ),
        },
        "notes": [
            "只有真实独立 VLM 输出可导入；空模板、人工 winner 复制、缺 judge 身份都不计入校准。",
            "即使 judge 校准通过，也只允许作为 fail-closed 辅助门禁，不会自动 promote 默认生成链路。",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    gate = report.get("production_gate") if isinstance(report.get("production_gate"), dict) else {}
    lines = [
        "# T51 Independent VLM Judge Calibration",
        "",
        f"- status: {report.get('calibration_status')}",
        f"- decision: {report.get('decision')}",
        f"- judge items: {report.get('judge_item_count')}",
        f"- human labels: {report.get('human_label_count')}",
        f"- submitted VLM judgments: {report.get('submitted_judgment_count')}",
        f"- accepted VLM judgments: {report.get('accepted_judgment_count')}",
        f"- agreement: {report.get('agreement_count')} / {report.get('accepted_judgment_count')} ({report.get('agreement_rate')})",
        f"- false candidate promotion: {report.get('false_candidate_promotion_count')}",
        f"- candidate promotion guardrail: {(report.get('candidate_promotion_guardrail') or {}).get('guardrail_status')}",
        f"- production_ready: {gate.get('production_ready')}",
        f"- promote_to_default: {gate.get('promote_to_default')}",
        "",
        "## Gate",
        "",
        str(gate.get("reason") or report.get("decision") or ""),
        "",
        "## Candidate Promotion Guardrail",
        "",
        f"- hard veto: {(report.get('candidate_promotion_guardrail') or {}).get('hard_veto_count')}",
        f"- manual review required: {(report.get('candidate_promotion_guardrail') or {}).get('manual_review_required_count')}",
        f"- evidence-supported candidate wins: {(report.get('candidate_promotion_guardrail') or {}).get('candidate_quality_clearance_count')}",
        "",
        "## Next",
        "",
        "- Send only `judge_items` from the packet to a real independent VLM judge.",
        "- Import the real VLM output into the results template shape.",
        "- Re-run this calibration report; blocked or low-agreement results must remain fail-closed.",
    ]
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and evaluate independent VLM judge calibration for ComfyUI A/B.")
    parser.add_argument("--manifest-json", type=Path, required=True)
    parser.add_argument("--human-decisions-json", type=Path, required=True)
    parser.add_argument("--judge-results-json", type=Path)
    parser.add_argument("--packet-output", type=Path, required=True)
    parser.add_argument("--results-template-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--report-md-output", type=Path)
    parser.add_argument("--min-judgments", type=int, default=20)
    parser.add_argument("--min-agreement", type=float, default=0.8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    packet = build_vlm_judge_packet(_load_json(args.manifest_json), _load_json(args.human_decisions_json))
    template = build_results_template(packet)
    judge_results = _load_json(args.judge_results_json) if args.judge_results_json else None
    report = evaluate_vlm_calibration(
        packet,
        judge_results=judge_results,
        min_judgments=int(args.min_judgments),
        min_agreement=float(args.min_agreement),
    )

    _write_json(args.packet_output, packet)
    _write_json(args.results_template_output, template)
    _write_json(args.report_output, report)
    if args.report_md_output:
        args.report_md_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_md_output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
