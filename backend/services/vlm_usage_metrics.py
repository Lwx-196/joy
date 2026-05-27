"""VLM observability — summary metrics over vlm_usage_log + image_observations.

PLAN P2 wave-1: provider-level p50/p95 latency, cost, token breakdown, and
classifier output distribution (phase/view/body_part/confidence) for the 763
queue that completed apply on 2026-05-26.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def _percentile(sorted_values: list[int | float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    k = (len(sorted_values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = k - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def _build_filter(
    since: str | None,
    until: str | None,
    purpose: str | None,
    provider: str | None,
    status: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at < ?")
        params.append(until)
    if purpose:
        clauses.append("purpose = ?")
        params.append(purpose)
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ""
    if clauses:
        where = " WHERE " + " AND ".join(clauses)
    return where, params


def summarize_usage(
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    purpose: str | None = None,
    provider: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Aggregate vlm_usage_log rows into actionable observability metrics.

    Returns a dict with: total count, status/purpose/provider breakdowns,
    latency (p50/p95/p99/max), token totals, cost totals, and per-provider
    drill-down (counts + latency p50/p95 + cost sum). All filters are optional;
    omitting all filters reports the full table.
    """
    where, params = _build_filter(since, until, purpose, provider, status)

    summary: dict[str, Any] = {
        "filters": {
            "since": since, "until": until, "purpose": purpose,
            "provider": provider, "status": status,
        },
        "total_calls": 0,
        "status_breakdown": {},
        "purpose_breakdown": {},
        "provider_breakdown": {},
        "latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0, "mean": 0.0},
        "tokens": {"input_total": 0, "output_total": 0, "input_mean": 0.0, "output_mean": 0.0},
        "cost_usd": {"total": 0.0, "mean": 0.0},
        "per_provider": {},
    }

    rows = conn.execute(
        f"SELECT purpose, provider, status, latency_ms, input_tokens, output_tokens, cost_usd "
        f"FROM vlm_usage_log{where}",
        params,
    ).fetchall()

    if not rows:
        return summary

    summary["total_calls"] = len(rows)
    latencies: list[int] = []
    input_total = 0
    output_total = 0
    cost_total = 0.0
    by_provider: dict[str, dict[str, Any]] = {}

    for row in rows:
        st = row["status"] or "unknown"
        ps = row["purpose"] or "unknown"
        pr = row["provider"] or "unknown"
        lat = int(row["latency_ms"] or 0)
        in_tk = int(row["input_tokens"] or 0)
        out_tk = int(row["output_tokens"] or 0)
        cost = float(row["cost_usd"] or 0.0)

        summary["status_breakdown"][st] = summary["status_breakdown"].get(st, 0) + 1
        summary["purpose_breakdown"][ps] = summary["purpose_breakdown"].get(ps, 0) + 1
        summary["provider_breakdown"][pr] = summary["provider_breakdown"].get(pr, 0) + 1

        latencies.append(lat)
        input_total += in_tk
        output_total += out_tk
        cost_total += cost

        bucket = by_provider.setdefault(pr, {
            "count": 0, "latency_ms": [], "cost_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0,
        })
        bucket["count"] += 1
        bucket["latency_ms"].append(lat)
        bucket["cost_usd"] += cost
        bucket["input_tokens"] += in_tk
        bucket["output_tokens"] += out_tk

    sorted_lat = sorted(latencies)
    summary["latency_ms"] = {
        "p50": round(_percentile(sorted_lat, 0.50), 1),
        "p95": round(_percentile(sorted_lat, 0.95), 1),
        "p99": round(_percentile(sorted_lat, 0.99), 1),
        "max": max(sorted_lat),
        "mean": round(sum(sorted_lat) / len(sorted_lat), 1),
    }
    summary["tokens"] = {
        "input_total": input_total,
        "output_total": output_total,
        "input_mean": round(input_total / len(rows), 1),
        "output_mean": round(output_total / len(rows), 1),
    }
    summary["cost_usd"] = {
        "total": round(cost_total, 6),
        "mean": round(cost_total / len(rows), 8),
    }
    summary["per_provider"] = {
        pr: {
            "count": b["count"],
            "latency_p50_ms": round(_percentile(sorted(b["latency_ms"]), 0.50), 1),
            "latency_p95_ms": round(_percentile(sorted(b["latency_ms"]), 0.95), 1),
            "cost_usd_total": round(b["cost_usd"], 6),
            "tokens_input_total": b["input_tokens"],
            "tokens_output_total": b["output_tokens"],
        }
        for pr, b in by_provider.items()
    }
    return summary


def summarize_classifier_outputs(
    conn: sqlite3.Connection,
    *,
    source: str = "vlm_classifier",
) -> dict[str, Any]:
    """Distribution of phase/view/body_part/confidence for image_observations
    written by a given source. Default to ``vlm_classifier`` to inspect the
    763 entries produced by the 2026-05-26 batch."""
    rows = conn.execute(
        "SELECT phase, view, body_part, confidence FROM image_observations WHERE source = ?",
        (source,),
    ).fetchall()

    distribution: dict[str, Any] = {
        "source": source,
        "total": 0,
        "phase": {},
        "view": {},
        "body_part": {},
        "confidence_buckets": {
            "0.0-0.5": 0,
            "0.5-0.8": 0,
            "0.8-0.9": 0,
            "0.9-1.0": 0,
        },
        "confidence_mean": 0.0,
    }
    if not rows:
        return distribution

    distribution["total"] = len(rows)
    confidences: list[float] = []
    for row in rows:
        ph = row["phase"] or "unknown"
        vw = row["view"] or "unknown"
        bp = row["body_part"] or "unknown"
        cf = float(row["confidence"] or 0.0)
        distribution["phase"][ph] = distribution["phase"].get(ph, 0) + 1
        distribution["view"][vw] = distribution["view"].get(vw, 0) + 1
        distribution["body_part"][bp] = distribution["body_part"].get(bp, 0) + 1
        confidences.append(cf)
        if cf < 0.5:
            distribution["confidence_buckets"]["0.0-0.5"] += 1
        elif cf < 0.8:
            distribution["confidence_buckets"]["0.5-0.8"] += 1
        elif cf < 0.9:
            distribution["confidence_buckets"]["0.8-0.9"] += 1
        else:
            distribution["confidence_buckets"]["0.9-1.0"] += 1
    distribution["confidence_mean"] = round(sum(confidences) / len(confidences), 4)
    return distribution
