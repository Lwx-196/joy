"""Board-level delivery QA via single-board VLM assessment (D6).

The legacy `render_quality.quality_score` gate is structurally blind to
layout / 合成 / 抠图 / 留白 / 标注 defects: a 2026-05-31 read-only inventory of
the 39-board publishable pool found 35% (14/39) are delivery-grade blockers,
6 of them scored a perfect 100.0. This module adds a fail-closed VLM gate that
judges each *rendered output board JPG* for those defect families and holds
blockers out of the auto-ship set until a human clears them.

Design (calibrated + held-out validated 2026-05-31, see journal 第N段):
* Single VLM call per board, `PROMPT` (v1) frozen — recall 14/14 on the labeled
  set, generalized on held-out boards (caught a title=path defect + a mask tear
  on unseen boards). `v2` was rejected (recall fell to 11/14).
* `confidence` proven non-discriminative (FP all 0.9-0.95) — never threshold on it.
* Verdict cached by *board content hash* + prompt version, so `list_deliverables`
  may be called repeatedly without re-spending VLM. A re-rendered board changes
  its bytes → new hash → fresh assessment. Bonus: while the VLM is down, already
  cached boards still pass via cache; only *uncached* new boards fail-closed.
* Fail-closed: a VLM error / timeout / unparseable reply → `unavailable` verdict
  (held, never cached, never silently shipped).
* `blocker` / `unavailable` → held → human-review queue (NOT auto-reject). A human
  `clear_board(...)`s (or rejects) a hash; cleared boards then pass.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.vlm_provider import VLMProvider, VLMRequestError

PROMPT_VERSION = "v1"

# Frozen, calibrated v1 prompt (recall 14/14, held-out validated). Do NOT edit
# without re-running `vlm_calibrate.py full + heldout`; bump PROMPT_VERSION on any
# change so stale cache rows are re-assessed instead of silently reused.
PROMPT = (
    "你是医美「术前/术后」对比板的交付质检员。判断这张已渲染对比板能否交付给客户。"
    "只看 render/合成/抠图/版式/标注质量，不判医美治疗效果好坏。\n"
    "板结构：1-3 行（正面对比/45°侧对比/侧面对比），每行左=术前 右=术后两格。整板原样发客户，任一格硬伤=整板 blocker。\n"
    "硬伤（任一出现=blocker，必须撤回重做）：\n"
    "- 某格背景是没清的诊所灰/杂乱实景/补光灯架/挂布夹子/计时器入镜，而同行另一格干净（背景不一致）\n"
    "- 某格照片明显小于同行另一格、悬浮在大片灰或留白里、没填满格子\n"
    "- 术前术后脸大小或眼睛高度明显不一致（对比板核心是对齐）\n"
    "- 某格脸部模糊涂抹/抠图蒙版撕裂白斑/灰色模糊团块\n"
    "- 标题区姓名或术式渲染成文件夹名(如 derived / 陈院案例(1))、乱码、缺失，或术前术后标签贴反\n"
    "重要：深色或纯色影棚背景只要同行术前术后一致、对称 letterbox 边，就不是缺陷；只判明显硬伤。\n"
    "轻微瑕疵=warning；无明显 render/合成/抠图/标注问题=clean。\n"
    '只输出 JSON 一行，无其它文字：{"verdict":"blocker|warning|clean","confidence":0.0-1.0,'
    '"primary_defect":"最严重问题一句话，clean写无","families":["bg_letterbox|eye_align|cutout_artifact|title_text|none 适用的"]}'
)

VALID_VERDICTS = frozenset({"blocker", "warning", "clean"})
# verdicts that keep a board OUT of the auto-ship set (subject to human override)
HELD_VERDICTS = frozenset({"blocker", "unavailable"})

REVIEW_PENDING = "pending"
REVIEW_CLEARED = "cleared"
REVIEW_REJECTED = "rejected"

_DEFAULT_TIMEOUT = 90.0

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS board_delivery_qa (
    content_hash   TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    case_id        INTEGER,
    job_id         INTEGER,
    source_path    TEXT,
    verdict        TEXT NOT NULL,
    primary_defect TEXT NOT NULL DEFAULT '',
    families       TEXT NOT NULL DEFAULT '[]',
    confidence     REAL,
    provider       TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    assessed_at    TEXT NOT NULL,
    review_status  TEXT NOT NULL DEFAULT 'pending',
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    review_note    TEXT,
    PRIMARY KEY (content_hash, prompt_version)
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class BoardQAVerdict:
    """Result of assessing one rendered board JPG."""

    content_hash: str
    verdict: str  # blocker | warning | clean | unavailable
    primary_defect: str = ""
    families: tuple[str, ...] = field(default_factory=tuple)
    confidence: float | None = None
    cached: bool = False
    review_status: str = REVIEW_PENDING
    review_note: str | None = None
    error: str | None = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    assessed_at: str | None = None

    @property
    def deliverable(self) -> bool:
        """Whether this board may enter the auto-ship set."""
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
            return f"human-rejected: {self.review_note or self.primary_defect}"
        if self.verdict == "unavailable":
            return f"VLM unavailable (fail-closed): {self.error or 'no assessment'}"
        return f"VLM={self.verdict}: {self.primary_defect}"


class BoardDeliveryQA:
    """Fail-closed single-board VLM delivery QA with content-hash caching."""

    def __init__(
        self,
        provider: VLMProvider,
        conn: sqlite3.Connection | None = None,
        *,
        prompt: str = PROMPT,
        prompt_version: str = PROMPT_VERSION,
        timeout: float = _DEFAULT_TIMEOUT,
        purpose: str = "judge",
    ) -> None:
        self._provider = provider
        self._conn = conn
        self._prompt = prompt
        self._prompt_version = prompt_version
        self._timeout = float(timeout)
        self._purpose = purpose
        if conn is not None:
            conn.row_factory = sqlite3.Row
            conn.execute(_CREATE_TABLE)
            conn.commit()

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------

    @staticmethod
    def content_hash(board_path: str | Path) -> str:
        """SHA-256 of the board JPG bytes (re-render → new bytes → new hash)."""
        digest = hashlib.sha256()
        with open(board_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    # ------------------------------------------------------------------
    # Assessment (cache-first, fail-closed)
    # ------------------------------------------------------------------

    def assess(
        self,
        board_path: str | Path,
        *,
        case_id: int | None = None,
        job_id: int | None = None,
    ) -> BoardQAVerdict:
        try:
            content_hash = self.content_hash(board_path)
        except OSError as exc:
            return BoardQAVerdict(
                content_hash="",
                verdict="unavailable",
                error=f"board unreadable: {exc}",
            )

        cached = self._cache_get(content_hash)
        if cached is not None:
            self._sync_render_quality_metrics(cached, case_id=case_id, job_id=job_id, source_path=str(board_path))
            return cached

        try:
            response = self._provider.call_vision(
                self._prompt,
                [Path(board_path)],
                timeout=self._timeout,
                purpose=self._purpose,
            )
        except VLMRequestError as exc:
            return BoardQAVerdict(content_hash=content_hash, verdict="unavailable", error=str(exc)[:200])
        except Exception as exc:  # noqa: BLE001 - any provider failure is fail-closed
            return BoardQAVerdict(
                content_hash=content_hash,
                verdict="unavailable",
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
            )

        parsed = response.parsed or {}
        verdict = str(parsed.get("verdict") or "").strip().lower()
        if verdict not in VALID_VERDICTS:
            # Unparseable / out-of-vocabulary reply → fail-closed, do NOT cache.
            snippet = (getattr(response, "text", "") or "")[:120]
            return BoardQAVerdict(
                content_hash=content_hash,
                verdict="unavailable",
                error=f"unparsed verdict: {snippet!r}",
            )

        families = _coerce_families(parsed.get("families"))
        confidence = _coerce_float(parsed.get("confidence"))
        assessed_at = _now()
        result = BoardQAVerdict(
            content_hash=content_hash,
            verdict=verdict,
            primary_defect=str(parsed.get("primary_defect") or "").strip(),
            families=families,
            confidence=confidence,
            cached=False,
            review_status=REVIEW_PENDING,
            provider=getattr(response, "provider", "") or "",
            model=getattr(response, "model", "") or "",
            latency_ms=int(getattr(response, "latency_ms", 0) or 0),
            assessed_at=assessed_at,
        )
        self._cache_put(result, case_id=case_id, job_id=job_id, source_path=str(board_path))
        return result

    # ------------------------------------------------------------------
    # Human review queue (held → cleared / rejected)
    # ------------------------------------------------------------------

    def pending_reviews(self) -> list[BoardQAVerdict]:
        """Cached boards currently held for human review (blocker + pending)."""
        if self._conn is None:
            return []
        rows = self._conn.execute(
            """SELECT * FROM board_delivery_qa
               WHERE prompt_version = ?
                 AND review_status = ?
                 AND verdict IN ('blocker')
               ORDER BY assessed_at""",
            (self._prompt_version, REVIEW_PENDING),
        ).fetchall()
        return [self._row_to_verdict(row) for row in rows]

    def clear_board(
        self,
        content_hash: str,
        *,
        reviewed_by: str = "",
        note: str = "",
        status: str = REVIEW_CLEARED,
    ) -> None:
        """Record a human decision on a held board (cleared → ships; rejected → stays held).

        UPSERTs so an operator can also force-decide a board that was never
        VLM-assessed (e.g. an `unavailable` board) by its content hash.
        """
        if self._conn is None:
            raise RuntimeError("clear_board requires a DB connection")
        if status not in {REVIEW_CLEARED, REVIEW_REJECTED, REVIEW_PENDING}:
            raise ValueError(f"invalid review status: {status!r}")
        now = _now()
        self._conn.execute(
            """INSERT INTO board_delivery_qa
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
        row = self._conn.execute(
            "SELECT * FROM board_delivery_qa WHERE content_hash = ? AND prompt_version = ?",
            (content_hash, self._prompt_version),
        ).fetchone()
        if row is not None:
            self._sync_render_quality_metrics(
                self._row_to_verdict(row),
                case_id=_coerce_int(_row_value(row, "case_id")),
                job_id=_coerce_int(_row_value(row, "job_id")),
                source_path=str(_row_value(row, "source_path") or ""),
            )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Cache plumbing
    # ------------------------------------------------------------------

    def _cache_get(self, content_hash: str) -> BoardQAVerdict | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM board_delivery_qa WHERE content_hash = ? AND prompt_version = ?",
            (content_hash, self._prompt_version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_verdict(row)

    def _cache_put(
        self,
        verdict: BoardQAVerdict,
        *,
        case_id: int | None,
        job_id: int | None,
        source_path: str,
    ) -> None:
        if self._conn is None:
            return
        assessed_at = verdict.assessed_at or _now()
        self._conn.execute(
            """INSERT INTO board_delivery_qa
                 (content_hash, prompt_version, case_id, job_id, source_path,
                  verdict, primary_defect, families, confidence, provider, model,
                  latency_ms, assessed_at, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(content_hash, prompt_version) DO NOTHING""",
            (
                verdict.content_hash,
                self._prompt_version,
                case_id,
                job_id,
                source_path,
                verdict.verdict,
                verdict.primary_defect,
                json.dumps(list(verdict.families), ensure_ascii=False),
                verdict.confidence,
                verdict.provider,
                verdict.model,
                verdict.latency_ms,
                assessed_at,
                REVIEW_PENDING,
            ),
        )
        self._sync_render_quality_metrics(verdict, case_id=case_id, job_id=job_id, source_path=source_path)
        self._conn.commit()

    def _row_to_verdict(self, row: sqlite3.Row) -> BoardQAVerdict:
        return BoardQAVerdict(
            content_hash=row["content_hash"],
            verdict=row["verdict"],
            primary_defect=row["primary_defect"] or "",
            families=_coerce_families(row["families"]),
            confidence=row["confidence"],
            cached=True,
            review_status=row["review_status"] or REVIEW_PENDING,
            provider=row["provider"] or "",
            model=row["model"] or "",
            latency_ms=int(row["latency_ms"] or 0),
            review_note=_row_value(row, "review_note"),
            assessed_at=_row_value(row, "assessed_at"),
        )

    def _sync_render_quality_metrics(
        self,
        verdict: BoardQAVerdict,
        *,
        case_id: int | None,
        job_id: int | None,
        source_path: str,
    ) -> None:
        """Mirror D6 verdict into render_quality.metrics_json when possible.

        This is SSoT telemetry only. It does not alter quality_status,
        quality_score, can_publish, or the D6 delivery gate decision.
        """
        if self._conn is None or job_id is None:
            return
        try:
            row = self._conn.execute(
                "SELECT metrics_json FROM render_quality WHERE render_job_id = ?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error:
            return
        if row is None:
            return
        metrics = _json_load(_row_value(row, "metrics_json"), {})
        if not isinstance(metrics, dict):
            metrics = {}
        metrics["d6_qa"] = {
            "verdict": verdict.verdict,
            "families": list(verdict.families),
            "primary_defect": verdict.primary_defect,
            "content_hash": verdict.content_hash,
            "prompt_version": self._prompt_version,
            "review_status": verdict.review_status,
            "held": verdict.held,
            "assessed_at": verdict.assessed_at,
            "source": "board_delivery_qa",
            "case_id": case_id,
            "job_id": job_id,
            "source_path": source_path,
            "confidence": verdict.confidence,
            "provider": verdict.provider,
            "model": verdict.model,
            "error": verdict.error,
        }
        metrics["delivery_verdict"] = verdict.verdict
        metrics["delivery_held"] = verdict.held
        try:
            self._conn.execute(
                "UPDATE render_quality SET metrics_json = ?, updated_at = ? WHERE render_job_id = ?",
                (json.dumps(metrics, ensure_ascii=False), _now(), job_id),
            )
        except sqlite3.Error:
            return


def _row_value(row: sqlite3.Row, key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _json_load(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _coerce_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_families(raw: Any) -> tuple[str, ...]:
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
