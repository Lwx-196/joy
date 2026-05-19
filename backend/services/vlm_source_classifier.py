"""VLM source image classifier for low-confidence image observations."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .vlm_provider import VLMProvider, VLMRequest, VLMRequestError, VLMResponse
from .vlm_usage import record_vlm_usage

CLASSIFICATION_PROMPT = """You are a medical-aesthetic source image classifier.
Classify exactly one source image. Return strict JSON only:
{"phase":"before|during|after","view":"front|45deg|side|back","body_part":"face|body","confidence":0.0,"reasoning":"short visual evidence"}

Rules:
- phase before means pre-treatment/source baseline; after means post-treatment result; during means intraoperative/procedure image.
- view 45deg means oblique/three-quarter face view.
- confidence must be a JSON number between 0 and 1.
- Use only visual evidence from the image.
"""

LOW_CONFIDENCE_THRESHOLD = 0.65
MIN_VLM_APPLY_CONFIDENCE = 0.85
VALID_PHASES = {"before", "intraop", "after"}
VALID_VIEWS = {"front", "oblique", "side", "back"}
VALID_BODY_PARTS = {"face", "body", "unknown"}


@dataclass(frozen=True)
class ClassificationQueueItem:
    observation_id: int
    group_id: int
    case_id: int | None
    image_path: str
    image_abs_path: Path
    phase: str
    view: str
    body_part: str
    confidence: float
    source: str


@dataclass(frozen=True)
class ClassificationResult:
    image_path: Path
    phase: str
    view: str
    body_part: str
    confidence: float
    reasoning: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_raw: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed is not None else fallback


def _float_0_1(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number between 0 and 1")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number between 0 and 1") from exc
    if parsed < 0 or parsed > 1:
        raise ValueError(f"{field_name} must be a number between 0 and 1")
    return round(parsed, 4)


def _normalize_phase(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "pre": "before",
        "preop": "before",
        "pre-op": "before",
        "before": "before",
        "术前": "before",
        "post": "after",
        "postop": "after",
        "post-op": "after",
        "after": "after",
        "术后": "after",
        "during": "intraop",
        "intraop": "intraop",
        "intra-op": "intraop",
        "procedure": "intraop",
        "术中": "intraop",
    }
    phase = mapping.get(raw, raw)
    if phase not in VALID_PHASES:
        raise ValueError(f"invalid VLM classification phase: {value!r}")
    return phase


def _normalize_view(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "frontal": "front",
        "front": "front",
        "正面": "front",
        "45": "oblique",
        "45deg": "oblique",
        "45-degree": "oblique",
        "three-quarter": "oblique",
        "oblique": "oblique",
        "斜侧": "oblique",
        "侧45": "oblique",
        "profile": "side",
        "side": "side",
        "侧面": "side",
        "back": "back",
        "背面": "back",
    }
    view = mapping.get(raw, raw)
    if view not in VALID_VIEWS:
        raise ValueError(f"invalid VLM classification view: {value!r}")
    return view


def _normalize_body_part(value: Any) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "face": "face",
        "facial": "face",
        "面部": "face",
        "body": "body",
        "身体": "body",
        "unknown": "unknown",
    }
    body_part = mapping.get(raw, raw or "unknown")
    if body_part not in VALID_BODY_PARTS:
        raise ValueError(f"invalid VLM classification body_part: {value!r}")
    return body_part


def _parse_result(image_path: Path, response: VLMResponse) -> ClassificationResult:
    parsed = response.parsed if isinstance(response.parsed, dict) else {}
    phase = _normalize_phase(parsed.get("phase"))
    view = _normalize_view(parsed.get("view"))
    body_part = _normalize_body_part(parsed.get("body_part"))
    confidence = _float_0_1(parsed.get("confidence"), field_name="confidence")
    return ClassificationResult(
        image_path=image_path,
        phase=phase,
        view=view,
        body_part=body_part,
        confidence=confidence,
        reasoning=str(parsed.get("reasoning") or parsed.get("rationale") or "").strip(),
        provider=response.provider,
        model=response.model,
        latency_ms=response.latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usage_raw=response.usage_raw,
        raw=parsed,
    )


def classify_image(image_path: Path, provider: VLMProvider, *, timeout: float = 30.0) -> ClassificationResult:
    path = Path(image_path)
    response = provider.call_vision(CLASSIFICATION_PROMPT, [path], timeout=timeout)
    return _parse_result(path, response)


def classify_batch(
    image_paths: list[Path],
    provider: VLMProvider,
    *,
    concurrency: int = 3,
    timeout: float = 30.0,
) -> list[ClassificationResult]:
    paths = [Path(path) for path in image_paths]
    requests = [VLMRequest(prompt=CLASSIFICATION_PROMPT, images=[path], timeout=timeout, purpose="classifier") for path in paths]
    responses = provider.call_vision_batch(requests, concurrency=max(1, int(concurrency or 1)))
    return [_parse_result(path, response) for path, response in zip(paths, responses)]


def _has_manual_override(conn: sqlite3.Connection, case_id: int | None, image_path: str) -> bool:
    if case_id is None:
        return False
    filename = str(image_path or "").strip()
    if not filename:
        return False
    names = list(dict.fromkeys([filename, Path(filename).name]))
    placeholders = ",".join("?" * len(names))
    row = conn.execute(
        f"""
        SELECT 1
        FROM case_image_overrides
        WHERE case_id = ?
          AND filename IN ({placeholders})
          AND (manual_phase IS NOT NULL OR manual_view IS NOT NULL)
        LIMIT 1
        """,
        (case_id, *names),
    ).fetchone()
    return row is not None


def _queue_item_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> ClassificationQueueItem | None:
    case_id = int(row["case_id"]) if row["case_id"] is not None else None
    image_path = str(row["image_path"] or "")
    source = str(row["source"] or "")
    if source == "manual_override" or _has_manual_override(conn, case_id, image_path):
        return None
    root = Path(str(row["root_path"] or ""))
    raw_path = Path(image_path)
    image_abs_path = raw_path if raw_path.is_absolute() else root / raw_path
    if not image_abs_path.is_file():
        return None
    return ClassificationQueueItem(
        observation_id=int(row["id"]),
        group_id=int(row["group_id"]),
        case_id=case_id,
        image_path=image_path,
        image_abs_path=image_abs_path,
        phase=str(row["phase"] or "unknown"),
        view=str(row["view"] or "unknown"),
        body_part=str(row["body_part"] or "unknown"),
        confidence=float(row["confidence"] or 0),
        source=source,
    )


def fetch_classification_queue(
    conn: sqlite3.Connection,
    *,
    case_id: int | None = None,
    max_items: int = 50,
) -> list[ClassificationQueueItem]:
    filters = [
        "(o.confidence < ? OR o.phase = 'unknown' OR o.view = 'unknown')",
        "o.source <> 'manual_override'",
    ]
    params: list[Any] = [LOW_CONFIDENCE_THRESHOLD]
    if case_id is not None:
        filters.append("o.case_id = ?")
        params.append(int(case_id))
    sql = f"""
        SELECT o.*, g.root_path
        FROM image_observations o
        JOIN case_groups g ON g.id = o.group_id
        WHERE {' AND '.join(filters)}
        ORDER BY o.confidence ASC, o.id ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (*params, max(1, int(max_items)))).fetchall()
    out: list[ClassificationQueueItem] = []
    for row in rows:
        item = _queue_item_from_row(conn, row)
        if item is not None:
            out.append(item)
    return out


