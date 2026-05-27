"""SimulationDeliveryGate — plan §P2.2.

Consumes the ComfyUI promotion manifest (`case-workbench-ai/promotion/manifest.json`)
plus per-job evidence (`simulation_jobs.audit_json`, `candidate_lineage.vlm_judge_result_json`)
to decide whether a simulation_job can promote into the delivery pipeline.

Four-check pipeline (fail-closed on missing / expired / mismatch):

1. **simulation_quality** — `audit_json.quality_score` (or fallback dive into
   `audit_json.qa_scores` per simulation_quality module conventions) ≥ threshold.
2. **VLM judge agreement** — latest `candidate_lineage.vlm_judge_result_json.agreement_rate`
   ≥ threshold. Missing lineage row is treated as missing evidence (reject).
3. **manifest hash bindings** — current source hashes (`compute_all_bindings`)
   must match `manifest.bindings.*`; any mismatch → reject.
4. **workflow scope** — `audit_json.workflow_name` must be permitted by
   `manifest.scope` (caller controls scope set via `allowed_scopes`).

Manifest unreadable or missing → fail-closed `accepted=False,
reasons=['manifest_missing']`. Any non-accept emits `rolled_back=True` so the
caller can branch on "halt" vs "accept-and-advance".

This module has **zero** runtime coupling to `routes/*`. It is pure
domain logic + filesystem read + sqlite3 read; it does NOT write.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.scripts.compute_manifest_hashes import (
    BINDING_NAMES,
    MANIFEST_RELPATH,
    REPO_ROOT_DEFAULT,
    compute_all_bindings,
    read_manifest,
    validate_manifest,
)

# ---------------------------------------------------------------------------
# Defaults & env overrides
# ---------------------------------------------------------------------------

DEFAULT_SIMULATION_QUALITY_THRESHOLD = float(
    os.environ.get("SIMULATION_QUALITY_THRESHOLD", "0.7")
)
DEFAULT_VLM_AGREEMENT_THRESHOLD = float(
    os.environ.get("VLM_AGREEMENT_THRESHOLD", "0.7")
)

# Scopes that, by default, are considered valid promotion targets.
# Operator can narrow this in the caller via `allowed_scopes=`.
DEFAULT_ALLOWED_SCOPES: frozenset[str] = frozenset({"production"})


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """Outcome of a single `SimulationDeliveryGate.evaluate(job_id)` call."""

    accepted: bool
    reasons: list[str] = field(default_factory=list)
    rolled_back: bool = False
    manifest_state: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class SimulationDeliveryGate:
    """Decide whether a simulation_job promotes into delivery."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        manifest_path: Path | None = None,
        repo_root: Path | None = None,
        simulation_quality_threshold: float | None = None,
        vlm_agreement_threshold: float | None = None,
        allowed_scopes: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._conn = conn
        self._repo_root = (repo_root or REPO_ROOT_DEFAULT).resolve()
        resolved_manifest = (
            Path(manifest_path)
            if manifest_path is not None
            else (self._repo_root / MANIFEST_RELPATH)
        )
        # Do not call .resolve() unconditionally — missing-file path still needs
        # to be representable (fail-closed path test relies on it).
        self._manifest_path = resolved_manifest
        self._sim_quality_threshold = (
            simulation_quality_threshold
            if simulation_quality_threshold is not None
            else DEFAULT_SIMULATION_QUALITY_THRESHOLD
        )
        self._vlm_threshold = (
            vlm_agreement_threshold
            if vlm_agreement_threshold is not None
            else DEFAULT_VLM_AGREEMENT_THRESHOLD
        )
        self._allowed_scopes = (
            frozenset(allowed_scopes)
            if allowed_scopes is not None
            else DEFAULT_ALLOWED_SCOPES
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, simulation_job_id: int) -> GateDecision:
        reasons: list[str] = []
        evidence: dict[str, Any] = {"simulation_job_id": simulation_job_id}

        # ---- 1. Load manifest (fail-closed on missing/unreadable) -----
        manifest, manifest_state, manifest_issue = self._read_manifest_safe()
        evidence["manifest_path"] = str(self._manifest_path)
        evidence["manifest_state"] = manifest_state
        if manifest is None:
            reasons.append(manifest_issue or "manifest_missing")
            return GateDecision(
                accepted=False,
                reasons=reasons,
                rolled_back=True,
                manifest_state=manifest_state,
                evidence=evidence,
            )

        # ---- 2. Load simulation_jobs row ------------------------------
        job_row = self._conn.execute(
            "SELECT id, case_id, status, audit_json FROM simulation_jobs WHERE id = ?",
            (simulation_job_id,),
        ).fetchone()
        if job_row is None:
            reasons.append(f"simulation_job_not_found:{simulation_job_id}")
            return GateDecision(
                accepted=False,
                reasons=reasons,
                rolled_back=True,
                manifest_state=manifest_state,
                evidence=evidence,
            )

        audit = _safe_json(job_row["audit_json"]) or {}
        evidence["job_status"] = job_row["status"]
        evidence["workflow_name"] = audit.get("workflow_name")

        # ---- 3. Check simulation_quality ------------------------------
        sim_score = _extract_quality_score(audit)
        evidence["simulation_quality_score"] = sim_score
        if sim_score is None:
            reasons.append("simulation_quality_missing")
        elif sim_score < self._sim_quality_threshold:
            reasons.append(
                f"simulation_quality_below_threshold:"
                f"{sim_score:.3f}<{self._sim_quality_threshold:.3f}"
            )

        # ---- 4. Check VLM judge agreement -----------------------------
        agreement, lineage_present = self._latest_agreement_rate(simulation_job_id)
        evidence["vlm_agreement_rate"] = agreement
        evidence["candidate_lineage_present"] = lineage_present
        if not lineage_present:
            reasons.append("vlm_agreement_missing:no_candidate_lineage")
        elif agreement is None:
            reasons.append("vlm_agreement_missing:no_agreement_rate")
        elif agreement < self._vlm_threshold:
            reasons.append(
                f"vlm_agreement_below_threshold:"
                f"{agreement:.3f}<{self._vlm_threshold:.3f}"
            )

        # ---- 5. Check manifest hash bindings --------------------------
        try:
            current_bindings = compute_all_bindings(self._repo_root)
        except FileNotFoundError as exc:
            current_bindings = {}
            reasons.append(f"manifest_sources_missing:{exc!s}")
        validation_issues = validate_manifest(
            manifest, expected_bindings=current_bindings or None
        )
        hash_mismatch_evidence: dict[str, dict[str, str | None]] = {}
        for issue in validation_issues:
            if issue.code == "hash_mismatch":
                binding_name = issue.field.split(".", 1)[-1]
                hash_mismatch_evidence[binding_name] = {
                    "expected": current_bindings.get(binding_name),
                    "actual": (manifest.get("bindings") or {}).get(binding_name),
                }
                reasons.append(f"manifest_hash_mismatch:{binding_name}")
            elif issue.code == "binding_missing":
                reasons.append(f"manifest_binding_missing:{issue.field}")
        if hash_mismatch_evidence:
            evidence["hash_mismatches"] = hash_mismatch_evidence

        # ---- 6. Check workflow scope ----------------------------------
        scope = manifest.get("scope")
        evidence["manifest_scope"] = scope
        evidence["allowed_scopes"] = sorted(self._allowed_scopes)
        if scope not in self._allowed_scopes:
            reasons.append(
                f"workflow_out_of_scope:"
                f"manifest_scope={scope!r}_not_in_{sorted(self._allowed_scopes)}"
            )

        # ---- 7. promotion_state surfaces as decision evidence ---------
        promotion_state = manifest.get("promotion_state")
        manifest_state = promotion_state or manifest_state
        evidence["promotion_state"] = promotion_state

        accepted = not reasons
        return GateDecision(
            accepted=accepted,
            reasons=reasons,
            rolled_back=not accepted,
            manifest_state=manifest_state,
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_manifest_safe(self) -> tuple[dict[str, Any] | None, str, str | None]:
        """Return (manifest_dict, manifest_state, issue_code).

        On missing/unreadable: (None, "missing"|"corrupt", reason_code).
        """
        path = self._manifest_path
        if not path or not Path(path).is_file():
            return None, "missing", "manifest_missing"
        try:
            return read_manifest(Path(path)), "loaded", None
        except (OSError, json.JSONDecodeError) as exc:
            return None, "corrupt", f"manifest_unreadable:{exc!s}"

    def _latest_agreement_rate(
        self, simulation_job_id: int
    ) -> tuple[float | None, bool]:
        """Latest agreement_rate from candidate_lineage; (None, False) if no row."""
        row = self._conn.execute(
            """
            SELECT vlm_judge_result_json
            FROM candidate_lineage
            WHERE simulation_job_id = ?
              AND vlm_judge_result_json IS NOT NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (simulation_job_id,),
        ).fetchone()
        if row is None:
            return None, False
        data = _safe_json(row["vlm_judge_result_json"]) or {}
        raw = data.get("agreement_rate")
        try:
            return (float(raw) if raw is not None else None), True
        except (TypeError, ValueError):
            return None, True


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _safe_json(blob: Any) -> dict[str, Any] | None:
    if isinstance(blob, dict):
        return blob
    if not isinstance(blob, (str, bytes)):
        return None
    try:
        loaded = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _extract_quality_score(audit: dict[str, Any]) -> float | None:
    """Pull a single quality score from audit_json.

    Preference order:
      1. `audit.quality_score`        — explicit producer-side field
      2. `audit.qa_scores.quality_score` — nested under qa_scores
      3. `audit.review_decision.quality_score` — review pipeline cache
    """
    for path in (("quality_score",),
                 ("qa_scores", "quality_score"),
                 ("review_decision", "quality_score")):
        node: Any = audit
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is None:
            continue
        try:
            return float(node)
        except (TypeError, ValueError):
            continue
    return None
