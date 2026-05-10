"""Render executor — subprocess into the case-layout-board skill.

Why subprocess:
- The skill needs cv2/mediapipe/numpy/pillow (~500MB). v1.5 already established
  the pattern of spawning system Python 3.12 instead of polluting case-workbench
  venv. We follow the same approach here for `render_brand_clean.render_from_manifest`.

Output layout:
- Each render writes to `<case_dir>/.case-layout-output/<brand>/<template>/render/`
- Files: `final-board.jpg`, `manifest.final.json`
- This path is `.case-layout-*` prefixed so the scanner skips it (already
  enforced by case_layout_board.is_generated_case_layout_path).

Usage:
    result = run_render(case_dir, brand="fumei", template="tri-compare", semantic_judge="auto")
    # result keys: output_path, manifest_path, status, blocking_issue_count, manifest_summary
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import stress

# Same skill paths as skill_bridge — keep in sync.
SKILL_ROOT = Path.home() / "Desktop" / "飞书Claude" / "skills" / "case-layout-board"
SKILL_SCRIPT = SKILL_ROOT / "scripts" / "case_layout_board.py"
RENDER_SCRIPT = SKILL_ROOT / "scripts" / "render_brand_clean.py"
SKILL_PYTHON = os.environ.get("CASE_LAYOUT_SKILL_PYTHON") or shutil.which("python3") or "/usr/bin/python3"

DEFAULT_RENDER_TIMEOUT_SEC = int(os.environ.get("CASE_WORKBENCH_RENDER_TIMEOUT_SEC", "240"))
DEFAULT_SEMANTIC_SCREEN_TIMEOUT_SEC = "3"
DEFAULT_SEMANTIC_PAIR_REVIEW_TIMEOUT_SEC = "8"
DEFAULT_SEMANTIC_FINAL_QA_TIMEOUT_SEC = "8"

# Keep at most this many archived final-board.jpg snapshots per (case, brand, template).
# LRU evicts the oldest beyond the limit so the case directory doesn't grow unbounded.
RENDER_HISTORY_MAX_VERSIONS = int(os.environ.get("RENDER_HISTORY_MAX_VERSIONS", "10"))

_STDERR_NOISE_MARKERS = (
    "gl_context.cc:",
    "inference_feedback_manager.cc:",
    "face_landmarker_graph.cc:",
    "Feedback manager requires a model with a single signature inference",
    "FaceBlendshapesGraph acceleration",
    "Created TensorFlow Lite XNNPACK delegate",
    "Initialized TensorFlow Lite runtime",
)


def _summarize_subprocess_error(stderr: str, stdout: str = "", max_chars: int = 4000) -> str:
    """Return the useful part of a noisy MediaPipe/TFLite subprocess error.

    MediaPipe writes hundreds of informational lines to stderr. If a Python
    traceback appears after them, keeping the raw tail can still bury the actual
    exception once the queue truncates error_message for the UI. Prefer the
    traceback, then non-noise stderr lines, then the raw tail as a last resort.
    """
    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()
    combined = "\n".join(part for part in (stderr, stdout) if part)
    if not combined:
        return ""

    traceback_idx = combined.rfind("Traceback (most recent call last):")
    if traceback_idx >= 0:
        return combined[traceback_idx:][-max_chars:].strip()

    useful_lines: list[str] = []
    for line in combined.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(marker in stripped for marker in _STDERR_NOISE_MARKERS):
            continue
        useful_lines.append(stripped)

    useful = "\n".join(useful_lines).strip()
    if useful:
        return useful[-max_chars:].strip()
    return combined[-max_chars:].strip()


def _archive_existing_final_board(out_root: Path) -> str | None:
    """Archive the current final-board.jpg (if any) into `.history/` before the
    next render overwrites it.

    Called from `run_render` right before the subprocess executes, so each
    render preserves the previous result for side-by-side comparison. Failures
    are intentionally silent — archiving is best-effort and must never block a
    render attempt.

    Returns:
      The new archive timestamp (e.g. "20260429T143022Z") on success, or None
      when there is no existing final-board.jpg to archive, or the OS refused
      the write. `run_render` discards the value; `restore_archived_final_board`
      uses it to report `previous_archived_at` to the caller.
    """
    final_path = out_root / "final-board.jpg"
    if not final_path.exists() or not final_path.is_file():
        return None
    history_dir = out_root / ".history"
    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = history_dir / f"{ts}.jpg"
        shutil.copy2(final_path, archived)
        # LRU prune: keep only the newest RENDER_HISTORY_MAX_VERSIONS files.
        snapshots = sorted(
            (p for p in history_dir.iterdir() if p.is_file() and p.suffix == ".jpg"),
            key=lambda p: p.name,
            reverse=True,
        )
        for p in snapshots[RENDER_HISTORY_MAX_VERSIONS:]:
            try:
                p.unlink()
            except OSError:
                pass
        return ts
    except OSError:
        # Disk full / permission — let the actual render attempt continue.
        # No telemetry path here yet; render_queue logs the outer success/fail.
        return None


def restore_archived_final_board(out_root: Path, archived_at: str) -> dict[str, Any]:
    """Restore `<out_root>/.history/<archived_at>.jpg` → `<out_root>/final-board.jpg`.

    Steps:
      1. Auto-archive the current final-board.jpg into `.history/` (if any), so
         the operation is reversible — the user can re-restore the previous
         state by selecting the just-created snapshot.
      2. Copy the requested snapshot over `final-board.jpg`. We use `shutil.copy`
         (not `copy2`) so the new file's mtime reflects the restore moment, not
         the original render time. This avoids confusing downstream cache logic.

    Returns:
      {
        "previous_archived_at": str | None,   # ts of the just-archived prev final, None if no prev
        "restored_from": archived_at,
        "output_path": str(<out_root>/final-board.jpg),
      }

    Raises:
      FileNotFoundError: snapshot file or out_root missing.
      OSError: copy failed (disk full / permission). On failure the existing
        final-board.jpg is unchanged because copy is overwrite-in-place.
    """
    history_dir = out_root / ".history"
    snapshot_path = history_dir / f"{archived_at}.jpg"
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"snapshot not found: {snapshot_path}")
    final_path = out_root / "final-board.jpg"
    # Step 1: archive current final (if any). May return None if no current final.
    previous_ts = _archive_existing_final_board(out_root)
    # Step 2: copy without metadata so final's mtime = now.
    out_root.mkdir(parents=True, exist_ok=True)
    shutil.copy(snapshot_path, final_path)
    return {
        "previous_archived_at": previous_ts,
        "restored_from": archived_at,
        "output_path": str(final_path),
    }


def _build_render_runner() -> str:
    """Inline script the subprocess executes.

    Loads case_layout_board + render_brand_clean, calls build_manifest with the
    given brand/template/semantic mode, then calls render_brand_clean.render_from_manifest
    to write final-board.jpg. Emits one JSON line on stdout with output paths
    and the manifest summary.

    Stage B: argv[8] is a JSON dict of {filename: {phase, view}} manual overrides.
    After build_manifest() returns, the script mutates each matching entry's
    `phase` / `view.bucket` / `angle` so render labels and any post-build_manifest
    consumer sees the user's override. The original skill auto-judgment is
    preserved under `phase_skill_auto` / `view_skill_auto` for traceability.
    The dict is also written to the manifest top-level as `manual_overrides`
    so future skill versions can pick it up before pairing.
    """
    return r"""
import importlib.util
import json
import re
import sys
from pathlib import Path

skill_script_path = Path(sys.argv[1])
render_script_path = Path(sys.argv[2])
case_dir = sys.argv[3]
brand_token = sys.argv[4]
template = sys.argv[5]
semantic_judge_mode = sys.argv[6]
out_root = Path(sys.argv[7])
manual_overrides_json = sys.argv[8] if len(sys.argv) > 8 else "{}"
selection_plan_json = sys.argv[9] if len(sys.argv) > 9 else "{}"
case_dir_path = Path(case_dir).resolve()

try:
    manual_overrides = json.loads(manual_overrides_json) or {}
    if not isinstance(manual_overrides, dict):
        manual_overrides = {}
