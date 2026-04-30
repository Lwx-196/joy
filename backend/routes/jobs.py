"""Unified job stream — fan-in of render and upgrade SSE feeds.

GET /api/jobs/stream emits every event from both queues, each tagged with
`job_type='render'|'upgrade'` (the queues self-tag in their _publish methods).

The legacy /api/render/stream is preserved in routes/render.py for
backwards compatibility, but the new code path on the frontend uses this
unified stream so a single EventSource serves both batch types.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..render_queue import RENDER_QUEUE
from ..upgrade_queue import UPGRADE_QUEUE

router = APIRouter(tags=["jobs"])


async def _fan_in(*sources: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Yield events from any of the given async iterators as they arrive.

    Implementation: pull one task per source, await whichever finishes first,
    yield it, immediately re-prime the consumed source. Cancels remaining
    tasks when the consumer stops iterating.
    """
    iterators = [s.__aiter__() for s in sources]
    pending = {asyncio.create_task(it.__anext__()): it for it in iterators}
    try:
        while pending:
            done, _ = await asyncio.wait(
                pending.keys(), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                it = pending.pop(task)
                try:
                    event = task.result()
                except StopAsyncIteration:
                    continue
                yield event
                pending[asyncio.create_task(it.__anext__())] = it
    finally:
        for task in pending:
            task.cancel()


@router.get("/api/jobs/stream")
async def jobs_stream(request: Request) -> StreamingResponse:
    """SSE feed of every job event (render + upgrade)."""

    async def event_source() -> AsyncIterator[str]:
        yield ":ok\n\n"
        merged = _fan_in(RENDER_QUEUE.subscribe(), UPGRADE_QUEUE.subscribe())
        async for event in merged:
            if await request.is_disconnected():
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
