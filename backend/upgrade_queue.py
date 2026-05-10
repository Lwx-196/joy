"""V3 upgrade job queue — async-friendly singleton with SSE broadcast.

Mirror of `render_queue.py` for v3 upgrades, with these intentional differences:
- Worker pool comes from `_job_pool` (shared with render_queue, max_workers=2)
  — the same 2 slots cap mediapipe concurrency across both queues.
- The heavy work (`_upgrade_executor.execute_upgrade`) already writes
  audit op='upgrade' inside its own DB connection, so the queue does not call
  audit again — undo-by-batch goes through `audit.apply_undo` per case.
- Batch undo is a queue-level operation (mass apply_undo + flip job rows to
  'undone'); single-case undo continues to flow through the regular
  /api/cases/{id}/undo endpoint.

Public API used by routes/upgrade.py:
- enqueue(case_id, brand, batch_id=None)         -> job_id
- enqueue_batch(case_ids, brand)                  -> (batch_id, job_ids)
- cancel(job_id)                                  -> bool
- undo_batch(batch_id, source_route=None)         -> dict
- retry(job_id)                                   -> int (new job_id)
- subscribe()                                     -> async iterator
- recover()                                       -> dict
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from . import _job_pool, _upgrade_executor, audit, db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Subscriber:
    """One SSE listener. Bound to the event loop where subscribe() was called."""

    __slots__ = ("queue", "loop", "closed")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.loop = loop
        self.closed = False


class UpgradeQueue:
    """Single global queue. Construct once at import time."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._sub_lock = threading.Lock()

    # ------------------------------------------------------------------
    # SSE broadcast
    # ------------------------------------------------------------------

    def _publish(self, event: dict[str, Any]) -> None:
        """Push event to every active subscriber. Safe to call from any thread.

        Always tags events with `job_type='upgrade'` for the unified
        /api/jobs/stream router.
        """
        event.setdefault("job_type", "upgrade")
        with self._sub_lock:
            subs = list(self._subscribers)
        for sub in subs:
            if sub.closed:
                continue
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, event)
            except RuntimeError:
                sub.closed = True

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
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

    def enqueue(self, case_id: int, brand: str, batch_id: str | None = None) -> int:
        if not brand:
            raise ValueError("brand required")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM cases WHERE id = ? AND trashed_at IS NULL", (case_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"case {case_id} not found")
            cur = conn.execute(
                """
                INSERT INTO upgrade_jobs (case_id, brand, status, batch_id, enqueued_at)
                VALUES (?, ?, 'queued', ?, ?)
                """,
                (case_id, brand, batch_id, _now_iso()),
            )
            job_id = cur.lastrowid or 0
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "brand": brand,
                "status": "queued",
            }
        )
        _job_pool.submit(self._execute_safe, job_id)
        return job_id

    def enqueue_batch(
        self, case_ids: list[int], brand: str
    ) -> tuple[str, list[int]]:
        if not case_ids:
            raise ValueError("case_ids cannot be empty")
        batch_id = f"upgrade-{uuid.uuid4().hex[:12]}"
        job_ids: list[int] = []
        for case_id in case_ids:
            try:
                jid = self.enqueue(case_id, brand, batch_id=batch_id)
                job_ids.append(jid)
            except ValueError:
                continue
        return batch_id, job_ids

    def retry(self, job_id: int) -> int:
        """Re-enqueue a failed/cancelled job with the same case_id + brand.

        Does not reuse the row — creates a new job record so historical state
        is preserved.
        """
        with db.connect() as conn:
            row = conn.execute(
                "SELECT case_id, brand, batch_id FROM upgrade_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            raise ValueError("job not found")
        return self.enqueue(row["case_id"], row["brand"], batch_id=row["batch_id"])

    def cancel(self, job_id: int) -> bool:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, status, case_id, batch_id FROM upgrade_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            if row["status"] != "queued":
                return False
            conn.execute(
                "UPDATE upgrade_jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
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
    # Worker (runs in shared pool)
    # ------------------------------------------------------------------

    def _execute_safe(self, job_id: int) -> None:
        try:
            self._execute_upgrade(job_id)
        except Exception as e:  # noqa: BLE001 — final safety net
            self._mark_failed(job_id, f"unexpected: {e}")

    def _execute_upgrade(self, job_id: int) -> None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM upgrade_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return
            if row["status"] != "queued":
                return
            conn.execute(
                "UPDATE upgrade_jobs SET status = 'running', started_at = ? WHERE id = ?",
                (_now_iso(), job_id),
            )
            case_id = row["case_id"]
            brand = row["brand"]
            batch_id = row["batch_id"]

        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "status": "running",
            }
        )

        try:
            summary = _upgrade_executor.execute_upgrade(
                case_id,
                brand,
                source_route=f"/api/cases/upgrade/batch (job={job_id})",
            )
        except ValueError as e:
            self._mark_failed(job_id, f"missing: {e}")
            return
        except FileNotFoundError as e:
            self._mark_failed(job_id, f"missing case dir: {e}")
            return
        except RuntimeError as e:
            self._mark_failed(job_id, f"skill unavailable: {e}")
            return
        except Exception as e:  # noqa: BLE001
            self._mark_failed(job_id, f"upgrade failed: {e}")
            return

        with db.connect() as conn:
            conn.execute(
                """
                UPDATE upgrade_jobs
                SET status = 'done', finished_at = ?, meta_json = ?
                WHERE id = ?
                """,
                (
                    _now_iso(),
                    json.dumps(summary, ensure_ascii=False),
                    job_id,
                ),
            )

        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "status": "done",
                "summary": summary,
            }
        )

    def _mark_failed(self, job_id: int, message: str) -> None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT case_id, batch_id FROM upgrade_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE upgrade_jobs
                SET status = 'failed', finished_at = ?, error_message = ?
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
    # Batch undo
    # ------------------------------------------------------------------

    def undo_batch(self, batch_id: str, source_route: str | None = None) -> dict[str, Any]:
        """Undo every done job in a batch via audit.apply_undo.

        Skips cases whose latest_active_revision is not the upgrade — user has
        done newer edits since, so undoing the upgrade now would be surprising.
        Returns {undone: [case_id...], skipped: [{case_id, reason}...], errors: [...]}.
        """
        route = source_route or f"/api/jobs/upgrade/batches/{batch_id}/undo"
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT id, case_id FROM upgrade_jobs WHERE batch_id = ? AND status = 'done'",
                (batch_id,),
            ).fetchall()
            undone: list[int] = []
            skipped: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for r in rows:
                case_id = r["case_id"]
                try:
                    rev = audit.latest_active_revision(conn, case_id)
                    if rev is None:
                        skipped.append({"case_id": case_id, "reason": "no active revision"})
                        continue
                    if rev["op"] != "upgrade":
                        skipped.append(
                            {"case_id": case_id, "reason": f"latest revision is {rev['op']} (newer edits)"}
                        )
                        continue
                    audit.apply_undo(conn, case_id, source_route=route)
                    conn.execute(
                        "UPDATE upgrade_jobs SET status = 'undone' WHERE id = ?",
                        (r["id"],),
                    )
                    undone.append(case_id)
                except Exception as e:  # noqa: BLE001
                    errors.append({"case_id": case_id, "message": str(e)})

        for cid in undone:
            self._publish(
                {
                    "type": "job_update",
                    "case_id": cid,
                    "batch_id": batch_id,
                    "status": "undone",
                }
            )
        return {"undone": undone, "skipped": skipped, "errors": errors, "batch_id": batch_id}

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def recover(self) -> dict[str, int]:
        with db.connect() as conn:
            running_to_queued = conn.execute(
                "UPDATE upgrade_jobs SET status = 'queued', started_at = NULL WHERE status = 'running'"
            ).rowcount
            queued_rows: list[sqlite3.Row] = conn.execute(
                "SELECT id FROM upgrade_jobs WHERE status = 'queued' ORDER BY enqueued_at"
            ).fetchall()
        for r in queued_rows:
            _job_pool.submit(self._execute_safe, r["id"])
        return {"requeued_running": running_to_queued, "resubmitted_queued": len(queued_rows)}


UPGRADE_QUEUE = UpgradeQueue()
