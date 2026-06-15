"""Tests for backend.services.board_delivery_qa + its DeliveryGate wiring (D6).

Calibration adopted the v1 prompt (recall 14/14, held-out validated); these
tests lock the *gate mechanics* around it with a stub VLM provider — no real
ADC / network. The four mandated paths:

    blocker  -> held   (excluded from auto-ship)
    clean    -> pass
    warning  -> pass   (warnings are still deliverable)
    VLM down -> held   (fail-closed, never silently shipped, never cached)

plus content-hash caching (one VLM call per board version), cache-serves-during-
downtime, re-render → fresh assessment, and the human-review override.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from backend import db
from backend.services.board_delivery_qa import (
    REVIEW_CLEARED,
    REVIEW_REJECTED,
    BoardDeliveryQA,
)
from backend.services.delivery_gate import DeliveryGate, HeldBoard
from backend.services.vlm_provider import VLMRequestError


# ---------------------------------------------------------------------------
# Stub VLM provider
# ---------------------------------------------------------------------------


class StubProvider:
    """Records call count; returns a canned verdict or fails (fail-closed path)."""

    def __init__(
        self,
        verdict: str = "clean",
        *,
        primary: str = "",
        families: list[str] | None = None,
        down: bool = False,
    ) -> None:
        self.verdict = verdict
        self.primary = primary
        self.families = families if families is not None else []
        self.down = down
        self.calls = 0

    def call_vision(self, prompt, images, *, timeout=30.0, purpose=None, max_dimension=None):
        self.calls += 1
        self.last_prompt = prompt
        if self.down:
            raise VLMRequestError("stub provider down")
        parsed = {
            "verdict": self.verdict,
            "confidence": 0.95,
            "primary_defect": self.primary,
            "families": self.families,
        }
        return SimpleNamespace(
            parsed=parsed,
            text=json.dumps(parsed, ensure_ascii=False),
            provider="stub",
            model="stub-flash",
            latency_ms=10,
        )


def _board(tmp_path: Path, name: str, payload: bytes) -> Path:
    fp = tmp_path / name
    fp.write_bytes(payload)
    return fp


def _mem() -> sqlite3.Connection:
    """In-memory SQLite with the Row factory production uses (db.connect)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# assess() — the four mandated paths
# ---------------------------------------------------------------------------


def test_assess_blocker_is_held(tmp_path: Path) -> None:
    board = _board(tmp_path, "b.jpg", b"\xff\xd8blocker-bytes")
    qa = BoardDeliveryQA(
        StubProvider("blocker", primary="术后抠图崩", families=["cutout_artifact"]),
        _mem(),
    )
    v = qa.assess(board, case_id=88, job_id=179)
    assert v.verdict == "blocker"
    assert v.held is True
    assert v.deliverable is False
    assert v.primary_defect == "术后抠图崩"
    assert v.families == ("cutout_artifact",)  # families carried for review queue


def test_assess_clean_passes(tmp_path: Path) -> None:
    board = _board(tmp_path, "c.jpg", b"\xff\xd8clean-bytes")
    qa = BoardDeliveryQA(StubProvider("clean"), _mem())
    v = qa.assess(board)
    assert v.verdict == "clean"
    assert v.deliverable is True
    assert v.held is False


def test_assess_warning_passes(tmp_path: Path) -> None:
    # Point #4: warnings are still deliverable.
    board = _board(tmp_path, "w.jpg", b"\xff\xd8warning-bytes")
    qa = BoardDeliveryQA(
        StubProvider("warning", primary="边角轻微背景", families=["bg_letterbox"]),
        _mem(),
    )
    v = qa.assess(board)
    assert v.verdict == "warning"
    assert v.deliverable is True


def test_assess_vlm_unavailable_is_held_and_uncached(tmp_path: Path) -> None:
    board = _board(tmp_path, "u.jpg", b"\xff\xd8down-bytes")
    conn = _mem()
    provider = StubProvider("clean", down=True)
    qa = BoardDeliveryQA(provider, conn)
    v = qa.assess(board)
    assert v.verdict == "unavailable"
    assert v.held is True  # fail-closed
    assert v.deliverable is False
    # never cached: nothing written for this hash
    cached_rows = conn.execute("SELECT COUNT(*) FROM board_delivery_qa").fetchone()[0]
    assert cached_rows == 0


