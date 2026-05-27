"""Compute and validate the ComfyUI promotion manifest hash bindings.

P2.1 deliverable. Pure-additive, zero coupling to `backend.services.*` or
`backend.routes.*` — the script reads source files + AI artifacts under
`case-workbench-ai/` and produces a deterministic sha256 per binding.

CLI usage:

    # Print computed hashes (no write):
    python -m backend.scripts.compute_manifest_hashes

    # Recompute and write into manifest.json (only `bindings.*` fields):
    python -m backend.scripts.compute_manifest_hashes --write

    # Validate manifest (exits non-zero on issues):
    python -m backend.scripts.compute_manifest_hashes --validate

    # Pin a custom manifest path or repo root:
    python -m backend.scripts.compute_manifest_hashes \
        --manifest /tmp/m.json --repo-root /tmp/proj

The script does NOT consume `SimulationDeliveryGate` (P2.2); it only
emits the artifact that the gate will later read.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants & schema
# ---------------------------------------------------------------------------

REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]

MANIFEST_RELPATH = Path("case-workbench-ai/promotion/manifest.json")

BINDING_NAMES: tuple[str, ...] = (
    "vlm_calibration_hash",
    "production_gate_hash",
    "ab_report_hash",
    "render_quality_baseline_hash",
)

VALID_SCOPES: frozenset[str] = frozenset({"production", "staging", "canary"})
VALID_PROMOTION_STATES: frozenset[str] = frozenset(
    {"shadow", "p10", "p25", "p50", "p100", "rolled_back"}
)
# States that require an approver + non-expired approved_at (rollout levels).
ROLLOUT_PROMOTION_STATES: frozenset[str] = frozenset(
    {"p10", "p25", "p50", "p100"}
)

# Approvals older than this many days are considered stale / expired.
APPROVAL_TTL_DAYS = 30

# Source binding inputs (relative to repo root).
VLM_CALIBRATION_SOURCES: tuple[Path, ...] = (
    Path("backend/services/vlm_calibration.py"),
)
PRODUCTION_GATE_SOURCES: tuple[Path, ...] = (
    Path("backend/services/pre_render_gate.py"),
    Path("backend/simulation_quality.py"),
)
AB_RUNS_DIR = Path("case-workbench-ai/ab_runs")
RENDER_QUALITY_SAMPLES_DIR = Path("case-workbench-ai/render_quality_samples")

HASH_PREFIX = "sha256:"


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return HASH_PREFIX + h.hexdigest()


def _sha256_of_files(paths: Iterable[Path], *, repo_root: Path) -> str:
    """Deterministic combined hash: order-independent over file *content*.

    Each input file contributes `relative_path\0sha256\n` to the rolling
    hash, then we sort and concatenate so result is stable regardless of
    filesystem enumeration order.
    """
    lines: list[str] = []
    for p in paths:
        rel = p.relative_to(repo_root).as_posix() if p.is_absolute() else p.as_posix()
        lines.append(f"{rel}\0{_sha256_file(p)}")
    lines.sort()
    return _sha256_bytes("\n".join(lines).encode("utf-8"))


# ---------------------------------------------------------------------------
# Per-binding compute
# ---------------------------------------------------------------------------


def compute_vlm_calibration_hash(repo_root: Path) -> str:
    """Hash the vlm_calibration.py source + (optional) recent outputs."""
    paths = [repo_root / p for p in VLM_CALIBRATION_SOURCES]
    # Optional: recent calibration JSON outputs (if operator dropped them).
    outputs_dir = repo_root / "case-workbench-ai" / "vlm_calibration_outputs"
    if outputs_dir.is_dir():
        paths.extend(sorted(outputs_dir.glob("*.json")))
    return _sha256_of_files((p for p in paths if p.is_file()), repo_root=repo_root)


def compute_production_gate_hash(repo_root: Path) -> str:
    paths = [repo_root / p for p in PRODUCTION_GATE_SOURCES if (repo_root / p).is_file()]
    if not paths:
        raise FileNotFoundError(
            f"production gate sources not found under {repo_root}: "
            f"{[str(p) for p in PRODUCTION_GATE_SOURCES]}"
        )
    return _sha256_of_files(paths, repo_root=repo_root)


def _latest_ab_summary(repo_root: Path) -> Path | None:
    ab_dir = repo_root / AB_RUNS_DIR
    if not ab_dir.is_dir():
        return None
    candidates = sorted(
        (d for d in ab_dir.iterdir() if d.is_dir() and (d / "summary.json").is_file()),
        key=lambda d: d.name,
        reverse=True,
    )
    return (candidates[0] / "summary.json") if candidates else None


def compute_ab_report_hash(repo_root: Path) -> str:
    summary = _latest_ab_summary(repo_root)
    if summary is None:
        # No AB runs locally: emit a stable empty marker so the manifest is
        # still computable. Downstream gate decides whether to refuse.
        return _sha256_bytes(b"<no-ab-runs>")
    return _sha256_file(summary)


def compute_render_quality_baseline_hash(repo_root: Path) -> str:
    samples_dir = repo_root / RENDER_QUALITY_SAMPLES_DIR
    if not samples_dir.is_dir():
        return _sha256_bytes(b"<no-render-quality-samples>")
    paths = sorted(samples_dir.glob("*.json")) + sorted(samples_dir.glob("*.jsonl"))
    if not paths:
        return _sha256_bytes(b"<no-render-quality-samples>")
    return _sha256_of_files(paths, repo_root=repo_root)


def compute_all_bindings(repo_root: Path) -> dict[str, str]:
    return {
        "vlm_calibration_hash": compute_vlm_calibration_hash(repo_root),
        "production_gate_hash": compute_production_gate_hash(repo_root),
        "ab_report_hash": compute_ab_report_hash(repo_root),
        "render_quality_baseline_hash": compute_render_quality_baseline_hash(repo_root),
    }


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


def _parse_iso8601(value: str) -> _dt.datetime | None:
    try:
        # Accept trailing 'Z' as UTC.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def validate_manifest(
    manifest: dict[str, Any],
    *,
    expected_bindings: dict[str, str] | None = None,
    now: _dt.datetime | None = None,
    approval_ttl_days: int = APPROVAL_TTL_DAYS,
) -> list[ValidationIssue]:
    """Run all schema + freshness checks.

    `expected_bindings` (if given) triggers hash-mismatch detection.
    `now` defaults to UTC `datetime.now`; injected for deterministic tests.
    """
    issues: list[ValidationIssue] = []
    now = now or _dt.datetime.now(_dt.timezone.utc)

    # scope
    scope = manifest.get("scope")
    if scope not in VALID_SCOPES:
        issues.append(
            ValidationIssue(
                "scope_invalid",
                "scope",
                f"scope={scope!r} not in {sorted(VALID_SCOPES)}",
            )
        )

    # promotion_state
    state = manifest.get("promotion_state")
    if state not in VALID_PROMOTION_STATES:
        issues.append(
            ValidationIssue(
                "promotion_state_invalid",
                "promotion_state",
                f"promotion_state={state!r} not in {sorted(VALID_PROMOTION_STATES)}",
            )
        )

    # approver — required for any rollout state (anything above shadow).
    approver = manifest.get("approver")
    if state and state != "shadow" and not (isinstance(approver, str) and approver.strip()):
        issues.append(
            ValidationIssue(
                "approver_missing",
                "approver",
                f"approver must be a non-empty string when promotion_state={state!r}",
            )
        )

    # approved_at — required for rollout states (p10/p25/p50/p100); freshness checked.
    approved_at_raw = manifest.get("approved_at")
    if state in ROLLOUT_PROMOTION_STATES:
        if not isinstance(approved_at_raw, str):
            issues.append(
                ValidationIssue(
                    "approved_at_missing",
                    "approved_at",
                    f"approved_at must be ISO-8601 string when promotion_state={state!r}",
                )
            )
        else:
            approved_at = _parse_iso8601(approved_at_raw)
            if approved_at is None:
                issues.append(
                    ValidationIssue(
                        "approved_at_invalid",
                        "approved_at",
                        f"approved_at={approved_at_raw!r} is not ISO-8601",
                    )
                )
            else:
                age = now - approved_at
                if age > _dt.timedelta(days=approval_ttl_days):
                    issues.append(
                        ValidationIssue(
                            "approved_at_expired",
                            "approved_at",
                            f"approval is {age.days}d old (TTL {approval_ttl_days}d); "
                            f"re-approval required",
                        )
                    )

    # bindings — shape + (optional) hash equality.
    bindings = manifest.get("bindings")
    if not isinstance(bindings, dict):
        issues.append(
            ValidationIssue("bindings_missing", "bindings", "bindings must be an object")
        )
    else:
        for name in BINDING_NAMES:
            if name not in bindings:
                issues.append(
                    ValidationIssue(
                        "binding_missing",
                        f"bindings.{name}",
                        f"required binding {name!r} missing",
                    )
                )
        if expected_bindings is not None:
            for name, expected in expected_bindings.items():
                actual = bindings.get(name)
                if actual != expected:
                    issues.append(
                        ValidationIssue(
                            "hash_mismatch",
                            f"bindings.{name}",
                            f"expected={expected} actual={actual}",
                        )
                    )

    return issues


# ---------------------------------------------------------------------------
# Manifest read/write
# ---------------------------------------------------------------------------


def read_manifest(manifest_path: Path) -> dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_manifest_bindings(
    manifest_path: Path,
    bindings: dict[str, str],
) -> dict[str, Any]:
    """Update only the `bindings` block; leave approval fields untouched."""
    manifest = read_manifest(manifest_path)
    existing = manifest.get("bindings") if isinstance(manifest.get("bindings"), dict) else {}
    merged = dict(existing)
    merged.update(bindings)
    manifest["bindings"] = merged
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_bindings(bindings: dict[str, str]) -> str:
    width = max(len(k) for k in bindings)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in bindings.items())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute / validate ComfyUI promotion manifest hash bindings",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help=f"repo root (default: {REPO_ROOT_DEFAULT})",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="manifest path (default: <repo-root>/case-workbench-ai/promotion/manifest.json)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="recompute bindings and write back into manifest.json",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate manifest against current source hashes; exit non-zero on issues",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root.resolve()
    manifest_path: Path = (args.manifest or (repo_root / MANIFEST_RELPATH)).resolve()
    bindings = compute_all_bindings(repo_root)

    if args.write:
        if not manifest_path.is_file():
            print(f"manifest not found: {manifest_path}", file=sys.stderr)
            return 2
        manifest = write_manifest_bindings(manifest_path, bindings)
        if args.format == "json":
            print(json.dumps({"manifest_path": str(manifest_path), "manifest": manifest},
                             ensure_ascii=False, indent=2))
        else:
            print(f"updated bindings in {manifest_path}:")
            print(_format_bindings(bindings))
        return 0

    if args.validate:
        if not manifest_path.is_file():
            print(f"manifest not found: {manifest_path}", file=sys.stderr)
            return 2
        manifest = read_manifest(manifest_path)
        issues = validate_manifest(manifest, expected_bindings=bindings)
        payload = {
            "manifest_path": str(manifest_path),
            "ok": not issues,
            "issues": [i.as_dict() for i in issues],
        }
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if not issues:
                print(f"OK: {manifest_path} validates against current source hashes")
            else:
                print(f"FAIL: {len(issues)} issue(s) in {manifest_path}")
                for i in issues:
                    print(f"  [{i.code}] {i.field}: {i.message}")
        return 0 if not issues else 1

    if args.format == "json":
        print(json.dumps({"bindings": bindings}, ensure_ascii=False, indent=2))
    else:
        print(_format_bindings(bindings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