except (json.JSONDecodeError, TypeError):
    manual_overrides = {}

try:
    selection_plan = json.loads(selection_plan_json) or {}
    if not isinstance(selection_plan, dict):
        selection_plan = {}
except (json.JSONDecodeError, TypeError):
    selection_plan = {}

render_excluded_keys = {
    str(key)
    for key, value in manual_overrides.items()
    if isinstance(value, dict) and value.get("render_excluded")
}

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

case_layout = _load("case_layout_board", skill_script_path)
render_module = _load("render_brand_clean", render_script_path)

def _is_bound_render_input_path(path):
    try:
        Path(path).absolute().relative_to(case_dir_path)
    except Exception:
        return False
    return any(str(part) == ".case-workbench-bound-render" for part in case_dir_path.parts)

def _is_workbench_generated_path(path):
    path = Path(path)
    allow_bound_input = _is_bound_render_input_path(path)
    return any(
        str(part).startswith(".case-workbench-")
        and not (allow_bound_input and str(part) == ".case-workbench-bound-render")
        for part in path.parts
    )

def _path_keys(path):
    p = Path(path)
    keys = {p.name, str(p)}
    try:
        keys.add(str(p.resolve().relative_to(case_dir_path)))
    except Exception:
        pass
    return {key for key in keys if key}

def _is_render_excluded_path(path):
    return bool(render_excluded_keys and (_path_keys(path) & render_excluded_keys))

if hasattr(case_layout, "is_image_file"):
    _original_is_image_file = case_layout.is_image_file

    def _is_image_file_without_workbench_outputs(path):
        if _is_workbench_generated_path(Path(path)):
            return False
        if _is_render_excluded_path(Path(path)):
            return False
        return _original_is_image_file(path)

    case_layout.is_image_file = _is_image_file_without_workbench_outputs

if hasattr(case_layout, "is_generated_case_layout_path"):
    _original_is_generated_case_layout_path = case_layout.is_generated_case_layout_path

    def _is_generated_or_workbench_path(path, scan_root=None):
        if _is_workbench_generated_path(Path(path)):
            return True
        return _original_is_generated_case_layout_path(path, scan_root)

    case_layout.is_generated_case_layout_path = _is_generated_or_workbench_path

if hasattr(render_module, "render_aligned_pair") and hasattr(render_module, "render_side_profile_contain_cell"):
    _original_render_aligned_pair = render_module.render_aligned_pair

    def _render_aligned_pair_with_side_contain_fallback(
        before_path,
        after_paths,
        size,
        slot,
        allow_direction_mismatch=False,
        protection_targets=None,
        render_plan_records=None,
    ):
        try:
            return _original_render_aligned_pair(
                before_path,
                after_paths,
                size,
                slot,
                allow_direction_mismatch=allow_direction_mismatch,
                protection_targets=protection_targets,
                render_plan_records=render_plan_records,
            )
        except Exception as exc:
            if slot != "side":
                raise
            fallback_errors = []
            for path in after_paths or []:
                if not path:
                    continue
                try:
                    before_arr = render_module.render_side_profile_contain_cell(before_path, size)
                    after_arr = render_module.render_side_profile_contain_cell(path, size)
                    before_arr, after_arr = render_module.CASE_LAYOUT.FACE_ALIGN.harmonize_pair(before_arr, after_arr)
                    after_arr = render_module.CASE_LAYOUT.FACE_ALIGN.lift_face_shadows(after_arr, slot=slot)
                    if render_plan_records is not None:
                        render_plan_records.append({
                            "slot": slot,
                            "strategy": "side_profile_contain_after_face_detection_error",
                            "targets": protection_targets or [],
                            "before": Path(before_path).name,
                            "after": Path(path).name,
                            "error": str(exc),
                            "composition_diagnostic": {
                                "slot": slot,
                                "alerts": [
                                    {
                                        "code": "side_face_alignment_fallback",
                                        "severity": "warning",
                                        "message": "侧面人脸检测失败，已使用整图等比留白对齐兜底",
                                        "recommended_action": "复核侧面轮廓和术前术后构图，必要时换片",
                                    }
                                ],
                                "metrics": {"fallback": "contain_after_face_detection_error"},
                            },
                        })
                    return (
                        render_module.whiten_background(render_module.CASE_LAYOUT.cv_to_pil(before_arr)),
                        render_module.whiten_background(render_module.CASE_LAYOUT.cv_to_pil(after_arr)),
                    )
                except Exception as fallback_exc:
                    fallback_errors.append(f"{Path(path).name}: {fallback_exc}")
            joined = "; ".join(fallback_errors) if fallback_errors else "无可用术后图"
            raise RuntimeError(f"{exc}; 侧面 contain 兜底失败: {joined}") from exc

    render_module.render_aligned_pair = _render_aligned_pair_with_side_contain_fallback

def _manual_override_for_entry(entry):
    if not manual_overrides:
        return None
    keys = [
        entry.get("name"),
        entry.get("relative_path"),
        entry.get("group_relative_path"),
    ]
    path_value = entry.get("path")
    if path_value:
        keys.append(Path(path_value).name)
    for key in keys:
        if key and key in manual_overrides:
            return manual_overrides[key]
    return None

def _remove_issue_contains(entry, tokens):
    issues = []
    for issue in entry.get("issues") or []:
        if any(token in str(issue) for token in tokens):
            continue
        issues.append(issue)
    entry["issues"] = issues

def _attach_selection_fields(entry, override):
    for field in (
        "selection_score",
        "selection_reasons",
        "quality_warnings",
        "risk_level",
        "source_case_id",
        "source_filename",
        "source_role",
        "selection_source",
    ):
        if field in override:
            entry[field] = override.get(field)

def _apply_manual_override_to_entry(entry, override):
    if not override:
        return entry
    phase = override.get("phase")
    if phase in {"before", "after"}:
        entry["phase_skill_auto"] = entry.get("phase")
        entry["phase"] = phase
        entry["phase_source"] = override.get("phase_source") or "manual"
        _remove_issue_contains(entry, ["缺少术前/术后", "无法判定术前/术后"])
        if entry.get("rejection_reason") == "phase_missing":
            entry["rejection_reason"] = None
    view_name = override.get("view")
    if view_name in {"front", "oblique", "side"}:
        view = entry.get("view") if isinstance(entry.get("view"), dict) else {}
        entry["view_skill_auto"] = {"bucket": view.get("bucket"), "angle": entry.get("angle")}
        view = dict(view)
        view["bucket"] = view_name
        confidence = override.get("angle_confidence")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = None
        view["confidence"] = confidence if confidence is not None else (1.0 if (override.get("view_source") or "manual") == "manual" else view.get("confidence"))
        if view_name == "front":
            view["direction"] = "center"
            entry["direction"] = "center"
        elif entry.get("direction") in {None, "unknown"} and view.get("direction"):
            entry["direction"] = view.get("direction")
        entry["view"] = view
        entry["angle"] = view_name
        entry["angle_source"] = override.get("view_source") or "manual"
        if confidence is not None:
            entry["angle_confidence"] = confidence
        elif (override.get("view_source") or "manual") == "manual":
            entry["angle_confidence"] = 1.0
        _remove_issue_contains(entry, ["无法判定角度"])
        if entry.get("rejection_reason") == "angle_unknown":
            entry["rejection_reason"] = None
    transform = override.get("transform") or override.get("manual_transform")
    if isinstance(transform, dict):
        entry["manual_transform"] = transform
        entry["manual_transform_source"] = "manual"
    verdict = override.get("review_verdict") or override.get("selection_review_verdict")
    if verdict:
        entry["review_verdict"] = verdict
    if override.get("body_part"):
        entry["body_part"] = override.get("body_part")
    if override.get("treatment_area"):
        entry["treatment_area"] = override.get("treatment_area")
    if verdict == "usable":
        entry["selection_priority"] = "reviewed_usable"
        if entry.get("angle_confidence") is not None:
            entry["angle_confidence"] = max(float(entry.get("angle_confidence") or 0), 0.92)
        if entry.get("sharpness_score") is not None:
            entry["sharpness_score"] = round(float(entry.get("sharpness_score") or 0) + 5, 2)
    elif verdict == "deferred":
        entry["selection_priority"] = "low_priority_reviewed"
        if entry.get("sharpness_score") is not None:
            entry["sharpness_score"] = max(0, round(float(entry.get("sharpness_score") or 0) - 8, 2))
        issues = entry.get("issues")
        if not isinstance(issues, list):
            issues = []
        if "低优先复核图，仅在没有更好候选时使用" not in [str(x) for x in issues]:
            issues.append("低优先复核图，仅在没有更好候选时使用")
        entry["issues"] = issues
    elif verdict == "needs_repick":
        entry["selection_priority"] = "blocked_needs_repick"
        entry["rejection_reason"] = "needs_repick"
    _attach_selection_fields(entry, override)
    return entry

