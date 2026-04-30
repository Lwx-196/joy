"""Shared ThreadPoolExecutor for all heavy job queues.

Both `render_queue` and `upgrade_queue` submit work here so the pool size acts
as a global concurrency cap. The cap is 2 because each running job spawns a
mediapipe-backed subprocess (~150MB resident); 4 in parallel would push a dev
laptop into swap. v1.5 already validated 2 concurrent skill subprocesses as the
sweet spot.

Sharing the pool means a busy render batch and a busy upgrade batch take turns
on the same workers rather than each running their own pair.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

MAX_WORKERS = 2

_pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="job")


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> "Future[Any]":
    return _pool.submit(fn, *args, **kwargs)


def shutdown(wait: bool = True) -> None:
    _pool.shutdown(wait=wait)
