"""出框实时 gate 在 render selection plan 中的集成行为（剔除 / fail-open / env 开关）."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.tests.test_vlm_source_classifier import _seed_observation, _write_tiny_png


def _seed_case(conn, case_id: int, tmp_path: Path, filenames: list[str]) -> dict:
    """建 case + 每张图一条高置信 VLM 观测（front 视角，文件名定 phase）。"""
    now = "2026-06-10T00:00:00+00:00"
    scan_id = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (now, "[]", "unit"),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO cases (
          id, scan_id, abs_path, category, meta_json, last_modified, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            scan_id,
            str(tmp_path),
            "standard_face",
            json.dumps({"image_files": filenames}, ensure_ascii=False),
            now,
            now,
        ),
    )
    for name in filenames:
        _write_tiny_png(tmp_path / name)
        _seed_observation(
            conn,
            case_id=case_id,
            root_path=str(tmp_path),
            image_path=name,
            phase="before" if "before" in name else "after",
            view="front",
            confidence=0.95,
            source="vlm_classifier",
        )
    return dict(conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone())


def _build_context(temp_db, tmp_path, monkeypatch, fake_evaluate, case_id: int):
    from backend import db, render_queue

    monkeypatch.setattr(render_queue.face_frame_gate, "evaluate_face_frame", fake_evaluate)
    with db.connect() as conn:
        row = _seed_case(conn, case_id, tmp_path, ["before.png", "after.png"])
        return render_queue._build_render_selection_context(conn, [row])


def test_truncated_candidate_excluded(temp_db: Path, tmp_path: Path, monkeypatch) -> None:
    """超阈值候选被剔除：不进 slot，override 标 face_frame_truncated。"""

    def fake_evaluate(path):
        if Path(path).name == "after.png":
            return {
                "status": "evaluated",
                "truncation": 0.352,
                "edge_overflow": {"left": 0.0, "top": 0.352, "right": 0.0, "bottom": 0.0},
                "exceeded": True,
            }
        return {"status": "evaluated", "truncation": 0.0, "edge_overflow": {}, "exceeded": False}

    context = _build_context(temp_db, tmp_path, monkeypatch, fake_evaluate, case_id=201)

    front = context["plan"]["slots"].get("front") or {}
    assert (front.get("before") or {}).get("filename") == "before.png"
    assert front.get("after") is None
    override = context["overrides_by_render_name"]["after.png"]
    assert override["render_excluded"] is True
    assert override["exclusion_reason"] == "face_frame_truncated"
    assert override["face_frame_truncation"] == 0.352
    # 被剔槽位计入缺失，供 case 报缺
    assert {"view": "front", "missing": ["after"]} in context["plan"]["missing_slots"]


def test_fail_open_keeps_candidates(temp_db: Path, tmp_path: Path, monkeypatch) -> None:
    """gate 不可用（no_face/unavailable）→ 不剔除，链路照常。"""

    def fake_evaluate(path):
        return {"status": "unavailable", "truncation": None, "edge_overflow": None, "exceeded": False}

    context = _build_context(temp_db, tmp_path, monkeypatch, fake_evaluate, case_id=202)

    front = context["plan"]["slots"]["front"]
    assert front["before"]["filename"] == "before.png"
    assert front["after"]["filename"] == "after.png"
    assert not any(
        override.get("exclusion_reason") == "face_frame_truncated"
        for override in context["overrides_by_render_name"].values()
    )


def test_env_switch_bypasses_gate(temp_db: Path, tmp_path: Path, monkeypatch) -> None:
    """SKIP_FACE_FRAME_GATE=1 → gate 完全不调用（照 SKIP_CLASSIFICATION_GATE 先例）。"""
    monkeypatch.setenv("SKIP_FACE_FRAME_GATE", "1")

    def fake_evaluate(path):  # pragma: no cover - 不应被调用
        pytest.fail("SKIP_FACE_FRAME_GATE 开启时不应调用 evaluate_face_frame")

    context = _build_context(temp_db, tmp_path, monkeypatch, fake_evaluate, case_id=203)

    front = context["plan"]["slots"]["front"]
    assert front["before"]["filename"] == "before.png"
    assert front["after"]["filename"] == "after.png"