if manual_overrides:
    _auto_analyze_image = case_layout.analyze_image

    def _analyze_image_with_manual_overrides(*args, **kwargs):
        entry = _auto_analyze_image(*args, **kwargs)
        return _apply_manual_override_to_entry(entry, _manual_override_for_entry(entry))

    case_layout.analyze_image = _analyze_image_with_manual_overrides

def _manifest_issue_count(manifest, key):
    value = manifest.get(key)
    return len(value) if isinstance(value, list) else 0

def _append_blocking_issue(manifest, message):
    issues = manifest.get("blocking_issues")
    if not isinstance(issues, list):
        issues = []
        manifest["blocking_issues"] = issues
    if message not in [str(item) for item in issues]:
        issues.append(message)
    manifest["blocking_issue_count"] = len(issues)
    manifest["status"] = "error"

def _count_renderable_slots(manifest):
    if manifest.get("case_mode") == "body":
        return 1
    total = 0
    default_slots = list(getattr(case_layout, "ANGLE_SLOTS", []))
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected_slots = group.get("selected_slots")
        if not isinstance(selected_slots, dict):
            continue
        render_slots = group.get("render_slots") or default_slots
        for slot in render_slots:
            selection = selected_slots.get(slot)
            if not isinstance(selection, dict):
                continue
            before = selection.get("before")
            after = selection.get("after")
            if isinstance(before, dict) and before.get("path") and isinstance(after, dict) and after.get("path"):
                total += 1
    return total

def _composition_alert_layers(manifest):
    alerts = []
    accepted_alerts = []
    render_plan = manifest.get("render_plan")
    if not isinstance(render_plan, dict):
        return {"active": alerts, "accepted_review": accepted_alerts}
    selected_files_by_slot = _selected_slot_files_map(manifest) if "_selected_slot_files_map" in globals() else {}
    for record in render_plan.get("slots") or []:
        if not isinstance(record, dict):
            continue
        diagnostic = record.get("composition_diagnostic")
        if not isinstance(diagnostic, dict):
            continue
        for alert in diagnostic.get("alerts") or []:
            if not isinstance(alert, dict):
                continue
            normalized = {
                "slot": diagnostic.get("slot") or record.get("slot"),
                "slot_label": diagnostic.get("slot_label") or diagnostic.get("slot") or record.get("slot"),
                "code": alert.get("code"),
                "severity": alert.get("severity") or "warning",
                "message": alert.get("message"),
                "recommended_action": alert.get("recommended_action"),
                "metrics": diagnostic.get("metrics") or {},
            }
            accepted = _accepted_warning_match(str(normalized.get("message") or ""), selected_files_by_slot)
            if accepted:
                accepted_alert = dict(accepted)
                accepted_alert["message"] = normalized.get("message")
                accepted_alert["alert"] = normalized
                accepted_alerts.append(accepted_alert)
            else:
                alerts.append(normalized)
    return {"active": alerts, "accepted_review": accepted_alerts}

def _composition_alerts(manifest):
    return _composition_alert_layers(manifest)["active"]

def _warning_audit_with_composition_acceptance(audit, accepted_alerts):
    if not accepted_alerts:
        return audit if isinstance(audit, dict) else {}
    merged = dict(audit) if isinstance(audit, dict) else {}
    suppressed_layers = dict(merged.get("suppressed_layers") or {})
    accepted_review = list(suppressed_layers.get("accepted_review") or [])
    accepted_review.extend(accepted_alerts)
    suppressed_layers["accepted_review"] = accepted_review
    merged["suppressed_layers"] = suppressed_layers
    suppressed_counts = dict(merged.get("suppressed_counts") or {})
    suppressed_counts["accepted_review"] = len(accepted_review)
    merged["suppressed_counts"] = suppressed_counts
    return merged

def _apply_composition_alert_layers(manifest):
    layers = _composition_alert_layers(manifest)
    manifest["composition_alerts"] = layers["active"]
    manifest["composition_audit"] = {"accepted_review": layers["accepted_review"]}
    if layers["accepted_review"]:
        manifest["warning_audit"] = _warning_audit_with_composition_acceptance(
            manifest.get("warning_audit") or {},
            layers["accepted_review"],
        )
    return layers

def _result_payload(manifest, final_path, manifest_path, output_path=None, render_error=None):
    if "composition_alerts" in manifest or "composition_audit" in manifest:
        composition_alerts = manifest.get("composition_alerts") if isinstance(manifest.get("composition_alerts"), list) else []
        composition_audit = manifest.get("composition_audit") if isinstance(manifest.get("composition_audit"), dict) else {}
        warning_audit = manifest.get("warning_audit") if isinstance(manifest.get("warning_audit"), dict) else {}
    else:
        composition_layers = _composition_alert_layers(manifest)
        composition_alerts = composition_layers["active"]
        composition_audit = {"accepted_review": composition_layers["accepted_review"]}
        warning_audit = _warning_audit_with_composition_acceptance(
            manifest.get("warning_audit") or {},
            composition_audit.get("accepted_review") or [],
        )
    payload = {
        "output_path": str(output_path) if output_path else None,
        "manifest_path": str(manifest_path),
        "status": str(manifest.get("status") or ""),
        "blocking_issue_count": int(manifest.get("blocking_issue_count") or _manifest_issue_count(manifest, "blocking_issues")),
        "warning_count": int(manifest.get("warning_count") or _manifest_issue_count(manifest, "warnings")),
        "case_mode": str(manifest.get("case_mode") or ""),
        "effective_templates": manifest.get("effective_templates") or [],
        "manual_overrides_applied": list(manifest.get("manual_overrides_applied") or []),
        "ai_usage": {
            "used_after_enhancement": any(
                bool((entry.get("enhancement") or {}).get("enhanced_path"))
                for group in (manifest.get("groups") or [])
                if isinstance(group, dict)
                for entry in (group.get("entries") or [])
                if isinstance(entry, dict)
            ),
            "used_ai_padfill": str(manifest.get("padfill_mode") or "").lower() == "ai",
        },
        "blocking_issues": [str(item) for item in (manifest.get("blocking_issues") or [])],
        "warnings": [str(item) for item in (manifest.get("warnings") or [])],
        "composition_alerts": composition_alerts,
        "composition_audit": composition_audit,
        "selection_quality": manifest.get("selection_quality") or [],
        "render_selection_audit": manifest.get("render_selection_audit") or {},
        "render_selection_source_provenance": manifest.get("render_selection_source_provenance") or [],
        "render_selection_missing_slots": manifest.get("render_selection_missing_slots") or [],
        "render_selection_dropped_slots": manifest.get("render_selection_dropped_slots") or [],
        "render_selection_plan": manifest.get("render_selection_plan") or {},
        "warning_layers": manifest.get("warning_layers") or {},
        "warning_display_layers": manifest.get("warning_display_layers") or {},
        "warning_audit": warning_audit,
        "warning_layer_counts": manifest.get("warning_layer_counts") or {},
    }
    if render_error:
        payload["render_error"] = str(render_error)
    return payload

brand_dict = case_layout.resolve_brand(brand_token)
manifest = case_layout.build_manifest(
    Path(case_dir),
    brand_dict,
    template,
    semantic_judge_mode=semantic_judge_mode,
)

