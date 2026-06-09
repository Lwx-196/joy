"""VLM source image classifier for low-confidence image observations."""
from __future__ import annotations

import json
import logging
import sqlite3
import traceback as _traceback
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .vlm_provider import VLMProvider, VLMRequest, VLMRequestError, VLMResponse

logger = logging.getLogger(__name__)
from .vlm_usage import record_vlm_usage

CLASSIFICATION_PROMPT = """You are a medical-aesthetic clinical photography analyst.
Classify exactly one source image. Return strict JSON only:
{"phase":"before|after|healing|uncertain","view":"front|45deg|side|back","body_part":"face|body","confidence":0.0,"reasoning":"short visual evidence","visual_cues":[],"is_composite":false}

## Phase Classification — Visual Cue Checklist

**Signs suggesting POST-treatment (术后)**:
- Localized redness (erythema) at treatment site
- Swelling — especially periorbital, nasolabial, lip areas
- Bruising at any stage: red/purple (fresh) → green/yellow (healing)
- Needle puncture marks or injection site dots
- Surgical marking pen traces (purple/blue ink on skin)
- Adhesive tape or bandage residue
- Visible volume increase in specific areas (lip fullness, cheek projection, tear trough fill)
- Suture lines or wound closure strips
- Skin surface texture change confined to treatment zone (smoother, tighter)

**Signs suggesting PRE-treatment (术前)**:
- Natural facial hollowing (tear troughs, temple concavity, nasolabial folds)
- Consistent skin texture across face — no localized smoothing
- No redness, bruising, swelling, or puncture marks
- Natural volume distribution without augmentation
- Wrinkles, fine lines, or skin laxity in treatment-candidate areas

**Signs suggesting HEALING (恢复期)**:
- Yellowing bruise (late-stage healing, 7-14 days post)
- Residual mild swelling without acute redness
- Partially settled filler (slight asymmetry or firmness)

**When uncertain**:
- Final healed result may look identical to pre-treatment with better proportions — use "uncertain" if no clear post-treatment visual cues
- Phone screenshots with UI chrome (status bar, navigation) → flag in reasoning
- Collage/composite images with multiple photos → set is_composite=true

## Composite Detection
- is_composite: true if the image contains multiple photos stitched together (before/after side-by-side, annotated comparison boards, multi-panel collages)
- Also true if the image has visible annotation lines, arrows, text overlays marking treatment areas, or comparison labels
- A single photo with a simple watermark/logo is NOT composite

## View Classification
- front: full frontal face, both ears potentially visible
- 45deg: three-quarter oblique view, one ear hidden
- side: true lateral/profile view, nose bridge silhouette visible
- back: posterior view

## Rules
- confidence must be 0-1; use lower values when cues are ambiguous
- visual_cues: list the specific signs you detected (e.g. ["localized redness at tear trough", "no bruising"])
- If image contains UI chrome, watermarks, or is a phone screenshot, note in reasoning
"""

LOW_CONFIDENCE_THRESHOLD = 0.65
MIN_VLM_APPLY_CONFIDENCE = 0.85
VALID_PHASES = {"before", "intraop", "after", "unknown"}
VALID_VIEWS = {"front", "oblique", "side", "back"}
VALID_BODY_PARTS = {"face", "body", "unknown"}

COMPOSITE_DETECTION_PROMPT = """Is this image a composite, collage, or comparison image?

Return ONLY strict JSON: {"is_composite": true or false, "reason": "short explanation"}

Composite means:
- Multiple photos stitched together (before/after side-by-side or stacked)
- Annotated comparison with text labels like 术前/术后, before/after
- Multi-panel collage with visible borders between photos
- Images with annotation lines, arrows, or comparison markers

NOT composite:
- Single photo with watermark or logo
- Single photo with minor text overlay (clinic name)
"""

# P0.5 (review H-2): cap traceback size to avoid multi-MB blobs landing in DB rows.
TRACEBACK_MAX_CHARS = 16384


