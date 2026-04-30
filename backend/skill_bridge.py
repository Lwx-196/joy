"""Bridge to the case-layout-board skill — for on-demand v3 upgrades.

Why this exists:
- The lite scanner uses filename heuristics (~40ms for the whole library) but
  produces approximate category/tier values.
- case-layout-board's `build_manifest()` runs MediaPipe pose detection and
  returns precise `category / template_tier / blocking_issues / pose_delta_max
  / sharp_ratio_min` per case (~5-30s per case).
- This module dynamically imports the skill so the heavy deps (mediapipe, cv2,
  numpy, pillow) are only loaded when an upgrade is actually requested. The
  scanner itself stays cheap.

Usage:
    payload = upgrade_case_to_v3(case_dir, brand="fumei")
    # payload has keys: category, template_tier, blocking_issues_json,
    # pose_delta_max, sharp_ratio_min, meta_extras

The skill path is hardcoded to ~/Desktop/飞书Claude/skills/case-layout-board/.
If that directory moves, update SKILL_PATH below.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Locate the case-layout-board skill. The skill lives outside this repo.
SKILL_ROOT = Path.home() / "Desktop" / "飞书Claude" / "skills" / "case-layout-board"
SKILL_SCRIPT = SKILL_ROOT / "scripts" / "case_layout_board.py"

# The skill needs cv2/mediapipe/numpy/pillow. Those are installed in the system
# Python 3.12, not in case-workbench's venv. We spawn a subprocess so case-
# workbench doesn't have to pull ~500MB of CV deps into its venv.
SKILL_PYTHON = os.environ.get("CASE_LAYOUT_SKILL_PYTHON") or shutil.which("python3") or "/usr/bin/python3"


def _build_skill_runner() -> str:
    """Inline script the subprocess will execute. It loads the skill and prints
    the raw manifest dict as JSON on stdout."""
    return r"""
import importlib.util
import json
import sys
from pathlib import Path

skill_script = Path(sys.argv[1])
case_dir = sys.argv[2]
brand_token = sys.argv[3]
template = sys.argv[4]
semantic_judge_mode = sys.argv[5]