# Stage B: manual overrides are injected into analyze_image above, before the
# skill builds phase/slot candidates. This post-pass keeps the manifest explicit
# for downstream readers and for old manifests produced before the pre-pair hook.
if manual_overrides:
    applied = []
    def _apply_manifest_override_dict(item):
        if not isinstance(item, dict):
            return
        ov = _manual_override_for_entry(item)
        if not ov:
            return
        if ov.get("phase"):
            item["phase_source"] = ov.get("phase_source") or "manual"
        if ov.get("view"):
            item["angle_source"] = ov.get("view_source") or "manual"
        _attach_selection_fields(item, ov)
        transform = ov.get("transform") or ov.get("manual_transform")
        if isinstance(transform, dict):
            item["manual_transform"] = transform
            item["manual_transform_source"] = "manual"
        if ov.get("review_verdict"):
            item["review_verdict"] = ov.get("review_verdict")
        if ov.get("body_part"):
            item["body_part"] = ov.get("body_part")
        if ov.get("treatment_area"):
            item["treatment_area"] = ov.get("treatment_area")

    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            ov = _manual_override_for_entry(entry)
            if not ov:
                continue
            if ov.get("phase"):
                entry["phase_skill_auto"] = entry.get("phase")
                entry["phase"] = ov["phase"]
                entry["phase_source"] = ov.get("phase_source") or "manual"
            if ov.get("view"):
                view = entry.get("view") if isinstance(entry.get("view"), dict) else {}
                entry["view_skill_auto"] = {"bucket": view.get("bucket"), "angle": entry.get("angle")}
                view["bucket"] = ov["view"]
                entry["view"] = view
                entry["angle"] = ov["view"]
                entry["angle_source"] = ov.get("view_source") or "manual"
                if ov.get("angle_confidence") is not None:
                    entry["angle_confidence"] = ov.get("angle_confidence")
            transform = ov.get("transform") or ov.get("manual_transform")
            if isinstance(transform, dict):
                entry["manual_transform"] = transform
                entry["manual_transform_source"] = "manual"
            if ov.get("review_verdict"):
                entry["review_verdict"] = ov.get("review_verdict")
            if ov.get("body_part"):
                entry["body_part"] = ov.get("body_part")
            if ov.get("treatment_area"):
                entry["treatment_area"] = ov.get("treatment_area")
            _attach_selection_fields(entry, ov)
            applied.append(entry.get("name"))
        for selection in (group.get("selected_slots") or {}).values():
            if not isinstance(selection, dict):
                continue
            _apply_manifest_override_dict(selection.get("before"))
            _apply_manifest_override_dict(selection.get("after"))
    manifest["manual_overrides"] = manual_overrides
    manifest["manual_overrides_applied"] = applied
if render_excluded_keys:
    manifest["render_excluded_files"] = sorted(render_excluded_keys)

def _entry_match_keys(entry):
    if not isinstance(entry, dict):
        return set()
    keys = {
        entry.get("name"),
        entry.get("relative_path"),
        entry.get("group_relative_path"),
    }
    path_value = entry.get("path")
    if path_value:
        p = Path(path_value)
        keys.add(p.name)
        try:
            keys.add(str(p.resolve().relative_to(case_dir_path)))
        except Exception:
            pass
    return {str(key) for key in keys if key}

def _plan_match_keys(candidate):
    if not isinstance(candidate, dict):
        return set()
    keys = {
        candidate.get("render_filename"),
        candidate.get("filename"),
    }
    return {str(key) for key in keys if key}

def _find_group_entry(group, candidate):
    targets = _plan_match_keys(candidate)
    if not targets:
        return None
    for entry in group.get("entries") or []:
        if _entry_match_keys(entry) & targets:
            return entry
    for selection in (group.get("selected_slots") or {}).values():
        if not isinstance(selection, dict):
            continue
        for role in ("before", "after"):
            entry = selection.get(role)
            if _entry_match_keys(entry) & targets:
                return entry
    return None

def _slot_selection_summary(selection):
    if not isinstance(selection, dict):
        return None
    return {
        "before": (selection.get("before") or {}).get("name") if isinstance(selection.get("before"), dict) else None,
        "after": (selection.get("after") or {}).get("name") if isinstance(selection.get("after"), dict) else None,
        "pose_delta": selection.get("pose_delta"),
    }

