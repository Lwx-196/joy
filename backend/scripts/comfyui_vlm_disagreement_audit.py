"""Build a fail-closed review packet for VLM/human disagreement samples."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_DECISION_ROLES = {"baseline", "candidate", "manual_review"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _unit_id(value: dict[str, Any]) -> str:
    return str(value.get("ab_unit_id") or value.get("unit_id") or value.get("case_id") or "").strip()


def _winner_role(value: dict[str, Any]) -> str:
    return str(value.get("winner_role") or value.get("preferred_role") or "").strip().lower()


def _asset_path(asset: dict[str, Any], packet_root: Path) -> Path | None:
    for key in ("packet_path", "source_path"):
        raw = str(asset.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = packet_root / path
        if path.is_file():
            return path
    rel = str(asset.get("packet_relative_path") or "").strip()
    if rel:
        path = packet_root / rel
        if path.is_file():
            return path
    return None


def _sha256(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _asset_ref(asset: dict[str, Any], packet_root: Path) -> dict[str, Any]:
    path = _asset_path(asset, packet_root)
    return {
        "variant": asset.get("variant"),
        "packet_relative_path": asset.get("packet_relative_path"),
        "packet_path": str(path) if path else asset.get("packet_path"),
        "source_path": asset.get("source_path"),
        "sha256": _sha256(path),
        "exists": bool(path and path.is_file()),
    }


def _judge_items_by_id(packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _unit_id(item): item
        for item in packet.get("judge_items") or []
        if isinstance(item, dict) and _unit_id(item)
    }


def _guardrail_by_id(calibration: dict[str, Any]) -> dict[str, dict[str, Any]]:
    guardrail = calibration.get("candidate_promotion_guardrail")
    raw_items = guardrail.get("items") if isinstance(guardrail, dict) else []
    return {
        _unit_id(item): item
        for item in raw_items or []
        if isinstance(item, dict) and _unit_id(item)
    }


def _disagreement_type(judgment: dict[str, Any], guardrail_item: dict[str, Any] | None) -> str:
    human = str(judgment.get("human_winner_role") or "").strip().lower()
    judge = str(judgment.get("judge_winner_role") or "").strip().lower()
    if human == "baseline" and judge == "candidate":
        return "false_candidate_promotion"
    if human == "candidate" and judge == "baseline":
        return "false_baseline_rejection"
    if guardrail_item and guardrail_item.get("action") == "requires_human_review":
        return str(guardrail_item.get("reason_code") or "manual_review_required")
    return "candidate_guardrail_review"


def build_disagreement_review_packet(
    calibration: dict[str, Any],
    packet: dict[str, Any],
    *,
    packet_root: Path,
    max_items: int = 10,
) -> dict[str, Any]:
    items_by_id = _judge_items_by_id(packet)
    guardrail_by_id = _guardrail_by_id(calibration)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for judgment in calibration.get("accepted_judgments") or []:
        if not isinstance(judgment, dict):
            continue
        unit_id = _unit_id(judgment)
        if not unit_id or unit_id in seen:
            continue
        if judgment.get("human_winner_role") != judgment.get("judge_winner_role"):
            selected.append(judgment)
            seen.add(unit_id)

    for judgment in calibration.get("auxiliary_judgments") or []:
        if len(selected) >= int(max_items):
            break
        if not isinstance(judgment, dict):
            continue
        unit_id = _unit_id(judgment)
        if not unit_id or unit_id in seen:
            continue
        selected.append(judgment)
        seen.add(unit_id)

    for guardrail_item in (calibration.get("candidate_promotion_guardrail") or {}).get("items") or []:
        if len(selected) >= int(max_items):
            break
        if not isinstance(guardrail_item, dict) or guardrail_item.get("action") != "requires_human_review":
            continue
        unit_id = _unit_id(guardrail_item)
        if not unit_id or unit_id in seen:
            continue
        selected.append(guardrail_item)
        seen.add(unit_id)

    review_units: list[dict[str, Any]] = []
    for source in selected[: max(0, int(max_items))]:
        unit_id = _unit_id(source)
        item = items_by_id.get(unit_id, {})
        baseline = _asset_ref(item.get("baseline") if isinstance(item.get("baseline"), dict) else {}, packet_root)
        candidate = _asset_ref(item.get("candidate") if isinstance(item.get("candidate"), dict) else {}, packet_root)
        guardrail_item = guardrail_by_id.get(unit_id)
        review_units.append(
            {
                "ab_unit_id": unit_id,
                "case_id": source.get("case_id") or item.get("case_id"),
                "view": source.get("view") or item.get("view"),
                "workflow": source.get("workflow") or item.get("workflow"),
                "disagreement_type": _disagreement_type(source, guardrail_item),
                "previous_human_winner_role": source.get("human_winner_role"),
                "previous_judge_winner_role": source.get("judge_winner_role"),
                "judge_provider": source.get("judge_provider"),
                "judge_model": source.get("judge_model"),
                "confidence": source.get("confidence"),
                "rationale": source.get("rationale"),
                "risk_flags": source.get("risk_flags") if isinstance(source.get("risk_flags"), list) else [],
                "baseline": baseline,
                "candidate": candidate,
                "ready_for_review": bool(baseline.get("exists") and candidate.get("exists")),
            }
        )

    return {
        "generated_at": _now(),
        "scope": "vlm_disagreement_review_packet_v1",
        "source_calibration_scope": calibration.get("scope"),
        "review_unit_count": len(review_units),
        "ready_for_review_count": sum(1 for item in review_units if item.get("ready_for_review")),
        "review_units": review_units,
        "instructions": [
            "Do not trust previous_human_winner_role or previous_judge_winner_role as the new decision.",
            "Reviewer must inspect baseline/candidate assets and fill reviewer + winner_role in a separate decisions file.",
            "This packet intentionally does not prefill winner_role.",
        ],
    }


def _decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("decisions") or payload.get("review_decisions") or payload.get("accepted_decisions") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def validate_review_decisions_dry_run(review_packet: dict[str, Any], decisions_payload: dict[str, Any]) -> dict[str, Any]:
    known_units = {
        _unit_id(item)
        for item in review_packet.get("review_units") or []
        if isinstance(item, dict) and _unit_id(item)
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for decision in _decisions(decisions_payload):
        unit_id = _unit_id(decision)
        if not unit_id or unit_id not in known_units:
            rejected.append({**decision, "reason_code": "unknown_review_unit"})
            continue
        if unit_id in seen:
            rejected.append({**decision, "reason_code": "duplicate_review_unit"})
            continue
        winner_role = _winner_role(decision)
        if winner_role not in VALID_DECISION_ROLES:
            rejected.append({**decision, "reason_code": "invalid_winner_role"})
            continue
        if not str(decision.get("reviewer") or "").strip():
            rejected.append({**decision, "reason_code": "missing_reviewer"})
            continue
        seen.add(unit_id)
        accepted.append(
            {
                "ab_unit_id": unit_id,
                "winner_role": winner_role,
                "reviewer": str(decision.get("reviewer") or "").strip(),
                "review_note": decision.get("review_note"),
            }
        )
    candidate_win_count = sum(1 for item in accepted if item.get("winner_role") == "candidate")
    baseline_win_count = sum(1 for item in accepted if item.get("winner_role") == "baseline")
    manual_review_count = sum(1 for item in accepted if item.get("winner_role") == "manual_review")
    return {
        "generated_at": _now(),
        "scope": "vlm_disagreement_review_import_dry_run_v1",
        "validation_status": "ready_for_report" if accepted and not rejected else "blocked",
        "review_unit_count": len(known_units),
        "accepted_decision_count": len(accepted),
        "rejected_decision_count": len(rejected),
        "candidate_win_count": candidate_win_count,
        "baseline_win_count": baseline_win_count,
        "manual_review_count": manual_review_count,
        "accepted_decisions": accepted,
        "rejected_decisions": rejected,
        "dry_run": True,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or validate VLM disagreement review packets.")
    parser.add_argument("--calibration-report", type=Path, required=True)
    parser.add_argument("--packet-json", type=Path, required=True)
    parser.add_argument("--packet-root", type=Path)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--decisions-json", type=Path)
    parser.add_argument("--max-items", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    packet_root = args.packet_root.resolve() if args.packet_root else args.packet_json.resolve().parent
    review = build_disagreement_review_packet(
        _load_json(args.calibration_report),
        _load_json(args.packet_json),
        packet_root=packet_root,
        max_items=int(args.max_items),
    )
    _write_json(args.review_output, review)
    if args.decisions_json:
        report = validate_review_decisions_dry_run(review, _load_json(args.decisions_json))
    else:
        report = {
            "generated_at": _now(),
            "scope": "vlm_disagreement_review_packet_report_v1",
            "review_unit_count": review.get("review_unit_count"),
            "ready_for_review_count": review.get("ready_for_review_count"),
            "decision": "review packet generated; no decisions applied.",
            "dry_run": True,
        }
    _write_json(args.report_output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