spec = importlib.util.spec_from_file_location("case_layout_board", skill_script)
if spec is None or spec.loader is None:
    raise RuntimeError(f"无法加载 case_layout_board.py: {skill_script}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

brand_dict = module.resolve_brand(brand_token)
manifest = module.build_manifest(
    Path(case_dir),
    brand_dict,
    template,
    semantic_judge_mode=semantic_judge_mode,
)
sys.stdout.write(json.dumps(manifest, ensure_ascii=False, default=str))
"""


def _run_skill(case_dir: Path, brand: str, template: str, semantic_judge_mode: str, timeout: int = 120) -> dict[str, Any]:
    """Spawn system Python to run the skill and return the parsed manifest."""
    if not SKILL_SCRIPT.exists():
        raise RuntimeError(
            f"case-layout-board skill not found at {SKILL_SCRIPT}. "
            f"Install or move the skill, or set CASE_LAYOUT_SKILL_PYTHON."
        )
    if not Path(SKILL_PYTHON).exists():
        raise RuntimeError(
            f"Skill Python interpreter not found at {SKILL_PYTHON}. "
            f"Set CASE_LAYOUT_SKILL_PYTHON to a python3 with cv2 + mediapipe + numpy + pillow installed."
        )

    proc = subprocess.run(
        [
            SKILL_PYTHON,
            "-c",
            _build_skill_runner(),
            str(SKILL_SCRIPT),
            str(case_dir),
            brand,
            template,
            semantic_judge_mode,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        # Skill's heavy deps (cv2/mediapipe) need a clean env; pass through
        # PATH/HOME so MediaPipe can find its model cache.
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONIOENCODING": "utf-8",
        },
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"skill subprocess exited {proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    if not proc.stdout.strip():
        raise RuntimeError("skill subprocess produced empty output")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"skill output was not valid JSON: {e}; first 500 chars: {proc.stdout[:500]}")


# Mapping from case-layout-board templates → case-workbench template_tier values.
# face mode + bi-compare → "bi", etc.
TEMPLATE_TIER_MAP_FACE = {
    "tri-compare": "tri",
    "bi-compare": "bi",
    "single-compare": "single",
}


def _coerce_blocking_issue(item: Any) -> dict[str, Any] | None:
    """Convert a raw blocking issue (string or dict) into the v2 envelope used
    by case-workbench: {code, files, severity}.
    Returns None if the item has no usable code."""
    if isinstance(item, str):
        if not item:
            return None
        return {"code": item, "files": [], "severity": "block"}
    if isinstance(item, dict):
        code = str(item.get("code") or item.get("flag") or "").strip()
        if not code:
            return None
        # Some skill outputs use "files" / "image_files" / "paths"; accept all.
        files_raw = (
            item.get("files")
            or item.get("image_files")
            or item.get("paths")
            or []
        )
        files = [str(f) for f in files_raw if f]
        severity = str(item.get("severity") or "block")
        if severity not in {"block", "warn"}:
            severity = "block"
        return {"code": code, "files": files, "severity": severity}
    return None


def _extract_geometric_aggregates(groups: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    """Reduce per-group pose/sharpness signals into case-level max/min.

    Each group typically contains a `pair_review` dict with `pose_delta_max`,
    `sharp_ratio_min`, etc. We take the highest pose_delta and lowest sharp_ratio
    across all groups so the case-level number reflects the worst slot.
    """
    pose_deltas: list[float] = []
    sharp_ratios: list[float] = []
    for group in groups or []:
        pair = group.get("pair_review") if isinstance(group, dict) else None
        if isinstance(pair, dict):
            pd = pair.get("pose_delta_max")
            if isinstance(pd, (int, float)):
                pose_deltas.append(float(pd))
            sr = pair.get("sharp_ratio_min")
            if isinstance(sr, (int, float)):
                sharp_ratios.append(float(sr))
        # Some groups expose pose_delta directly on slots
        slots = group.get("selected_slots") if isinstance(group, dict) else None
        if isinstance(slots, dict):
            for slot_data in slots.values():
                if isinstance(slot_data, dict):
                    pd = slot_data.get("pose_delta_max") or slot_data.get("pose_delta")
                    if isinstance(pd, (int, float)):
                        pose_deltas.append(float(pd))
                    sr = slot_data.get("sharp_ratio_min") or slot_data.get("sharp_ratio")
                    if isinstance(sr, (int, float)):
                        sharp_ratios.append(float(sr))
    pose_max = max(pose_deltas) if pose_deltas else None
    sharp_min = min(sharp_ratios) if sharp_ratios else None
    return pose_max, sharp_min


def upgrade_case_to_v3(
    case_dir: Path | str,
    brand: str = "fumei",
    template: str = "auto",
    semantic_judge_mode: str = "off",
) -> dict[str, Any]:
    """Run case-layout-board's `build_manifest()` and map the result into the
    case-workbench schema.

    Returns a dict with these keys (suitable for direct DB UPDATE):
        category: str  (e.g., "standard_face" / "body" / "non_labeled" / ...)
        template_tier: str | None  ("tri" / "bi" / "single" / "body-dual-compare" / None)
        blocking_issues_json: str  (JSON-encoded list of v2 envelopes)
        pose_delta_max: float | None
        sharp_ratio_min: float | None
        meta_extras: dict  (additional info to merge into cases.meta_json)
        raw_status: str  (case-layout-board's "ok" / "error")
        raw_blocking_count: int
    """
    case_dir = Path(case_dir).resolve()
    if not case_dir.exists() or not case_dir.is_dir():
        raise ValueError(f"case_dir not a directory: {case_dir}")

    manifest = _run_skill(case_dir, brand, template, semantic_judge_mode)

    case_mode = str(manifest.get("case_mode") or "")
    effective_templates = manifest.get("effective_templates") or []
    if isinstance(effective_templates, list) and effective_templates:
        primary_template = str(effective_templates[0])
    else:
        primary_template = ""

    # --- Map to case-workbench category ---
    if case_mode == "body":
        category = "body"
        template_tier: str | None = "body-dual-compare"
    elif case_mode == "face":
        category = "standard_face"
        template_tier = TEMPLATE_TIER_MAP_FACE.get(primary_template)
    else:
        # Unknown case_mode (e.g., empty manifest, fragment_only). Fall back to
        # whatever the skill says without forcing a specific bucket.
        category = case_mode or "unsupported"
        template_tier = None

    # --- Blocking issues → v2 envelopes ---
    raw_blocking = manifest.get("blocking_issues") or []
    v2_issues: list[dict[str, Any]] = []
    for item in raw_blocking:
        norm = _coerce_blocking_issue(item)
        if norm is not None:
            v2_issues.append(norm)
    # Deduplicate by code, merging files
    by_code: dict[str, dict[str, Any]] = {}
    for issue in v2_issues:
        existing = by_code.get(issue["code"])
        if existing is None:
            by_code[issue["code"]] = {
                "code": issue["code"],
                "files": list(issue["files"]),
                "severity": issue["severity"],
            }
        else:
            existing["files"] = list({*existing["files"], *issue["files"]})
            if issue["severity"] == "block":
                existing["severity"] = "block"
    deduped = list(by_code.values())

    # --- Pose / sharpness aggregates from groups ---
    groups = manifest.get("groups") or []
    pose_max, sharp_min = _extract_geometric_aggregates(groups)

    # --- meta_extras: things we want to surface in UI but don't have a column for ---
    meta_extras = {
        "source": "skill_v3",
        "skill_template": primary_template,
        "skill_case_mode": case_mode,
        "skill_status": manifest.get("status"),
        "skill_warning_count": manifest.get("warning_count"),
        "skill_blocking_issue_count": manifest.get("blocking_issue_count"),
        "skill_upgraded_at": manifest.get("created_at"),
    }

    return {
        "category": category,
        "template_tier": template_tier,
        "blocking_issues_json": json.dumps(deduped, ensure_ascii=False),
        "pose_delta_max": pose_max,
        "sharp_ratio_min": sharp_min,
        "meta_extras": meta_extras,
        "raw_status": str(manifest.get("status") or ""),
        "raw_blocking_count": int(manifest.get("blocking_issue_count") or 0),
    }