def _float_or_zero(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

def _basic_pose_delta(before_pose, after_pose):
    before_pose = before_pose or {}
    after_pose = after_pose or {}
    yaw = abs(_float_or_zero(before_pose.get("yaw")) - _float_or_zero(after_pose.get("yaw")))
    pitch = abs(_float_or_zero(before_pose.get("pitch")) - _float_or_zero(after_pose.get("pitch")))
    roll = abs(_float_or_zero(before_pose.get("roll")) - _float_or_zero(after_pose.get("roll")))
    return {
        "yaw": round(yaw, 2),
        "pitch": round(pitch, 2),
        "roll": round(roll, 2),
        "weighted": round(yaw + pitch + roll * 0.5, 2),
    }

def _profile_pose_delta(slot, before_item, after_item, raw_delta):
    if slot not in {"oblique", "side"}:
        return raw_delta
    before_direction = before_item.get("direction")
    after_direction = after_item.get("direction")
    same_direction = (
        before_direction
        and after_direction
        and before_direction == after_direction
        and before_direction not in {"center", "unknown", "unspecified"}
    )
    manual_same_slot = (
        before_item.get("angle_source") == "manual"
        and after_item.get("angle_source") == "manual"
        and before_item.get("angle") == after_item.get("angle") == slot
    )
    if not same_direction and not manual_same_slot:
        return raw_delta
    before_pose = before_item.get("pose") or {}
    after_pose = after_item.get("pose") or {}
    yaw = abs(abs(_float_or_zero(before_pose.get("yaw"))) - abs(_float_or_zero(after_pose.get("yaw"))))
    pitch = abs(_float_or_zero(before_pose.get("pitch")) - _float_or_zero(after_pose.get("pitch")))
    roll = abs(_float_or_zero(before_pose.get("roll")) - _float_or_zero(after_pose.get("roll")))
    normalized = {
        "yaw": round(yaw, 2),
        "pitch": round(pitch, 2),
        "roll": round(roll, 2),
        "weighted": round(yaw + pitch + roll * 0.5, 2),
        "normalization": "profile_abs_yaw_same_direction",
        "raw": raw_delta,
    }
    if _float_or_zero(normalized.get("weighted")) < _float_or_zero((raw_delta or {}).get("weighted")):
        return normalized
    return raw_delta

def _selection_pose_delta(slot, before_item, after_item):
    if hasattr(case_layout, "compute_pose_delta"):
        raw_delta = case_layout.compute_pose_delta(before_item.get("pose"), after_item.get("pose"))
    else:
        raw_delta = _basic_pose_delta(before_item.get("pose"), after_item.get("pose"))
    if not isinstance(raw_delta, dict):
        raw_delta = _basic_pose_delta(before_item.get("pose"), after_item.get("pose"))
    return _profile_pose_delta(slot, before_item, after_item, raw_delta)

def _pose_delta_within_threshold(slot, pose_delta):
    if not isinstance(pose_delta, dict):
        return False
    if hasattr(case_layout, "pose_delta_within_threshold"):
        try:
            return bool(case_layout.pose_delta_within_threshold(slot, pose_delta))
        except Exception:
            pass
    fallback = {
        "front": {"yaw": 4.5, "pitch": 7.0, "roll": 4.0, "weighted": 11.0},
        "oblique": {"yaw": 8.0, "pitch": 8.0, "roll": 5.0, "weighted": 14.0},
        "side": {"yaw": 10.0, "pitch": 8.0, "roll": 5.0, "weighted": 16.0},
    }.get(slot, {"weighted": 12.0})
    return all(_float_or_zero(pose_delta.get(key)) <= float(limit) for key, limit in fallback.items())

def _warning_slot(text):
    labels = getattr(case_layout, "ANGLE_LABELS", {})
    for slot in getattr(case_layout, "ANGLE_SLOTS", ["front", "oblique", "side"]):
        label = labels.get(slot, slot)
        if slot in text or (label and label in text):
            return slot
    if "正面" in text:
        return "front"
    if "45" in text:
        return "oblique"
    if "侧面" in text or "侧脸" in text:
        return "side"
    return None

def _entry_from_plan(entry, candidate, role):
    item = dict(entry)
    item["render_selection_role"] = role
    item["render_selection_policy"] = selection_plan.get("policy") or "source_selection_v1"
    for field in (
        "case_id",
        "source_role",
        "filename",
        "render_filename",
        "phase",
        "phase_source",
        "view",
        "view_source",
        "review_verdict",
        "angle_confidence",
        "selection_score",
        "selection_reasons",
        "quality_warnings",
        "risk_level",
    ):
        if field in candidate and candidate.get(field) is not None:
            target_field = "source_case_id" if field == "case_id" else field
            item[target_field] = candidate.get(field)
    if candidate.get("phase") in {"before", "after"}:
        item["phase"] = candidate.get("phase")
    if candidate.get("view") in {"front", "oblique", "side"}:
        view = item.get("view") if isinstance(item.get("view"), dict) else {}
        view = dict(view)
        view["bucket"] = candidate.get("view")
        if candidate.get("angle_confidence") is not None:
            view["confidence"] = candidate.get("angle_confidence")
        item["view"] = view
        item["angle"] = candidate.get("view")
    return item

def _blocker_belongs_to_applied_slot(message, applied_slots):
    text = str(message or "")
    removable_tokens = ("缺少", "方向不一致", "命中过多", "没有可渲染")
    if not any(token in text for token in removable_tokens):
        return False
    for slot in applied_slots:
        label = getattr(case_layout, "ANGLE_LABELS", {}).get(slot, slot)
        if slot in text or (label and label in text):
            return True
    return False

def _filter_group_rejections(rejections, applied_slots):
    kept = []
    removable_reasons = {"missing_phase", "direction_mismatch", "ambiguous_candidates", "duplicate_slot_material"}
    for item in rejections or []:
        if (
            isinstance(item, dict)
            and item.get("slot") in applied_slots
            and item.get("reason") in removable_reasons
        ):
            continue
        kept.append(item)
    return kept

def _apply_render_selection_plan(manifest, plan):
    slots_plan = plan.get("slots") if isinstance(plan, dict) else None
    if not isinstance(slots_plan, dict) or not slots_plan:
        manifest["render_selection_plan"] = plan if isinstance(plan, dict) else {}
        return {"policy": (plan or {}).get("policy"), "applied_slots": [], "missing_entries": [], "overrode": []}
    audit = {
        "policy": plan.get("policy") or "source_selection_v1",
        "applied_slots": [],
        "dropped_slots": list(plan.get("dropped_slots") or []),
        "removed_unplanned_slots": [],
        "missing_entries": [],
        "overrode": [],
    }
    angle_slots = list(getattr(case_layout, "ANGLE_SLOTS", ["front", "oblique", "side"]))
    planned_slot_set = {
        slot
        for slot in angle_slots
        if isinstance((slots_plan.get(slot) or {}).get("before"), dict)
        and isinstance((slots_plan.get(slot) or {}).get("after"), dict)
    }
    dropped_slot_set = {
        str(item.get("view") or "")
        for item in (plan.get("dropped_slots") or [])
        if isinstance(item, dict) and item.get("view")
    }
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected_slots = dict(group.get("selected_slots") or {})
        original = {slot: _slot_selection_summary(selected_slots.get(slot)) for slot in angle_slots if selected_slots.get(slot)}
        for slot in angle_slots:
            if slot in planned_slot_set:
                continue
            removed = selected_slots.pop(slot, None)
            if isinstance(removed, dict):
                audit["removed_unplanned_slots"].append({
                    "group": group.get("name"),
                    "slot": slot,
                    "reason": "dropped_by_selection_plan" if slot in dropped_slot_set else "not_in_selection_plan",
                    "before": _slot_selection_summary(removed),
                })
        applied_for_group = []
        for slot in angle_slots:
            slot_plan = slots_plan.get(slot)
            if not isinstance(slot_plan, dict):
                continue
            before_plan = slot_plan.get("before")
            after_plan = slot_plan.get("after")
            if not isinstance(before_plan, dict) or not isinstance(after_plan, dict):
                continue
            before_entry = _find_group_entry(group, before_plan)
            after_entry = _find_group_entry(group, after_plan)
            if not isinstance(before_entry, dict) or not isinstance(after_entry, dict):
                audit["missing_entries"].append({
                    "group": group.get("name"),
                    "slot": slot,
                    "before": before_plan.get("render_filename") or before_plan.get("filename"),
                    "after": after_plan.get("render_filename") or after_plan.get("filename"),
                })
                continue
            before_item = _entry_from_plan(before_entry, before_plan, "before")
            after_item = _entry_from_plan(after_entry, after_plan, "after")
            pose_delta = _selection_pose_delta(slot, before_item, after_item)
            previous = _slot_selection_summary(selected_slots.get(slot))
            selection = {
                "label": getattr(case_layout, "ANGLE_LABELS", {}).get(slot, slot),
                "direction": after_item.get("direction") or before_item.get("direction") or "unknown",
                "pose_delta": pose_delta,
                "semantic_pair_review": None,
                "before": before_item,
                "after": after_item,
                "selection_source": audit["policy"],
                "pair_quality": slot_plan.get("pair_quality"),
            }
            selected_slots[slot] = selection
            applied_for_group.append(slot)
            audit["applied_slots"].append({
                "group": group.get("name"),
                "slot": slot,
                "before": before_item.get("name"),
                "after": after_item.get("name"),
                "pose_delta": pose_delta,
                "pair_quality": slot_plan.get("pair_quality"),
            })
            new_summary = _slot_selection_summary(selection)
            if previous and previous != new_summary:
                audit["overrode"].append({
                    "group": group.get("name"),
                    "slot": slot,
                    "before": previous,
                    "after": new_summary,
                })
        if not applied_for_group:
            continue
        group["source_selection_original_slots"] = original
        group["selected_slots"] = selected_slots
        candidate_slots = [slot for slot in angle_slots if slot in planned_slot_set and isinstance(selected_slots.get(slot), dict)]
        if hasattr(case_layout, "derive_effective_template"):
            effective_template, render_slots = case_layout.derive_effective_template(candidate_slots, manifest.get("angle_priority_profile"))
            if render_slots:
                group["render_slots"] = render_slots
            if effective_template:
                group["effective_template"] = effective_template
        group["render_selection_note"] = f"正式出图已按 source_selection_v1 候选排序覆盖 {len(applied_for_group)} 个槽位"
        group["blocking_issues"] = [
            item for item in (group.get("blocking_issues") or [])
            if not _blocker_belongs_to_applied_slot(item, set(applied_for_group))
        ]
        group["rejection_reasons"] = _filter_group_rejections(group.get("rejection_reasons") or [], set(applied_for_group))
        group["status"] = "ok" if not group.get("blocking_issues") else group.get("status")
    applied_slots = {item.get("slot") for item in audit["applied_slots"] if item.get("slot")}
    manifest["blocking_issues"] = [
        item for item in (manifest.get("blocking_issues") or [])
        if not _blocker_belongs_to_applied_slot(item, applied_slots)
    ]
    manifest["rejection_reasons"] = _filter_group_rejections(manifest.get("rejection_reasons") or [], applied_slots)
    warnings = manifest.get("warnings") if isinstance(manifest.get("warnings"), list) else []
    manifest["warnings"] = warnings
    manifest["warning_count"] = len(warnings)
    manifest["blocking_issue_count"] = len(manifest.get("blocking_issues") or [])
    if _count_renderable_slots(manifest) > 0 and not manifest.get("blocking_issues"):
        manifest["status"] = "ok"
    manifest["render_selection_plan"] = plan
    manifest["render_selection_audit"] = audit
    manifest["render_selection_source_provenance"] = plan.get("source_provenance") if isinstance(plan, dict) else []
    manifest["render_selection_missing_slots"] = plan.get("missing_slots") if isinstance(plan, dict) else []
    manifest["render_selection_dropped_slots"] = plan.get("dropped_slots") if isinstance(plan, dict) else []
    effective_templates = []
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        effective_template = group.get("effective_template")
        if effective_template and effective_template not in effective_templates:
            effective_templates.append(effective_template)
    if effective_templates:
        manifest["effective_templates"] = effective_templates
    return audit

manifest["render_selection_audit"] = _apply_render_selection_plan(manifest, selection_plan)

IMAGE_REF_RE = re.compile(r"\.(?:jpg|jpeg|png|heic|webp|bmp)\b", re.IGNORECASE)

def _selected_file_names(manifest):
    selected = set()
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for selection in (group.get("selected_slots") or {}).values():
            if not isinstance(selection, dict):
                continue
            for role in ("before", "after"):
                item = selection.get(role)
                if not isinstance(item, dict):
                    continue
                for key in ("name", "relative_path", "group_relative_path"):
                    value = item.get(key)
                    if value:
                        text = str(value)
                        selected.add(text)
                        selected.add(Path(text).name)
    return selected

def _selected_slot_pose_map(manifest):
    out = {}
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for slot, selection in (group.get("selected_slots") or {}).items():
            if isinstance(selection, dict):
                out[str(slot)] = selection.get("pose_delta") if isinstance(selection.get("pose_delta"), dict) else {}
    return out

def _selected_file_slot_map(manifest):
    out = {}
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for slot, selection in (group.get("selected_slots") or {}).items():
            if not isinstance(selection, dict):
                continue
            for role in ("before", "after"):
                item = selection.get(role)
                if not isinstance(item, dict):
                    continue
                for key in ("name", "relative_path", "group_relative_path"):
                    value = item.get(key)
                    if value:
                        text = str(value)
                        out[text] = str(slot)
                        out[Path(text).name] = str(slot)
    return out

def _selected_slot_files_map(manifest):
    out = {}
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for slot, selection in (group.get("selected_slots") or {}).items():
            if not isinstance(selection, dict):
                continue
            slot_key = str(slot)
            files = out.setdefault(slot_key, set())
            for role in ("before", "after"):
                item = selection.get(role)
                if not isinstance(item, dict):
                    continue
                for key in ("name", "relative_path", "group_relative_path"):
                    value = item.get(key)
                    if value:
                        text = str(value)
                        files.add(text)
                        files.add(Path(text).name)
    return out

def _pose_metric_text(pose_delta):
    if not isinstance(pose_delta, dict):
        return "yaw=0.00, pitch=0.00, roll=0.00, weighted=0.00"
    return "yaw={yaw:.2f}, pitch={pitch:.2f}, roll={roll:.2f}, weighted={weighted:.2f}".format(
        yaw=_float_or_zero(pose_delta.get("yaw")),
        pitch=_float_or_zero(pose_delta.get("pitch")),
        roll=_float_or_zero(pose_delta.get("roll")),
        weighted=_float_or_zero(pose_delta.get("weighted")),
    )

def _entry_display_name(item):
    if not isinstance(item, dict):
        return ""
    return str(item.get("name") or item.get("relative_path") or item.get("group_relative_path") or "").strip()

def _current_selected_pose_warnings(manifest):
    out = {}
    labels = getattr(case_layout, "ANGLE_LABELS", {})
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "group")
        for slot, selection in (group.get("selected_slots") or {}).items():
            if not isinstance(selection, dict):
                continue
            pose_delta = selection.get("pose_delta") if isinstance(selection.get("pose_delta"), dict) else {}
            if _pose_delta_within_threshold(str(slot), pose_delta):
                continue
            before_name = _entry_display_name(selection.get("before"))
            after_name = _entry_display_name(selection.get("after"))
            label = labels.get(slot, slot)
            out[str(slot)] = (
                f"{group_name}：{label} 当前入选 {before_name} / {after_name} "
                f"术前术后姿态差过大({_pose_metric_text(pose_delta)})，请回源组候选重选姿态更接近的配对"
            )
    return out

