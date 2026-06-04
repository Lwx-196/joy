"""Sequential compute queue for best-pair candidate scans."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future
from typing import Any

from .. import _job_pool
from ..services import best_pair_service


class BestPairComputeQueue:
    """Run best-pair compute jobs one at a time while sharing the global pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[tuple[str, int]] = []
        self._batches: dict[str, dict[str, Any]] = {}
        self._inflight: Future | None = None

    def submit_batch(self, case_ids: list[int]) -> str:
        batch_id = f"bp-{uuid.uuid4().hex[:12]}"
        normalized = [int(case_id) for case_id in case_ids]
        should_start = False
        with self._lock:
            self._batches[batch_id] = {
                "batch_id": batch_id,
                "total": len(normalized),
                "done": 0,
                "failed": 0,
                "errors": [],
                "case_ids": normalized,
                "status": "queued",
            }
            self._pending.extend((batch_id, case_id) for case_id in normalized)
            should_start = self._inflight is None or self._inflight.done()
        if should_start:
            future = _job_pool.submit(self._drain)
            with self._lock:
                self._inflight = future
        return batch_id

    def status(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._batches.get(batch_id)
            if item is None:
                return None
            return {
                **item,
                "errors": list(item.get("errors") or []),
                "case_ids": list(item.get("case_ids") or []),
            }

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    return
                batch_id, case_id = self._pending.pop(0)
                batch = self._batches.get(batch_id)
                if batch is not None and batch["status"] == "queued":
                    batch["status"] = "running"
            try:
                best_pair_service.compute_best_pair(case_id)
            except Exception as exc:  # noqa: BLE001 - queue records and continues.
                with self._lock:
                    batch = self._batches.get(batch_id)
                    if batch is not None:
                        batch["failed"] += 1
                        batch["errors"].append({"case_id": case_id, "error": str(exc)})
            else:
                with self._lock:
                    batch = self._batches.get(batch_id)
                    if batch is not None:
                        batch["done"] += 1
            finally:
                with self._lock:
                    batch = self._batches.get(batch_id)
                    if batch is not None and batch["done"] + batch["failed"] >= batch["total"]:
                        batch["status"] = "done"


COMPUTE_QUEUE = BestPairComputeQueue()
