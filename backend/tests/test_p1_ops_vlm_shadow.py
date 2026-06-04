"""P1.1.C POST /api/render/ops/vlm-shadow — 收编 vlm_classify_batch.py --live-no-apply。

dry_run=true（默认）：扫描候选 image_observations 返回 candidate_count + 截断
candidates，不发任何 VLM 请求；mode='dry-run' 给 cron / UI 判断当日 shadow 工作量。
dry_run=false：暂返 501，等 owner 把 VLMProvider 真 classifier purpose 跑批
路径合 main 后再开。

target 至少一个：
  - all_low_confidence=true → 选 source='vlm_classifier' AND confidence < threshold
  - case_ids=[...] → 该 case 集合下所有 image_observations
"""
from __future__ import annotations

from datetime import datetime, timezone


_GROUP_COUNTER = [0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_low_confidence_observation(seed_case_factory, *, confidence: float,
                                      source: str = "vlm_classifier") -> tuple[int, int]:
    """Seed a (case, group, observation) tuple. Returns (case_id, observation_id)."""
    from backend import db
    _GROUP_COUNTER[0] += 1
    nonce = _GROUP_COUNTER[0]
    cid = seed_case_factory(abs_path=f"/tmp/shadow-{nonce}")
    now_iso = _now()
    with db.connect() as conn:
        gid = conn.execute(
            """INSERT INTO case_groups
               (group_key, primary_case_id, title, root_path,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"grp-{nonce}", cid, f"title-{nonce}", f"/tmp/grp-{nonce}",
             now_iso, now_iso),
        ).lastrowid
        cur = conn.execute(
            """INSERT INTO image_observations
               (group_id, case_id, image_path, source, confidence,
                phase, view, body_part, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (gid, cid, f"/tmp/img-{nonce}.jpg", source, confidence,
             "before", "front", "face", now_iso, now_iso),
        )
        return cid, int(cur.lastrowid)


def test_vlm_shadow_dry_run_lists_candidates(client, seed_case) -> None:
    # 3 low-confidence + 1 high-confidence (should be excluded by default threshold 0.85)
    for _ in range(3):
        _seed_low_confidence_observation(seed_case, confidence=0.5)
    _seed_low_confidence_observation(seed_case, confidence=0.95)

    resp = client.post(
        "/api/render/ops/vlm-shadow",
        json={
            "all_low_confidence": True,
            "max_items": 50,
            "dry_run": True,
            "reviewer": "cron@example.com",
            "reason": "nightly shadow audit",
        },
        headers={"X-Request-Id": "req-shadow-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["mode"] == "dry-run"
    assert body["candidate_count"] == 3
    assert len(body["candidates"]) == 3
    assert body["request_id"] == "req-shadow-1"
    assert body["shadow_run_id"].startswith("shadow-")

    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT request_id, endpoint, reviewer, outcome, http_status, payload_json "
            "FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["request_id"] == "req-shadow-1"
    assert row["endpoint"] == "POST /api/render/ops/vlm-shadow"
    assert row["reviewer"] == "cron@example.com"
    assert row["outcome"] == "dry_run"
    assert row["http_status"] == 200


def test_vlm_shadow_requires_target_400(client) -> None:
    resp = client.post(
        "/api/render/ops/vlm-shadow",
        json={"dry_run": True, "reviewer": "x", "reason": "y"},
    )
    assert resp.status_code == 400, resp.text
    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, http_status FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["outcome"] == "error"
    assert row["http_status"] == 400


def test_vlm_shadow_live_fire_returns_501_pending_owner(client) -> None:
    resp = client.post(
        "/api/render/ops/vlm-shadow",
        json={
            "all_low_confidence": True,
            "dry_run": False,
            "reviewer": "op@example.com",
            "reason": "fire",
        },
    )
    assert resp.status_code == 501, resp.text
    from backend import db
    with db.connect() as conn:
        row = conn.execute(
            "SELECT outcome, http_status FROM ops_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["outcome"] == "error"
    assert row["http_status"] == 501


def test_vlm_shadow_case_ids_target_filters(client, seed_case) -> None:
    cid_a, _ = _seed_low_confidence_observation(seed_case, confidence=0.5)
    cid_b, _ = _seed_low_confidence_observation(seed_case, confidence=0.5)
    cid_skip, _ = _seed_low_confidence_observation(seed_case, confidence=0.5)
    resp = client.post(
        "/api/render/ops/vlm-shadow",
        json={
            "case_ids": [cid_a, cid_b],
            "dry_run": True,
            "reviewer": "op@x.com",
            "reason": "spot",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    case_ids_in_resp = {c["case_id"] for c in body["candidates"]}
    assert cid_a in case_ids_in_resp
    assert cid_b in case_ids_in_resp
    assert cid_skip not in case_ids_in_resp


def test_vlm_shadow_reviewer_required_422(client) -> None:
    resp = client.post(
        "/api/render/ops/vlm-shadow",
        json={"all_low_confidence": True, "dry_run": True, "reason": "y"},
    )
    assert resp.status_code == 422
