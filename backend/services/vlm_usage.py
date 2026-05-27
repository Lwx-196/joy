"""VLM usage persistence helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def record_vlm_usage(
    conn: sqlite3.Connection,
    *,
    purpose: str,
    provider: str,
    model: str,
    case_id: int | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd_micros: int | None = None,
    cost_source: str = "unknown",
    latency_ms: int = 0,
    status: str,
    error_detail: str | None = None,
    usage_raw: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> int:
    """Insert one append-only VLM usage row and return its id.

    cost_usd_micros: USD expressed as integer micro-dollars (1 USD = 1_000_000).
    Schema column still named `cost_usd` for compatibility with the in-flight
    migration; values written are integers in micro-USD units.
    """
    usage_raw_json = json.dumps(usage_raw or {}, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        """
        INSERT INTO vlm_usage_log (
          purpose, provider, model, case_id, input_tokens, output_tokens,
          cost_usd, cost_source, latency_ms, status, error_detail,
          usage_raw_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(purpose),
            str(provider),
            str(model),
            case_id,
            _int_or_zero(input_tokens),
            _int_or_zero(output_tokens),
            _int_or_zero(cost_usd_micros),
            str(cost_source or "unknown"),
            _int_or_zero(latency_ms),
            str(status),
            error_detail,
            usage_raw_json,
            created_at or _now(),
        ),
    )
    return int(cursor.lastrowid)