def _truncate_tb(tb: str) -> str:
    if len(tb) <= TRACEBACK_MAX_CHARS:
        return tb
    return tb[:TRACEBACK_MAX_CHARS] + f"\n... [truncated {len(tb) - TRACEBACK_MAX_CHARS} chars]"


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
    visual_cues: list[str] = field(default_factory=list)
    is_composite: bool = False


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
    if "|" in raw:
        raw = raw.split("|")[0].strip()
    mapping = {
        "pre": "before",
        "preop": "before",
        "pre-op": "before",
        "pre-treatment": "before",
        "before": "before",
        "术前": "before",
        "post": "after",
        "postop": "after",
        "post-op": "after",
        "post-treatment": "after",
        "after": "after",
        "术后": "after",
        "healing": "after",
        "恢复期": "after",
        "during": "intraop",
        "intraop": "intraop",
        "intra-op": "intraop",
        "procedure": "intraop",
        "术中": "intraop",
        "uncertain": "unknown",
        "不确定": "unknown",
    }
    phase = mapping.get(raw, raw)
    if phase not in VALID_PHASES:
        raise ValueError(f"invalid VLM classification phase: {value!r}")
    return phase


def _normalize_view(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if "|" in raw:
        raw = raw.split("|")[0].strip()
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
    if "|" in raw:
        raw = raw.split("|")[0].strip()
    mapping = {
        "face": "face",
        "facial": "face",
        "面部": "face",
        "body": "body",
        "身体": "body",
        "neck": "body",
        "shoulder": "body",
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
    visual_cues = parsed.get("visual_cues", [])
    if not isinstance(visual_cues, list):
        visual_cues = []
    visual_cues = [str(c) for c in visual_cues if c][:10]  # cap at 10 cues
    is_composite = bool(parsed.get("is_composite"))
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
        visual_cues=visual_cues,
        is_composite=is_composite,
    )


def classify_image(image_path: Path, provider: VLMProvider, *, timeout: float = 30.0) -> ClassificationResult:
    path = Path(image_path)
    response = provider.call_vision(CLASSIFICATION_PROMPT, [path], timeout=timeout, purpose="classifier")
    return _parse_result(path, response)


def classify_batch(
    image_paths: list[Path],
    provider: VLMProvider,
    *,
    concurrency: int = 3,
    timeout: float = 30.0,
    return_exceptions: bool = False,
) -> list[ClassificationResult] | list[ClassificationResult | BaseException]:
    paths = [Path(path) for path in image_paths]
    requests = [VLMRequest(prompt=CLASSIFICATION_PROMPT, images=[path], timeout=timeout, purpose="classifier") for path in paths]
    responses = provider.call_vision_batch(
        requests,
        concurrency=max(1, int(concurrency or 1)),
        return_exceptions=return_exceptions,
    )
    results: list[ClassificationResult | BaseException] = []
    for path, response in zip(paths, responses):
        if return_exceptions and isinstance(response, BaseException):
            results.append(response)
            continue
        try:
            results.append(_parse_result(path, response))
        except (ValueError, TypeError, AttributeError) as exc:
            if not return_exceptions:
                raise
            results.append(exc)
    return results


_RETRYABLE_EXCEPTION_TYPES = (
    TimeoutError,
    ConnectionError,
    urllib.error.URLError,
    VLMRequestError,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, VLMRequestError) and exc.status_code is not None:
        return exc.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, _RETRYABLE_EXCEPTION_TYPES)


def classify_batch_with_retry(
    image_paths: list[Path],
    provider: VLMProvider,
    *,
    concurrency: int = 3,
    timeout: float = 30.0,
    max_retries: int = 2,
) -> list[ClassificationResult | BaseException]:
    """Wraps classify_batch with per-item retry for transient/timeout errors."""
    results = classify_batch(
        image_paths, provider,
        concurrency=concurrency, timeout=timeout, return_exceptions=True,
    )
    for retry_round in range(max_retries):
        retry_indices = [
            i for i, r in enumerate(results)
            if isinstance(r, BaseException) and _is_retryable(r)
        ]
        if not retry_indices:
            break
        retry_paths = [image_paths[i] for i in retry_indices]
        logger.info(
            "classify_batch retry round %d: %d items", retry_round + 1, len(retry_paths),
        )
        retry_results = classify_batch(
            retry_paths, provider,
            concurrency=concurrency, timeout=timeout, return_exceptions=True,
        )
        for idx, retry_result in zip(retry_indices, retry_results):
            results[idx] = retry_result
    return results


