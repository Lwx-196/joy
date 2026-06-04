"""VLM usage persistence."""
from __future__ import annotations


def test_vlm_usage_log_schema_exists_after_init_schema(temp_db) -> None:
    from backend import db

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(vlm_usage_log)")}

    assert {
        "id",
        "purpose",
        "provider",
        "model",
        "case_id",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "cost_source",
        "latency_ms",
        "status",
        "error_detail",
        "usage_raw_json",
        "created_at",
    } <= columns


def test_record_vlm_usage_inserts_queryable_row(temp_db) -> None:
    from backend import db
    from backend.services.vlm_usage import record_vlm_usage

    with db.connect() as conn:
        now = "2026-05-18T00:00:00+00:00"
        scan_id = conn.execute(
            "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
            (now, "[]", "unit"),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO cases (
              id, scan_id, abs_path, category, last_modified, indexed_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (126, scan_id, "/tmp/case-126", "face", now, now),
        )
        usage_id = record_vlm_usage(
            conn,
            purpose="judge",
            provider="vertex_generate_content_adc",
            model="gemini-2.5-flash",
            case_id=126,
            input_tokens=123,
            output_tokens=45,
            cost_usd=0.0012,
            cost_source="estimated",
            latency_ms=987,
            status="success",
            usage_raw={"prompt_token_count": 123, "response_token_count": 45},
        )
        row = conn.execute("SELECT * FROM vlm_usage_log WHERE id = ?", (usage_id,)).fetchone()

    assert row is not None
    assert row["purpose"] == "judge"
    assert row["provider"] == "vertex_generate_content_adc"
    assert row["model"] == "gemini-2.5-flash"
    assert row["case_id"] == 126
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 45
    assert row["cost_usd"] == 0.0012
    assert row["cost_source"] == "estimated"
    assert row["latency_ms"] == 987
    assert row["status"] == "success"
    assert "prompt_token_count" in row["usage_raw_json"]
