"""C0.5.2 — Validate the three A/B validation evidence reports against the
canonical contract documented in ``docs/contracts/ab-validation-schema.md``.

This validator only checks **shape, types, value membership, and
cross-report invariants**. It does NOT recompute sha256 (that is the
responsibility of ``backend/scripts/compute_manifest_hashes.py --validate``)
nor verify the approval signature against the manifest.

CLI::

    # Validate the three canonical files in their default locations:
    python -m backend.scripts.validate_ab_validation_reports

    # Override one or more paths (used by tests + the staging soak gate):
    python -m backend.scripts.validate_ab_validation_reports \\
        --report /tmp/ab.json --vlm /tmp/vlm.json --production /tmp/pg.json

    # Validate the bundled sample fixtures (smoke test):
    python -m backend.scripts.validate_ab_validation_reports --samples

Exit code is ``0`` iff zero issues are emitted across all three reports.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
AB_RUNS_DIR_RELPATH = Path("case-workbench-ai/ab_runs")

CANONICAL_AB_REPORT_FILENAME = "t47_comfyui_ab_report.json"
CANONICAL_VLM_GUARDRAIL_FILENAME = "vlm_guardrail_report.json"
CANONICAL_PRODUCTION_GATE_FILENAME = "comfyui_production_gate.json"

SAMPLES_DIR_RELPATH = AB_RUNS_DIR_RELPATH / "samples"
SAMPLE_AB_REPORT_FILENAME = "t47_comfyui_ab_report.sample.json"
SAMPLE_VLM_GUARDRAIL_FILENAME = "vlm_guardrail_report.sample.json"
SAMPLE_PRODUCTION_GATE_FILENAME = "comfyui_production_gate.sample.json"

REQUIRED_APPROVAL_STATUS = "approved"
REQUIRED_APPROVAL_SCOPE = "comfyui_default_promotion_v1"
REQUIRED_APPROVAL_DECISION = "approve_default_promotion"
REQUIRED_PRODUCTION_REASON_CODE = "promotion_approval_required"

VALID_CALIBRATION_STATUSES: frozenset[str] = frozenset(
    {"calibrated_for_fail_closed_review", "not_calibrated_fail_closed", "pending"}
)
VALID_GUARDRAIL_STATUSES: frozenset[str] = frozenset(
    {"pass", "manual_review_required", "hard_veto"}
)
REQUIRED_EVIDENCE_SHA_KEYS: frozenset[str] = frozenset(
    {"vlm_guardrail_report", "production_gate_report"}
)
SHA256_PREFIX = "sha256:"
SHA256_HEX_LEN = 64


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_negative_int(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _is_float_in_unit_interval(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return 0.0 <= float(value) <= 1.0


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_iso8601(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _is_sha256_hex(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(SHA256_PREFIX):
        return False
    body = value[len(SHA256_PREFIX):]
    if len(body) != SHA256_HEX_LEN:
        return False
    try:
        int(body, 16)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Approval block validation (shared by AB report)
# ---------------------------------------------------------------------------


def _validate_promotion_approval(
    approval: Any,
    *,
    required_workflow: str | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(approval, dict):
        issues.append(
            ValidationIssue(
                "approval_missing",
                "promotion_approval",
                "promotion_approval must be an object when promote_to_default=true",
            )
        )
        return issues

    if approval.get("status") != REQUIRED_APPROVAL_STATUS:
        issues.append(
            ValidationIssue(
                "approval_status_invalid",
                "promotion_approval.status",
                f"status must be {REQUIRED_APPROVAL_STATUS!r}",
            )
        )
    if approval.get("approved") is not True:
        issues.append(
            ValidationIssue(
                "approval_not_approved",
                "promotion_approval.approved",
                "approved must be the literal boolean true",
            )
        )
    if approval.get("scope") != REQUIRED_APPROVAL_SCOPE:
        issues.append(
            ValidationIssue(
                "approval_scope_invalid",
                "promotion_approval.scope",
                f"scope must be {REQUIRED_APPROVAL_SCOPE!r}",
            )
        )
    if approval.get("decision") != REQUIRED_APPROVAL_DECISION:
        issues.append(
            ValidationIssue(
                "approval_decision_invalid",
                "promotion_approval.decision",
                f"decision must be {REQUIRED_APPROVAL_DECISION!r}",
            )
        )

    approver = approval.get("approver") or approval.get("approved_by")
    if not _is_non_empty_str(approver):
        issues.append(
            ValidationIssue(
                "approver_missing",
                "promotion_approval.approver",
                "approver (or approved_by) must be a non-empty string",
            )
        )

    if _parse_iso8601(approval.get("approved_at")) is None:
        issues.append(
            ValidationIssue(
                "approved_at_invalid",
                "promotion_approval.approved_at",
                "approved_at must be an ISO-8601 timestamp",
            )
        )

    raw_expires = approval.get("expires_at")
    if raw_expires is not None and _parse_iso8601(raw_expires) is None:
        issues.append(
            ValidationIssue(
                "expires_at_invalid",
                "promotion_approval.expires_at",
                "expires_at, when present, must be an ISO-8601 timestamp",
            )
        )

    workflows = approval.get("approved_workflows")
    if not isinstance(workflows, list) or not workflows or not all(
        isinstance(item, str) and item.strip() for item in workflows
    ):
        issues.append(
            ValidationIssue(
                "approved_workflows_invalid",
                "promotion_approval.approved_workflows",
                "approved_workflows must be a non-empty list of non-empty strings",
            )
        )
    elif required_workflow and required_workflow not in workflows:
        issues.append(
            ValidationIssue(
                "approved_workflows_missing_ga_scope",
                "promotion_approval.approved_workflows",
                f"GA scope requires {required_workflow!r} in approved_workflows",
            )
        )

    evidence = approval.get("approved_evidence_sha256") or approval.get("evidence_sha256")
    if not isinstance(evidence, dict):
        issues.append(
            ValidationIssue(
                "evidence_sha256_missing",
                "promotion_approval.approved_evidence_sha256",
                "approved_evidence_sha256 must be an object",
            )
        )
    else:
        missing = REQUIRED_EVIDENCE_SHA_KEYS - set(evidence.keys())
        for key in sorted(missing):
            issues.append(
                ValidationIssue(
                    "evidence_sha256_key_missing",
                    f"promotion_approval.approved_evidence_sha256.{key}",
                    f"required key {key!r} missing from approved_evidence_sha256",
                )
            )
        for key, value in evidence.items():
            if not _is_sha256_hex(value):
                issues.append(
                    ValidationIssue(
                        "evidence_sha256_invalid",
                        f"promotion_approval.approved_evidence_sha256.{key}",
                        f"value must look like sha256:<64 hex>, got {value!r}",
                    )
                )

    return issues


# ---------------------------------------------------------------------------
# Per-report validators
# ---------------------------------------------------------------------------


def validate_ab_validation_report(
    report: Any,
    *,
    required_workflow: str | None = None,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(report, dict):
        issues.append(
            ValidationIssue(
                "report_invalid_type",
                "",
                "AB validation report must be a JSON object",
            )
        )
        return issues

    status = report.get("validation_status")
    if not _is_non_empty_str(status):
        issues.append(
            ValidationIssue(
                "validation_status_missing",
                "validation_status",
                "validation_status must be a non-empty string",
            )
        )

    if not isinstance(report.get("ready_for_human_review"), bool):
        issues.append(
            ValidationIssue(
                "ready_for_human_review_invalid",
                "ready_for_human_review",
                "ready_for_human_review must be boolean",
            )
        )

    promote = report.get("promote_to_default")
    if not isinstance(promote, bool):
        issues.append(
            ValidationIssue(
                "promote_to_default_invalid",
                "promote_to_default",
                "promote_to_default must be boolean",
            )
        )
        promote = None

    comparable = report.get("comparable_pair_count")
    winner = report.get("winner_evidence_count")
    candidate_wins = report.get("candidate_win_count")

    for field, value in (
        ("comparable_pair_count", comparable),
        ("winner_evidence_count", winner),
        ("candidate_win_count", candidate_wins),
    ):
        if not _is_non_negative_int(value):
            issues.append(
                ValidationIssue(
                    "count_invalid",
                    field,
                    f"{field} must be a non-negative integer, got {value!r}",
                )
            )

    if (
        _is_non_negative_int(winner)
        and _is_non_negative_int(candidate_wins)
        and winner < candidate_wins
    ):
        issues.append(
            ValidationIssue(
                "winner_evidence_below_wins",
                "winner_evidence_count",
                f"winner_evidence_count ({winner}) must be >= candidate_win_count ({candidate_wins})",
            )
        )

    if promote is True:
        issues.extend(
            _validate_promotion_approval(
                report.get("promotion_approval"),
                required_workflow=required_workflow,
            )
        )

    return issues


def _read_guardrail_field(
    report: dict[str, Any],
    name: str,
    *,
    nested_keys: Sequence[str] = ("vlm_guardrail", "candidate_promotion_guardrail"),
) -> Any:
    """Resolve a guardrail field, preferring the top-level then nested aliases."""
    if name in report:
        return report.get(name)
    for nested_key in nested_keys:
        nested = report.get(nested_key)
        if isinstance(nested, dict) and name in nested:
            return nested.get(name)
    return None


def validate_vlm_guardrail_report(report: Any) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(report, dict):
        issues.append(
            ValidationIssue(
                "report_invalid_type",
                "",
                "VLM guardrail report must be a JSON object",
            )
        )
        return issues

    calibration = _read_guardrail_field(report, "calibration_status")
    if calibration not in VALID_CALIBRATION_STATUSES:
        issues.append(
            ValidationIssue(
                "calibration_status_invalid",
                "calibration_status",
                f"calibration_status must be one of {sorted(VALID_CALIBRATION_STATUSES)}, got {calibration!r}",
            )
        )

    for field in ("accepted_judgment_count", "required_judgment_count_min", "false_candidate_promotion_count"):
        value = _read_guardrail_field(report, field)
        if not _is_non_negative_int(value):
            issues.append(
                ValidationIssue(
                    "count_invalid",
                    field,
                    f"{field} must be a non-negative integer, got {value!r}",
                )
            )

    for field in ("agreement_rate", "required_agreement_rate_min"):
        value = _read_guardrail_field(report, field)
        if not _is_float_in_unit_interval(value):
            issues.append(
                ValidationIssue(
                    "agreement_rate_out_of_range",
                    field,
                    f"{field} must be a float in [0.0, 1.0], got {value!r}",
                )
            )

    guardrail = report.get("candidate_promotion_guardrail")
    if not isinstance(guardrail, dict):
        issues.append(
            ValidationIssue(
                "candidate_promotion_guardrail_missing",
                "candidate_promotion_guardrail",
                "candidate_promotion_guardrail must be an object",
            )
        )
    else:
        status = guardrail.get("guardrail_status")
        if status not in VALID_GUARDRAIL_STATUSES:
            issues.append(
                ValidationIssue(
                    "guardrail_status_invalid",
                    "candidate_promotion_guardrail.guardrail_status",
                    f"guardrail_status must be one of {sorted(VALID_GUARDRAIL_STATUSES)}, got {status!r}",
                )
            )
        if not _is_non_negative_int(guardrail.get("manual_review_required_count")):
            issues.append(
                ValidationIssue(
                    "count_invalid",
                    "candidate_promotion_guardrail.manual_review_required_count",
                    "manual_review_required_count must be a non-negative integer",
                )
            )

    return issues


def validate_production_gate_report(report: Any) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not isinstance(report, dict):
        issues.append(
            ValidationIssue(
                "report_invalid_type",
                "",
                "Production gate report must be a JSON object",
            )
        )
        return issues

    gate = report.get("production_gate")
    if not isinstance(gate, dict):
        issues.append(
            ValidationIssue(
                "production_gate_missing",
                "production_gate",
                "production_gate must be an object",
            )
        )
        return issues

    if gate.get("reason_code") != REQUIRED_PRODUCTION_REASON_CODE:
        issues.append(
            ValidationIssue(
                "reason_code_invalid",
                "production_gate.reason_code",
                f"reason_code must be {REQUIRED_PRODUCTION_REASON_CODE!r}, got {gate.get('reason_code')!r}",
            )
        )

    defects = gate.get("hard_defect_codes")
    if not isinstance(defects, list) or not all(isinstance(item, str) for item in defects):
        issues.append(
            ValidationIssue(
                "hard_defect_codes_invalid",
                "production_gate.hard_defect_codes",
                "hard_defect_codes must be a list of strings",
            )
        )

    for field in ("candidate_win_count", "required_candidate_wins_min"):
        value = gate.get(field)
        if not _is_non_negative_int(value):
            issues.append(
                ValidationIssue(
                    "count_invalid",
                    f"production_gate.{field}",
                    f"{field} must be a non-negative integer, got {value!r}",
                )
            )

    return issues


def validate_cross_report_invariants(
    ab_report: Any,
    production_report: Any,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not (isinstance(ab_report, dict) and isinstance(production_report, dict)):
        return issues
    ab_wins = ab_report.get("candidate_win_count")
    gate = production_report.get("production_gate")
    pg_wins = gate.get("candidate_win_count") if isinstance(gate, dict) else None
    if _is_non_negative_int(ab_wins) and _is_non_negative_int(pg_wins) and ab_wins != pg_wins:
        issues.append(
            ValidationIssue(
                "cross_report_drift",
                "candidate_win_count",
                f"AB report ({ab_wins}) and production gate ({pg_wins}) disagree on candidate_win_count",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReportPaths:
    ab_report: Path
    vlm_guardrail: Path
    production_gate: Path


def _canonical_paths(repo_root: Path) -> _ReportPaths:
    base = repo_root / AB_RUNS_DIR_RELPATH
    return _ReportPaths(
        ab_report=base / CANONICAL_AB_REPORT_FILENAME,
        vlm_guardrail=base / CANONICAL_VLM_GUARDRAIL_FILENAME,
        production_gate=base / CANONICAL_PRODUCTION_GATE_FILENAME,
    )


def _sample_paths(repo_root: Path) -> _ReportPaths:
    base = repo_root / SAMPLES_DIR_RELPATH
    return _ReportPaths(
        ab_report=base / SAMPLE_AB_REPORT_FILENAME,
        vlm_guardrail=base / SAMPLE_VLM_GUARDRAIL_FILENAME,
        production_gate=base / SAMPLE_PRODUCTION_GATE_FILENAME,
    )


def _load_json(path: Path) -> tuple[Any, list[ValidationIssue]]:
    if not path.is_file():
        return None, [
            ValidationIssue(
                "report_missing",
                str(path),
                f"file not found: {path}",
            )
        ]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except (OSError, json.JSONDecodeError) as exc:
        return None, [
            ValidationIssue(
                "report_unreadable",
                str(path),
                f"failed to read/parse: {exc}",
            )
        ]


def validate_all(
    paths: _ReportPaths,
    *,
    required_workflow: str | None = None,
) -> dict[str, list[ValidationIssue]]:
    ab_data, ab_io_issues = _load_json(paths.ab_report)
    vlm_data, vlm_io_issues = _load_json(paths.vlm_guardrail)
    pg_data, pg_io_issues = _load_json(paths.production_gate)
    results: dict[str, list[ValidationIssue]] = {
        "ab_report": list(ab_io_issues),
        "vlm_guardrail_report": list(vlm_io_issues),
        "production_gate_report": list(pg_io_issues),
        "cross_report": [],
    }
    if ab_data is not None:
        results["ab_report"].extend(
            validate_ab_validation_report(ab_data, required_workflow=required_workflow)
        )
    if vlm_data is not None:
        results["vlm_guardrail_report"].extend(validate_vlm_guardrail_report(vlm_data))
    if pg_data is not None:
        results["production_gate_report"].extend(validate_production_gate_report(pg_data))
    if ab_data is not None and pg_data is not None:
        results["cross_report"].extend(validate_cross_report_invariants(ab_data, pg_data))
    return results


def _flatten_issues(results: dict[str, list[ValidationIssue]]) -> list[tuple[str, ValidationIssue]]:
    return [(name, issue) for name, issues in results.items() for issue in issues]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the C0.5 A/B validation evidence reports",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT_DEFAULT)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="path to t47_comfyui_ab_report.json (default: canonical)",
    )
    parser.add_argument(
        "--vlm",
        type=Path,
        default=None,
        help="path to vlm_guardrail_report.json (default: canonical)",
    )
    parser.add_argument(
        "--production",
        type=Path,
        default=None,
        help="path to comfyui_production_gate.json (default: canonical)",
    )
    parser.add_argument(
        "--required-workflow",
        type=str,
        default="portrait_focal_enhance_v1",
        help="workflow name that must appear in approved_workflows (default: portrait_focal_enhance_v1)",
    )
    parser.add_argument(
        "--samples",
        action="store_true",
        help="validate the bundled sample fixtures instead of canonical paths",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    default_paths = _sample_paths(repo_root) if args.samples else _canonical_paths(repo_root)
    paths = _ReportPaths(
        ab_report=(args.report or default_paths.ab_report).resolve(),
        vlm_guardrail=(args.vlm or default_paths.vlm_guardrail).resolve(),
        production_gate=(args.production or default_paths.production_gate).resolve(),
    )

    results = validate_all(paths, required_workflow=args.required_workflow)
    flat = _flatten_issues(results)

    if args.format == "json":
        payload = {
            "paths": {
                "ab_report": str(paths.ab_report),
                "vlm_guardrail_report": str(paths.vlm_guardrail),
                "production_gate_report": str(paths.production_gate),
            },
            "ok": not flat,
            "issues_by_report": {name: [i.as_dict() for i in issues] for name, issues in results.items()},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if not flat:
            print("OK: 3 reports validate against the C0.5 contract")
        else:
            print(f"FAIL: {len(flat)} issue(s) across 3 reports")
            for report_name, issue in flat:
                print(f"  [{report_name}][{issue.code}] {issue.field}: {issue.message}")

    return 0 if not flat else 1


if __name__ == "__main__":
    sys.exit(main())