def _warning_mentions_selected_file(text, selected):
    if not selected:
        return True
    return any(name and name in text for name in selected)

def _warning_selected_slot(text, selected_slot_by_file):
    for name, slot in selected_slot_by_file.items():
        if name and name in text:
            return slot
    return None

def _accepted_warning_match(text, selected_files_by_slot):
    accepted = selection_plan.get("accepted_warnings") if isinstance(selection_plan, dict) else []
    if not isinstance(accepted, list):
        return None
    slot = _warning_slot(text)
    for item in accepted:
        if not isinstance(item, dict):
            continue
        accepted_slot = str(item.get("slot") or "")
        if accepted_slot and slot and accepted_slot != slot:
            continue
        scoped_files = {str(value or "").strip() for value in item.get("selected_files") or [] if str(value or "").strip()}
        if scoped_files:
            scoped_slot = accepted_slot or slot or ""
            selected_for_slot = selected_files_by_slot.get(scoped_slot, set())
            if not scoped_files <= selected_for_slot:
                continue
        contains = str(item.get("message_contains") or "").strip()
        code = str(item.get("code") or "").strip()
        if contains and contains not in text:
            continue
        if not contains:
            code_tokens = {
                "direction_mismatch": ("方向不一致",),
                "pose_delta_large": ("姿态差过大",),
                "sharpness_delta": ("清晰度差", "清晰度"),
                "side_face_alignment_fallback": ("侧面人脸检测失败", "构图"),
            }.get(code, ())
            if code_tokens and not any(token in text for token in code_tokens):
                continue
        matched = dict(item)
        matched["message"] = text
        return matched
    return None

def _build_warning_layers(manifest):
    selected = _selected_file_names(manifest)
    selected_slot_by_file = _selected_file_slot_map(manifest)
    selected_files_by_slot = _selected_slot_files_map(manifest)
    pose_by_slot = _selected_slot_pose_map(manifest)
    current_pose_warnings = _current_selected_pose_warnings(manifest)
    emitted_current_pose_slots = set()
    raw_warnings = [str(item) for item in (manifest.get("warnings") or [])]
    dropped_slot_set = {
        str(item.get("view") or "")
        for item in (manifest.get("render_selection_dropped_slots") or [])
        if isinstance(item, dict) and item.get("view")
    }
    layers = {
        "selected_actionable": [],
        "selected_expected_profile": [],
        "accepted_review": [],
        "candidate_noise": [],
        "stale_pose_noise": [],
        "dropped_slot_noise": [],
    }
    for warning in raw_warnings:
        text = str(warning)
        mentions_image = bool(IMAGE_REF_RE.search(text))
        mentions_selected = _warning_mentions_selected_file(text, selected)
        if mentions_image and not mentions_selected:
            layers["candidate_noise"].append(text)
            continue
        if "姿态推断候选" in text:
            layers["candidate_noise"].append(text)
            continue
        slot = _warning_slot(text)
        if slot and slot in dropped_slot_set:
            layers["dropped_slot_noise"].append(text)
            continue
        accepted = _accepted_warning_match(text, selected_files_by_slot)
        if accepted:
            layers["accepted_review"].append(accepted)
            continue
        if "姿态差过大" in text:
            pose_delta = pose_by_slot.get(slot or "")
            current_pose_warning = current_pose_warnings.get(slot or "") if slot else None
            if current_pose_warning:
                accepted_current = _accepted_warning_match(current_pose_warning, selected_files_by_slot)
                if accepted_current:
                    layers["accepted_review"].append(accepted_current)
                elif slot not in emitted_current_pose_slots:
                    layers["selected_actionable"].append(current_pose_warning)
                    emitted_current_pose_slots.add(slot)
                if text != current_pose_warning:
                    layers["stale_pose_noise"].append(text)
            elif slot and _pose_delta_within_threshold(slot, pose_delta):
                layers["stale_pose_noise"].append(text)
            else:
                layers["selected_actionable"].append(text)
            continue
        if "正脸检测失败，已使用侧脸检测兜底" in text:
            layers["selected_expected_profile"].append(text)
            continue
        if "面部检测失败" in text or "正脸检测失败" in text:
            if _warning_selected_slot(text, selected_slot_by_file) == "side":
                layers["selected_expected_profile"].append(text)
                continue
            layers["selected_actionable"].append(text)
            continue
        layers["selected_actionable"].append(text)
    for slot, current_pose_warning in current_pose_warnings.items():
        if slot in emitted_current_pose_slots:
            continue
        accepted_current = _accepted_warning_match(current_pose_warning, selected_files_by_slot)
        if accepted_current:
            layers["accepted_review"].append(accepted_current)
        else:
            layers["selected_actionable"].append(current_pose_warning)
        emitted_current_pose_slots.add(slot)
    manifest["warning_layers"] = layers
    manifest["warning_layer_counts"] = {key: len(value) for key, value in layers.items()}
    manifest["warning_display_layers"] = {
        "selected_actionable": list(layers["selected_actionable"]),
    }
    manifest["warning_audit"] = {
        "raw_warning_count": len(raw_warnings),
        "raw_warnings": raw_warnings,
        "suppressed_layers": {
            "accepted_review": list(layers["accepted_review"]),
            "selected_expected_profile": list(layers["selected_expected_profile"]),
            "candidate_noise": list(layers["candidate_noise"]),
            "stale_pose_noise": list(layers["stale_pose_noise"]),
            "dropped_slot_noise": list(layers["dropped_slot_noise"]),
        },
        "suppressed_counts": {
            "accepted_review": len(layers["accepted_review"]),
            "selected_expected_profile": len(layers["selected_expected_profile"]),
            "candidate_noise": len(layers["candidate_noise"]),
            "stale_pose_noise": len(layers["stale_pose_noise"]),
            "dropped_slot_noise": len(layers["dropped_slot_noise"]),
        },
    }
    return layers

