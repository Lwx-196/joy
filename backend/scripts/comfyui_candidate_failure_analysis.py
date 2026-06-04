"""Analyze why ComfyUI candidates failed human A/B review."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

UNVERIFIED = "未验证/无法获取"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _variant_role(record: dict[str, Any]) -> str:
    return str(record.get("variant_role") or record.get("role") or "").strip().lower()


def _workflow(value: dict[str, Any]) -> str:
    return str(value.get("workflow") or value.get("workflow_name") or "unknown").strip() or "unknown"


def _unit_id(value: dict[str, Any]) -> str:
    return str(value.get("ab_unit_id") or value.get("unit_id") or value.get("case_id") or "").strip()


def _winner_role(decision: dict[str, Any]) -> str:
    return str(decision.get("winner_role") or decision.get("preferred_role") or "").strip().lower()


def _review_note(decision: dict[str, Any]) -> str:
    return str(decision.get("review_note") or "").strip()


def _has_substantive_review_note(decision: dict[str, Any]) -> bool:
    note = _review_note(decision)
    return len(note) >= 4


def _is_real_record(record: dict[str, Any]) -> bool:
    if bool(record.get("dry_run")):
        return False
    return bool(_unit_id(record))


def _qa_warning_counts(records: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for record in records:
        qa = record.get("qa_scores") if isinstance(record.get("qa_scores"), dict) else {}
        if not qa:
            continue
        if qa.get("dimension_match") is False:
            counts["dimension_mismatch"] += 1
        if float(qa.get("halo_score") or 0) >= 8:
            counts["halo_score"] += 1
        if float(qa.get("subject_scale_delta") or 0) > 0.08:
            counts["subject_scale_delta"] += 1
        if float(qa.get("mask_outside_delta") or 0) >= 8:
            counts["mask_outside_delta"] += 1
        if float(qa.get("slot_center_delta") or 0) > 0.08:
            counts["slot_center_delta"] += 1
    return counts


def _qa_metric_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("halo_score", "mask_outside_delta", "subject_scale_delta", "slot_center_delta")
    out: dict[str, Any] = {}
    for key in keys:
        values: list[float] = []
        for record in records:
            qa = record.get("qa_scores") if isinstance(record.get("qa_scores"), dict) else {}
            if key in qa:
                try:
                    values.append(float(qa[key]))
                except (TypeError, ValueError):
                    pass
        if values:
            out[key] = {
                "count": len(values),
                "avg": round(mean(values), 4),
                "max": round(max(values), 4),
            }
    return out


def _recommended_action(candidate_wins: int, total: int) -> str:
    if total <= 0:
        return "no_human_decision_evidence"
    if candidate_wins <= 0:
        return "fallback_baseline_disable_candidate"
    return "tune_workflow_then_retest"


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_decisions(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    raw = data.get("decisions") or data.get("accepted_decisions") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _load_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                records.append(item)
    return records


def analyze_candidate_failures(
    records: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    min_candidate_wins: int = 20,
) -> dict[str, Any]:
    usable_decisions = [item for item in decisions if _winner_role(item) in {"baseline", "candidate"} and _unit_id(item)]
    candidate_win_count = sum(1 for item in usable_decisions if _winner_role(item) == "candidate")
    baseline_win_count = sum(1 for item in usable_decisions if _winner_role(item) == "baseline")
    note_count = sum(1 for item in usable_decisions if _has_substantive_review_note(item))

    records_by_unit_role: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if not _is_real_record(record):
            continue
        records_by_unit_role[_unit_id(record)][_variant_role(record)].append(record)

    decisions_by_workflow: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in usable_decisions:
        decisions_by_workflow[_workflow(decision)].append(decision)

    workflow_summary: list[dict[str, Any]] = []
    for workflow, workflow_decisions in sorted(decisions_by_workflow.items(), key=lambda item: item[0]):
        workflow_candidate_wins = sum(1 for item in workflow_decisions if _winner_role(item) == "candidate")
        workflow_baseline_wins = sum(1 for item in workflow_decisions if _winner_role(item) == "baseline")
        workflow_total = len(workflow_decisions)
        candidate_records: list[dict[str, Any]] = []
        baseline_records: list[dict[str, Any]] = []
        for decision in workflow_decisions:
            unit_records = records_by_unit_role.get(_unit_id(decision), {})
            candidate_records.extend(unit_records.get("candidate", []))
            baseline_records.extend(unit_records.get("baseline", []))
        workflow_note_count = sum(1 for item in workflow_decisions if _has_substantive_review_note(item))
        workflow_summary.append(
            {
                "workflow": workflow,
                "total_decision_count": workflow_total,
                "candidate_win_count": workflow_candidate_wins,
                "baseline_win_count": workflow_baseline_wins,
                "candidate_win_rate": round(workflow_candidate_wins / workflow_total, 4) if workflow_total else 0.0,
                "recommended_action": _recommended_action(workflow_candidate_wins, workflow_total),
                "visual_failure_reason_status": "review_notes_available" if workflow_note_count else UNVERIFIED,
                "human_review_note_count": workflow_note_count,
                "candidate_status_counts": dict(sorted(Counter(str(item.get("status") or "unknown") for item in candidate_records).items())),
                "baseline_status_counts": dict(sorted(Counter(str(item.get("status") or "unknown") for item in baseline_records).items())),
                "candidate_qa_record_count": sum(1 for item in candidate_records if isinstance(item.get("qa_scores"), dict)),
                "qa_warning_counts": dict(sorted(_qa_warning_counts(candidate_records).items())),
                "qa_metric_summary": _qa_metric_summary(candidate_records),
            }
        )

    production_ready = candidate_win_count >= min_candidate_wins
    reason_code = "candidate_wins_meet_threshold" if production_ready else "candidate_wins_below_threshold"
    return {
        "generated_at": _now(),
        "scope": "t50_comfyui_candidate_failure_analysis_v1",
        "decision_count": len(usable_decisions),
        "candidate_win_count": candidate_win_count,
        "baseline_win_count": baseline_win_count,
        "required_candidate_wins_min": min_candidate_wins,
        "candidate_win_rate": round(candidate_win_count / len(usable_decisions), 4) if usable_decisions else 0.0,
        "human_review_note_count": note_count,
        "visual_failure_reason_status": "review_notes_available" if note_count else UNVERIFIED,
        "delivery_gate": {
            "production_ready": production_ready,
            "promote_to_default": False,
            "reason_code": reason_code,
            "reason": (
                f"candidate 人工胜出 {candidate_win_count} < {min_candidate_wins}，不能交付放行。"
                if not production_ready
                else "candidate 人工胜出达到最低门槛；仍需单独 promotion approval。"
            ),
        },
        "strategy": {
            "default_policy": "fallback_to_baseline" if not production_ready else "eligible_for_limited_human_review",
            "vlm_judge": "required_before_future_promotion",
            "vlm_judge_reason": "生成链路不能自证质量；独立 judge 必须先用人工 A/B 结果校准。",
            "review_note_gap": UNVERIFIED if not note_count else "review_notes_available",
        },
        "workflow_summary": workflow_summary,
        "notes": [
            "人工审核只提供胜负时，具体视觉失败原因不能从数据中可靠恢复。",
            "0 胜 workflow 默认建议回退 baseline；低胜率 workflow 先调参再重测，不允许整体 promote。",
        ],
    }


def render_markdown(analysis: dict[str, Any]) -> str:
    lines = [
        "# T50 ComfyUI Candidate Failure Analysis",
        "",
        f"- validation: {analysis['delivery_gate']['reason_code']}",
        f"- candidate wins: {analysis['candidate_win_count']}/{analysis['decision_count']}",
        f"- baseline wins: {analysis['baseline_win_count']}/{analysis['decision_count']}",
        f"- visual failure reason status: {analysis['visual_failure_reason_status']}",
        f"- default policy: {analysis['strategy']['default_policy']}",
        f"- VLM judge: {analysis['strategy']['vlm_judge']}",
        "",
        "## Workflow Summary",
        "",
        "| workflow | candidate wins | baseline wins | win rate | action | visual reason | candidate status | QA warnings |",
        "| --- | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for item in analysis.get("workflow_summary") or []:
        lines.append(
            "| {workflow} | {candidate_win_count} | {baseline_win_count} | {candidate_win_rate} | {recommended_action} | {visual_failure_reason_status} | {candidate_status_counts} | {qa_warning_counts} |".format(
                **item
            )
        )
    lines.extend(
        [
            "",
            "## Decision",
            "",
            analysis["delivery_gate"]["reason"],
            "",
            "## Next Actions",
            "",
            "- Keep baseline as default.",
            "- Disable candidate by default for 0-win workflows until workflow fixes are proven.",
            "- Tune low-win workflows with bounded changes, then rerun real A/B.",
            "- Add an independent VLM judge only after calibrating it against this human A/B set.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze ComfyUI candidate losses from human A/B decisions.")
    parser.add_argument("--records-jsonl", type=Path, action="append", default=[])
    parser.add_argument("--decisions-json", type=Path, required=True)
    parser.add_argument("--min-candidate-wins", type=int, default=20)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    analysis = analyze_candidate_failures(
        _load_records(args.records_jsonl),
        _load_decisions(args.decisions_json),
        min_candidate_wins=int(args.min_candidate_wins),
    )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(analysis), encoding="utf-8")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
