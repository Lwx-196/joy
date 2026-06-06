"""P0.1 — 失败观测结构化：simulation_jobs.audit_json.failure + vlm_usage_log.error_json.

每条 simulation_job 失败 / VLM per-image 失败必须能 SQL 归因到结构化字段，
而不是只有 4000 字符截断的 error_message。
"""
from __future__ import annotations

import json
from pathlib import Path



# ---------- Schema-level checks ----------


def test_vlm_usage_log_has_error_json_column(temp_db: Path) -> None:
    """P0.1 schema: vlm_usage_log.error_json TEXT 列必须存在（新建库或 migration 后）。"""
    from backend import db

    with db.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(vlm_usage_log)").fetchall()}
    assert "error_json" in columns, (
        "vlm_usage_log.error_json column missing — P0.1 schema migration 未生效"
    )


# ---------- simulation_jobs.audit_json.failure ----------


def _seed_run_after(monkeypatch, *, raises: Exception | None = None):
    """Stub the ps_model_router adapter to either succeed or raise."""
    from backend import ai_generation_adapter

    def fake_run(**kwargs):
        if raises is not None:
            raise raises
        return {
            "status": "done",
            "output_refs": [{"kind": "image", "path": "/tmp/fake.png"}],
            "audit": {
                "provider": "ps_model_router",
                "model_name": "stub-model",
                "focus_targets": kwargs.get("focus_targets") or [],
                "focus_regions": kwargs.get("focus_regions") or [],
                "policy": {"candidate_only": True},
            },
            "watermarked": True,
            "error_message": None,
        }

    monkeypatch.setattr(ai_generation_adapter, "run_ps_model_router_after_simulation", fake_run)