_build_warning_layers(manifest)

def _selected_slot_quality(manifest):
    records = []
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for slot, selection in (group.get("selected_slots") or {}).items():
            if not isinstance(selection, dict):
                continue
            pose_delta = selection.get("pose_delta") if isinstance(selection.get("pose_delta"), dict) else {}
            pair_record = {
                "group": group.get("name"),
                "slot": slot,
                "pose_delta": pose_delta,
                "before": None,
                "after": None,
                "actions": [],
            }
            for role in ("before", "after"):
                item = selection.get(role)
                if not isinstance(item, dict):
                    continue
                pair_record[role] = {
                    "name": item.get("name"),
                    "review_verdict": item.get("review_verdict"),
                    "selection_priority": item.get("selection_priority"),
                    "sharpness_score": item.get("sharpness_score"),
                    "angle_confidence": item.get("angle_confidence"),
                    "profile_fallback": item.get("profile_fallback"),
                }
                if item.get("review_verdict") == "deferred":
                    pair_record["actions"].append(f"{role}:低优先候选")
                if item.get("profile_fallback") and slot == "side":
                    pair_record["actions"].append(f"{role}:侧脸兜底")
            weighted = pose_delta.get("weighted") if isinstance(pose_delta, dict) else None
            if isinstance(weighted, (int, float)) and weighted >= 12:
                pair_record["actions"].append("姿态差需复核")
            records.append(pair_record)
    return records

manifest["selection_quality"] = _selected_slot_quality(manifest)

out_root.mkdir(parents=True, exist_ok=True)
final_path = out_root / "final-board.jpg"
manifest_path = out_root / "manifest.final.json"

if _count_renderable_slots(manifest) == 0:
    message = "没有可渲染的角度槽位：请先确认术前/术后阶段与正面/45度/侧面角度配对"
    _append_blocking_issue(manifest, message)
    manifest["render_error"] = message
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    sys.stdout.write(json.dumps(_result_payload(manifest, final_path, manifest_path, render_error=message), ensure_ascii=False))
    raise SystemExit(0)

try:
    render_module.render_from_manifest(manifest, final_path)
except ValueError as exc:
    if "没有可渲染的角度槽位" not in str(exc):
        raise
    message = "没有可渲染的角度槽位：请先确认术前/术后阶段与正面/45度/侧面角度配对"
    _append_blocking_issue(manifest, message)
    manifest["render_error"] = message
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )
    sys.stdout.write(json.dumps(_result_payload(manifest, final_path, manifest_path, render_error=message), ensure_ascii=False))
    raise SystemExit(0)

_apply_composition_alert_layers(manifest)

manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, default=str, indent=2),
    encoding="utf-8",
)

# Compact summary returned to parent.
result = _result_payload(manifest, final_path, manifest_path, output_path=final_path)
sys.stdout.write(json.dumps(result, ensure_ascii=False))
"""


def _build_manual_preview_runner() -> str:
    """Inline subprocess script for one-view manual preview rendering."""
    return r"""
import importlib.util
import json
import sys
from pathlib import Path

skill_script_path = Path(sys.argv[1])
render_script_path = Path(sys.argv[2])
payload = json.loads(sys.argv[3])

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

case_layout = _load("case_layout_board", skill_script_path)
render_module = _load("render_brand_clean", render_script_path)

case_dir = Path(payload["case_dir"])
preview_dir = Path(payload["preview_dir"])
brand = case_layout.resolve_brand(payload.get("brand") or "fumei")
view = payload.get("view") or "front"
before_path = str(Path(payload["before_path"]))
after_path = str(Path(payload["after_path"]))
before_transform = payload.get("before_transform")
out_path = preview_dir / "preview.jpg"
manifest_path = preview_dir / "manifest.preview.json"

before_entry = {
    "name": Path(before_path).name,
    "path": before_path,
    "phase": "before",
    "phase_source": "manual",
    "angle": view,
    "angle_source": "manual",
    "view": {"bucket": view, "confidence": 1.0},
}
if isinstance(before_transform, dict):
    before_entry["manual_transform"] = before_transform
    before_entry["manual_transform_source"] = "manual-preview"

after_entry = {
    "name": Path(after_path).name,
    "path": after_path,
    "phase": "after",
    "phase_source": "manual",
    "angle": view,
    "angle_source": "manual",
    "view": {"bucket": view, "confidence": 1.0},
}

manifest = {
    "status": "ok",
    "case_mode": "face",
    "case_dir": str(case_dir),
    "brand": brand,
    "template": "manual-preview",
    "effective_templates": ["single-compare"],
    "warnings": [],
    "blocking_issues": [],
    "groups": [
        {
            "name": case_dir.name,
            "render_slots": [view],
            "selected_slots": {
                view: {
                    "before": before_entry,
                    "after": after_entry,
                }
            },
        }
    ],
}

preview_dir.mkdir(parents=True, exist_ok=True)
meta = render_module.resolve_meta(manifest)
protection_targets = render_module.collect_protection_targets(manifest, meta)
render_plan_records = []
before_img, after_img = render_module.render_aligned_pair(
    before_path,
    [after_path],
    (516, 624),
    view,
    allow_direction_mismatch=True,
    protection_targets=protection_targets,
    render_plan_records=render_plan_records,
)
before_img, manual_transform_record = render_module.apply_manual_preop_transform(before_img, before_transform)
if manual_transform_record.get("enabled"):
    render_module.attach_manual_preop_transform_record(
        render_plan_records,
        view,
        before_path,
        after_path,
        manual_transform_record,
    )
manifest["render_plan"] = {
    "version": 1,
    "renderer": "render_brand_clean",
    "mode": "manual_preview_single_slot",
    "alignment_policy": "protected_region_first_when_targeted",
    "protection_targets": protection_targets,
    "slots": render_plan_records,
}

