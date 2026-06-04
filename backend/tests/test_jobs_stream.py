"""Tests for backend.routes.jobs — SSE fan-in stream."""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator
from unittest.mock import patch

import pytest

from backend.routes.jobs import _fan_in


async def _async_iter(items: list[dict[str, Any]], delay: float = 0) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        if delay:
            await asyncio.sleep(delay)
        yield item


class TestFanIn:
    @pytest.mark.asyncio
    async def test_single_source(self) -> None:
        events = [{"type": "render", "id": 1}, {"type": "render", "id": 2}]
        result = [e async for e in _fan_in(_async_iter(events))]
        assert result == events

    @pytest.mark.asyncio
    async def test_two_sources_interleaved(self) -> None:
        src_a = [{"src": "a", "i": i} for i in range(3)]
        src_b = [{"src": "b", "i": i} for i in range(3)]
        result = [e async for e in _fan_in(_async_iter(src_a), _async_iter(src_b))]
        assert len(result) == 6
        a_events = [e for e in result if e["src"] == "a"]
        b_events = [e for e in result if e["src"] == "b"]
        assert a_events == src_a
        assert b_events == src_b

    @pytest.mark.asyncio
    async def test_empty_sources(self) -> None:
        result = [e async for e in _fan_in(_async_iter([]), _async_iter([]))]
        assert result == []

    @pytest.mark.asyncio
    async def test_one_empty_one_full(self) -> None:
        events = [{"x": 1}, {"x": 2}]
        result = [e async for e in _fan_in(_async_iter([]), _async_iter(events))]
        assert result == events

    @pytest.mark.asyncio
    async def test_preserves_order_within_source(self) -> None:
        slow = [{"src": "slow", "i": i} for i in range(5)]
        fast = [{"src": "fast", "i": i} for i in range(5)]
        result = [e async for e in _fan_in(_async_iter(slow, delay=0.01), _async_iter(fast))]
        slow_order = [e["i"] for e in result if e["src"] == "slow"]
        fast_order = [e["i"] for e in result if e["src"] == "fast"]
        assert slow_order == list(range(5))
        assert fast_order == list(range(5))

    @pytest.mark.asyncio
    async def test_cancellation_on_break(self) -> None:
        async def infinite() -> AsyncIterator[dict[str, Any]]:
            i = 0
            while True:
                yield {"i": i}
                i += 1
                await asyncio.sleep(0)

        collected = []
        async for event in _fan_in(infinite()):
            collected.append(event)
            if len(collected) >= 3:
                break
        assert len(collected) == 3

    @pytest.mark.asyncio
    async def test_three_sources(self) -> None:
        a = [{"s": "a"}]
        b = [{"s": "b"}, {"s": "b"}]
        c = [{"s": "c"}, {"s": "c"}, {"s": "c"}]
        result = [e async for e in _fan_in(_async_iter(a), _async_iter(b), _async_iter(c))]
        assert len(result) == 6


class TestJobsStreamEndpoint:
    def test_stream_returns_sse_headers_and_ok(self, client) -> None:
        async def _one_event() -> AsyncIterator[dict[str, Any]]:
            yield {"job_type": "render", "id": 1}

        with (
            patch("backend.routes.jobs.RENDER_QUEUE") as mock_rq,
            patch("backend.routes.jobs.UPGRADE_QUEUE") as mock_uq,
        ):
            mock_rq.subscribe = _one_event
            mock_uq.subscribe = lambda: _async_iter([])

            with client.stream("GET", "/api/jobs/stream") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                assert resp.headers["cache-control"] == "no-cache, no-transform"
                assert resp.headers["x-accel-buffering"] == "no"
                lines = [line for line in resp.iter_lines() if line]
                assert lines[0] == ":ok"
                data_line = next((line for line in lines if line.startswith("data:")), None)
                assert data_line is not None
                payload = json.loads(data_line.removeprefix("data:").strip())
                assert payload["job_type"] == "render"
                assert payload["id"] == 1