def test_simulate_after_failure_writes_structured_audit_failure_block(
    client, seed_case, monkeypatch, tmp_path: Path, sync_job_pool
) -> None:
    """P0.1: simulate-after provider 抛异常 → simulation_jobs.audit_json.failure 含 7 必填 key。"""
    from backend import db

    case_dir = tmp_path / "case-fail"
    case_dir.mkdir()
    (case_dir / "术后.jpg").write_bytes(b"bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    _seed_run_after(monkeypatch, raises=RuntimeError("boom-from-provider"))

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后.jpg",
            "focus_targets": ["下颌线"],
            "focus_regions": [],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
            "model_name": "stub-model",
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["simulation_job_id"]

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, audit_json, error_message FROM simulation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "failed"
    audit = json.loads(row["audit_json"])
    assert "failure" in audit, "audit_json.failure block missing on failure"
    failure = audit["failure"]
    assert failure is not None
    for key in (
        "failure_stage",
        "error_class",
        "error_message",
        "provider_attempts",
        "workflow_name",
        "retry_trace",
        "traceback",
    ):
        assert key in failure, f"failure block missing key: {key}"
    assert failure["error_class"] == "RuntimeError"
    assert "boom-from-provider" in failure["error_message"]
    assert isinstance(failure["provider_attempts"], list)
    assert failure["provider_attempts"], "provider_attempts must list at least 1 attempt"
    assert failure["provider_attempts"][0].get("provider") == "ps_model_router"
    assert isinstance(failure["retry_trace"], list)
    assert "RuntimeError" in failure["traceback"]
    # legacy error_message still populated for back-compat
    assert "boom-from-provider" in (row["error_message"] or "")


def test_simulate_after_success_writes_explicit_failure_null(
    client, seed_case, monkeypatch, tmp_path: Path, sync_job_pool
) -> None:
    """P0.1: 成功路径 audit_json.failure 显式为 null，避免 missing-key 歧义。"""
    from backend import db

    case_dir = tmp_path / "case-ok"
    case_dir.mkdir()
    (case_dir / "术后.jpg").write_bytes(b"bytes")
    case_id = seed_case(abs_path=str(case_dir), category="standard_face", template_tier="single")

    _seed_run_after(monkeypatch, raises=None)

    resp = client.post(
        f"/api/cases/{case_id}/simulate-after",
        json={
            "after_image_path": "术后.jpg",
            "focus_targets": ["下颌线"],
            "focus_regions": [],
            "ai_generation_authorized": True,
            "provider": "ps_model_router",
            "model_name": "stub-model",
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["simulation_job_id"]

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, audit_json FROM simulation_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "done"
    audit = json.loads(row["audit_json"])
    assert "failure" in audit, "success path must also have explicit failure key"
    assert audit["failure"] is None, "success path must have failure: null (not missing)"


# ---------- vlm_usage_log.error_json on per-image classifier failure ----------


def _write_tiny_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _seed_observation_for_classifier(conn, *, case_id: int, root_path: str, image_path: str) -> int:
    now = "2026-05-27T00:00:00+00:00"
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    conn.execute(
        """
        INSERT OR IGNORE INTO cases (id, scan_id, abs_path, category, last_modified, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (case_id, scan_id, root_path, "standard_face", now, now),
    )
    group_id = conn.execute(
        """
        INSERT INTO case_groups (
          group_key, primary_case_id, title, root_path, case_ids_json,
          status, diagnosis_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{root_path}-{image_path}",
            case_id,
            "unit",
            root_path,
            json.dumps([case_id]),
            "needs_review",
            "{}",
            now,
            now,
        ),
    ).lastrowid
    return int(
        conn.execute(
            """
            INSERT INTO image_observations (
              group_id, case_id, image_path, phase, body_part, view,
              quality_json, confidence, source, reasons_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                case_id,
                image_path,
                "unknown",
                "face",
                "unknown",
                "{}",
                0.25,
                "rules",
                "[]",
                now,
                now,
            ),
        ).lastrowid
    )


class _PartialFailureProvider:
    """call_vision raises on filenames containing 'fail'; mirrors HEIC failure."""

    name = "fake-classifier"
    model = "fake-model"

    def call_vision_batch(self, requests, *, concurrency: int = 1, return_exceptions: bool = False):
        out = []
        for req in requests:
            try:
                out.append(self.call_vision(req.prompt, req.images, timeout=req.timeout, purpose=req.purpose))
            except BaseException as exc:  # noqa: BLE001
                if return_exceptions:
                    out.append(exc)
                else:
                    raise
        return out

    def call_vision(self, prompt, images, *, timeout=30.0, purpose=None):
        from backend.services.vlm_provider import VLMResponse

        if any("fail" in str(p) for p in images):
            raise ValueError("simulated HEIC decode failure")
        parsed = {
            "phase": "after",
            "view": "front",
            "body_part": "face",
            "confidence": 0.95,
            "reasoning": "stub",
        }
        return VLMResponse(
            text=json.dumps(parsed),
            parsed=parsed,
            provider="fake-classifier",
            model="fake-model",
            latency_ms=10,
            input_tokens=10,
            output_tokens=10,
            usage_raw={},
        )


def test_vlm_usage_log_error_json_persisted_on_per_image_exception(
    temp_db: Path, tmp_path: Path
) -> None:
    """P0.1: classifier per-image 异常 → vlm_usage_log 写一条 status=error 行，error_json 含结构化失败上下文。"""
    from backend import db
    from backend.services.vlm_source_classifier import run_classification

    good = tmp_path / "good.png"
    bad = tmp_path / "fail-heic.png"
    _write_tiny_png(good)
    _write_tiny_png(bad)

    with db.connect() as conn:
        _seed_observation_for_classifier(conn, case_id=126, root_path=str(tmp_path), image_path="good.png")
        _seed_observation_for_classifier(conn, case_id=126, root_path=str(tmp_path), image_path="fail-heic.png")
        report = run_classification(
            conn,
            provider=_PartialFailureProvider(),
            case_id=126,
            mode="apply",
        )
        rows = conn.execute(
            "SELECT status, error_detail, error_json FROM vlm_usage_log "
            "WHERE purpose = 'classifier' ORDER BY id"
        ).fetchall()

    assert report["error_count"] == 1
    statuses = [r["status"] for r in rows]
    assert "error" in statuses, f"expected at least one status=error row, got {statuses}"
    error_rows = [r for r in rows if r["status"] == "error"]
    assert error_rows, "no error row written to vlm_usage_log"
    err_row = error_rows[0]
    assert err_row["error_json"] is not None, "error_json must be populated on classifier exception"
    payload = json.loads(err_row["error_json"])
    assert payload.get("error_class") == "ValueError"
    assert "HEIC" in (payload.get("error_message") or "") or "HEIC" in (err_row["error_detail"] or "")
    assert "provider" in payload
    assert "attempt" in payload
    assert "traceback" in payload
