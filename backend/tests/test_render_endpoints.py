"""Tests for render queue HTTP endpoints (enqueue/list/get/cancel).

These tests exercise the route layer + DB-row insertion behaviour. The
shared `no_job_pool` fixture replaces `_job_pool.submit` with a no-op so
enqueued jobs stay in 'queued' status — we never actually run mediapipe.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from backend import db


def test_enqueue_single_404_for_missing_case(client, no_job_pool):
    resp = client.post("/api/cases/9999/render", json={"brand": "fumei"})
    assert resp.status_code == 404
    assert "case 9999 not found" in resp.json()["detail"]


def test_enqueue_single_400_for_bad_brand(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(f"/api/cases/{case_id}/render", json={"brand": "nope"})
    assert resp.status_code == 400
    assert "unsupported brand" in resp.json()["detail"]


def test_enqueue_single_400_for_bad_semantic_judge(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(
        f"/api/cases/{case_id}/render",
        json={"brand": "fumei", "semantic_judge": "wrong"},
    )
    assert resp.status_code == 400
    assert "semantic_judge must be one of" in resp.json()["detail"]


def test_enqueue_single_inserts_queued_row(client, seed_case, no_job_pool):
    case_id = seed_case()
    # force=True bypasses pre_render_gate; seed_case has no real image files
    # so the gate would block with no_real_source_photos. This test verifies
    # enqueue mechanics, not gate behavior — gate has dedicated tests.
    resp = client.post(f"/api/cases/{case_id}/render", json={"brand": "fumei", "force": True})
    assert resp.status_code == 200
    body = resp.json()
    job_id = body["job_id"]
    assert isinstance(job_id, int) and job_id > 0
    assert body["batch_id"] is None

    # Verify via GET /api/render/jobs/{id}
    detail = client.get(f"/api/render/jobs/{job_id}").json()
    assert detail["case_id"] == case_id
    assert detail["status"] == "queued"
    assert detail["brand"] == "fumei"
    assert detail["template"] == "tri-compare"
    assert detail["semantic_judge"] == "auto"


def test_batch_enqueue_400_empty(client, no_job_pool):
    resp = client.post("/api/cases/render/batch", json={"case_ids": []})
    assert resp.status_code == 400
    assert "case_ids cannot be empty" in resp.json()["detail"]


def test_batch_enqueue_400_oversize(client, no_job_pool):
    resp = client.post(
        "/api/cases/render/batch",
        json={"case_ids": list(range(1, 100))},
    )
    assert resp.status_code == 400
    assert "exceeds maximum" in resp.json()["detail"]


def test_batch_enqueue_404_when_all_cases_missing(client, no_job_pool):
    resp = client.post(
        "/api/cases/render/batch",
        json={"case_ids": [9990, 9991, 9992]},
    )
    assert resp.status_code == 404
    assert "no valid case ids" in resp.json()["detail"]


def test_batch_enqueue_partial_success_returns_skipped_count(
    client, seed_case, no_job_pool
):
    a = seed_case(abs_path="/tmp/case-a")
    b = seed_case(abs_path="/tmp/case-b")
    resp = client.post(
        "/api/cases/render/batch",
        # force=True bypasses pre_render_gate (see test_enqueue_single comment)
        json={"case_ids": [a, b, 9999], "force": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["batch_id"], str) and body["batch_id"].startswith("batch-")
    assert len(body["job_ids"]) == 2
    assert body["skipped_count"] == 1

    # Both jobs visible in batch detail
    summary = client.get(f"/api/render/batches/{body['batch_id']}").json()
    assert summary["total"] == 2
    assert summary["counts"] == {"queued": 2}


def test_list_case_jobs_empty_then_populated(client, seed_case, no_job_pool):
    case_id = seed_case()
    empty = client.get(f"/api/cases/{case_id}/render/jobs").json()
    assert empty == []

    job_id = client.post(
        f"/api/cases/{case_id}/render", json={"brand": "fumei", "force": True}
    ).json()["job_id"]
    listing = client.get(f"/api/cases/{case_id}/render/jobs").json()
    assert len(listing) == 1
    assert listing[0]["id"] == job_id
    assert listing[0]["status"] == "queued"


def test_latest_job_null_when_no_history(client, seed_case, no_job_pool):
    case_id = seed_case()
    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body == {"job": None}


def test_latest_job_returns_most_recent(client, seed_case, no_job_pool):
    case_id = seed_case()
    j1 = client.post(f"/api/cases/{case_id}/render", json={"brand": "fumei", "force": True}).json()[
        "job_id"
    ]
    j2 = client.post(f"/api/cases/{case_id}/render", json={"brand": "fumei", "force": True}).json()[
        "job_id"
    ]
    assert j2 > j1
    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body["job"]["id"] == j2


def test_latest_job_prefers_latest_blocked_output_over_older_done(client, seed_case, tmp_path):
    case_id = seed_case(abs_path=str(tmp_path / "case-latest-blocked-output"))
    old_output = tmp_path / "old-final-board.jpg"
    blocked_output = tmp_path / "blocked-final-board.jpg"
    old_output.write_bytes(b"old board")
    blocked_output.write_bytes(b"blocked board")

    with db.connect() as conn:
        old_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done', '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:10+00:00', ?, 'off', '{}')
            """,
            (case_id, str(old_output)),
        ).lastrowid
        blocked_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, enqueued_at, finished_at, output_path,
               semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'blocked', '2026-01-01T00:01:00+00:00',
                    '2026-01-01T00:01:10+00:00', ?, 'off', '{}')
            """,
            (case_id, str(blocked_output)),
        ).lastrowid

    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert old_id < blocked_id
    assert body["job"]["id"] == blocked_id
    assert body["job"]["status"] == "blocked"
    assert body["job"]["output_path"] == str(blocked_output)


def test_render_queue_passes_review_exclusions_to_runner(seed_case, tmp_path, monkeypatch):
    from backend import db, render_queue

    case_dir = tmp_path / "case-render-exclusion"
    case_dir.mkdir()
    filenames = [
        "术前正面.jpg",
        "术后正面.jpg",
        "术前45.jpg",
        "术后45.jpg",
        "术前侧面.jpg",
        "术后侧面.jpg",
        "术后正面-废片.jpg",
    ]
    for filename in filenames:
        (case_dir / filename).write_bytes(b"fake")
    case_id = seed_case(abs_path=str(case_dir))
    meta = {
        "image_files": filenames,
        "image_review_states": {
            "术后正面-废片.jpg": {
                "verdict": "excluded",
                "render_excluded": True,
                "reviewer": "tester",
            }
        },
    }
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), case_id),
        )
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
                (case_id, brand, template, status, enqueued_at, semantic_judge, render_mode)
            VALUES (?, 'fumei', 'tri-compare', 'queued', ?, 'off', 'standard')
            """,
            (case_id, now),
        ).lastrowid

    captured: dict[str, dict] = {}

    def fake_run_render(case_dir_arg, **kwargs):
        captured.update(kwargs.get("manual_overrides") or {})
        out = tmp_path / "final-board.jpg"
        manifest = tmp_path / "manifest.final.json"
        out.write_bytes(b"fake")
        manifest.write_text(json.dumps({"groups": []}), encoding="utf-8")
        return {
            "output_path": str(out),
            "manifest_path": str(manifest),
            "status": "ok",
            "blocking_issue_count": 0,
            "warning_count": 0,
            "case_mode": "face",
            "effective_templates": ["tri-compare"],
            "ai_usage": {},
            "blocking_issues": [],
            "warnings": [],
            "composition_alerts": [],
        }

    monkeypatch.setattr(render_queue.render_executor, "run_render", fake_run_render)
    worker = render_queue.RenderQueue()
    worker._execute_render(job_id)

    assert captured["术后正面-废片.jpg"]["render_excluded"] is True
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM render_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"


