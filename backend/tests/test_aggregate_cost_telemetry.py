"""Tests for backend/scripts/aggregate_cost_telemetry.py (Phase C5.3, Stream C).

Pure read-only aggregator; we exercise the sub-functions against a hand-built
tmp sqlite so the assertions are deterministic (no dependence on now()/window
math) and cover the review findings:
  - S6: cost_per_case joins VLM cost on the finished-case case_id set
  - S7: regression guards for percentile / div-by-zero / NULL coalescing /
        window exclusion / drill exclusion / invalid --window exit code
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.scripts import aggregate_cost_telemetry as agg

# Cutoff that includes every fixture row (well before any inserted timestamp).
ALL = "2000-01-01T00:00:00+00:00"


def _make_db() -> sqlite3.Connection:
    """Minimal schema covering exactly the columns the aggregator SELECTs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE vlm_usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purpose TEXT, provider TEXT, model TEXT, case_id INTEGER,
            cost_usd REAL, latency_ms INTEGER, status TEXT, created_at TIMESTAMP
        );
        CREATE TABLE render_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER, status TEXT,
            enqueued_at TIMESTAMP, started_at TIMESTAMP, finished_at TIMESTAMP
        );
        CREATE TABLE simulation_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT, audit_json TEXT, created_at TIMESTAMP
        );
        CREATE TABLE candidate_lineage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER, attempt INTEGER, failure_reason TEXT,
            created_at TIMESTAMP
        );
        """
    )
    return conn


# --- _percentile (pure) -----------------------------------------------------

def test_percentile_empty_and_singleton():
    assert agg._percentile([], 50) == 0.0
    assert agg._percentile([], 95) == 0.0
    assert agg._percentile([42.0], 50) == 42.0
    assert agg._percentile([42.0], 95) == 42.0


def test_percentile_interpolation():
    values = [10.0, 20.0, 30.0, 40.0]
    assert agg._percentile(values, 50) == 25.0  # midpoint of 20 and 30
    assert agg._percentile(values, 0) == 10.0
    assert agg._percentile(values, 100) == 40.0


# --- empty DB → all zeros, no div-by-zero -----------------------------------

def test_empty_db_zeros():
    conn = _make_db()
    vlm = agg._aggregate_vlm_usage(conn, ALL)
    renders = agg._aggregate_render_jobs(conn, ALL)
    cost = agg._compute_cost_per_case(vlm, renders)

    assert vlm["total_calls"] == 0
    assert vlm["total_cost_usd"] == 0.0
    assert vlm["cost_usd_per_call_avg"] == 0.0
    assert vlm["latency_ms_p50"] == 0.0
    assert vlm["latency_ms_p99"] == 0.0
    assert renders["total_finished"] == 0
    assert renders["duration_ms_p50"] == 0.0
    assert renders["duration_ms_p99"] == 0.0
    assert cost["vlm_api_cost_usd"] == 0.0
    assert cost["estimated_eligible_cases"] == 0


# --- NULL coalescing --------------------------------------------------------

def test_null_cost_and_latency_coalesced():
    conn = _make_db()
    conn.executemany(
        "INSERT INTO vlm_usage_log (purpose, provider, model, case_id, "
        "cost_usd, latency_ms, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("judge", "p", "m", 1, None, None, "ok", "2026-05-01T00:00:00+00:00"),
            ("judge", "p", "m", 1, 2.0, 100, "ok", "2026-05-01T00:00:00+00:00"),
        ],
    )
    conn.commit()
    vlm = agg._aggregate_vlm_usage(conn, ALL)
    assert vlm["total_calls"] == 2
    assert vlm["total_cost_usd"] == 2.0  # None treated as 0.0
    assert vlm["_cost_by_case_id"] == {1: 2.0}


# --- window exclusion -------------------------------------------------------

def test_window_exclusion():
    conn = _make_db()
    conn.executemany(
        "INSERT INTO vlm_usage_log (purpose, provider, model, case_id, "
        "cost_usd, latency_ms, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("judge", "p", "m", 1, 5.0, 100, "ok", "2026-05-10T00:00:00+00:00"),
            ("judge", "p", "m", 2, 9.0, 100, "ok", "2020-01-01T00:00:00+00:00"),
        ],
    )
    conn.commit()
    cutoff = "2026-01-01T00:00:00+00:00"
    vlm = agg._aggregate_vlm_usage(conn, cutoff)
    assert vlm["total_calls"] == 1
    assert vlm["total_cost_usd"] == 5.0  # old row excluded


# --- S6 regression: cost_per_case joins on finished case_id -----------------

def test_cost_per_case_joins_on_finished_case_id():
    conn = _make_db()
    # case 1 finished; case 2 never finished (blocked).
    conn.executemany(
        "INSERT INTO render_jobs (case_id, status, enqueued_at, started_at, "
        "finished_at) VALUES (?,?,?,?,?)",
        [
            (1, "done", "2026-05-01T00:00:00+00:00",
             "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:30+00:00"),
            (2, "blocked", "2026-05-01T00:00:00+00:00", None, None),
        ],
    )
    # VLM cost: $1 on finished case 1, $9 on non-finished case 2.
    conn.executemany(
        "INSERT INTO vlm_usage_log (purpose, provider, model, case_id, "
        "cost_usd, latency_ms, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("judge", "p", "m", 1, 1.0, 100, "ok", "2026-05-01T00:00:00+00:00"),
            ("judge", "p", "m", 2, 9.0, 100, "ok", "2026-05-01T00:00:00+00:00"),
        ],
    )
    conn.commit()

    vlm = agg._aggregate_vlm_usage(conn, ALL)
    renders = agg._aggregate_render_jobs(conn, ALL)
    cost = agg._compute_cost_per_case(vlm, renders)

    assert cost["estimated_eligible_cases"] == 1
    # Pre-S6 (buggy) would be total 10.0 / 1 = 10.0; joined is 1.0 / 1 = 1.0.
    assert cost["vlm_api_cost_usd"] == 1.0
    assert cost["matched_vlm_cost_usd"] == 1.0
    assert cost["total_vlm_api_cost_usd"] == 10.0


def test_render_finished_states_and_duration():
    conn = _make_db()
    conn.executemany(
        "INSERT INTO render_jobs (case_id, status, enqueued_at, started_at, "
        "finished_at) VALUES (?,?,?,?,?)",
        [
            (1, "done", "2026-05-01T00:00:00+00:00",
             "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:10+00:00"),
            (2, "done_with_issues", "2026-05-01T00:00:00+00:00",
             "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:30+00:00"),
            (3, "blocked", "2026-05-01T00:00:00+00:00", None, None),
        ],
    )
    conn.commit()
    renders = agg._aggregate_render_jobs(conn, ALL)
    assert renders["total_finished"] == 2  # done + done_with_issues
    assert renders["by_status"]["blocked"] == 1
    assert renders["_finished_case_ids"] == {1, 2}
    # 10s and 30s → p50 midpoint ≈ 20000 ms (±2ms: julianday float granularity)
    assert renders["duration_ms_p50"] == pytest.approx(20000.0, abs=2.0)


# --- p99 emitted + monotonic (closes cost-telemetry-cli-usage.md p99 blind spot) ---

def test_latency_p99_emitted_and_monotonic():
    conn = _make_db()
    # 100 rows latency = 1..100 → deterministic percentiles via linear interp.
    conn.executemany(
        "INSERT INTO vlm_usage_log (purpose, provider, model, case_id, "
        "cost_usd, latency_ms, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("judge", "p", "m", i, 0.0, i, "ok", "2026-05-01T00:00:00+00:00")
            for i in range(1, 101)
        ],
    )
    conn.commit()
    vlm = agg._aggregate_vlm_usage(conn, ALL)
    assert vlm["latency_ms_p50"] == 50.5
    assert vlm["latency_ms_p95"] == 95.05
    assert vlm["latency_ms_p99"] == 99.01
    assert vlm["latency_ms_p99"] > vlm["latency_ms_p95"] > vlm["latency_ms_p50"]


def test_render_duration_p99_present():
    conn = _make_db()
    conn.executemany(
        "INSERT INTO render_jobs (case_id, status, enqueued_at, started_at, "
        "finished_at) VALUES (?,?,?,?,?)",
        [
            (i, "done", "2026-05-01T00:00:00+00:00",
             "2026-05-01T00:00:00+00:00", f"2026-05-01T00:00:{i:02d}+00:00")
            for i in range(1, 21)
        ],
    )
    conn.commit()
    renders = agg._aggregate_render_jobs(conn, ALL)
    assert "duration_ms_p99" in renders
    # p99 >= p95 >= p50 (>= because small sample can tie near the top).
    assert renders["duration_ms_p99"] >= renders["duration_ms_p95"]
    assert renders["duration_ms_p95"] >= renders["duration_ms_p50"]


def test_p99_keys_in_serialized_output():
    conn = _make_db()
    conn.execute(
        "INSERT INTO render_jobs (case_id, status, enqueued_at, started_at, "
        "finished_at) VALUES (1,'done','2026-05-01T00:00:00+00:00',"
        "'2026-05-01T00:00:00+00:00','2026-05-01T00:00:05+00:00')"
    )
    conn.commit()
    out = agg._aggregate(conn, window_days=36500)
    assert "latency_ms_p99" in out["vlm_usage"]
    assert "duration_ms_p99" in out["render_jobs"]


# --- drill exclusion --------------------------------------------------------

def test_simulation_drill_exclusion():
    conn = _make_db()
    conn.executemany(
        "INSERT INTO simulation_jobs (status, audit_json, created_at) "
        "VALUES (?,?,?)",
        [
            ("done", '{"foo": 1}', "2026-05-01T00:00:00+00:00"),
            ("done", '{"drill_marker": true}', "2026-05-01T00:00:00+00:00"),
            ("failed", '{"drill_id": "x"}', "2026-05-01T00:00:00+00:00"),
        ],
    )
    conn.commit()
    sims = agg._aggregate_simulation_jobs(conn, ALL)
    assert sims["drill_excluded"] == 2
    assert sims["total"] == 1
    assert sims["by_status"] == {"done": 1}


# --- candidate_lineage ------------------------------------------------------

def test_candidate_lineage_attempts_and_reasons():
    conn = _make_db()
    conn.executemany(
        "INSERT INTO candidate_lineage (case_id, attempt, failure_reason, "
        "created_at) VALUES (?,?,?,?)",
        [
            (1, 1, None, "2026-05-01T00:00:00+00:00"),
            (1, 2, "vlm_reject", "2026-05-01T00:00:00+00:00"),
            (2, 1, "vlm_reject", "2026-05-01T00:00:00+00:00"),
        ],
    )
    conn.commit()
    lineage = agg._aggregate_candidate_lineage(conn, ALL)
    assert lineage["total_attempts"] == 3
    assert lineage["unique_cases"] == 2
    assert lineage["attempts_per_case_avg"] == 1.5
    assert lineage["failure_reasons"] == {"vlm_reject": 2}


# --- main() argument guards (exit code 2, no DB touched) --------------------

@pytest.mark.parametrize("bad", ["0", "-5"])
def test_main_invalid_window_exit_2(bad: str):
    assert agg.main(["--window", bad]) == 2


def test_aggregate_top_level_keys_and_no_scratch():
    conn = _make_db()
    conn.execute(
        "INSERT INTO render_jobs (case_id, status, enqueued_at, started_at, "
        "finished_at) VALUES (1,'done','2026-05-01T00:00:00+00:00',"
        "'2026-05-01T00:00:00+00:00','2026-05-01T00:00:05+00:00')"
    )
    conn.commit()
    out = agg._aggregate(conn, window_days=36500)  # huge window includes all
    for key in (
        "schema_version", "window_days", "vlm_usage", "render_jobs",
        "simulation_jobs", "candidate_lineage", "cost_per_case", "limitations",
    ):
        assert key in out
    # internal scratch fields must be stripped before serialization
    assert "_finished_case_ids" not in out["render_jobs"]
    assert "_cost_by_case_id" not in out["vlm_usage"]
    assert out["schema_version"] == agg.SCHEMA_VERSION