def test_assess_unparseable_reply_is_held_and_uncached(tmp_path: Path) -> None:
    board = _board(tmp_path, "g.jpg", b"\xff\xd8garbage-bytes")
    conn = _mem()
    qa = BoardDeliveryQA(StubProvider("not-a-verdict"), conn)
    v = qa.assess(board)
    assert v.verdict == "unavailable"
    assert v.held is True
    assert conn.execute("SELECT COUNT(*) FROM board_delivery_qa").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Caching — one VLM call per board version
# ---------------------------------------------------------------------------


def test_cache_one_call_per_board(tmp_path: Path) -> None:
    board = _board(tmp_path, "cache.jpg", b"\xff\xd8cache-bytes")
    provider = StubProvider("clean")
    qa = BoardDeliveryQA(provider, _mem())
    first = qa.assess(board)
    second = qa.assess(board)
    assert provider.calls == 1  # second served from cache
    assert first.cached is False
    assert second.cached is True
    assert second.verdict == "clean"


def test_cache_serves_during_downtime(tmp_path: Path) -> None:
    """Cached boards still pass while the VLM is down; only *uncached* new
    boards fail-closed (point #2 bonus)."""
    conn = _mem()
    warm = _board(tmp_path, "warm.jpg", b"\xff\xd8warm-bytes")
    provider = StubProvider("clean")
    qa = BoardDeliveryQA(provider, conn)
    qa.assess(warm)  # caches clean

    provider.down = True  # VLM goes down
    warm_again = qa.assess(warm)
    assert warm_again.cached is True
    assert warm_again.deliverable is True  # cache carries it through downtime

    fresh = _board(tmp_path, "fresh.jpg", b"\xff\xd8fresh-bytes")
    fresh_v = qa.assess(fresh)
    assert fresh_v.verdict == "unavailable"
    assert fresh_v.held is True  # uncached + down → fail-closed


def test_rerender_new_hash_reassessed(tmp_path: Path) -> None:
    board = tmp_path / "rerender.jpg"
    board.write_bytes(b"\xff\xd8v1-bytes")
    provider = StubProvider("blocker")
    qa = BoardDeliveryQA(provider, _mem())
    first = qa.assess(board)
    assert first.verdict == "blocker"

    board.write_bytes(b"\xff\xd8v2-fixed-bytes")  # re-rendered: new content
    provider.verdict = "clean"
    second = qa.assess(board)
    assert provider.calls == 2  # new hash → fresh VLM call
    assert second.verdict == "clean"
    assert second.cached is False


# ---------------------------------------------------------------------------
# Human-review override
# ---------------------------------------------------------------------------


def test_clear_board_overrides_blocker_to_deliverable(tmp_path: Path) -> None:
    board = _board(tmp_path, "ov.jpg", b"\xff\xd8override-bytes")
    conn = _mem()
    qa = BoardDeliveryQA(StubProvider("blocker"), conn)
    v = qa.assess(board)
    assert v.held is True

    qa.clear_board(v.content_hash, reviewed_by="op", note="false alarm, ships")
    after = qa.assess(board)
    assert after.review_status == REVIEW_CLEARED
    assert after.deliverable is True  # human override wins


def test_reject_board_keeps_clean_held(tmp_path: Path) -> None:
    board = _board(tmp_path, "rej.jpg", b"\xff\xd8reject-bytes")
    conn = _mem()
    qa = BoardDeliveryQA(StubProvider("clean"), conn)
    qa.assess(board)
    v_hash = BoardDeliveryQA.content_hash(board)
    qa.clear_board(v_hash, status=REVIEW_REJECTED, note="actually bad")
    after = qa.assess(board)
    assert after.review_status == REVIEW_REJECTED
    assert after.deliverable is False  # rejected stays held even though verdict=clean


def test_pending_reviews_lists_then_drops_after_clear(tmp_path: Path) -> None:
    board = _board(tmp_path, "pend.jpg", b"\xff\xd8pending-bytes")
    conn = _mem()
    qa = BoardDeliveryQA(StubProvider("blocker", primary="背景未清"), conn)
    v = qa.assess(board)
    pending = qa.pending_reviews()
    assert [p.content_hash for p in pending] == [v.content_hash]
    assert pending[0].primary_defect == "背景未清"

    qa.clear_board(v.content_hash)
    assert qa.pending_reviews() == []


