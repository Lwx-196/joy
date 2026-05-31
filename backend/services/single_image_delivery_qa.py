"""Fail-closed QA for standalone single-image closeup delivery."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.ai_generation_adapter import _focal_crop_bbox
from backend.scripts.comfyui_vlm_judge_runner import run_vlm_judge
from backend.scripts.single_image_packet_builder import (
    ARM_CRITERIA,
    JUDGE_PROFILE,
    _prepare_judge_image,
)
from backend.services.fidelity_probes import compute_fidelity_probes, prescreen_verdict

PROMPT_VERSION = f"{JUDGE_PROFILE}:v1"

REVIEW_PENDING = "pending"
REVIEW_CLEARED = "cleared"
REVIEW_REJECTED = "rejected"

_DEFAULT_TIMEOUT = 120.0

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS single_image_delivery_qa (
    content_hash      TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    case_id           INTEGER,
    customer          TEXT NOT NULL DEFAULT '',
    raw_path          TEXT NOT NULL DEFAULT '',
    enhanced_path     TEXT NOT NULL DEFAULT '',
    verdict           TEXT NOT NULL,
    winner_role       TEXT NOT NULL DEFAULT '',
    hard_veto_reason  TEXT NOT NULL DEFAULT '',
    confidence        REAL,
    prescreen_passed  INTEGER,
    prescreen_reasons TEXT NOT NULL DEFAULT '[]',
    provider          TEXT NOT NULL DEFAULT '',
    model             TEXT NOT NULL DEFAULT '',
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    assessed_at       TEXT NOT NULL,
    review_status     TEXT NOT NULL DEFAULT 'pending',
    reviewed_by       TEXT,
    reviewed_at       TEXT,
    review_note       TEXT,
    PRIMARY KEY (content_hash, prompt_version)
)
"""