def test_latest_job_prefers_visible_output_over_newer_failed_job(client, seed_case, tmp_path, no_job_pool):
    case_dir = tmp_path / "case-with-output"
    case_dir.mkdir()
    out_dir = case_dir / ".case-layout-output" / "fumei" / "tri-compare" / "render"
    out_dir.mkdir(parents=True)
    output_path = out_dir / "final-board.jpg"
    output_path.write_bytes(b"fake-jpg")
    now = datetime.now(timezone.utc).isoformat()
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        done_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, batch_id, enqueued_at, started_at, finished_at,
               output_path, manifest_path, error_message, semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done_with_issues', NULL, ?, ?, ?,
                    ?, NULL, NULL, 'auto', ?)
            """,
            (case_id, now, now, now, str(output_path), json.dumps({"status": "ok"})),
        ).lastrowid
        failed_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, batch_id, enqueued_at, started_at, finished_at,
               output_path, manifest_path, error_message, semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'failed', NULL, datetime(?, '+1 second'), ?, datetime(?, '+1 second'),
                    NULL, NULL, 'render failed', 'auto', '{}')
            """,
            (case_id, now, now, now),
        ).lastrowid
    assert failed_id > done_id

    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body["job"]["id"] == done_id
    assert body["job"]["status"] == "done_with_issues"
    assert body["job"]["output_path"] == str(output_path)
    assert body["job"]["output_mtime"] is not None


