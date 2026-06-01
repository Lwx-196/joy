"""Tests for backend.services.effect_delivery_qa (anchored-simulation Phase 3.2).

A parallel of the D6 board_delivery_qa gate, but for AI *effect-projection*
deliverables (mask-anchored after images / triptychs). It judges each candidate
against its baseline via the effect_projection judge profile (4 criteria:
effect_direction / identity_preserved / only_treated_regions / natural_not_overdone)
and locks the gate mechanics with a stub VLM provider — no real ADC / network.

Mandated paths:

    candidate     -> pass  (effect projection deliverable)
    baseline      -> fail  (held: wrong-direction / identity drift / mask leak)
    tie           -> fail  (held: no effect applied — an honest negative, not a win)
    manual_review -> fail  (held: ambiguous / safety-relevant)
    VLM down      -> unavailable (held, fail-closed, never cached, never shipped)
    malformed     -> unavailable (held, fail-closed, never cached)

plus content-hash caching (one judge call per baseline+candidate+spec version),
cache-serves-during-downtime, re-render → fresh assessment, spec change → fresh
assessment, the human-review override, and the screen() pass/held split.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from backend.services.effect_delivery_qa import (
    REVIEW_CLEARED,
    REVIEW_REJECTED,
    EffectDeliverable,
    EffectDeliveryQA,
    HeldEffect,
)
from backend.services.vlm_provider import VLMRequestError


# ---------------------------------------------------------------------------
# Stub effect judge (call_vision-compatible)
# ---------------------------------------------------------------------------


class StubEffectJudge:
    """Records call count; returns a canned effect-projection judgment, fails
    (fail-closed path), or returns a malformed reply (no winner_role)."""

    def __init__(
        self,
        winner_role: str = "candidate",
        *,
        confidence: float = 0.9,
        hard_veto_reason: str | None = None,
        rationale: str = "",
        risk_flags: list[str] | None = None,
        down: bool = False,
        malformed: bool = False,
    ) -> None:
        self.winner_role = winner_role
        self.confidence = confidence
        self.hard_veto_reason = hard_veto_reason
        self.rationale = rationale
        self.risk_flags = risk_flags if risk_flags is not None else []
        self.down = down
        self.malformed = malformed
        self.calls = 0
        self.last_images: list[Path] = []

    def call_vision(self, prompt, images, *, timeout=30.0, purpose=None, max_dimension=None):
        self.calls += 1
        self.last_images = list(images)
        if self.down:
            raise VLMRequestError("stub effect judge down")
        if self.malformed:
            parsed = {"foo": "bar"}  # no winner_role → unparseable
        else:
            parsed = {
                "ab_unit_id": "demo",
                "winner_role": self.winner_role,
                "confidence": self.confidence,
                "criterion_scores": {
                    "effect_direction": {"baseline": 1, "candidate": 4},
                },
                "visual_evidence_summary": "泪沟填平可见",
                "rationale": self.rationale,
                "risk_flags": self.risk_flags,
                "hard_veto_reason": self.hard_veto_reason,
            }
        return SimpleNamespace(
            parsed=parsed,
            text=json.dumps(parsed, ensure_ascii=False),
            provider="stub",
            model="stub-effect-judge",
            latency_ms=12,
            response_id="stub-1",
        )


def _img(tmp_path: Path, name: str, payload: bytes) -> Path:
    fp = tmp_path / name
    fp.write_bytes(payload)
    return fp


def _pair(tmp_path: Path, tag: str) -> tuple[Path, Path]:
    baseline = _img(tmp_path, f"{tag}-pre.jpg", b"\xff\xd8" + tag.encode() + b"-pre")
    candidate = _img(tmp_path, f"{tag}-after.jpg", b"\xff\xd8" + tag.encode() + b"-after")
    return baseline, candidate


def _mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _qa(provider, conn=None) -> EffectDeliveryQA:
    return EffectDeliveryQA(provider, conn if conn is not None else _mem())


# ---------------------------------------------------------------------------
# assess() — the mandated winner_role → verdict mapping
# ---------------------------------------------------------------------------


def test_candidate_passes(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "ok")
    qa = _qa(StubEffectJudge("candidate"))
    v = qa.assess(baseline=baseline, candidate=candidate, ab_unit_id="ok")
    assert v.winner_role == "candidate"
    assert v.verdict == "pass"
    assert v.deliverable is True
    assert v.held is False


def test_baseline_is_held(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "drift")
    qa = _qa(StubEffectJudge("baseline", hard_veto_reason="identity drifted / 磨皮换脸"))
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.winner_role == "baseline"
    assert v.verdict == "fail"
    assert v.held is True
    assert v.deliverable is False
    assert "identity drifted" in v.reason


def test_tie_is_held_no_effect(tmp_path: Path) -> None:
    # tie = no effect applied at all → an honest negative result, NOT a win.
    baseline, candidate = _pair(tmp_path, "noop")
    qa = _qa(StubEffectJudge("tie"))
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.winner_role == "tie"
    assert v.verdict == "fail"
    assert v.held is True


def test_manual_review_is_held(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "ambig")
    qa = _qa(StubEffectJudge("manual_review"))
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.winner_role == "manual_review"
    assert v.verdict == "fail"
    assert v.held is True


def test_vlm_down_unavailable_held_uncached(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "down")
    conn = _mem()
    qa = EffectDeliveryQA(StubEffectJudge("candidate", down=True), conn)
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.verdict == "unavailable"
    assert v.held is True  # fail-closed
    assert v.deliverable is False
    # never cached
    assert conn.execute("SELECT COUNT(*) FROM effect_delivery_qa").fetchone()[0] == 0


def test_malformed_reply_unavailable_uncached(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "garbage")
    conn = _mem()
    qa = EffectDeliveryQA(StubEffectJudge(malformed=True), conn)
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.verdict == "unavailable"
    assert v.held is True
    assert conn.execute("SELECT COUNT(*) FROM effect_delivery_qa").fetchone()[0] == 0


def test_unreadable_image_unavailable(tmp_path: Path) -> None:
    candidate = _img(tmp_path, "present.jpg", b"\xff\xd8present")
    missing = tmp_path / "does-not-exist.jpg"
    provider = StubEffectJudge("candidate")
    qa = _qa(provider)
    v = qa.assess(baseline=missing, candidate=candidate)
    assert v.verdict == "unavailable"
    assert v.held is True
    assert provider.calls == 0  # never even reached the judge


# ---------------------------------------------------------------------------
# Caching — one judge call per baseline+candidate+spec version
# ---------------------------------------------------------------------------


def test_cache_one_call_per_effect(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "cache")
    provider = StubEffectJudge("candidate")
    qa = _qa(provider)
    first = qa.assess(baseline=baseline, candidate=candidate)
    second = qa.assess(baseline=baseline, candidate=candidate)
    assert provider.calls == 1  # second served from cache
    assert first.cached is False
    assert second.cached is True
    assert second.verdict == "pass"


def test_cache_serves_during_downtime(tmp_path: Path) -> None:
    conn = _mem()
    warm_b, warm_c = _pair(tmp_path, "warm")
    provider = StubEffectJudge("candidate")
    qa = EffectDeliveryQA(provider, conn)
    qa.assess(baseline=warm_b, candidate=warm_c)  # caches pass

    provider.down = True  # judge goes down
    again = qa.assess(baseline=warm_b, candidate=warm_c)
    assert again.cached is True
    assert again.deliverable is True  # cache carries it through downtime

    fresh_b, fresh_c = _pair(tmp_path, "fresh")
    fresh = qa.assess(baseline=fresh_b, candidate=fresh_c)
    assert fresh.verdict == "unavailable"
    assert fresh.held is True  # uncached + down → fail-closed


def test_rerender_new_candidate_hash_reassessed(tmp_path: Path) -> None:
    baseline = _img(tmp_path, "re-pre.jpg", b"\xff\xd8re-pre")
    candidate = tmp_path / "re-after.jpg"
    candidate.write_bytes(b"\xff\xd8v1-after")
    provider = StubEffectJudge("baseline")
    qa = _qa(provider)
    first = qa.assess(baseline=baseline, candidate=candidate)
    assert first.verdict == "fail"

    candidate.write_bytes(b"\xff\xd8v2-fixed-after")  # re-rendered candidate
    provider.winner_role = "candidate"
    second = qa.assess(baseline=baseline, candidate=candidate)
    assert provider.calls == 2  # new candidate bytes → new hash → fresh judge call
    assert second.verdict == "pass"
    assert second.cached is False


def test_spec_change_new_hash_reassessed(tmp_path: Path) -> None:
    # Same images, different evidence spec → must re-judge (the prompt differs).
    baseline, candidate = _pair(tmp_path, "spec")
    provider = StubEffectJudge("candidate")
    qa = _qa(provider)
    qa.assess(baseline=baseline, candidate=candidate, effect_pairs=[["玻尿酸", "泪沟"]])
    qa.assess(baseline=baseline, candidate=candidate, effect_pairs=[["玻尿酸", "唇"]])
    assert provider.calls == 2  # spec change → new hash → fresh judge call


def test_baseline_and_candidate_passed_to_judge_in_order(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "order")
    provider = StubEffectJudge("candidate")
    qa = _qa(provider)
    qa.assess(baseline=baseline, candidate=candidate)
    # Image A = baseline, Image B = candidate (the effect_projection prompt order).
    assert provider.last_images == [baseline, candidate]


# ---------------------------------------------------------------------------
# Human-review override
# ---------------------------------------------------------------------------


def test_clear_effect_overrides_fail_to_deliverable(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "ov")
    conn = _mem()
    qa = EffectDeliveryQA(StubEffectJudge("baseline"), conn)
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.held is True

    qa.clear_effect(v.content_hash, reviewed_by="doctor", note="医生确认效果合理")
    after = qa.assess(baseline=baseline, candidate=candidate)
    assert after.review_status == REVIEW_CLEARED
    assert after.deliverable is True  # human override wins


def test_reject_effect_keeps_pass_held(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "rej")
    conn = _mem()
    qa = EffectDeliveryQA(StubEffectJudge("candidate"), conn)
    v = qa.assess(baseline=baseline, candidate=candidate)
    assert v.deliverable is True
    qa.clear_effect(v.content_hash, status=REVIEW_REJECTED, note="过度填充")
    after = qa.assess(baseline=baseline, candidate=candidate)
    assert after.review_status == REVIEW_REJECTED
    assert after.deliverable is False  # rejected stays held even though winner=candidate


def test_pending_reviews_lists_then_drops_after_clear(tmp_path: Path) -> None:
    baseline, candidate = _pair(tmp_path, "pend")
    conn = _mem()
    qa = EffectDeliveryQA(
        StubEffectJudge("baseline", hard_veto_reason="泪沟没填到位"), conn
    )
    v = qa.assess(baseline=baseline, candidate=candidate)
    pending = qa.pending_reviews()
    assert [p.content_hash for p in pending] == [v.content_hash]
    assert pending[0].winner_role == "baseline"

    qa.clear_effect(v.content_hash)
    assert qa.pending_reviews() == []


# ---------------------------------------------------------------------------
# screen_effect_deliverables — pass/held split
# ---------------------------------------------------------------------------


def test_screen_splits_pass_and_held(tmp_path: Path) -> None:
    good_b, good_c = _pair(tmp_path, "good")
    bad_b, bad_c = _pair(tmp_path, "bad")
    conn = _mem()
    # one provider can't return two different verdicts; assess each separately
    # by seeding the cache first, then screening reads from cache (calls=0).
    EffectDeliveryQA(StubEffectJudge("candidate"), conn).assess(
        baseline=good_b, candidate=good_c, case_id=45, ab_unit_id="good"
    )
    EffectDeliveryQA(StubEffectJudge("baseline"), conn).assess(
        baseline=bad_b, candidate=bad_c, case_id=87, ab_unit_id="bad"
    )

    screening_provider = StubEffectJudge("candidate")
    qa = EffectDeliveryQA(screening_provider, conn)
    result = qa.screen_effect_deliverables(
        [
            EffectDeliverable(
                baseline_path=good_b, candidate_path=good_c, case_id=45,
                ab_unit_id="good", customer="康巧佳", case_name="泪沟",
            ),
            EffectDeliverable(
                baseline_path=bad_b, candidate_path=bad_c, case_id=87,
                ab_unit_id="bad", customer="许楚楚", case_name="唇",
            ),
        ]
    )
    assert screening_provider.calls == 0  # both served from cache
    assert [p.ab_unit_id for p in result.passed] == ["good"]
    assert len(result.held) == 1
    held = result.held[0]
    assert isinstance(held, HeldEffect)
    assert held.case_id == 87
    assert held.ab_unit_id == "bad"
    assert held.verdict == "fail"
    assert held.winner_role == "baseline"
    assert held.customer == "许楚楚"


def test_screen_unavailable_all_held_failclosed(tmp_path: Path) -> None:
    b1, c1 = _pair(tmp_path, "u1")
    b2, c2 = _pair(tmp_path, "u2")
    conn = _mem()
    qa = EffectDeliveryQA(StubEffectJudge("candidate", down=True), conn)
    result = qa.screen_effect_deliverables(
        [
            EffectDeliverable(baseline_path=b1, candidate_path=c1, ab_unit_id="u1"),
            EffectDeliverable(baseline_path=b2, candidate_path=c2, ab_unit_id="u2"),
        ]
    )
    assert result.passed == []
    assert len(result.held) == 2
    assert all(h.verdict == "unavailable" for h in result.held)