JudgeRunner = Callable[..., tuple[dict[str, Any], dict[str, Any]]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SingleImageQAVerdict:
    """Result of screening one raw/enhanced closeup pair."""

    content_hash: str
    verdict: str  # pass | fail_baseline | fail_veto | manual_review | unavailable
    winner_role: str = ""
    hard_veto_reason: str = ""
    confidence: float | None = None
    prescreen_passed: bool | None = None
    prescreen_reasons: tuple[str, ...] = field(default_factory=tuple)
    cached: bool = False
    review_status: str = REVIEW_PENDING
    review_note: str | None = None
    error: str | None = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0

    @property
    def deliverable(self) -> bool:
        if self.review_status == REVIEW_CLEARED:
            return True
        if self.review_status == REVIEW_REJECTED:
            return False
        return self.verdict == "pass"

    @property
    def held(self) -> bool:
        return not self.deliverable

    @property
    def reason(self) -> str:
        if self.review_status == REVIEW_REJECTED:
            return f"human-rejected: {self.review_note or self.verdict}"
        if self.verdict == "unavailable":
            return f"VLM unavailable (fail-closed): {self.error or 'no assessment'}"
        if self.verdict == "fail_veto":
            return f"hard veto: {self.hard_veto_reason}"
        if self.verdict == "fail_baseline" and self.prescreen_reasons:
            return "; ".join(self.prescreen_reasons)
        return f"winner_role={self.winner_role or self.verdict}"


class SingleImageDeliveryQA:
    """Single-image fidelity screen with dual-hash caching and manual override."""

    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        *,
        env: dict[str, str] | None = None,
        prompt_version: str = PROMPT_VERSION,
        timeout: float = _DEFAULT_TIMEOUT,
        judge_runner: JudgeRunner = run_vlm_judge,
    ) -> None:
        self._conn = conn
        self._env = dict(os.environ)
        if env is not None:
            self._env.update(env)
        self._prompt_version = prompt_version
        self._timeout = float(timeout)
        self._judge_runner = judge_runner
        if conn is not None:
            conn.execute(_CREATE_TABLE)
            conn.commit()

    @staticmethod
    def content_hash(raw_path: str | Path, enhanced_path: str | Path) -> str:
        return f"{_sha256_file(raw_path)}:{_sha256_file(enhanced_path)}"

    def assess(
        self,
        raw_path: str | Path,
        enhanced_path: str | Path,
        mask_path: str | Path,
        *,
        case_id: int | None = None,
        customer: str = "",
        focus_targets: list[str] | tuple[str, ...] | None = None,
    ) -> SingleImageQAVerdict:
        raw = Path(raw_path)
        enhanced = Path(enhanced_path)
        mask = Path(mask_path)
        try:
            content_hash = self.content_hash(raw, enhanced)
        except OSError as exc:
            return SingleImageQAVerdict(
                content_hash="",
                verdict="unavailable",
                error=f"image unreadable: {exc}",
            )

        cached = self._cache_get(content_hash)
        if cached is not None:
            return cached

        try:
            probes = compute_fidelity_probes(raw, enhanced, mask)
            prescreen = prescreen_verdict(probes)
        except Exception as exc:  # noqa: BLE001 - bad probe inputs hold the image
            result = SingleImageQAVerdict(
                content_hash=content_hash,
                verdict="fail_baseline",
                winner_role="baseline",
                prescreen_passed=False,
                prescreen_reasons=(f"probe error: {type(exc).__name__}: {str(exc)[:160]}",),
            )
            self._cache_put(result, case_id=case_id, customer=customer, raw_path=raw, enhanced_path=enhanced)
            return result

        prescreen_reasons = _coerce_str_tuple(prescreen.get("reasons"))
        if prescreen.get("passed") is not True:
            result = SingleImageQAVerdict(
                content_hash=content_hash,
                verdict="fail_baseline",
                winner_role="baseline",
                prescreen_passed=False,
                prescreen_reasons=prescreen_reasons,
            )
            self._cache_put(result, case_id=case_id, customer=customer, raw_path=raw, enhanced_path=enhanced)
            return result

        try:
            judgment, provider, model = self._run_judge(
                raw,
                enhanced,
                mask,
                content_hash=content_hash,
                case_id=case_id,
                focus_targets=tuple(str(t) for t in focus_targets or ()),
            )
        except Exception as exc:  # noqa: BLE001 - VLM failures are held and uncached
            return SingleImageQAVerdict(
                content_hash=content_hash,
                verdict="unavailable",
                prescreen_passed=True,
                prescreen_reasons=prescreen_reasons,
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )

        if judgment is None:
            return SingleImageQAVerdict(
                content_hash=content_hash,
                verdict="unavailable",
                prescreen_passed=True,
                prescreen_reasons=prescreen_reasons,
                error="no successful VLM judgment",
                provider=provider,
                model=model,
            )
        if _is_provider_error_judgment(judgment):
            return SingleImageQAVerdict(
                content_hash=content_hash,
                verdict="unavailable",
                winner_role=str(judgment.get("winner_role") or ""),
                hard_veto_reason=str(judgment.get("hard_veto_reason") or ""),
                prescreen_passed=True,
                prescreen_reasons=prescreen_reasons,
                error=str(judgment.get("fail_closed_reason") or "provider error")[:200],
                provider=provider or str(judgment.get("judge_provider") or ""),
                model=model or str(judgment.get("judge_model") or ""),
            )

        winner_role = str(judgment.get("winner_role") or "").strip().lower()
        hard_veto_reason = str(judgment.get("hard_veto_reason") or "").strip()
        if hard_veto_reason:
            verdict = "fail_veto"
        elif winner_role == "candidate":
            verdict = "pass"
        elif winner_role == "baseline":
            verdict = "fail_baseline"
        elif winner_role in {"tie", "manual_review"}:
            verdict = "manual_review"
        else:
            return SingleImageQAVerdict(
                content_hash=content_hash,
                verdict="unavailable",
                prescreen_passed=True,
                prescreen_reasons=prescreen_reasons,
                error=f"invalid winner_role: {winner_role!r}",
                provider=provider,
                model=model,
            )

        result = SingleImageQAVerdict(
            content_hash=content_hash,
            verdict=verdict,
            winner_role=winner_role,
            hard_veto_reason=hard_veto_reason,
            confidence=_coerce_float(judgment.get("confidence")),
            prescreen_passed=True,
            prescreen_reasons=prescreen_reasons,
            provider=provider or str(judgment.get("judge_provider") or ""),
            model=model or str(judgment.get("judge_model") or ""),
        )
        self._cache_put(result, case_id=case_id, customer=customer, raw_path=raw, enhanced_path=enhanced)
        return result

    def clear_image(
        self,
        content_hash: str,
        *,
        reviewed_by: str = "",
        note: str = "",
        status: str = REVIEW_CLEARED,
    ) -> None:
        if self._conn is None:
            raise RuntimeError("clear_image requires a DB connection")
        if status not in {REVIEW_CLEARED, REVIEW_REJECTED, REVIEW_PENDING}:
            raise ValueError(f"invalid review status: {status!r}")
        now = _now()
        self._conn.execute(
            """INSERT INTO single_image_delivery_qa
                 (content_hash, prompt_version, verdict, assessed_at,
                  review_status, reviewed_by, reviewed_at, review_note)
               VALUES (?, ?, 'manual_override', ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, prompt_version) DO UPDATE SET
                 review_status = excluded.review_status,
                 reviewed_by   = excluded.reviewed_by,
                 reviewed_at   = excluded.reviewed_at,
                 review_note   = excluded.review_note""",
            (content_hash, self._prompt_version, now, status, reviewed_by, now, note),
        )
        self._conn.commit()

    def _run_judge(
        self,
        raw: Path,
        enhanced: Path,
        mask: Path,
        *,
        content_hash: str,
        case_id: int | None,
        focus_targets: tuple[str, ...],
    ) -> tuple[dict[str, Any] | None, str, str]:
        judge_dir = enhanced.parent / "_delivery_qa_judge"
        judge_dir.mkdir(parents=True, exist_ok=True)
        crop_bbox = _focal_crop_bbox(mask)
        judge_baseline = _prepare_judge_image(
            raw, judge_dir / "judge_baseline.jpg", crop_bbox=crop_bbox
        )
        judge_candidate = _prepare_judge_image(
            enhanced, judge_dir / "judge_candidate.jpg", crop_bbox=crop_bbox
        )
        item = {
            "ab_unit_id": f"case_{case_id or 'unknown'}__{content_hash[:12]}",
            "case_id": case_id,
            "focus_targets": list(focus_targets),
            "judge_profile": JUDGE_PROFILE,
            "judge_view": "focal",
            "criteria": ARM_CRITERIA,
            "view": "single_image_after_focal",
            "workflow": "classical",
            "baseline": {"source_path": str(judge_baseline), "role_note": "raw after photo"},
            "candidate": {
                "source_path": str(judge_candidate),
                "role_note": "clarity-enhanced after photo",
            },
        }
        packet = {
            "scope": "single_image_delivery_qa_v1",
            "judge_profile": JUDGE_PROFILE,
            "judge_item_count": 1,
            "judge_items": [item],
        }
        results, report = self._judge_runner(
            packet,
            packet_root=judge_dir,
            env=self._env,
            max_items=1,
            timeout_seconds=self._timeout,
            concurrency=1,
        )
        provider = str(results.get("provider") or report.get("provider") or "")
        model = str(results.get("model") or report.get("model") or "")
        judgments = [j for j in results.get("judgments") or [] if isinstance(j, dict)]
        manual = [j for j in results.get("manual_review_judgments") or [] if isinstance(j, dict)]
        if judgments:
            return judgments[0], provider, model
        if manual:
            return manual[0], provider, model
        if report.get("run_status"):
            raise RuntimeError(str(report.get("decision") or report.get("run_status")))
        return None, provider, model

    def _cache_get(self, content_hash: str) -> SingleImageQAVerdict | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM single_image_delivery_qa WHERE content_hash = ? AND prompt_version = ?",
            (content_hash, self._prompt_version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_verdict(row)

    def _cache_put(
        self,
        verdict: SingleImageQAVerdict,
        *,
        case_id: int | None,
        customer: str,
        raw_path: Path,
        enhanced_path: Path,
    ) -> None:
        if self._conn is None:
            return
        self._conn.execute(
            """INSERT INTO single_image_delivery_qa
                 (content_hash, prompt_version, case_id, customer, raw_path, enhanced_path,
                  verdict, winner_role, hard_veto_reason, confidence, prescreen_passed,
                  prescreen_reasons, provider, model, latency_ms, assessed_at, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, prompt_version) DO NOTHING""",
            (
                verdict.content_hash,
                self._prompt_version,
                case_id,
                customer,
                str(raw_path),
                str(enhanced_path),
                verdict.verdict,
                verdict.winner_role,
                verdict.hard_veto_reason,
                verdict.confidence,
                None if verdict.prescreen_passed is None else int(verdict.prescreen_passed),
                json.dumps(list(verdict.prescreen_reasons), ensure_ascii=False),
                verdict.provider,
                verdict.model,
                verdict.latency_ms,
                _now(),
                REVIEW_PENDING,
            ),
        )
        self._conn.commit()

    def _row_to_verdict(self, row: sqlite3.Row) -> SingleImageQAVerdict:
        return SingleImageQAVerdict(
            content_hash=row["content_hash"],
            verdict=row["verdict"],
            winner_role=row["winner_role"] or "",
            hard_veto_reason=row["hard_veto_reason"] or "",
            confidence=row["confidence"],
            prescreen_passed=_coerce_bool(row["prescreen_passed"]),
            prescreen_reasons=_coerce_str_tuple(row["prescreen_reasons"]),
            cached=True,
            review_status=row["review_status"] or REVIEW_PENDING,
            review_note=_row_value(row, "review_note"),
            provider=row["provider"] or "",
            model=row["model"] or "",
            latency_ms=int(row["latency_ms"] or 0),
        )


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_provider_error_judgment(judgment: dict[str, Any]) -> bool:
    return (
        "fail_closed_reason" in judgment
        or str(judgment.get("hard_veto_reason") or "") == "vlm_provider_error_fail_closed"
        or "vlm_provider_error" in (judgment.get("risk_flags") or [])
    )


def _row_value(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _coerce_bool(raw: Any) -> bool | None:
    if raw is None:
        return None
    return bool(raw)


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
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
