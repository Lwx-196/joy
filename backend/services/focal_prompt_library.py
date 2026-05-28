"""Focal prompt library — Step 2 of the 4-mode dispatch plan.

Builds positive / negative prompts for the M3 FOCAL enhancement workflow
based on the case's ``focus_targets`` list.

Design intent
-------------

The v2 ComfyUI workflow ships with a single hardcoded positive prompt that
talks generically about "subtle localized medical aesthetic enhancement
inside the selected mask only." For M3 FOCAL we want to specialise the
prompt per anatomical region so the SDXL inpaint actually optimises for
the right outcome (e.g., lip plumping vs. tear-trough smoothing — different
visual priors).

This module is pure-function: takes the list of focus targets, returns the
positive + negative prompt strings. The downstream helper patches these
into the workflow JSON before submission to ComfyUI.

Per ``~/.claude/plans/md-ai-4-mode-router.md`` Step 2.
"""

from __future__ import annotations

# Per-target additional prompt fragments. These get concatenated AFTER the
# base preservation clause so identity / pose / hair safety is always first.
_PROMPT_FRAGMENTS: dict[str, str] = {
    # Chin / jaw line
    "下巴": "defined jawline with natural contour, subtle volume restoration on chin",
    "下颌线": "sharper jawline definition, restored mandibular angle",
    "chin": "natural chin definition, subtle volume restoration",
    # Lips
    "唇": "natural lip definition, subtle volume, healthy lip color, preserved cupid's bow",
    "lip": "natural lip definition, subtle volume, healthy color",
    "lips": "natural lip definition, subtle volume, healthy color",
    # Cheek
    "面颊": "lifted apple cheeks, subtle volume restoration, smooth cheek contour",
    "苹果肌": "lifted apple cheeks, subtle volume, natural cheek highlight",
    "cheek": "subtle cheek volume, lifted contour",
    # Nasolabial fold
    "法令纹": "smoothed nasolabial fold, restored natural cheek-mouth transition",
    "nasolabial": "smoothed nasolabial fold, restored cheek-mouth transition",
    # Tear trough / undereye
    "泪沟": "smoothed tear trough, brighter undereye, restored natural contour",
    "眼袋": "reduced undereye bag, smoothed lower eyelid, brighter look",
    "卧蚕": "natural defined aegyo-sal (lower-eye fullness), softly highlighted",
    "tear_trough": "smoothed tear trough, brighter undereye",
    "undereye": "reduced undereye bag, smoothed lower eyelid",
    # Nose
    "鼻尖": "natural refined nose tip, subtle definition",
    "鼻基底": "balanced nasal base, natural shadowing",
    "鼻翼": "balanced alar contour, natural symmetry",
    "nose": "natural refined nose contour",
    # Full face fallback
    "face": "subtle natural skin clarity, balanced clinical-grade enhancement",
    "面部": "subtle natural skin clarity, balanced clinical-grade enhancement",
}

_BASE_POSITIVE = (
    "natural medical aesthetic enhancement inside the selected mask only, "
    "preserve the same person identity, preserve exact facial structure outside mask, "
    "preserve pose hair clothing background, "
    "realistic clinic portrait photo, high quality skin texture"
)

_NEGATIVE = (
    "identity change, different person, face shape change outside mask, "
    "global retouch, skin smoothing outside mask, overdone beauty filter, "
    "different pose, different lighting, artifacts, blur, glow, halo, "
    "watermark, text, logo, low quality, deformed, distorted"
)


def build_focal_prompts(focus_targets: list[str]) -> tuple[str, str]:
    """Build (positive, negative) SDXL prompts for the given focus targets.

    Returns the base preservation clause + region-specific enhancements
    (deduplicated, preserving input order). If no recognised target,
    returns base prompts only — workflow still safe to run.
    """
    fragments: list[str] = []
    seen: set[str] = set()
    for target in focus_targets:
        frag = _PROMPT_FRAGMENTS.get(target)
        if frag and frag not in seen:
            fragments.append(frag)
            seen.add(frag)
    if fragments:
        positive = _BASE_POSITIVE + ", " + ", ".join(fragments)
    else:
        positive = _BASE_POSITIVE
    return positive, _NEGATIVE


__all__ = ["build_focal_prompts"]
