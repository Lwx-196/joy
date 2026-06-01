"""Effect-projection delivery QA via the effect_projection VLM judge (Phase 3.2).

A parallel of the D6 `board_delivery_qa` gate, for AI mask-anchored
*effect-projection* deliverables (after images / triptychs). Where D6 judges one
rendered board for layout/合成/抠图/留白/标注 defects, this gate judges a
(baseline, candidate) pair through the effect_projection judge profile — the
INVERSE of the保真 fidelity judge: image B is SUPPOSED to differ from image A,
and it passes ONLY when all four criteria hold:
① effect_direction ② identity_preserved ③ only_treated_regions ④ natural_not_overdone.

Single source of truth = `comfyui_vlm_judge_runner` (reused, never modified):
* `_judge_prompt(item)` with judge_profile="effect_projection" builds the
  evidence-anchored prompt (循证库 injected via effect_pairs, 反臆造);
* `parse_vlm_provider_judgment` normalizes the VLM reply into a winner_role.

Gate mechanics mirror D6 exactly:
* winner_role → verdict:  candidate=pass · baseline/tie/manual_review=fail.
  A `tie` (no effect applied at all) is an honest negative result, NOT a win —
  so it is held, not shipped.
* Fail-closed: VLM error / timeout / out-of-vocabulary winner_role → `unavailable`
  (held, never cached, never silently shipped).
* Verdict cached by content hash of (baseline bytes ‖ candidate bytes ‖ judge spec)
  + prompt version. The same pair+spec is judged once; a re-rendered candidate or a
  changed spec → new bytes/spec → new hash → fresh assessment. Bonus: while the VLM
  is down, already-cached pairs still pass via cache; only *uncached* new pairs
  fail-closed. (D6 hashes only the board JPG; here the prompt also depends on the
  baseline image and the evidence spec, so both are folded into the hash.)
* `fail` / `unavailable` → held → human-review queue (NOT auto-reject). A human
  `clear_effect(...)`s (or rejects) a hash; cleared pairs then ship.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.scripts import comfyui_vlm_judge_runner as judge_runner
from backend.services.vlm_provider import VLMProvider, VLMRequestError

PROMPT_VERSION = "effect-v1"
JUDGE_PROFILE = "effect_projection"

# Gate-level pass conditions (effect_projection profile, 4 criteria). The judge
# is anchored to the循证库 via the injected effect_pairs; these are the
# human-readable criterion lines fed into the prompt when a caller does not pass
# its own. Frozen with the profile — bump PROMPT_VERSION on any change so stale
# cache rows are re-assessed instead of silently reused.
DEFAULT_CRITERIA: tuple[str, ...] = (
    "effect_direction：每个治疗区朝循证 do_right 方向出现可见、正确方向的效果（botox 静止中性脸可不明显）",
    "identity_preserved：明确是同一个人（脸型/骨相/五官/肤色/毛孔/痣斑全保住）",
    "only_treated_regions：只有治疗区变化；mask 外像素与原图一致，无磨皮/美白/瘦脸",
    "natural_not_overdone：自然不夸张，未命中循证红线（香肠唇/巫婆下巴/僵额头/Spock 眉）",
)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNAVAILABLE = "unavailable"
# verdicts that keep a deliverable OUT of the auto-ship set (subject to override)
HELD_VERDICTS = frozenset({VERDICT_FAIL, VERDICT_UNAVAILABLE})

# winner_role vocabulary (from the judge). `candidate` ships; the rest are held.
VALID_WINNER_ROLES = frozenset({"candidate", "baseline", "tie", "manual_review"})
_WINNER_TO_VERDICT = {
    "candidate": VERDICT_PASS,
    "baseline": VERDICT_FAIL,
    "tie": VERDICT_FAIL,  # no effect applied → honest negative, NOT a win
    "manual_review": VERDICT_FAIL,
}

REVIEW_PENDING = "pending"
REVIEW_CLEARED = "cleared"
REVIEW_REJECTED = "rejected"

_DEFAULT_TIMEOUT = 120.0
_OVERRIDE_PLACEHOLDER_VERDICT = "manual_override"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS effect_delivery_qa (
    content_hash     TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    case_id          INTEGER,
    job_id           INTEGER,
    ab_unit_id       TEXT NOT NULL DEFAULT '',
    baseline_path    TEXT,
    candidate_path   TEXT,
    verdict          TEXT NOT NULL,
    winner_role      TEXT NOT NULL DEFAULT '',
    hard_veto_reason TEXT,
    rationale        TEXT NOT NULL DEFAULT '',
    risk_flags       TEXT NOT NULL DEFAULT '[]',
    confidence       REAL,
    provider         TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL DEFAULT '',
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    assessed_at      TEXT NOT NULL,
    review_status    TEXT NOT NULL DEFAULT 'pending',
    reviewed_by      TEXT,
    reviewed_at      TEXT,
    review_note      TEXT,
    PRIMARY KEY (content_hash, prompt_version)
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EffectQAVerdict:
    """Result of judging one (baseline, candidate) effect-projection pair."""

    content_hash: str
    verdict: str  # pass | fail | unavailable
    winner_role: str = ""  # candidate | baseline | tie | manual_review
    hard_veto_reason: str | None = None
    rationale: str = ""
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    confidence: float | None = None
    cached: bool = False
    review_status: str = REVIEW_PENDING
    review_note: str | None = None
    error: str | None = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    ab_unit_id: str = ""
    assessed_at: str | None = None

    @property
    def deliverable(self) -> bool:
        """Whether this effect projection may enter the auto-ship set."""
        if self.review_status == REVIEW_CLEARED:
            return True
        if self.review_status == REVIEW_REJECTED:
            return False
        return self.verdict not in HELD_VERDICTS

    @property
    def held(self) -> bool:
        return not self.deliverable

    @property
    def reason(self) -> str:
        if self.review_status == REVIEW_REJECTED:
            detail = self.review_note or self.hard_veto_reason or self.rationale
            return f"human-rejected: {detail}"
        if self.verdict == VERDICT_UNAVAILABLE:
            return f"effect judge unavailable (fail-closed): {self.error or 'no assessment'}"
        detail = self.hard_veto_reason or self.rationale or ""
        return f"effect judge={self.winner_role or self.verdict}: {detail}"


@dataclass(frozen=True)
class EffectDeliverable:
    """Input to `screen_effect_deliverables`: one effect-projection pair to judge."""

    baseline_path: Path | str
    candidate_path: Path | str
    effect_pairs: Any = field(default_factory=tuple)
    do_not_touch: Any = field(default_factory=tuple)
    criteria: Any = field(default_factory=tuple)
    case_id: int | None = None
    job_id: int | None = None
    ab_unit_id: str = ""
    customer: str = ""
    case_name: str = ""


@dataclass(frozen=True)
class HeldEffect:
    """An effect projection held out of the auto-ship set for human review."""

    case_id: int | None
    customer: str
    case_name: str
    job_id: int | None
    ab_unit_id: str
    baseline_path: str
    candidate_path: str
    content_hash: str
    verdict: str
    winner_role: str
    hard_veto_reason: str | None
    rationale: str
    confidence: float | None
    reason: str


@dataclass(frozen=True)
class EffectScreenResult:
    """Pass/held split for a batch of effect deliverables."""

    passed: list[EffectQAVerdict] = field(default_factory=list)
    held: list[HeldEffect] = field(default_factory=list)


class EffectDeliveryQA:
    """Fail-closed effect-projection delivery QA with content-hash caching."""

    def __init__(
        self,
        provider: VLMProvider,
        conn: sqlite3.Connection | None = None,
        *,
        prompt_version: str = PROMPT_VERSION,
        timeout: float = _DEFAULT_TIMEOUT,
        purpose: str = "judge",
    ) -> None:
        self._provider = provider
        self._conn = conn
        self._prompt_version = prompt_version
        self._timeout = float(timeout)
        self._purpose = purpose
        if conn is not None:
            conn.execute(_CREATE_TABLE)
            conn.commit()

    # ------------------------------------------------------------------
    # Hashing — baseline bytes ‖ candidate bytes ‖ judge spec
    # ------------------------------------------------------------------

    @staticmethod
    def content_hash(
        baseline: str | Path,
        candidate: str | Path,
        *,
        effect_pairs: Any = (),
        do_not_touch: Any = (),
        criteria: Any = (),
    ) -> str:
        """SHA-256 over both images AND the judge spec.

        The effect_projection prompt depends on the baseline image, the candidate
        image, and the evidence spec (effect_pairs / do_not_touch / criteria). Any
        of them changing must re-judge, so all three are folded into the key.
        """
        digest = hashlib.sha256()
        _hash_file(digest, baseline)
        digest.update(b"\x00candidate\x00")
        _hash_file(digest, candidate)
        spec = json.dumps(
            {
                "profile": JUDGE_PROFILE,
                "effect_pairs": _normalize_pairs(effect_pairs),
                "do_not_touch": [str(item) for item in (do_not_touch or ())],
                "criteria": [str(item) for item in (criteria or ())],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest.update(b"\x00spec\x00")
        digest.update(spec.encode("utf-8"))
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # Assessment (cache-first, fail-closed)
    # ------------------------------------------------------------------

    def assess(
        self,
        *,
        baseline: str | Path,
        candidate: str | Path,
        effect_pairs: Any = (),
        do_not_touch: Any = (),
        criteria: Any = (),
        case_id: int | None = None,
        job_id: int | None = None,
        ab_unit_id: str = "",
    ) -> EffectQAVerdict:
        try:
            content_hash = self.content_hash(
                baseline,
                candidate,
                effect_pairs=effect_pairs,
                do_not_touch=do_not_touch,
                criteria=criteria,
            )
        except OSError as exc:
            return EffectQAVerdict(
                content_hash="",
                verdict=VERDICT_UNAVAILABLE,
                ab_unit_id=str(ab_unit_id),
                error=f"image unreadable: {exc}",
            )

        cached = self._cache_get(content_hash)
        if cached is not None:
            return cached

        item = self._judge_item(
            effect_pairs=effect_pairs,
            do_not_touch=do_not_touch,
            criteria=criteria,
            case_id=case_id,
            ab_unit_id=ab_unit_id,
        )
        prompt = judge_runner._judge_prompt(item)

        try:
            response = self._provider.call_vision(
                prompt,
                [Path(baseline), Path(candidate)],
                timeout=self._timeout,
                purpose=self._purpose,
            )
        except VLMRequestError as exc:
            return EffectQAVerdict(
                content_hash=content_hash,
                verdict=VERDICT_UNAVAILABLE,
                ab_unit_id=str(ab_unit_id),
                error=str(exc)[:200],
            )
        except Exception as exc:  # noqa: BLE001 - any judge failure is fail-closed
            return EffectQAVerdict(
                content_hash=content_hash,
                verdict=VERDICT_UNAVAILABLE,
                ab_unit_id=str(ab_unit_id),
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )

        judgment = judge_runner.parse_vlm_provider_judgment(response, item=item)
        winner_role = str(judgment.get("winner_role") or "").strip().lower()
        if winner_role not in VALID_WINNER_ROLES:
            # Out-of-vocabulary / unparseable reply → fail-closed, do NOT cache.
            snippet = (getattr(response, "text", "") or "")[:120]
            return EffectQAVerdict(
                content_hash=content_hash,
                verdict=VERDICT_UNAVAILABLE,
                ab_unit_id=str(ab_unit_id),
                error=f"unparsed winner_role: {snippet!r}",
            )

        result = EffectQAVerdict(
            content_hash=content_hash,
            verdict=_WINNER_TO_VERDICT[winner_role],
            winner_role=winner_role,
            hard_veto_reason=(str(judgment.get("hard_veto_reason") or "").strip() or None),
            rationale=str(judgment.get("rationale") or "").strip(),
            risk_flags=_coerce_str_tuple(judgment.get("risk_flags")),
            confidence=_coerce_float(judgment.get("confidence")),
            cached=False,
            review_status=REVIEW_PENDING,
            provider=str(judgment.get("judge_provider") or getattr(response, "provider", "") or ""),
            model=str(judgment.get("judge_model") or getattr(response, "model", "") or ""),
            latency_ms=int(getattr(response, "latency_ms", 0) or 0),
            ab_unit_id=str(ab_unit_id),
            assessed_at=_now(),
        )
        self._cache_put(
            result,
            case_id=case_id,
            job_id=job_id,
            baseline_path=str(baseline),
            candidate_path=str(candidate),
        )
        return result

    def _judge_item(
        self,
        *,
        effect_pairs: Any,
        do_not_touch: Any,
        criteria: Any,
        case_id: int | None,
        ab_unit_id: str,
    ) -> dict[str, Any]:
        return {
            "judge_profile": JUDGE_PROFILE,
            "effect_pairs": list(effect_pairs or []),
            "do_not_touch": list(do_not_touch or []),
            "criteria": list(criteria or DEFAULT_CRITERIA),
            "ab_unit_id": str(ab_unit_id),
            "case_id": case_id,
        }

    # ------------------------------------------------------------------
    # Batch screen — pass/held split (parallel of D6 DeliveryGate.screen)
    # ------------------------------------------------------------------

    def screen_effect_deliverables(
        self, deliverables: Iterable[EffectDeliverable]
    ) -> EffectScreenResult:
        passed: list[EffectQAVerdict] = []
        held: list[HeldEffect] = []
        for item in deliverables:
            verdict = self.assess(
                baseline=item.baseline_path,
                candidate=item.candidate_path,
                effect_pairs=item.effect_pairs,
                do_not_touch=item.do_not_touch,
                criteria=item.criteria,
                case_id=item.case_id,
                job_id=item.job_id,
                ab_unit_id=item.ab_unit_id,
            )
            if verdict.deliverable:
                passed.append(verdict)
            else:
                held.append(
                    HeldEffect(
                        case_id=item.case_id,
                        customer=item.customer,
                        case_name=item.case_name,
                        job_id=item.job_id,
                        ab_unit_id=item.ab_unit_id or verdict.ab_unit_id,
                        baseline_path=str(item.baseline_path),
                        candidate_path=str(item.candidate_path),
                        content_hash=verdict.content_hash,
                        verdict=verdict.verdict,
                        winner_role=verdict.winner_role,
                        hard_veto_reason=verdict.hard_veto_reason,
                        rationale=verdict.rationale,
                        confidence=verdict.confidence,
                        reason=verdict.reason,
                    )
                )
        return EffectScreenResult(passed=passed, held=held)

    # ------------------------------------------------------------------
    # Human review queue (held → cleared / rejected)
    # ------------------------------------------------------------------

    def pending_reviews(self) -> list[EffectQAVerdict]:
        """Cached effect pairs currently held for human review (fail + pending)."""
        if self._conn is None:
            return []
        rows = self._conn.execute(
            """SELECT * FROM effect_delivery_qa
               WHERE prompt_version = ?
                 AND review_status = ?
                 AND verdict = ?
               ORDER BY assessed_at""",
            (self._prompt_version, REVIEW_PENDING, VERDICT_FAIL),
        ).fetchall()
        return [self._row_to_verdict(row) for row in rows]

    def clear_effect(
        self,
        content_hash: str,
        *,
        reviewed_by: str = "",
        note: str = "",
        status: str = REVIEW_CLEARED,
    ) -> None:
        """Record a human decision on a held effect pair (cleared → ships;
        rejected → stays held).

        UPSERTs so an operator can also force-decide a pair that was never
        successfully judged (e.g. an `unavailable` pair) by its content hash.
        The judged verdict is preserved on conflict — only the review fields move.
        """
        if self._conn is None:
            raise RuntimeError("clear_effect requires a DB connection")
        if status not in {REVIEW_CLEARED, REVIEW_REJECTED, REVIEW_PENDING}:
            raise ValueError(f"invalid review status: {status!r}")
        now = _now()
        self._conn.execute(
            """INSERT INTO effect_delivery_qa
                 (content_hash, prompt_version, verdict, assessed_at,
                  review_status, reviewed_by, reviewed_at, review_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, prompt_version) DO UPDATE SET
                 review_status = excluded.review_status,
                 reviewed_by   = excluded.reviewed_by,
                 reviewed_at   = excluded.reviewed_at,
                 review_note   = excluded.review_note""",
            (
                content_hash,
                self._prompt_version,
                _OVERRIDE_PLACEHOLDER_VERDICT,
                now,
                status,
                reviewed_by,
                now,
                note,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Cache plumbing
    # ------------------------------------------------------------------

    def _cache_get(self, content_hash: str) -> EffectQAVerdict | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM effect_delivery_qa WHERE content_hash = ? AND prompt_version = ?",
            (content_hash, self._prompt_version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_verdict(row)

    def _cache_put(
        self,
        verdict: EffectQAVerdict,
        *,
        case_id: int | None,
        job_id: int | None,
        baseline_path: str,
        candidate_path: str,
    ) -> None:
        if self._conn is None:
            return
        assessed_at = verdict.assessed_at or _now()
        self._conn.execute(
            """INSERT INTO effect_delivery_qa
                 (content_hash, prompt_version, case_id, job_id, ab_unit_id,
                  baseline_path, candidate_path, verdict, winner_role,
                  hard_veto_reason, rationale, risk_flags, confidence, provider,
                  model, latency_ms, assessed_at, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, prompt_version) DO NOTHING""",
            (
                verdict.content_hash,
                self._prompt_version,
                case_id,
                job_id,
                verdict.ab_unit_id,
                baseline_path,
                candidate_path,
                verdict.verdict,
                verdict.winner_role,
                verdict.hard_veto_reason,
                verdict.rationale,
                json.dumps(list(verdict.risk_flags), ensure_ascii=False),
                verdict.confidence,
                verdict.provider,
                verdict.model,
                verdict.latency_ms,
                assessed_at,
                REVIEW_PENDING,
            ),
        )
        self._conn.commit()

    def _row_to_verdict(self, row: sqlite3.Row) -> EffectQAVerdict:
        return EffectQAVerdict(
            content_hash=row["content_hash"],
            verdict=row["verdict"],
            winner_role=row["winner_role"] or "",
            hard_veto_reason=_row_value(row, "hard_veto_reason"),
            rationale=row["rationale"] or "",
            risk_flags=_coerce_str_tuple(_row_value(row, "risk_flags")),
            confidence=_row_value(row, "confidence"),
            cached=True,
            review_status=row["review_status"] or REVIEW_PENDING,
            review_note=_row_value(row, "review_note"),
            provider=row["provider"] or "",
            model=row["model"] or "",
            latency_ms=int(row["latency_ms"] or 0),
            ab_unit_id=row["ab_unit_id"] or "",
            assessed_at=_row_value(row, "assessed_at"),
        )


def _hash_file(digest: "hashlib._Hash", path: str | Path) -> None:
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)


def _normalize_pairs(raw: Any) -> list[list[str]]:
    pairs: list[list[str]] = []
    for pair in raw or ():
        if isinstance(pair, (list, tuple)):
            pairs.append([str(value) for value in pair])
    return pairs


def _row_value(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _coerce_str_tuple(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return (raw,) if raw else ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item))
    return ()


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
