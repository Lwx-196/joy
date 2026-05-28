"""MD-AI Enhancement Mode Router — Phase 1 of 4-mode dispatch plan.

After L2 quality validation (2026-05-28) confirmed that a single
"default ComfyUI workflow" choice cannot satisfy all use cases, this module
routes each render case to one of four enhancement pipelines based on the
case's context signals.

Modes
-----

POLISH    — Marketing showcase polish via P1 PS direct (external API).
ARCHIVE   — Clinical record raw fidelity: EXIF transpose + optional WB, NO AI.
FOCAL     — Local SDXL inpaint with InstantID + ControlNet + prompt + focus mask.
COMPOSITE — Before/after layout composition (no AI), reuses existing layout pipeline.
REJECTED  — Fail-closed: insufficient/ambiguous signals; caller MUST NOT silently
            run any enhancement pipeline. Operator must clarify case tags.

Signal Priority (highest → lowest)
----------------------------------

1. render_job ``meta_json.enhancement_mode`` — explicit operator override.
2. case ``tags_json`` — looks for ``"mode":"<value>"`` or marker tags
   (``clinical_archive`` / ``composite_required`` / ``focal_enhance``).
3. ``template == "tri-compare"`` or ``"composite"`` → COMPOSITE.
4. ``focus_targets`` non-empty AND brand allows AI → FOCAL.
5. Brand default map (``md_ai``/``meiji_ai`` → POLISH; others rejected by default).
6. REJECTED fail-closed.

Each signal returns immediately on first match — no late-stage overrides
("higher signal wins" is the contract).

Design notes
------------

* Pure-function module — no DB, no I/O, no side effects. Callers pass the
  raw payloads in, the router returns the mode enum + reason for audit.
* Enum-based return + ``RouteDecision`` carrier for forensics (the resolved
  mode AND the signal that triggered it so LOGGER + audit can record).
* Tests in ``backend/tests/test_md_ai_mode_router.py`` cover every priority
  branch + fail-closed.

Per ``~/.claude/plans/md-ai-4-mode-router.md`` Phase 1.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

LOGGER = logging.getLogger(__name__)


class EnhancementMode(str, Enum):
    """Four supported enhancement pipelines + fail-closed sentinel.

    Inherits from ``str`` so JSON serialisation works without custom encoder
    (mode.value == mode == "polish") and pattern-matching reads naturally.
    """

    POLISH = "polish"
    ARCHIVE = "archive"
    FOCAL = "focal"
    COMPOSITE = "composite"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RouteDecision:
    """Audit-friendly carrier for the resolved mode + the signal that won."""

    mode: EnhancementMode
    reason: str  # short signal id e.g. "meta_json.enhancement_mode" / "tags.clinical_archive"
    detail: str  # human-readable detail for LOGGER


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Brands that *may* run AI enhancement (POLISH or FOCAL) by default. Other
# brands fall through to REJECTED unless they explicitly opt in via tags.
_AI_ALLOWED_BRANDS: frozenset[str] = frozenset({"md_ai", "meiji_ai"})

# Brand → default mode map (signal #5). Brands not in this map AND lacking
# higher-priority signals get REJECTED fail-closed.
_BRAND_DEFAULT_MODE: dict[str, EnhancementMode] = {
    "md_ai": EnhancementMode.POLISH,
    "meiji_ai": EnhancementMode.POLISH,
}

# Templates whose semantics imply M4 composite layout (no AI enhancement).
_COMPOSITE_TEMPLATES: frozenset[str] = frozenset({"tri-compare", "composite", "before-after-pair"})

# Tag markers (signal #2). The presence of any of these in case_tags_json
# strongly suggests the corresponding mode regardless of brand defaults.
_TAG_TO_MODE: dict[str, EnhancementMode] = {
    "clinical_archive": EnhancementMode.ARCHIVE,
    "archive_only": EnhancementMode.ARCHIVE,
    "composite_required": EnhancementMode.COMPOSITE,
    "focal_enhance": EnhancementMode.FOCAL,
    "marketing_polish": EnhancementMode.POLISH,
}

# Accepted explicit override values in ``meta_json.enhancement_mode``
# (signal #1) and ``tags_json[].mode`` (a subset of signal #2).
_VALID_EXPLICIT_MODES: frozenset[str] = frozenset({m.value for m in EnhancementMode if m is not EnhancementMode.REJECTED})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_loads(raw: str | None) -> Any:
    """JSON-decode robustly: None / empty / malformed → None."""
    if raw is None or not str(raw).strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _coerce_mode(raw: Any) -> EnhancementMode | None:
    """Map an arbitrary value to ``EnhancementMode`` if it's a valid mode name."""
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    if candidate in _VALID_EXPLICIT_MODES:
        return EnhancementMode(candidate)
    return None