CLOUD_TIERS = ("flash", "tuzi")


def _make_cloud_provider(
    base_env: dict[str, str],
    tier: str,
    *,
    post_json: Any = None,
    sleep: Any = None,
    jitter: Any = None,
) -> VLMProvider | None:
    """Build a VLMProvider for cloud escalation. Returns None if not configured."""
    if tier == "flash":
        base_url = base_env.get("VLM_CLOUD_FLASH_BASE_URL", "").strip()
        api_key = base_env.get("VLM_CLOUD_FLASH_API_KEY", "").strip()
    elif tier == "tuzi":
        base_url = base_env.get("VLM_CLOUD_TUZI_BASE_URL", "").strip()
        api_key = base_env.get("VLM_CLOUD_TUZI_API_KEY", "").strip()
    else:
        return None
    if not base_url or not api_key:
        return None
    model = base_env.get("VLM_CLOUD_MODEL", "gemini-3.1-pro-preview").strip()
    cloud_env = dict(base_env)
    cloud_env["CASE_WORKBENCH_VLM_CLASSIFIER_PROVIDER"] = "openai-compatible"
    cloud_env["CASE_WORKBENCH_VLM_CLASSIFIER_MODEL"] = model
    cloud_env["CASE_WORKBENCH_VLM_CLASSIFIER_ENDPOINT"] = base_url
    cloud_env["VLM_API_KEY"] = api_key
    kwargs: dict[str, Any] = {}
    if post_json is not None:
        kwargs["post_json"] = post_json
    if sleep is not None:
        kwargs["sleep"] = sleep
    if jitter is not None:
        kwargs["jitter"] = jitter
    return VLMProvider(env=cloud_env, **kwargs)


@dataclass(frozen=True)
class EscalationTierStats:
    tier: str
    attempted: int
    upgraded: int
    failed: int


def classify_with_escalation(
    image_paths: list[Path],
    provider: VLMProvider,
    *,
    concurrency: int = 3,
    timeout: float = 30.0,
    max_retries: int = 2,
    escalation_threshold: float = MIN_VLM_APPLY_CONFIDENCE,
    cloud_tiers: tuple[str, ...] = CLOUD_TIERS,
) -> tuple[list[ClassificationResult | BaseException], list[EscalationTierStats]]:
    """Classify with local VLM, then escalate low-confidence items to cloud tiers.

    Returns (results, tier_stats) where tier_stats tracks per-tier escalation counts.
    """
    results = classify_batch_with_retry(
        image_paths, provider,
        concurrency=concurrency, timeout=timeout, max_retries=max_retries,
    )
    tier_stats: list[EscalationTierStats] = []

    for tier_name in cloud_tiers:
        escalation_indices = [
            i for i, r in enumerate(results)
            if isinstance(r, ClassificationResult) and r.confidence < escalation_threshold
        ]
        if not escalation_indices:
            break

        cloud_provider = _make_cloud_provider(provider.env, tier_name)
        if cloud_provider is None:
            logger.info("cloud tier %s not configured, skipping escalation", tier_name)
            continue

        escalation_paths = [image_paths[i] for i in escalation_indices]
        logger.info(
            "escalating %d low-confidence items to cloud tier %s",
            len(escalation_paths), tier_name,
        )

        cloud_results = classify_batch_with_retry(
            escalation_paths, cloud_provider,
            concurrency=concurrency, timeout=timeout, max_retries=max_retries,
        )

        upgraded = 0
        failed = 0
        for idx, cloud_result in zip(escalation_indices, cloud_results):
            if isinstance(cloud_result, BaseException):
                failed += 1
                continue
            original = results[idx]
            if isinstance(original, BaseException) or cloud_result.confidence > original.confidence:
                results[idx] = cloud_result
                upgraded += 1

        tier_stats.append(EscalationTierStats(
            tier=tier_name,
            attempted=len(escalation_indices),
            upgraded=upgraded,
            failed=failed,
        ))

    return results, tier_stats