def test_confirm_burn_injects_allow_burn_into_options(client, seed_case, no_job_pool):
    """F2（frugal-cache-guard）：确认卡点「确认出图」→ confirm_burn=true →
    enqueue 把 options.allow_burn=True 落进 meta_json；下游执行器据此授权 cache-miss 真烧。
    不带 confirm_burn 时 options 里没有 allow_burn（走预判护栏）。"""
    case_id = seed_case()
    # 确认烧钱 → allow_burn 进 options
    confirmed = client.post(
        f"/api/cases/{case_id}/render",
        json={"brand": "fumei", "force": True, "confirm_burn": True},
    )
    assert confirmed.status_code == 200
    detail = client.get(f"/api/render/jobs/{confirmed.json()['job_id']}").json()
    assert detail["meta"]["options"]["allow_burn"] is True

    # 默认（未确认）→ options 不含 allow_burn
    default = client.post(
        f"/api/cases/{case_id}/render",
        json={"brand": "fumei", "force": True},
    )
    assert default.status_code == 200
    detail2 = client.get(f"/api/render/jobs/{default.json()['job_id']}").json()
    assert "allow_burn" not in (detail2["meta"].get("options") or {})


def test_latest_job_prefers_needs_confirmation_over_older_done(client, seed_case, tmp_path, no_job_pool):
    """F2：needs_confirmation = cache-miss 待用户确认的在途决策点，必须优先于旧 done 板展示，
    否则确认卡被旧成品盖住，用户永远看不到烧钱确认提示。"""
    case_dir = tmp_path / "case-needs-confirm"
    case_dir.mkdir()
    out_dir = case_dir / ".case-layout-output" / "fumei" / "tri-compare" / "render"
    out_dir.mkdir(parents=True)
    output_path = out_dir / "final-board.jpg"
    output_path.write_bytes(b"fake-jpg")
    now = datetime.now(timezone.utc).isoformat()
    case_id = seed_case(abs_path=str(case_dir))
    with db.connect() as conn:
        done_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, batch_id, enqueued_at, started_at, finished_at,
               output_path, manifest_path, error_message, semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'done', NULL, ?, ?, ?,
                    ?, NULL, NULL, 'auto', ?)
            """,
            (case_id, now, now, now, str(output_path), json.dumps({"status": "ok"})),
        ).lastrowid
        # 同 enqueued_at（与 done 同格式串），靠 id DESC 决出 confirm 为 latest_row —
        # 避免 SQLite datetime() 改写时间串格式（'T'→空格）破坏字典序排序。
        confirm_id = conn.execute(
            """
            INSERT INTO render_jobs
              (case_id, brand, template, status, batch_id, enqueued_at, started_at, finished_at,
               output_path, manifest_path, error_message, semantic_judge, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'needs_confirmation', NULL, ?, ?, ?,
                    NULL, NULL, NULL, 'auto', ?)
            """,
            (
                case_id, now, now, now,
                json.dumps({
                    "status": "needs_confirmation",
                    "cache_miss_count": 2,
                    "cache_miss_total": 3,
                    "cache_miss_est_cost_usd": 0.114,
                    "cache_miss_est_seconds": 300,
                }),
            ),
        ).lastrowid
    assert confirm_id > done_id

    body = client.get(f"/api/cases/{case_id}/render/latest").json()
    assert body["job"]["id"] == confirm_id
    assert body["job"]["status"] == "needs_confirmation"
    assert body["job"]["meta"]["cache_miss_count"] == 2
    assert body["job"]["meta"]["cache_miss_est_cost_usd"] == 0.114


def test_render_queue_ai_path_passes_allow_burn_and_surfaces_cache_miss(seed_case, tmp_path, monkeypatch):
    """F2：render_mode='ai' job 的 meta.options.allow_burn 透传到执行器 allow_burn 形参；
    执行器返回 status='needs_confirmation' → job.status=needs_confirmation +
    cache_miss_* 进 meta_json（前端确认卡数据源）。"""
    from backend import db, render_queue

    case_dir = tmp_path / "case-ai-cache-miss"
    case_dir.mkdir()
    case_id = seed_case(abs_path=str(case_dir))
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
                (case_id, brand, template, status, enqueued_at, semantic_judge, render_mode, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'queued', ?, 'off', 'ai', ?)
            """,
            (
                case_id, now,
                json.dumps({"options": {"enhance_direction": "heal", "allow_burn": True}}),
            ),
        ).lastrowid

    captured: dict = {}

    def fake_run_ai_enhanced_render(case_dir_arg, **kwargs):
        captured.update(kwargs)
        return {
            "output_path": None,
            "manifest_path": None,
            "status": "needs_confirmation",
            "case_mode": "ai_enhanced_board",
            "enhance": {"direction": "heal", "model": "gemini-3-pro-image"},
            "blocking_issue_count": 0,
            "warning_count": 0,
            "effective_templates": {},
            "manual_overrides_applied": [],
            "cache_miss_count": 2,
            "cache_miss_total": 3,
            "cache_miss_est_cost_usd": 0.114,
            "cache_miss_est_seconds": 300,
        }

    monkeypatch.setattr(
        render_queue.render_executor, "run_ai_enhanced_render", fake_run_ai_enhanced_render
    )
    worker = render_queue.RenderQueue()
    worker._execute_render(job_id)

    assert captured.get("allow_burn") is True
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, meta_json FROM render_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row["status"] == "needs_confirmation"
    meta = json.loads(row["meta_json"])
    assert meta["status"] == "needs_confirmation"
    assert meta["cache_miss_count"] == 2
    assert meta["cache_miss_total"] == 3
    assert meta["cache_miss_est_cost_usd"] == 0.114
    assert meta["cache_miss_est_seconds"] == 300


