"""Delivery gate service.

Centralizes deliverability decisions previously scattered across
`backend/scripts/export_delivery_batch.py`:

* Source-image / render-output integrity (`preflight_check`)
* Quality-gate verdict per render (`quality_gate`)
* Deliverable case selection (`list_deliverables`)
* Final artifact copy (`export`)

P2.2: `list_deliverables(simulation_gate=...)` may now also surface
simulation_jobs (ComfyUI candidates) once they pass `SimulationDeliveryGate`.
Render_jobs still win per-case if both exist for the same case.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.services.simulation_delivery_gate import SimulationDeliveryGate

P0_THRESHOLD = 90.0
P1_THRESHOLD = 78.0

# ComfyUI / AI simulation surfacing in `list_deliverables`:
SIMULATION_ARTIFACT_MODE = "ai_simulation"
SIMULATION_DEFAULT_TEMPLATE_TIER = "ai_candidate"


def classify_tier(score: float) -> str:
    if score >= P0_THRESHOLD:
        return "P0"
    if score >= P1_THRESHOLD:
        return "P1"
    return "P2"


def _customer_name(abs_path: str) -> str:
    parts = Path(abs_path).parts
    return parts[-2] if len(parts) >= 2 else "unknown"


def _case_display_name(abs_path: str) -> str:
    return os.path.basename(abs_path)


@dataclass(frozen=True)
class DeliverableItem:
    case_id: int
    customer: str
    case_name: str
    category: str
    template_tier: str
    quality_score: float
    quality_status: str
    artifact_mode: str
    blocking_count: int
    warning_count: int
    source_path: str
    job_id: int

    @property
    def tier(self) -> str:
        return classify_tier(self.quality_score)

    def dest_filename(self) -> str:
        safe_name = self.case_name.replace("/", "_").replace("\\", "_")
        return f"{safe_name}__score{int(self.quality_score)}_{self.template_tier}.jpg"


class DeliveryGate:
    """Single-source-of-truth for delivery deliverability decisions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------

    def preflight_check(self, case_id: int) -> tuple[bool, list[str]]:
        """Verify a case is structurally ready for delivery.

        Checks: case row exists & not trashed; at least one render_job exists
        with `can_publish=1`; the best render's `output_path` is on disk.
        """
        reasons: list[str] = []
        row = self._conn.execute(
            "SELECT id, trashed_at FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            return False, [f"case #{case_id} not found"]
        if row["trashed_at"] is not None:
            return False, [f"case #{case_id} is trashed"]

        best = self._conn.execute(
            """
            SELECT j.id AS job_id, j.output_path, q.can_publish, q.quality_score
            FROM render_jobs j
            JOIN render_quality q ON q.render_job_id = j.id
            WHERE j.case_id = ?
            ORDER BY q.quality_score DESC
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        if best is None:
            return False, [f"case #{case_id} has no render_quality row"]
        if not best["can_publish"]:
            reasons.append(f"case #{case_id} best render not publishable")
        output_path = best["output_path"]
        if not output_path or not Path(output_path).is_file():
            reasons.append(f"case #{case_id} output file missing: {output_path!r}")

        return (not reasons), reasons

    # ------------------------------------------------------------------
    # Quality gate
    # ------------------------------------------------------------------

    def quality_gate(self, render_job_id: int) -> dict:
        row = self._conn.execute(
            """
            SELECT can_publish, quality_score, quality_status,
                   blocking_count, warning_count
            FROM render_quality
            WHERE render_job_id = ?
            """,
            (render_job_id,),
        ).fetchone()
        if row is None:
            return {
                "render_job_id": render_job_id,
                "can_publish": False,
                "quality_score": 0.0,
                "tier": "P2",
                "blockers": ["render_quality row missing"],
            }
        score = float(row["quality_score"] or 0.0)
        blockers: list[str] = []
        if int(row["blocking_count"] or 0) > 0:
            blockers.append(f"{row['blocking_count']} blocking issue(s)")
        if row["quality_status"] != "done":
            blockers.append(f"status={row['quality_status']}")
        return {
            "render_job_id": render_job_id,
            "can_publish": bool(row["can_publish"]),
            "quality_score": score,
            "quality_status": row["quality_status"],
            "tier": classify_tier(score),
            "blockers": blockers,
        }

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def list_deliverables(
        self,
        simulation_gate: "SimulationDeliveryGate | None" = None,
    ) -> list[DeliverableItem]:
        """Return one DeliverableItem per case, sorted by (customer, case_id).

        Render_jobs (real artifacts) are surfaced unconditionally as before.
        When `simulation_gate` is provided, ComfyUI / AI simulation_jobs that
        pass `SimulationDeliveryGate.evaluate(...)` are also surfaced — but
        only for cases that don't already have a render_jobs-backed entry
        (render wins). The caller passes a built gate to keep manifest +
        threshold config in their hands.

        BC: zero-arg form is identical to pre-P2.2 behaviour (render only).
        """
        # ---- 1. Render-backed candidates (unchanged from pre-P2.2) ----
        rows = self._conn.execute(
            """
            SELECT c.id AS case_id, c.abs_path, c.category,
                   COALESCE(c.template_tier, 'auto') AS template_tier,
                   j.id AS job_id, j.output_path,
                   q.quality_score, q.quality_status, q.artifact_mode,
                   q.blocking_count, q.warning_count
            FROM cases c
            JOIN render_jobs j ON j.case_id = c.id
            JOIN render_quality q ON q.render_job_id = j.id
            WHERE c.trashed_at IS NULL
              AND q.can_publish = 1
            ORDER BY c.id, q.quality_score DESC
            """
        ).fetchall()

        seen: set[int] = set()
        items: list[DeliverableItem] = []
        for row in rows:
            cid = row["case_id"]
            if cid in seen:
                continue
            output_path = row["output_path"]
            if not output_path or not Path(output_path).is_file():
                continue
            seen.add(cid)
            items.append(
                DeliverableItem(
                    case_id=cid,
                    customer=_customer_name(row["abs_path"]),
                    case_name=_case_display_name(row["abs_path"]),
                    category=row["category"],
                    template_tier=row["template_tier"],
                    quality_score=float(row["quality_score"]),
                    quality_status=row["quality_status"],
                    artifact_mode=row["artifact_mode"],
                    blocking_count=int(row["blocking_count"] or 0),
                    warning_count=int(row["warning_count"] or 0),
                    source_path=output_path,
                    job_id=row["job_id"],
                )
            )

        # ---- 2. Simulation-backed candidates (P2.2 union) -------------
        if simulation_gate is not None:
            items.extend(self._simulation_deliverables(simulation_gate, exclude=seen))

        items.sort(key=lambda d: (d.customer, d.case_id))
        return items

    def _simulation_deliverables(
        self,
        simulation_gate: "SimulationDeliveryGate",
        *,
        exclude: set[int],
    ) -> list[DeliverableItem]:
        """Surface simulation_jobs that pass the gate, skipping cases in
        `exclude` (which already have a render-backed item)."""
        rows = self._conn.execute(
            """
            SELECT s.id AS job_id, s.case_id, s.output_refs_json, s.audit_json,
                   c.abs_path, c.category,
                   COALESCE(c.template_tier, 'auto') AS template_tier
            FROM simulation_jobs s
            JOIN cases c ON c.id = s.case_id
            WHERE c.trashed_at IS NULL
              AND s.status IN ('done', 'done_with_issues')
              AND s.can_publish = 1
            ORDER BY c.id, s.updated_at DESC, s.id DESC
            """
        ).fetchall()

        results: list[DeliverableItem] = []
        seen_sim: set[int] = set()
        for row in rows:
            cid = row["case_id"]
            if cid in exclude or cid in seen_sim:
                continue
            output_path = _simulation_primary_output_path(row["output_refs_json"])
            if not output_path or not Path(output_path).is_file():
                continue
            decision = simulation_gate.evaluate(int(row["job_id"]))
            if not decision.accepted:
                continue
            audit = _safe_audit(row["audit_json"])
            score = _simulation_score(audit)
            seen_sim.add(cid)
            results.append(
                DeliverableItem(
                    case_id=cid,
                    customer=_customer_name(row["abs_path"]),
                    case_name=_case_display_name(row["abs_path"]),
                    category=row["category"],
                    template_tier=row["template_tier"] or SIMULATION_DEFAULT_TEMPLATE_TIER,
                    quality_score=score,
                    quality_status="done",
                    artifact_mode=SIMULATION_ARTIFACT_MODE,
                    blocking_count=0,
                    warning_count=0,
                    source_path=output_path,
                    job_id=int(row["job_id"]),
                )
            )
        return results

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Module helpers (private to delivery_gate; kept on class scope for
    # encapsulation and easier monkeypatch in tests).
    # ------------------------------------------------------------------

    def export(self, item: DeliverableItem, output_dir: Path, dry_run: bool = False) -> Path:
        """Copy the case's final-board.jpg into the customer subdirectory.

        Instance method (not staticmethod) for API symmetry with preflight_check /
        quality_gate / list_deliverables. `dry_run` skips the file copy.
        """
        customer_dir = output_dir / item.customer
        dest_path = customer_dir / item.dest_filename()
        if not dry_run:
            customer_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source_path, str(dest_path))
        return dest_path


# ---------------------------------------------------------------------------
# Module-level helpers for simulation surfacing
# ---------------------------------------------------------------------------


def _safe_audit(blob) -> dict:
    if isinstance(blob, dict):
        return blob
    if not isinstance(blob, (str, bytes)):
        return {}
    try:
        loaded = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _simulation_primary_output_path(output_refs_json) -> str | None:
    """Return the canonical AI simulation output path from output_refs_json.

    Preference: explicit `kind == 'ai_after_simulation'`, otherwise first
    `kind == 'image'`, otherwise first ref with a non-empty path.
    """
    if not output_refs_json:
        return None
    try:
        refs = json.loads(output_refs_json) if isinstance(output_refs_json, (str, bytes)) else output_refs_json
    except (TypeError, ValueError):
        return None
    if not isinstance(refs, list):
        return None
    preferred: str | None = None
    fallback_image: str | None = None
    fallback_any: str | None = None
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        path = str(ref.get("path") or "")
        kind = str(ref.get("kind") or "")
        if not path:
            continue
        if kind == "ai_after_simulation" and preferred is None:
            preferred = path
        elif kind == "image" and fallback_image is None:
            fallback_image = path
        elif fallback_any is None:
            fallback_any = path
    return preferred or fallback_image or fallback_any


def _simulation_score(audit: dict) -> float:
    """Coerce a quality_score into the 0-100 surface used by render_quality.

    audit_json.quality_score is typically 0-1; render_quality.quality_score
    is 0-100. We rescale so the existing P0/P1 tiering logic still applies
    sensibly downstream (a 0.92 simulation maps to ~92, comparable to a
    92.0 render).
    """
    raw = audit.get("quality_score")
    if raw is None:
        nested = audit.get("qa_scores") if isinstance(audit.get("qa_scores"), dict) else None
        if nested is not None:
            raw = nested.get("quality_score")
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value * 100.0 if 0.0 <= value <= 1.0 else value
