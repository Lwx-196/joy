"""Tests for backend/scripts/focal_p4_gate_report.py (P4 gate aggregation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.scripts import focal_p4_gate_report as gr


# --- parse_winner ----------------------------------------------------------

def test_parse_winner_explicit_role():
    assert gr.parse_winner({"winner_role": "candidate"}) == "candidate"
    assert gr.parse_winner({"winner_role": "Baseline"}) == "baseline"
    assert gr.parse_winner({"winner_role": "tie"}) == "tie"


def test_parse_winner_score_fallback():
    j = {"criterion_scores": {"a": {"baseline": 4, "candidate": 3},
                              "b": {"baseline": 4, "candidate": 4}}}
    assert gr.parse_winner(j) == "baseline"  # 8 vs 7
    j2 = {"criterion_scores": {"a": {"baseline": 3, "candidate": 3}}}
    assert gr.parse_winner(j2) == "tie"
    j3 = {"criterion_scores": {"a": {"baseline": 2, "candidate": 5}}}
    assert gr.parse_winner(j3) == "candidate"


def test_parse_winner_empty_is_tie():
    assert gr.parse_winner({}) == "tie"


# --- aggregate -------------------------------------------------------------

def test_aggregate_mixed():
    results = {
        "judgments": [
            {"ab_unit_id": "c1", "winner_role": "candidate"},
            {"ab_unit_id": "c2", "winner_role": "candidate"},
            {"ab_unit_id": "c3", "winner_role": "baseline"},
        ],
        "manual_review_judgments": [
            {"ab_unit_id": "c4", "criterion_scores": {"x": {"baseline": 3, "candidate": 3}}},
        ],
    }
    a = gr.aggregate(results)
    assert a["total"] == 4
    assert a["candidate_wins"] == 2
    assert a["baseline_wins"] == 1
    assert a["ties"] == 1
    assert a["win_rate"] == 0.5            # 2/4 (ties count against)
    assert a["decisive_win_rate"] == 2 / 3  # 2/(2+1)


def test_aggregate_decisive_overrides_manual_for_same_id():
    # If an id appears in both, the decisive judgment wins (setdefault).
    results = {
        "judgments": [{"ab_unit_id": "c1", "winner_role": "candidate"}],
        "manual_review_judgments": [{"ab_unit_id": "c1", "winner_role": "baseline"}],
    }
    a = gr.aggregate(results)
    assert a["total"] == 1 and a["candidate_wins"] == 1


# --- board_diff ------------------------------------------------------------

def _png(path: Path, color, size=(40, 40)):
    from PIL import Image
    Image.new("RGB", size, color).save(path)


def test_board_diff_identical_and_different(tmp_path):
    # board_diff 像素数学需 numpy（按设计不在 backend venv）→ CI 跳过，本地 dev 跑。
    # numpy 缺失时 board_diff 优雅返回 {"error":...}（见 test_board_diff_missing_file）。
    pytest.importorskip("numpy")
    from PIL import Image
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _png(a, (100, 100, 100))
    _png(b, (100, 100, 100))
    d = gr.board_diff(str(a), str(b))
    assert d["mean_delta"] == 0.0 and d["pct_pixels_gt5"] == 0.0
    # half the pixels strongly changed
    img = Image.new("RGB", (40, 40), (100, 100, 100))
    for y in range(20):
        for x in range(40):
            img.putpixel((x, y), (250, 250, 250))
    img.save(b)
    d2 = gr.board_diff(str(a), str(b))
    assert d2["mean_delta"] > 0 and d2["pct_pixels_gt5"] > 40


def test_board_diff_missing_file(tmp_path):
    d = gr.board_diff(str(tmp_path / "nope.png"), str(tmp_path / "nope2.png"))
    assert "error" in d


# --- build_report ----------------------------------------------------------

def test_build_report_gate_pass_fail(tmp_path):
    a = tmp_path / "base.png"
    c = tmp_path / "cand.png"
    _png(a, (10, 10, 10))
    _png(c, (10, 10, 10))
    packet = {"judge_items": [
        {"ab_unit_id": "c1", "baseline": {"source_path": str(a)}, "candidate": {"source_path": str(c)}},
        {"ab_unit_id": "c2", "baseline": {"source_path": str(a)}, "candidate": {"source_path": str(c)}},
    ]}
    # 1/2 candidate wins → 50% < 60% → FAIL
    results = {"real_vlm_judge": True, "model": "gemini-3.5-flash",
               "judgments": [{"ab_unit_id": "c1", "winner_role": "candidate"},
                             {"ab_unit_id": "c2", "winner_role": "baseline"}]}
    rep = gr.build_report(packet, results)
    assert rep["gate_pass"] is False
    assert rep["summary"]["win_rate"] == 0.5
    assert all("board_diff" in row for row in rep["cases"])

    # 2/2 candidate → 100% ≥ 60% → PASS
    results2 = {"judgments": [{"ab_unit_id": "c1", "winner_role": "candidate"},
                              {"ab_unit_id": "c2", "winner_role": "candidate"}]}
    assert gr.build_report(packet, results2, with_diff=False)["gate_pass"] is True
