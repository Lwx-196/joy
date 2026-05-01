"""Render job queue — async-friendly singleton with SSE broadcast.

Design:
- Worker pool comes from `_job_pool` (shared with upgrade_queue, max_workers=2)
  so render and upgrade compete for the same 2 slots and never run 4 mediapipe
  subprocesses (~150MB each) in parallel.
- Job lifecycle: queued → running → (done | failed | cancelled).
  Cancellation is only honored while still queued; running jobs run to completion
  to avoid leaving mediapipe / cv2 in undefined states.
- SSE: each /api/render/stream subscriber owns an asyncio.Queue. Worker threads
  publish events via loop.call_soon_threadsafe(queue.put_nowait, payload). The
  publish loop captures the FastAPI event loop on first subscription.
- Recovery: on import / startup, residual `status='running'` rows are demoted to
  'queued' (they were interrupted by a previous crash) and resubmitted to the
  pool. queued rows from a clean shutdown are also resubmitted.

Public API used by routes/render.py:
- enqueue(case_id, brand, template, semantic_judge) -> job_id
- enqueue_batch(case_ids, brand, template, semantic_judge) -> (batch_id, job_ids)
- cancel(job_id) -> bool (only if status == 'queued')
- undo_render(case_id) -> dict (delete output file + audit op=undo_render)
- subscribe() -> async iterator yielding event dicts (for SSE)
- recover() -> resubmits queued + running jobs (called once at startup)
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from . import _job_pool, audit, db, render_executor

DEFAULT_TEMPLATE = "tri-compare"
DEFAULT_SEMANTIC_JUDGE = "off"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Subscriber:
    """One SSE listener. Bound to the event loop where subscribe() was called."""

    __slots__ = ("queue", "loop", "closed")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.loop = loop
        self.closed = False


class RenderQueue:
    """Single global queue. Construct once at import time."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._sub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE broadcast
    # ------------------------------------------------------------------

    def _publish(self, event: dict[str, Any]) -> None:
        """Push event to every active subscriber. Safe to call from any thread.

        Always tags events with `job_type='render'` so the unified
        /api/jobs/stream consumer (and any future fan-in router) can route
        without sniffing payload shape.
        """
        event.setdefault("job_type", "render")
        with self._sub_lock:
            subs = list(self._subscribers)
        for sub in subs:
            if sub.closed:
                continue
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, event)
            except RuntimeError:
                # Loop closed — mark sub for removal on next iteration.
                sub.closed = True

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Async generator yielding render events. Caller iterates with async for."""
        loop = asyncio.get_running_loop()
        sub = _Subscriber(loop)
        with self._sub_lock:
            self._subscribers.append(sub)
        try:
            while True:
                event = await sub.queue.get()
                yield event
        finally:
            sub.closed = True
            with self._sub_lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        case_id: int,
        brand: str,
        template: str = DEFAULT_TEMPLATE,
        semantic_judge: str = DEFAULT_SEMANTIC_JUDGE,
        batch_id: str | None = None,
    ) -> int:
        """Insert job row + submit to thread pool. Returns job_id."""
        if not brand:
            raise ValueError("brand required")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"case {case_id} not found")
            cur = conn.execute(
                """
                INSERT INTO render_jobs
                    (case_id, brand, template, status, batch_id, enqueued_at, semantic_judge)
                VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (case_id, brand, template, batch_id, _now_iso(), semantic_judge),
            )
            job_id = cur.lastrowid or 0
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "brand": brand,
                "template": template,
                "status": "queued",
            }
        )
        _job_pool.submit(self._execute_safe, job_id)
        return job_id

    def enqueue_batch(
        self,
        case_ids: list[int],
        brand: str,
        template: str = DEFAULT_TEMPLATE,
        semantic_judge: str = DEFAULT_SEMANTIC_JUDGE,
    ) -> tuple[str, list[int]]:
        """Insert N job rows sharing one batch_id. Returns (batch_id, job_ids)."""
        if not case_ids:
            raise ValueError("case_ids cannot be empty")
        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        job_ids: list[int] = []
        for case_id in case_ids:
            try:
                jid = self.enqueue(case_id, brand, template, semantic_judge, batch_id=batch_id)
                job_ids.append(jid)
            except ValueError:
                # Skip missing cases but continue the batch.
                continue
        return batch_id, job_ids

    def cancel(self, job_id: int) -> bool:
        """Cancel a queued job. No-op if job is already running/done/failed."""
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status, case_id, batch_id FROM render_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            if row["status"] != "queued":
                return False
            conn.execute(
                "UPDATE render_jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                (_now_iso(), job_id),
            )
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": row["case_id"],
                "batch_id": row["batch_id"],
                "status": "cancelled",
            }
        )
        return True

    # ------------------------------------------------------------------
    # Render execution (runs in worker thread)
    # ------------------------------------------------------------------

    def _execute_safe(self, job_id: int) -> None:
        """Wrapper that always marks failure on unexpected exceptions."""
        try:
            self._execute_render(job_id)
        except Exception as e:  # noqa: BLE001 — final safety net
            self._mark_failed(job_id, f"unexpected: {e}")

    def _execute_render(self, job_id: int) -> None:
        # 1. Read job row + transition to running (skip if cancelled meanwhile).
        with db.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, c.abs_path AS case_dir
                FROM render_jobs j
                JOIN cases c ON c.id = j.case_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return
            if row["status"] != "queued":
                return  # Already cancelled or being handled elsewhere.
            conn.execute(
                "UPDATE render_jobs SET status = 'running', started_at = ? WHERE id = ?",
                (_now_iso(), job_id),
            )
            case_dir = row["case_dir"]
            brand = row["brand"]
            template = row["template"]
            semantic_judge = row["semantic_judge"]
            case_id = row["case_id"]
            batch_id = row["batch_id"]

            # Stage B: pull manual phase/view overrides for this case
            override_rows = conn.execute(
                "SELECT filename, manual_phase, manual_view FROM case_image_overrides WHERE case_id = ?",
                (case_id,),
            ).fetchall()
            manual_overrides: dict[str, dict[str, str | None]] = {
                r["filename"]: {"phase": r["manual_phase"], "view": r["manual_view"]}
                for r in override_rows
            }

        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "status": "running",
            }
        )

        # 2. Run the heavy subprocess.
        try:
            result = render_executor.run_render(
                case_dir,
                brand=brand,
                template=template,
                semantic_judge=semantic_judge,
                manual_overrides=manual_overrides,
            )
        except FileNotFoundError as e:
            self._mark_failed(job_id, f"missing: {e}")
            return
        except subprocess.TimeoutExpired as e:
            self._mark_failed(job_id, f"timeout after {e.timeout}s")
            return
        except RuntimeError as e:
            self._mark_failed(job_id, f"render failed: {e}")
            return

        # 3. Persist done state + audit + broadcast.
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE render_jobs
                SET status = 'done',
                    finished_at = ?,
                    output_path = ?,
                    manifest_path = ?,
                    meta_json = ?
                WHERE id = ?
                """,
                (
                    _now_iso(),
                    result.get("output_path"),
                    result.get("manifest_path"),
                    json.dumps(
                        {
                            k: result.get(k)
                            for k in (
                                "status",
                                "blocking_issue_count",
                                "warning_count",
                                "case_mode",
                                "effective_templates",
                            )
                        },
                        ensure_ascii=False,
                    ),
                    job_id,
                ),
            )
            audit.record_revision(
                conn,
                case_id,
                op="render",
                before={"render_output_path": None},
                after={
                    "render_output_path": result.get("output_path"),
                    "render_job_id": job_id,
                    "brand": brand,
                    "template": template,
                },
                source_route=f"/api/cases/{case_id}/render",
                actor="render",
            )

        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "status": "done",
                "output_path": result.get("output_path"),
                "manifest_path": result.get("manifest_path"),
                "summary": {
                    "status": result.get("status"),
                    "blocking_issue_count": result.get("blocking_issue_count"),
                    "warning_count": result.get("warning_count"),
                    "case_mode": result.get("case_mode"),
                    "effective_templates": result.get("effective_templates"),
                },
            }
        )

    def _mark_failed(self, job_id: int, message: str) -> None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT case_id, batch_id FROM render_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE render_jobs
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (_now_iso(), message[:1000], job_id),
            )
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": row["case_id"] if row else None,
                "batch_id": row["batch_id"] if row else None,
                "status": "failed",
                "error_message": message[:500],
            }
        )

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo_render(self, case_id: int, source_route: str | None = None) -> dict[str, Any]:
        """Delete the most recent done render's output file + write op=undo_render audit.

        Also flips the corresponding render_jobs row to status='undone' so the
        UI can reflect the change (UndoToast / RenderStatusCard rely on the
        latest job's status to render their state).

        Returns {"undone": bool, "output_path": str | None, "revision_id": int | None}.
        Raises ValueError if no recent render to undo.
        """
        with db.connect() as conn:
            rev = conn.execute(
                """
                SELECT * FROM case_revisions
                WHERE case_id = ?
                  AND op = 'render'
                  AND undone_at IS NULL
                ORDER BY changed_at DESC, id DESC
                LIMIT 1
                """,
                (case_id,),
            ).fetchone()
            if rev is None:
                raise ValueError("nothing to undo")
            after = json.loads(rev["after_json"] or "{}")
            output_path = after.get("render_output_path")
            job_id = after.get("render_job_id")
            removed = False
            if output_path:
                p = Path(output_path)
                if p.exists() and p.is_file():
                    try:
                        p.unlink()
                        removed = True
                    except OSError:
                        # Permission/race — leave file but mark revision undone anyway.
                        pass
            # Flip the corresponding job row to status='undone' so the UI
            # surfaces the new state. We keep output_path for forensic purposes
            # (the UI checks status, not file existence, to decide what to show).
            if isinstance(job_id, int):
                conn.execute(
                    """
                    UPDATE render_jobs
                    SET status = 'undone'
                    WHERE id = ? AND status = 'done'
                    """,
                    (job_id,),
                )
            # Write the undo revision.
            undo_id = audit.record_revision(
                conn,
                case_id,
                op="undo_render",
                before=after,
                after={"render_output_path": None, "removed_file": removed},
                source_route=source_route,
                actor="user",
            )
            # Mark source revision as undone.
            conn.execute(
                "UPDATE case_revisions SET undone_at = ? WHERE id = ?",
                (_now_iso(), rev["id"]),
            )
            # Cascade: any active evaluation pointing at this render job is also
            # invalidated — the artifact file is gone, so the evaluation has no
            # subject to refer back to. Reverse direction (evaluation undo) does
            # NOT cascade here; that's intentional.
            if isinstance(job_id, int):
                conn.execute(
                    """
                    UPDATE evaluations
                    SET undone_at = ?
                    WHERE subject_kind = 'render'
                      AND subject_id = ?
                      AND undone_at IS NULL
                    """,
                    (_now_iso(), job_id),
                )
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id if isinstance(job_id, int) else None,
                "case_id": case_id,
                "status": "undone",
                "output_path": output_path,
                "revision_id": undo_id,
            }
        )
        return {"undone": True, "output_path": output_path, "revision_id": undo_id, "removed_file": removed}

    # ------------------------------------------------------------------
    # Recovery (call once at process start)
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, int]:
        """Demote residual 'running' jobs to 'queued' and resubmit all queued jobs.

        Called from main.py module top-level after init_schema.
        """
        with db.connect() as conn:
            running_to_queued = conn.execute(
                """
                UPDATE render_jobs
                SET status = 'queued', started_at = NULL
                WHERE status = 'running'
                """
            ).rowcount
            queued_rows: list[sqlite3.Row] = conn.execute(
                "SELECT id FROM render_jobs WHERE status = 'queued' ORDER BY enqueued_at"
            ).fetchall()
        for r in queued_rows:
            _job_pool.submit(self._execute_safe, r["id"])
        return {"requeued_running": running_to_queued, "resubmitted_queued": len(queued_rows)}


# Module-level singleton. Imported by routes/render.py and main.py.
RENDER_QUEUE = RenderQueue()