def _eligible_value(current: str, current_confidence: float) -> bool:
    return current == "unknown" or float(current_confidence or 0) < LOW_CONFIDENCE_THRESHOLD


def apply_classification_result(
    conn: sqlite3.Connection,
    item: ClassificationQueueItem,
    result: ClassificationResult,
    *,
    min_confidence: float = MIN_VLM_APPLY_CONFIDENCE,
) -> bool:
    if result.confidence < min_confidence:
        return False
    phase = result.phase if _eligible_value(item.phase, item.confidence) else item.phase
    view = result.view if _eligible_value(item.view, item.confidence) else item.view
    body_part = result.body_part if item.body_part == "unknown" or item.confidence < LOW_CONFIDENCE_THRESHOLD else item.body_part
    if phase == item.phase and view == item.view and body_part == item.body_part and item.source == "vlm_classifier":
        return False
    reasons = _json_loads(
        conn.execute("SELECT reasons_json FROM image_observations WHERE id = ?", (item.observation_id,)).fetchone()["reasons_json"],
        [],
    )
    if not isinstance(reasons, list):
        reasons = []
    reasons = [str(reason) for reason in reasons if str(reason)]
    for reason in ("vlm_classifier", result.reasoning):
        if reason and reason not in reasons:
            reasons.append(reason)
    conn.execute(
        """
        UPDATE image_observations
        SET phase = ?, body_part = ?, view = ?, confidence = ?, source = ?,
            reasons_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            phase,
            body_part,
            view,
            result.confidence,
            "vlm_classifier",
            json.dumps(reasons[:8], ensure_ascii=False),
            _now(),
            item.observation_id,
        ),
    )
    return True


def _record_usage(
    conn: sqlite3.Connection,
    item: ClassificationQueueItem,
    result: ClassificationResult | None,
    *,
    status: str,
    error_detail: str | None = None,
) -> None:
    record_vlm_usage(
        conn,
        purpose="classifier",
        provider=(result.provider if result else "unknown"),
        model=(result.model if result else "unknown"),
        case_id=item.case_id,
        input_tokens=(result.input_tokens if result else 0),
        output_tokens=(result.output_tokens if result else 0),
        cost_usd_micros=0,
        cost_source="unknown",
        latency_ms=(result.latency_ms if result else 0),
        status=status,
        error_detail=error_detail,
        usage_raw=(result.usage_raw if result else {}),
    )


def run_classification(
    conn: sqlite3.Connection,
    *,
    provider: VLMProvider | None,
    case_id: int | None = None,
    all_low_confidence: bool = False,
    max_items: int = 50,
    dry_run: bool = True,
    concurrency: int = 3,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if case_id is None and not all_low_confidence:
        raise ValueError("case_id is required unless all_low_confidence is true")
    queue = fetch_classification_queue(conn, case_id=case_id, max_items=max_items)
    report: dict[str, Any] = {
        "run_status": "dry_run" if dry_run else "pending",
        "case_id": case_id,
        "all_low_confidence": bool(all_low_confidence),
        "candidate_count": len(queue),
        "classified_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "items": [
            {
                "observation_id": item.observation_id,
                "case_id": item.case_id,
                "image_path": item.image_path,
                "phase": item.phase,
                "view": item.view,
                "confidence": item.confidence,
                "source": item.source,
            }
            for item in queue
        ],
    }
    if dry_run:
        return report
    if provider is None:
        report["run_status"] = "blocked_missing_vlm_provider"
        return report
    paths = [item.image_abs_path for item in queue]
    try:
        results = classify_batch(paths, provider, concurrency=concurrency, timeout=timeout)
    except (VLMRequestError, ValueError, OSError) as exc:
        report["run_status"] = "blocked_vlm_classification_failed"
        report["error_count"] = len(queue)
        report["errors"] = [{"reason": str(exc)}]
        return report
    classified = 0
    skipped = 0
    for item, result in zip(queue, results):
        try:
            updated = apply_classification_result(conn, item, result)
            _record_usage(conn, item, result, status="success")
        except (sqlite3.Error, ValueError) as exc:
            report.setdefault("errors", []).append({"observation_id": item.observation_id, "reason": str(exc)})
            report["error_count"] += 1
            _record_usage(conn, item, result, status="error", error_detail=str(exc))
            continue
        if updated:
            classified += 1
        else:
            skipped += 1
    report["classified_count"] = classified
    report["skipped_count"] = skipped
    report["run_status"] = "completed_vlm_classification" if classified or skipped else "blocked_no_classification_candidates"
    return report