# ---------------------------------------------------------------------------
# DeliveryGate wiring — fail-closed exclusion + held queue
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_render(conn: sqlite3.Connection, *, case_id: int, output_path: str,
                 quality_score: float) -> int:
    now = _now()
    job_id = conn.execute(
        """INSERT INTO render_jobs
           (case_id, brand, template, status, enqueued_at, output_path)
           VALUES (?, 'brand-a', 'tri-compare', 'done', ?, ?)""",
        (case_id, now, output_path),
    ).lastrowid
    conn.execute(
        """INSERT INTO render_quality
           (render_job_id, quality_status, quality_score, can_publish,
            artifact_mode, blocking_count, warning_count, metrics_json,
            created_at, updated_at)
           VALUES (?, 'done', ?, 1, 'real_layout', 0, 0, '{}', ?, ?)""",
        (job_id, quality_score, now, now),
    )
    conn.commit()
    return job_id


def test_gate_screen_excludes_blocker_surfaces_held(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-a/blocked")
    with db.connect() as conn:
        board = _board(tmp_path, "gate-blocker.jpg", b"\xff\xd8gate-blocker")
        _seed_render(conn, case_id=case_id, output_path=str(board), quality_score=100.0)
        qa = BoardDeliveryQA(
            StubProvider("blocker", primary="术后抠图崩", families=["cutout_artifact"]),
            conn,
        )
        gate = DeliveryGate(conn, board_qa=qa)

        result = gate.screen_deliverables()
        assert result.passed == []  # blocker excluded from auto-ship
        assert len(result.held) == 1
        held = result.held[0]
        assert isinstance(held, HeldBoard)
        assert held.case_id == case_id
        assert held.verdict == "blocker"
        assert held.primary_defect == "术后抠图崩"
        assert held.families == ("cutout_artifact",)
        # list_deliverables (the legacy surface) now returns only the passed set
        assert gate.list_deliverables() == []


def test_d6_verdict_syncs_into_render_quality_metrics_without_publish_change(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-a/sync")
    with db.connect() as conn:
        board = _board(tmp_path, "gate-sync.jpg", b"\xff\xd8gate-sync")
        job_id = _seed_render(conn, case_id=case_id, output_path=str(board), quality_score=100.0)
        qa = BoardDeliveryQA(
            StubProvider("blocker", primary="术后抠图崩", families=["cutout_artifact"]),
            conn,
        )

        verdict = qa.assess(board, case_id=case_id, job_id=job_id)
        row = conn.execute(
            "SELECT can_publish, metrics_json FROM render_quality WHERE render_job_id = ?",
            (job_id,),
        ).fetchone()
        metrics = json.loads(row["metrics_json"])

    assert verdict.held is True
    assert row["can_publish"] == 1
    assert metrics["delivery_verdict"] == "blocker"
    assert metrics["delivery_held"] is True
    assert metrics["d6_qa"]["source"] == "board_delivery_qa"
    assert metrics["d6_qa"]["job_id"] == job_id
    assert metrics["d6_qa"]["case_id"] == case_id
    assert metrics["d6_qa"]["families"] == ["cutout_artifact"]


def test_gate_screen_passes_clean_and_warning(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-b/ok")
    with db.connect() as conn:
        board = _board(tmp_path, "gate-clean.jpg", b"\xff\xd8gate-clean")
        _seed_render(conn, case_id=case_id, output_path=str(board), quality_score=95.0)
        gate = DeliveryGate(conn, board_qa=BoardDeliveryQA(StubProvider("clean"), conn))
        result = gate.screen_deliverables()
        assert len(result.passed) == 1
        assert result.passed[0].case_id == case_id
        assert result.held == []


def test_gate_without_qa_is_backward_compatible(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-c/legacy")
    with db.connect() as conn:
        board = _board(tmp_path, "gate-legacy.jpg", b"\xff\xd8gate-legacy")
        _seed_render(conn, case_id=case_id, output_path=str(board), quality_score=99.0)
        gate = DeliveryGate(conn)  # no board_qa
        items = gate.list_deliverables()
    assert len(items) == 1
    assert items[0].case_id == case_id


def test_gate_unavailable_board_is_held_failclosed(
    temp_db: Path, seed_case, tmp_path: Path
) -> None:
    case_id = seed_case(abs_path="/cases/customer-d/down")
    with db.connect() as conn:
        board = _board(tmp_path, "gate-down.jpg", b"\xff\xd8gate-down")
        _seed_render(conn, case_id=case_id, output_path=str(board), quality_score=100.0)
        qa = BoardDeliveryQA(StubProvider("clean", down=True), conn)
        gate = DeliveryGate(conn, board_qa=qa)
        result = gate.screen_deliverables()
    assert result.passed == []
    assert len(result.held) == 1
    assert result.held[0].verdict == "unavailable"


# ---------------------------------------------------------------------------
# export_delivery_batch script wiring
# ---------------------------------------------------------------------------


def test_export_script_held_report_and_qa_default(monkeypatch, tmp_path: Path) -> None:
    """The export script defaults QA on, holds blockers out of the shipped set,
    and writes the held-review queue; --no-qa default flips with the env var."""
    from backend.scripts import export_delivery_batch as eb

    # QA-on default + env escape hatch
    monkeypatch.delenv("CASE_WORKBENCH_DELIVERY_QA", raising=False)
    assert eb._qa_enabled_default() is True
    monkeypatch.setenv("CASE_WORKBENCH_DELIVERY_QA", "0")
    assert eb._qa_enabled_default() is False

    # held report shape
    held = HeldBoard(
        case_id=88,
        customer="小绿",
        case_name="board-88",
        job_id=179,
        source_path="/x/board-88.jpg",
        content_hash="deadbeef",
        verdict="blocker",
        primary_defect="术后抠图崩",
        families=("cutout_artifact",),
        confidence=0.95,
        reason="VLM=blocker: 术后抠图崩",
    )
    out = tmp_path / "delivery"
    path = eb._write_held_report(out, [held], dry_run=False)
    assert path is not None and path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["held_count"] == 1
    assert payload["boards"][0]["primary_defect"] == "术后抠图崩"
    assert payload["boards"][0]["families"] == ["cutout_artifact"]
    # dry-run writes nothing
    assert eb._write_held_report(out, [held], dry_run=True) is None


# ---------------------------------------------------------------------------
# G3 近景行 prompt 扩展（v1 frozen 不动；带近景板走 v1+closeup 独立 cache）
# ---------------------------------------------------------------------------


def test_default_assess_prompt_is_frozen_v1(tmp_path: Path) -> None:
    """缺省路径回归锚：prompt 与 frozen v1 字节相等，cache 行版本 = v1。"""
    from backend.services.board_delivery_qa import PROMPT, PROMPT_VERSION

    conn = _mem()
    stub = StubProvider("clean")
    qa = BoardDeliveryQA(stub, conn)
    board = _board(tmp_path, "frozen.jpg", b"\xff\xd8frozen-v1")
    qa.assess(board)
    assert stub.last_prompt == PROMPT
    row = conn.execute("SELECT prompt_version FROM board_delivery_qa").fetchone()
    assert row["prompt_version"] == PROMPT_VERSION


def test_closeup_assess_appends_note_and_version(tmp_path: Path) -> None:
    """has_closeup_section=True → prompt = v1 + CLOSEUP_NOTE，版本带 +closeup 后缀。"""
    from backend.services.board_delivery_qa import (
        CLOSEUP_NOTE,
        CLOSEUP_VERSION_SUFFIX,
        PROMPT,
        PROMPT_VERSION,
    )

    conn = _mem()
    stub = StubProvider("clean")
    qa = BoardDeliveryQA(stub, conn)
    board = _board(tmp_path, "closeup.jpg", b"\xff\xd8with-closeup-row")
    qa.assess(board, has_closeup_section=True)
    assert stub.last_prompt == PROMPT + CLOSEUP_NOTE
    assert stub.last_prompt.startswith(PROMPT)  # v1 前缀原样
    row = conn.execute("SELECT prompt_version FROM board_delivery_qa").fetchone()
    assert row["prompt_version"] == PROMPT_VERSION + CLOSEUP_VERSION_SUFFIX


def test_closeup_and_default_cache_isolated(tmp_path: Path) -> None:
    """同一板字节在两个 prompt 版本下各评各存：互不命中、各自缓存复用。"""
    from backend.services.board_delivery_qa import PROMPT_VERSION

    conn = _mem()
    stub = StubProvider("clean")
    qa = BoardDeliveryQA(stub, conn)
    board = _board(tmp_path, "iso.jpg", b"\xff\xd8same-bytes")

    first = qa.assess(board)
    assert stub.calls == 1 and first.cached is False
    # 同板换 closeup 路径 → 不命中 v1 cache，真打一次
    second = qa.assess(board, has_closeup_section=True)
    assert stub.calls == 2 and second.cached is False
    # 各自重复 → 各命中各的 cache，调用数不再增长
    assert qa.assess(board).cached is True
    assert qa.assess(board, has_closeup_section=True).cached is True
    assert stub.calls == 2
    rows = conn.execute(
        "SELECT prompt_version FROM board_delivery_qa ORDER BY prompt_version"
    ).fetchall()
    # 用常量派生，prompt 版本 bump 时不脆断（v2: 2026-06-15 mask-leak 误报校准）
    assert [r["prompt_version"] for r in rows] == [
        PROMPT_VERSION,
        f"{PROMPT_VERSION}+closeup",
    ]


# ---------------------------------------------------------------------------
# Deterministic head-cut FP guard (2026-06-15)
# ---------------------------------------------------------------------------
# Refutes the judge's stubborn, prompt-resistant "头顶切平/裁断" false positive
# when the rendered board provably has black headroom above every person cell
# (胡志超 2026-06-15: real-data headroom 27%, judge still 0.99 blocker).


def _synthetic_board(tmp_path: Path, name: str, headroom_frac: float) -> Path:
    """A cream board with one photo band of two black cells; a bright 'person'
    blob in each cell whose top sits headroom_frac down from the cell top."""
    import numpy as np
    from PIL import Image

    h, w = 600, 900
    arr = np.full((h, w, 3), (244, 238, 231), dtype=np.uint8)  # cream board bg
    y0, y1 = 100, 550  # photo band (cell_h = 450 > 200)
    cells = [(100, 400), (500, 800)]  # two cells, gap 400..500 (>40)
    blob_top = y0 + int(round(headroom_frac * (y1 - y0)))
    for (x0, x1) in cells:
        arr[y0:y1, x0:x1] = (0, 0, 0)  # rembg-black photo cell
        cx0 = x0 + (x1 - x0) // 3
        cx1 = x1 - (x1 - x0) // 3
        arr[blob_top:y1, cx0:cx1] = (220, 200, 190)  # bright 'person' silhouette
    fp = tmp_path / name
    Image.fromarray(arr).save(fp)
    return fp


def test_measure_headroom_matches_constructed_geometry(tmp_path: Path) -> None:
    from backend.services.board_delivery_qa import _measure_person_headroom

    board = _synthetic_board(tmp_path, "hr.png", headroom_frac=0.25)
    geo = _measure_person_headroom(board)
    assert geo is not None
    assert geo["n_cells"] == 2
    assert abs(geo["min_headroom"] - 0.25) < 0.02  # ~25% black above each blob


def test_headcut_guard_downgrades_fp_when_headroom_present(tmp_path: Path) -> None:
    """blocker + head-cut defect text + provable headroom → blocker downgraded."""
    from backend.services.board_delivery_qa import HEADCUT_GUARD_FAMILY

    board = _synthetic_board(tmp_path, "fp.png", headroom_frac=0.25)
    stub = StubProvider("blocker", primary="45°行术后头顶被水平裁断切平", families=["cutout_artifact"])
    qa = BoardDeliveryQA(stub, _mem())
    v = qa.assess(board)
    assert v.verdict == "warning"
    assert v.deliverable is True and v.held is False
    assert HEADCUT_GUARD_FAMILY in v.families
    assert "几何守卫" in v.primary_defect


def test_headcut_guard_skips_non_headcut_defect(tmp_path: Path) -> None:
    """A real halo/bg blocker (not head-cut wording) is NOT touched even with headroom."""
    board = _synthetic_board(tmp_path, "halo.png", headroom_frac=0.25)
    stub = StubProvider("blocker", primary="人物头发边缘硬白边 halo 描边", families=["cutout_artifact"])
    qa = BoardDeliveryQA(stub, _mem())
    v = qa.assess(board)
    assert v.verdict == "blocker" and v.held is True


def test_headcut_guard_respects_real_cut(tmp_path: Path) -> None:
    """blocker + head-cut text but NO headroom (blob touches cell top) → stays blocker."""
    board = _synthetic_board(tmp_path, "cut.png", headroom_frac=0.0)
    stub = StubProvider("blocker", primary="头顶被切平裁断缺失", families=["cutout_artifact"])
    qa = BoardDeliveryQA(stub, _mem())
    v = qa.assess(board)
    assert v.verdict == "blocker" and v.held is True


def test_headcut_guard_fail_open_on_unreadable_board(tmp_path: Path) -> None:
    """Unmeasurable board (tiny fake JPG) → guard fails open, judge blocker stands."""
    board = _board(tmp_path, "tiny.jpg", b"\xff\xd8not-an-image")
    stub = StubProvider("blocker", primary="头顶切平", families=["cutout_artifact"])
    qa = BoardDeliveryQA(stub, _mem())
    v = qa.assess(board)
    assert v.verdict == "blocker" and v.held is True