Image = render_module.Image
ImageDraw = render_module.ImageDraw
bg = (244, 238, 231)
panel = (253, 250, 246)
ink = (56, 49, 43)
green = (132, 154, 98)
soft_green = (226, 235, 216)
outline_color = (227, 218, 209)
gap = 34
pad = 24
label_h = 34
canvas_w = before_img.width + after_img.width + gap + pad * 2
canvas_h = before_img.height + label_h + pad * 2 + 18
canvas = Image.new("RGB", (canvas_w, canvas_h), bg)
draw = ImageDraw.Draw(canvas)
draw.rounded_rectangle((0, 0, canvas_w - 1, canvas_h - 1), radius=18, fill=panel, outline=outline_color, width=2)
label_font = render_module.CASE_LAYOUT.load_font(24, bold=True)
left_x = pad
right_x = left_x + before_img.width + gap
label_y = pad
draw.rounded_rectangle((left_x, label_y, left_x + before_img.width, label_y + label_h), radius=10, fill=(245, 240, 234))
draw.rounded_rectangle((right_x, label_y, right_x + after_img.width, label_y + label_h), radius=10, fill=soft_green)
for x0, w, label, fill in ((left_x, before_img.width, "术前", ink), (right_x, after_img.width, "术后", green)):
    bb = render_module.CASE_LAYOUT.textbbox_with_fallback(draw, (0, 0), label, label_font, fill=fill, bold=True)
    tx = x0 + (w - (bb[2] - bb[0])) / 2
    ty = label_y + (label_h - (bb[3] - bb[1])) / 2 - 1
    render_module.CASE_LAYOUT.draw_text_with_fallback(draw, (tx, ty), label, label_font, fill, bold=True)
img_y = label_y + label_h + 14
canvas.paste(before_img, (left_x, img_y))
canvas.paste(after_img, (right_x, img_y))
canvas.save(out_path, "JPEG", quality=94)
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
sys.stdout.write(json.dumps({
    "output_path": str(out_path),
    "manifest_path": str(manifest_path),
    "render_plan": manifest.get("render_plan") or {},
    "warnings": manifest.get("warnings") or [],
}, ensure_ascii=False))
"""


def _render_subprocess_env() -> dict[str, str]:
    """Environment for the heavy skill subprocess.

    Semantic helpers improve classification, but a slow helper cannot be
    allowed to consume the whole formal render. These short defaults remain
    overrideable for dedicated deep runs.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONIOENCODING": "utf-8",
        "CASE_LAYOUT_FEISHU_COMPACT": "0",
        "CASE_LAYOUT_SCREEN_TIMEOUT_SEC": os.environ.get(
            "CASE_LAYOUT_SCREEN_TIMEOUT_SEC",
            DEFAULT_SEMANTIC_SCREEN_TIMEOUT_SEC,
        ),
        "CASE_LAYOUT_PAIR_REVIEW_TIMEOUT_SEC": os.environ.get(
            "CASE_LAYOUT_PAIR_REVIEW_TIMEOUT_SEC",
            DEFAULT_SEMANTIC_PAIR_REVIEW_TIMEOUT_SEC,
        ),
        "CASE_LAYOUT_FINAL_QA_TIMEOUT_SEC": os.environ.get(
            "CASE_LAYOUT_FINAL_QA_TIMEOUT_SEC",
            DEFAULT_SEMANTIC_FINAL_QA_TIMEOUT_SEC,
        ),
    }


def _run_render_subprocess(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run render in a killable process group.

    The skill can spawn Node helpers for semantic checks. On timeout, kill the
    whole process group so helper children cannot outlive the failed job.
    """
    proc = subprocess.Popen(  # noqa: S603 - args are fully constructed locally.
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_render_subprocess_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate()
        exc.output = stdout
        exc.stdout = stdout
        exc.stderr = stderr
        raise exc
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def run_render(
    case_dir: Path | str,
    brand: str = "fumei",
    template: str = "tri-compare",
    semantic_judge: str = "auto",
    timeout: int = DEFAULT_RENDER_TIMEOUT_SEC,
    manual_overrides: dict[str, dict[str, Any]] | None = None,
    selection_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spawn system Python and run build_manifest + render_brand_clean.

    Returns a dict with output_path / manifest_path / status / blocking_issue_count /
    warning_count / case_mode / effective_templates / manual_overrides_applied.

    Stage B: `manual_overrides` is the per-image phase/view/transform dict
    `{filename: {phase, view, transform?}}`.
    The runner script mutates matching manifest entries before render runs.

    Raises:
        FileNotFoundError: case_dir or skill scripts missing.
        RuntimeError: subprocess failure or invalid output.
        subprocess.TimeoutExpired: render exceeded timeout.
    """
    case_dir = Path(case_dir).resolve()
    if not case_dir.exists() or not case_dir.is_dir():
        raise FileNotFoundError(f"case_dir not a directory: {case_dir}")
    if not SKILL_SCRIPT.exists():
        raise FileNotFoundError(f"case-layout-board skill missing at {SKILL_SCRIPT}")
    if not RENDER_SCRIPT.exists():
        raise FileNotFoundError(f"render_brand_clean.py missing at {RENDER_SCRIPT}")
    if not Path(SKILL_PYTHON).exists():
        raise FileNotFoundError(f"skill python missing: {SKILL_PYTHON}")

    out_root = stress.render_output_root(case_dir, brand, template)

    _archive_existing_final_board(out_root)

    overrides_json = json.dumps(manual_overrides or {}, ensure_ascii=False)
    selection_plan_json = json.dumps(selection_plan or {}, ensure_ascii=False)

    proc = _run_render_subprocess(
        [
            SKILL_PYTHON,
            "-c",
            _build_render_runner(),
            str(SKILL_SCRIPT),
            str(RENDER_SCRIPT),
            str(case_dir),
            brand,
            template,
            semantic_judge,
            str(out_root),
            overrides_json,
            selection_plan_json,
        ],
        timeout,
    )
    if proc.returncode != 0:
        stderr = _summarize_subprocess_error(proc.stderr, proc.stdout)
        raise RuntimeError(
            f"render subprocess exit={proc.returncode}: {stderr}"
        )
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("render subprocess produced empty output")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"render output not valid JSON: {e}; first 500 chars: {out[:500]}"
        )


def run_manual_render_preview(
    *,
    case_dir: Path | str,
    preview_dir: Path | str,
    brand: str,
    view: str,
    before_path: Path | str,
    after_path: Path | str,
    before_transform: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """Render one temporary before/after preview using the formal renderer."""
    case_dir = Path(case_dir).resolve()
    preview_dir = Path(preview_dir).resolve()
    before_path = Path(before_path).resolve()
    after_path = Path(after_path).resolve()
    if view not in {"front", "oblique", "side"}:
        raise ValueError(f"unsupported preview view: {view}")
    if not case_dir.is_dir():
        raise FileNotFoundError(f"case_dir not a directory: {case_dir}")
    if not before_path.is_file():
        raise FileNotFoundError(f"before image missing: {before_path}")
    if not after_path.is_file():
        raise FileNotFoundError(f"after image missing: {after_path}")
    if not SKILL_SCRIPT.exists():
        raise FileNotFoundError(f"case-layout-board skill missing at {SKILL_SCRIPT}")
    if not RENDER_SCRIPT.exists():
        raise FileNotFoundError(f"render_brand_clean.py missing at {RENDER_SCRIPT}")
    if not Path(SKILL_PYTHON).exists():
        raise FileNotFoundError(f"skill python missing: {SKILL_PYTHON}")

    payload = {
        "case_dir": str(case_dir),
        "preview_dir": str(preview_dir),
        "brand": brand,
        "view": view,
        "before_path": str(before_path),
        "after_path": str(after_path),
        "before_transform": before_transform,
    }
    proc = subprocess.run(
        [
            SKILL_PYTHON,
            "-c",
            _build_manual_preview_runner(),
            str(SKILL_SCRIPT),
            str(RENDER_SCRIPT),
            json.dumps(payload, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONIOENCODING": "utf-8",
            "CASE_LAYOUT_FEISHU_COMPACT": "0",
        },
    )
    if proc.returncode != 0:
        stderr = _summarize_subprocess_error(proc.stderr, proc.stdout)
        raise RuntimeError(f"manual preview subprocess exit={proc.returncode}: {stderr}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError("manual preview subprocess produced empty output")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"manual preview output not valid JSON: {e}; first 500 chars: {out[:500]}"
        )
