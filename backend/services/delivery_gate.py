"""Delivery gate service.

Centralizes deliverability decisions previously scattered across
`backend/scripts/export_delivery_batch.py`:

* Source-image / render-output integrity (`preflight_check`)
* Quality-gate verdict per render (`quality_gate`)
* Deliverable case selection (`list_deliverables`)
* Final artifact copy (`export`)
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

P0_THRESHOLD = 90.0
P1_THRESHOLD = 78.0


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

    def list_deliverables(self) -> list[DeliverableItem]:
        """Return one DeliverableItem per case (best render only), with
        physically present output files. Sorted by (customer, case_id).
        """
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
            seen.add(cid)
            output_path = row["output_path"]
            if not output_path or not Path(output_path).is_file():
                continue
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
        items.sort(key=lambda d: (d.customer, d.case_id))
        return items

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    @staticmethod
    def export(item: DeliverableItem, output_dir: Path, dry_run: bool = False) -> Path:
        """Copy the case's final-board.jpg into the customer subdirectory.

        Returns the destination path. In `dry_run` mode no file is touched.
        """
        customer_dir = output_dir / item.customer
        dest_path = customer_dir / item.dest_filename()
        if not dry_run:
            customer_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source_path, str(dest_path))
        return dest_path