def detect_composite_batch(
    image_paths: list[Path],
    provider: VLMProvider,
    *,
    concurrency: int = 2,
    timeout: float = 60.0,
) -> dict[Path, bool]:
    """Focused composite detection using a short prompt. Returns {path: is_composite}."""
    if not image_paths:
        return {}
    requests = [
        VLMRequest(prompt=COMPOSITE_DETECTION_PROMPT, images=[p], timeout=timeout, purpose="composite_detect")
        for p in image_paths
    ]
    responses = provider.call_vision_batch(requests, concurrency=concurrency, return_exceptions=True)
    out: dict[Path, bool] = {}
    for path, resp in zip(image_paths, responses):
        if isinstance(resp, BaseException):
            logger.warning("composite detection failed for %s: %s", path.name, resp)
            out[path] = False
            continue
        parsed = resp.parsed if isinstance(resp.parsed, dict) else {}
        out[path] = bool(parsed.get("is_composite"))
    return out


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
    quality = _json_loads(
        conn.execute("SELECT quality_json FROM image_observations WHERE id = ?", (item.observation_id,)).fetchone()["quality_json"],
        {},
    )
    if not isinstance(quality, dict):
        quality = {}
    if result.is_composite:
        quality["is_composite"] = True
    elif "is_composite" in quality:
        quality["is_composite"] = False
    conn.execute(
        """
        UPDATE image_observations
        SET phase = ?, body_part = ?, view = ?, confidence = ?, source = ?,
            reasons_json = ?, quality_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            phase,
            body_part,
            view,
            result.confidence,
            "vlm_classifier",
            json.dumps(reasons[:8], ensure_ascii=False),
            json.dumps(quality, ensure_ascii=False),
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
    error_json: dict[str, Any] | None = None,
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
        error_json=error_json,
        usage_raw=(result.usage_raw if result else {}),
    )


VALID_RUN_MODES = {"dry-run", "live-no-apply", "apply"}


def run_classification(
    conn: sqlite3.Connection,
    *,
    provider: VLMProvider | None,
    case_id: int | None = None,
    all_low_confidence: bool = False,
    max_items: int = 50,
    dry_run: bool = True,
    mode: str | None = None,
    concurrency: int = 3,
    timeout: float = 30.0,
    max_retries: int = 2,
    escalate: bool = True,
) -> dict[str, Any]:
    if case_id is None and not all_low_confidence:
        raise ValueError("case_id is required unless all_low_confidence is true")
    resolved_mode = mode if mode is not None else ("dry-run" if dry_run else "apply")
    if resolved_mode not in VALID_RUN_MODES:
        raise ValueError(f"invalid mode: {resolved_mode!r}; expected one of {sorted(VALID_RUN_MODES)}")
    queue = fetch_classification_queue(conn, case_id=case_id, max_items=max_items)
    report: dict[str, Any] = {
        "run_status": "dry_run" if resolved_mode == "dry-run" else "pending",
        "mode": resolved_mode,
        # P0.5: 保留原始解析的 mode，让 caller 区分"用户要 apply + 系统降级"
        # 与"用户直接要 live-no-apply"。fail-closed 改写 mode 不改 requested_mode。
        "requested_mode": resolved_mode,
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
    if resolved_mode == "dry-run":
        return report
    if provider is None:
        report["run_status"] = "blocked_missing_vlm_provider"
        return report

    # ── EfficientNet pre-screen: skip VLM for items that only need phase ──
    en_resolved: dict[int, ClassificationResult] = {}  # index → result
    vlm_indices: list[int] = list(range(len(queue)))
    try:
        from .phase_classifier import EfficientNetPhaseClassifier
        en_classifier = EfficientNetPhaseClassifier.get_instance()
        en_skipped = 0
        new_vlm_indices: list[int] = []
        for i, item in enumerate(queue):
            only_phase_unknown = (
                (item.phase == "unknown" or item.confidence < LOW_CONFIDENCE_THRESHOLD)
                and item.view not in ("unknown", "")
                and item.body_part not in ("unknown", "")
            )
            if not only_phase_unknown:
                new_vlm_indices.append(i)
                continue
            en_result = en_classifier.classify(item.image_abs_path)
            if en_result.confidence >= 0.85:
                en_resolved[i] = ClassificationResult(
                    image_path=item.image_abs_path,
                    phase=en_result.phase,
                    view=item.view,
                    body_part=item.body_part,
                    confidence=round(en_result.confidence, 4),
                    reasoning=f"efficientnet phase={en_result.phase} conf={en_result.confidence:.2%}",
                    provider="efficientnet",
                    model="efficientnet-b0",
                )
                en_skipped += 1
            else:
                new_vlm_indices.append(i)
        vlm_indices = new_vlm_indices
        if en_skipped:
            logger.info("EfficientNet pre-screen: %d resolved, %d → VLM", en_skipped, len(vlm_indices))
    except Exception as exc:
        logger.warning("EfficientNet pre-screen unavailable (%s), full VLM fallback", exc)

    # ── VLM classification for remaining items ──
    vlm_paths = [queue[i].image_abs_path for i in vlm_indices]
    escalation_tier_stats: list[EscalationTierStats] = []
    vlm_results_map: dict[int, ClassificationResult | BaseException] = {}
    if vlm_paths:
        try:
            if escalate:
                vlm_raw, escalation_tier_stats = classify_with_escalation(
                    vlm_paths,
                    provider,
                    concurrency=concurrency,
                    timeout=timeout,
                    max_retries=max_retries,
                )
            else:
                vlm_raw = classify_batch_with_retry(
                    vlm_paths,
                    provider,
                    concurrency=concurrency,
                    timeout=timeout,
                    max_retries=max_retries,
                )
            for vi, vr in zip(vlm_indices, vlm_raw):
                vlm_results_map[vi] = vr
        except (VLMRequestError, ValueError, OSError) as exc:
            report["run_status"] = "blocked_vlm_classification_failed"
            report["error_count"] = len(queue)
            report["errors"] = [{"reason": str(exc)}]
            return report

    # ── Merge results in original order ──
    results: list[ClassificationResult | BaseException] = []
    for i in range(len(queue)):
        if i in en_resolved:
            results.append(en_resolved[i])
        elif i in vlm_results_map:
            results.append(vlm_results_map[i])
        else:
            results.append(ValueError(f"item {i} not classified"))
    report["efficientnet_resolved"] = len(en_resolved)

    # P0.3-b: fail-closed 守门 — 在 apply 之前先看整批分布是否坍缩；如坍缩则
    # 强制把 mode 从 apply 降到 live-no-apply，不写 image_observations，但保留
    # vlm_usage_log 留证。即便 confidence ≥ 0.85 也不放行。
    from . import vlm_calibration as _vlm_calibration

    batch_records = [
        {
            "phase": r.phase,
            "view": r.view,
            "body_part": r.body_part,
            "confidence": float(r.confidence or 0.0),
        }
        for r in results
        if not isinstance(r, BaseException)
    ]
    # P0.5 (review H-1)：空 batch_records → status="insufficient_data" 而非误报 "ok"。
    # 用 detect_distribution_collapse 对空列表返 status="ok" 会让 ops dashboard 看起来
    # "一切正常"，但实际上 0 个分类成功。明确降级 + fail_closed=True 留证。
    report["fail_closed"] = False
    if not batch_records:
        report["calibration_status"] = "insufficient_data"
        report["calibration_recommendation"] = (
            "0 successful classifications in batch; cannot evaluate distribution."
        )
        if resolved_mode == "apply":
            resolved_mode = "live-no-apply"
            report["mode"] = resolved_mode
            report["fail_closed"] = True
            report["fail_closed_reason"] = "insufficient_data: 0 successful classifications"
    else:
        calibration = _vlm_calibration.detect_distribution_collapse(batch_records)
        report["calibration_status"] = calibration.status
        report["calibration_recommendation"] = calibration.recommendation
        if calibration.status == "uncalibrated" and resolved_mode == "apply":
            resolved_mode = "live-no-apply"
            report["mode"] = resolved_mode
            report["fail_closed"] = True
            report["fail_closed_reason"] = (
                "distribution_collapse: " + calibration.recommendation
            )

    classified = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    previews: list[dict[str, Any]] = []
    for item, result in zip(queue, results):
        if isinstance(result, BaseException):
            error_class = type(result).__name__
            error_message = str(result)
            tb_text = _truncate_tb(
                "".join(_traceback.format_exception(type(result), result, result.__traceback__))
            )
            errors.append({
                "observation_id": item.observation_id,
                "case_id": item.case_id,
                "image_path": item.image_path,
                "reason": f"{error_class}: {error_message}",
            })
            _record_usage(
                conn,
                item,
                None,
                status="error",
                error_detail=f"{error_class}: {error_message}"[:4000],
                error_json={
                    "provider": getattr(provider, "name", "unknown"),
                    "attempt": 1,
                    "error_class": error_class,
                    "error_message": error_message,
                    "traceback": tb_text,
                    "image_path": item.image_path,
                    "observation_id": item.observation_id,
                },
            )
            continue
        try:
            if resolved_mode == "apply":
                updated = apply_classification_result(conn, item, result)
                _record_usage(conn, item, result, status="success")
            else:
                updated = False
                previews.append({
                    "observation_id": item.observation_id,
                    "case_id": item.case_id,
                    "image_path": item.image_path,
                    "predicted_phase": result.phase,
                    "predicted_view": result.view,
                    "predicted_body_part": result.body_part,
                    "predicted_confidence": result.confidence,
                    "would_apply": result.confidence >= MIN_VLM_APPLY_CONFIDENCE,
                    "current_phase": item.phase,
                    "current_view": item.view,
                    "current_confidence": item.confidence,
                    "visual_cues": result.visual_cues,
                })
                _record_usage(conn, item, result, status="live_no_apply")
        except (sqlite3.Error, ValueError) as exc:
            error_class = type(exc).__name__
            tb_text = _truncate_tb(_traceback.format_exc())
            errors.append({
                "observation_id": item.observation_id,
                "case_id": item.case_id,
                "image_path": item.image_path,
                "reason": str(exc),
            })
            _record_usage(
                conn,
                item,
                result,
                status="error",
                error_detail=str(exc)[:4000],
                error_json={
                    "provider": result.provider if result else "unknown",
                    "attempt": 1,
                    "error_class": error_class,
                    "error_message": str(exc),
                    "traceback": tb_text,
                    "image_path": item.image_path,
                    "observation_id": item.observation_id,
                },
            )
            continue
        if updated:
            classified += 1
        else:
            skipped += 1
    # --- Second pass: focused composite detection ---
    # The general classification prompt is too long for small VLMs (e.g. qwen2.5vl)
    # to reliably detect composites. Run a short, focused prompt on images that
    # weren't already marked is_composite by the main pass.
    composite_updated = 0
    if resolved_mode == "apply":
        needs_composite_check: list[tuple[ClassificationQueueItem, Path]] = []
        for item, result in zip(queue, results):
            if isinstance(result, BaseException) or result.is_composite:
                continue
            needs_composite_check.append((item, item.image_abs_path))
        if needs_composite_check:
            check_paths = [p for _, p in needs_composite_check]
            composite_map = detect_composite_batch(check_paths, provider, concurrency=concurrency, timeout=timeout)
            for item, path in needs_composite_check:
                if composite_map.get(path):
                    quality = _json_loads(
                        conn.execute("SELECT quality_json FROM image_observations WHERE id = ?", (item.observation_id,)).fetchone()["quality_json"],
                        {},
                    )
                    if not isinstance(quality, dict):
                        quality = {}
                    quality["is_composite"] = True
                    conn.execute(
                        "UPDATE image_observations SET quality_json = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(quality, ensure_ascii=False), _now(), item.observation_id),
                    )
                    composite_updated += 1
                    logger.info("composite detected (2nd pass): obs %d / %s", item.observation_id, path.name)

    report["classified_count"] = classified
    report["skipped_count"] = skipped
    report["error_count"] = len(errors)
    report["composite_detected_count"] = composite_updated
    if escalation_tier_stats:
        report["escalation"] = [
            {"tier": s.tier, "attempted": s.attempted, "upgraded": s.upgraded, "failed": s.failed}
            for s in escalation_tier_stats
        ]
    if errors:
        report["errors"] = errors
    if resolved_mode == "live-no-apply":
        report["previews"] = previews
        would_apply_count = sum(1 for p in previews if p["would_apply"])
        report["would_apply_count"] = would_apply_count
        report["run_status"] = "completed_vlm_classification_live_no_apply" if previews else "blocked_no_classification_candidates"
    else:
        report["run_status"] = "completed_vlm_classification" if classified or skipped else "blocked_no_classification_candidates"
    return report