def _extract_tag_markers(tags: Any) -> Iterable[str]:
    """Flatten case_tags_json contents into a list of marker strings."""
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for entry in tags:
        if isinstance(entry, str):
            out.append(entry.strip().lower())
        elif isinstance(entry, dict):
            # Handle ``{"mode": "archive"}`` form which is signal #1.5
            mode_val = entry.get("mode")
            if isinstance(mode_val, str):
                out.append(f"mode:{mode_val.strip().lower()}")
            # Also flatten any string fields under "tag" / "marker" keys.
            for key in ("tag", "marker", "name", "label"):
                v = entry.get(key)
                if isinstance(v, str):
                    out.append(v.strip().lower())
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def resolve_mode(
    *,
    case_tags_json: str | None = None,
    render_job_meta_json: str | None = None,
    focus_targets: list[str] | None = None,
    brand: str | None = None,
    template: str | None = None,
) -> RouteDecision:
    """Resolve which enhancement mode applies to a render dispatch.

    All parameters are optional; absent signals fall through to lower-priority
    rules. Caller MUST treat ``EnhancementMode.REJECTED`` as "do not run AI" —
    typically logging the rejection and skipping the entry (NOT silently
    defaulting to POLISH).

    Returns a ``RouteDecision`` carrying the resolved mode + the signal id +
    a human-readable detail string for audit / LOGGER.
    """
    brand_norm = (brand or "").strip().lower() or None
    template_norm = (template or "").strip().lower() or None
    focus_targets = focus_targets or []

    # --- Signal #1: operator override via render_job meta_json --------------
    meta = _safe_loads(render_job_meta_json)
    if isinstance(meta, dict):
        explicit = _coerce_mode(meta.get("enhancement_mode"))
        if explicit is not None:
            return RouteDecision(
                mode=explicit,
                reason="meta_json.enhancement_mode",
                detail=f"operator override → {explicit.value}",
            )

    # --- Signal #2: case tags markers + ``{"mode":...}`` form ---------------
    tags = _safe_loads(case_tags_json)
    markers = list(_extract_tag_markers(tags))

    # 2a: ``{"mode": "archive"}`` dict form within tags array
    for m in markers:
        if m.startswith("mode:"):
            explicit = _coerce_mode(m.split(":", 1)[1])
            if explicit is not None:
                return RouteDecision(
                    mode=explicit,
                    reason="tags_json.mode",
                    detail=f"explicit tag dict mode → {explicit.value}",
                )

    # 2b: marker tag string form
    for marker in markers:
        if marker in _TAG_TO_MODE:
            mode = _TAG_TO_MODE[marker]
            return RouteDecision(
                mode=mode,
                reason=f"tags_json.{marker}",
                detail=f"tag marker {marker!r} → {mode.value}",
            )

    # --- Signal #3: template-implied composite ------------------------------
    if template_norm in _COMPOSITE_TEMPLATES:
        return RouteDecision(
            mode=EnhancementMode.COMPOSITE,
            reason="template",
            detail=f"template={template_norm!r} → composite",
        )

    # --- Signal #4: focus_targets + AI-allowed brand → focal ----------------
    if focus_targets and brand_norm in _AI_ALLOWED_BRANDS:
        return RouteDecision(
            mode=EnhancementMode.FOCAL,
            reason="focus_targets_with_ai_brand",
            detail=f"focus_targets={focus_targets!r}, brand={brand_norm!r} → focal",
        )

    # --- Signal #5: brand default map ---------------------------------------
    if brand_norm and brand_norm in _BRAND_DEFAULT_MODE:
        mode = _BRAND_DEFAULT_MODE[brand_norm]
        return RouteDecision(
            mode=mode,
            reason="brand_default",
            detail=f"brand={brand_norm!r} default → {mode.value}",
        )

    # --- Signal #6: fail-closed ---------------------------------------------
    return RouteDecision(
        mode=EnhancementMode.REJECTED,
        reason="fail_closed",
        detail=(
            f"no recognized signals (brand={brand_norm!r}, "
            f"template={template_norm!r}, focus_targets={focus_targets!r}, "
            f"tags_count={len(markers)})"
        ),
    )


__all__ = [
    "EnhancementMode",
    "RouteDecision",
    "resolve_mode",
]
