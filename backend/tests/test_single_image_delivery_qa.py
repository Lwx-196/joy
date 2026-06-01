from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

from backend.services import single_image_delivery_qa as siqa
from backend.services.single_image_delivery_qa import (
    REVIEW_CLEARED,
    SingleImageDeliveryQA,
)


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _images(tmp_path: Path, *, enhanced_name: str = "enhanced.png") -> tuple[Path, Path, Path]:
    raw = tmp_path / "raw.png"
    enhanced = tmp_path / enhanced_name
    mask = tmp_path / "mask.png"
    Image.new("RGB", (120, 90), (90, 90, 90)).save(raw)
    img = Image.new("RGB", (120, 90), (90, 90, 90))
    draw = ImageDraw.Draw(img)
    draw.rectangle((45, 25, 75, 35), fill=(120, 120, 120))
    img.save(enhanced)
    mask_img = Image.new("L", (120, 90), 0)
    ImageDraw.Draw(mask_img).ellipse((35, 15, 85, 45), fill=255)
    mask_img.save(mask)
    return raw, enhanced, mask


def _patch_prescreen(monkeypatch, *, passed: bool, reasons: list[str] | None = None) -> None:
    monkeypatch.setattr(siqa, "compute_fidelity_probes", lambda raw, enhanced, mask: {"ok": True})
    monkeypatch.setattr(
        siqa,
        "prescreen_verdict",
        lambda probes: {"passed": passed, "reasons": reasons or []},
    )


class StubJudge:
    def __init__(
        self,
        winner_role: str = "candidate",
        *,
        hard_veto_reason: str = "",
        unavailable: bool = False,
    ) -> None:
        self.winner_role = winner_role
        self.hard_veto_reason = hard_veto_reason
        self.unavailable = unavailable
        self.calls = 0

    def __call__(self, packet, **kwargs):
        self.calls += 1
        if self.unavailable:
            raise RuntimeError("stub judge down")
        judgment = {
            "ab_unit_id": packet["judge_items"][0]["ab_unit_id"],
            "winner_role": self.winner_role,
            "confidence": 0.91,
            "hard_veto_reason": self.hard_veto_reason,
            "judge_provider": "stub",
            "judge_model": "stub-model",
        }
        key = "manual_review_judgments" if self.winner_role in {"tie", "manual_review"} else "judgments"
        return (
            {"provider": "stub", "model": "stub-model", "judgments": [] if key != "judgments" else [judgment], key: [judgment]},
            {"provider": "stub", "model": "stub-model", "run_status": "completed_real_vlm_judge"},
        )


def test_prescreen_fail_short_circuits_vlm_and_caches(monkeypatch, tmp_path: Path) -> None:
    _patch_prescreen(monkeypatch, passed=False, reasons=["background changed"])
    raw, enhanced, mask = _images(tmp_path)
    conn = _mem()
    judge = StubJudge()
    qa = SingleImageDeliveryQA(conn, judge_runner=judge)

    first = qa.assess(raw, enhanced, mask, case_id=7, customer="客户")
    second = qa.assess(raw, enhanced, mask, case_id=7, customer="客户")

    assert first.verdict == "fail_baseline"
    assert first.held is True
    assert second.cached is True
    assert judge.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM single_image_delivery_qa").fetchone()[0] == 1


def test_candidate_win_passes_and_dual_hash_cache(monkeypatch, tmp_path: Path) -> None:
    _patch_prescreen(monkeypatch, passed=True)
    raw, enhanced, mask = _images(tmp_path)
    conn = _mem()
    judge = StubJudge("candidate")
    qa = SingleImageDeliveryQA(conn, judge_runner=judge)

    first = qa.assess(raw, enhanced, mask, case_id=8, customer="客户")
    second = qa.assess(raw, enhanced, mask, case_id=8, customer="客户")
    _, enhanced2, _ = _images(tmp_path, enhanced_name="enhanced_v2.png")
    with enhanced2.open("ab") as handle:
        handle.write(b"v2")
    third = qa.assess(raw, enhanced2, mask, case_id=8, customer="客户")

    assert first.verdict == "pass"
    assert first.deliverable is True
    assert second.cached is True
    assert third.verdict == "pass"
    assert judge.calls == 2


def test_baseline_tie_and_hard_veto_are_held(monkeypatch, tmp_path: Path) -> None:
    _patch_prescreen(monkeypatch, passed=True)
    raw, enhanced, mask = _images(tmp_path)

    baseline = SingleImageDeliveryQA(_mem(), judge_runner=StubJudge("baseline")).assess(raw, enhanced, mask)
    tie = SingleImageDeliveryQA(_mem(), judge_runner=StubJudge("tie")).assess(raw, enhanced, mask)
    veto = SingleImageDeliveryQA(
        _mem(),
        judge_runner=StubJudge("candidate", hard_veto_reason="identity drift"),
    ).assess(raw, enhanced, mask)

    assert baseline.verdict == "fail_baseline"
    assert baseline.held is True
    assert tie.verdict == "manual_review"
    assert tie.held is True
    assert veto.verdict == "fail_veto"
    assert veto.held is True


def test_unavailable_is_fail_closed_and_uncached(monkeypatch, tmp_path: Path) -> None:
    _patch_prescreen(monkeypatch, passed=True)
    raw, enhanced, mask = _images(tmp_path)
    conn = _mem()
    qa = SingleImageDeliveryQA(conn, judge_runner=StubJudge(unavailable=True))

    verdict = qa.assess(raw, enhanced, mask)

    assert verdict.verdict == "unavailable"
    assert verdict.held is True
    assert conn.execute("SELECT COUNT(*) FROM single_image_delivery_qa").fetchone()[0] == 0


def test_clear_image_override_allows_held_pair(monkeypatch, tmp_path: Path) -> None:
    _patch_prescreen(monkeypatch, passed=True)
    raw, enhanced, mask = _images(tmp_path)
    conn = _mem()
    qa = SingleImageDeliveryQA(conn, judge_runner=StubJudge("baseline"))

    held = qa.assess(raw, enhanced, mask)
    qa.clear_image(held.content_hash, status=REVIEW_CLEARED, note="operator accepted")
    cleared = qa.assess(raw, enhanced, mask)

    assert held.held is True
    assert cleared.review_status == REVIEW_CLEARED
    assert cleared.deliverable is True