def test_render_queue_derives_title_customer_from_library_path(seed_case, tmp_path, monkeypatch):
    from backend import db, render_queue

    case_dir = tmp_path / "incoming" / "无创案例库" / "无创注射案例库" / "林惠贞"
    case_dir.mkdir(parents=True)
    case_id = seed_case(
        abs_path=str(case_dir),
        customer_raw="无创注射案例库",
        category="standard_face",
        template_tier="tri",
    )
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
                (case_id, brand, template, status, enqueued_at, semantic_judge, render_mode, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'queued', ?, 'off', 'ai', ?)
            """,
            (
                case_id,
                now,
                json.dumps({"options": {"enhance_direction": "heal", "allow_burn": True}}),
            ),
        ).lastrowid

    captured: dict = {}

    def fake_run_ai_enhanced_render(case_dir_arg, **kwargs):
        captured.update(kwargs)
        return {
            "output_path": None,
            "manifest_path": None,
            "status": "done",
            "case_mode": "ai_enhanced_board",
            "enhance": {"direction": "heal", "model": "gemini-3-pro-image"},
            "blocking_issue_count": 0,
            "warning_count": 0,
            "effective_templates": {},
            "manual_overrides_applied": [],
        }

    monkeypatch.setattr(render_queue.render_executor, "run_ai_enhanced_render", fake_run_ai_enhanced_render)
    render_queue.RenderQueue()._execute_render(job_id)

    assert captured["customer_name"] == "林惠贞"


def test_render_queue_ai_timeout_persists_provider_evidence(seed_case, tmp_path, monkeypatch):
    from backend import db, render_queue

    case_dir = tmp_path / "case-ai-timeout"
    case_dir.mkdir()
    (case_dir / "术前1.jpg").write_bytes(b"before")
    (case_dir / "术后1.jpg").write_bytes(b"after")
    case_id = seed_case(abs_path=str(case_dir))
    now = datetime.now(timezone.utc).isoformat()
    options = {"enhance_direction": "heal", "no_cache": True, "allow_burn": True}
    with db.connect() as conn:
        conn.execute(
            "UPDATE cases SET meta_json = ? WHERE id = ?",
            (json.dumps({"image_files": ["术前1.jpg", "术后1.jpg"]}, ensure_ascii=False), case_id),
        )
        job_id = conn.execute(
            """
            INSERT INTO render_jobs
                (case_id, brand, template, status, enqueued_at, semantic_judge, render_mode, meta_json)
            VALUES (?, 'fumei', 'tri-compare', 'queued', ?, 'off', 'ai', ?)
            """,
            (case_id, now, json.dumps({"options": options}, ensure_ascii=False)),
        ).lastrowid

    command = {
        "args": [
            "python3",
            "backend/scripts/render_ai_enhanced_boards.py",
            "--provider-order",
            "rsta,tuzi,flashapi,77code",
            "--no-cache",
            "--allow-cache-miss-burn",
        ],
        "provider_order_raw": "rsta,tuzi,flashapi,77code",
        "provider_order": ["rsta", "tuzi", "flashapi", "77code"],
        "no_cache": True,
        "allow_burn": True,
        "timeout_sec": 900,
    }

    def fake_run_ai_enhanced_render(case_dir_arg, **kwargs):
        exc = subprocess.TimeoutExpired(
            cmd=command["args"],
            timeout=900,
            output="partial stdout from ai child",
            stderr="partial stderr from ai child",
        )
        exc.ai_enhance_command = command
        exc.ai_enhance_stdout_tail = "partial stdout from ai child"
        exc.ai_enhance_stderr_tail = "partial stderr from ai child"
        exc.ai_enhance_progress = {
            "event_count": 2,
            "last_event": {"event": "provider_attempt_start", "slot": "front", "provider": "rsta", "attempt": 1},
            "recent_events": [
                {"event": "slot_external_call_start", "slot": "front"},
                {"event": "provider_attempt_start", "slot": "front", "provider": "rsta", "attempt": 1},
            ],
        }
        raise exc

    monkeypatch.setattr(
        render_queue.render_executor, "run_ai_enhanced_render", fake_run_ai_enhanced_render
    )
    worker = render_queue.RenderQueue()
    worker._execute_render(job_id)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, error_message, meta_json FROM render_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "timeout after 900s"
    meta = json.loads(row["meta_json"])
    evidence = meta["ai_usage"]["enhancement_evidence"]
    assert meta["options"]["no_cache"] is True
    assert meta["ai_usage"]["cache_disabled"] is True
    assert meta["ai_usage"]["external_call_count"] == 0
    assert meta["ai_usage"]["cache_hit_count"] == 0
    assert evidence["provider_order"] == ["rsta", "tuzi", "flashapi", "77code"]
    assert evidence["ai_enhance_command"]["provider_order_raw"] == "rsta,tuzi,flashapi,77code"
    assert evidence["ai_enhance_command"]["timeout_sec"] == 900
    assert evidence["stdout_tail"] == "partial stdout from ai child"
    assert evidence["stderr_tail"] == "partial stderr from ai child"
    assert evidence["progress"]["last_event"]["event"] == "provider_attempt_start"
    assert evidence["progress"]["last_event"]["provider"] == "rsta"


def test_get_job_404(client, no_job_pool):
    resp = client.get("/api/render/jobs/9999")
    assert resp.status_code == 404
    assert "job not found" in resp.json()["detail"]


def test_get_batch_404(client, no_job_pool):
    resp = client.get("/api/render/batches/batch-doesnotexist")
    assert resp.status_code == 404
    assert "batch not found" in resp.json()["detail"]


def test_cancel_queued_job_then_cancel_again_409(client, seed_case, no_job_pool):
    case_id = seed_case()
    job_id = client.post(
        f"/api/cases/{case_id}/render", json={"brand": "fumei", "force": True}
    ).json()["job_id"]

    first = client.post(f"/api/render/jobs/{job_id}/cancel")
    assert first.status_code == 200
    assert first.json() == {"cancelled": True, "job_id": job_id}

    again = client.post(f"/api/render/jobs/{job_id}/cancel")
    assert again.status_code == 409
    assert "not cancellable" in again.json()["detail"]

    detail = client.get(f"/api/render/jobs/{job_id}").json()
    assert detail["status"] == "cancelled"


def test_cancel_unknown_job_returns_409(client, no_job_pool):
    """Route maps `cancel(...) -> False` (job not found OR not queued) to 409.

    The current behaviour treats both as the same error; this test pins it.
    """
    resp = client.post("/api/render/jobs/9999/cancel")
    assert resp.status_code == 409


def test_undo_render_404_when_no_revision_to_undo(client, seed_case, no_job_pool):
    case_id = seed_case()
    resp = client.post(f"/api/cases/{case_id}/render/undo")
    assert resp.status_code == 404
    assert "nothing to undo" in resp.json()["detail"]
