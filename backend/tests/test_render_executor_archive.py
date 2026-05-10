"""Unit tests for `render_executor` archive + restore.

Targets the file-system-only paths:
  - `_archive_existing_final_board(out_root)` — copy current final-board.jpg
    to `.history/<ts>.jpg`, prune to RENDER_HISTORY_MAX_VERSIONS, return ts or None.
  - `restore_archived_final_board(out_root, archived_at)` — copy snapshot
    back to final-board.jpg AFTER auto-archiving the current one.

The mediapipe / cv2 render execution path is out of scope (other tests already
exercise the route layer + audit through monkeypatch).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from backend import render_executor


def _write_final(out_root: Path, content: bytes = b"jpeg") -> Path:
    out_root.mkdir(parents=True, exist_ok=True)
    p = out_root / "final-board.jpg"
    p.write_bytes(content)
    return p


# ----------------------------------------------------------------------
# _archive_existing_final_board
# ----------------------------------------------------------------------


def test_archive_returns_none_when_no_final_board(tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir()
    assert render_executor._archive_existing_final_board(out_root) is None


def test_archive_copies_existing_final_to_history(tmp_path):
    out_root = tmp_path / "out"
    _write_final(out_root, content=b"original")

    ts = render_executor._archive_existing_final_board(out_root)
    assert ts is not None
    snapshot = out_root / ".history" / f"{ts}.jpg"
    assert snapshot.is_file()
    assert snapshot.read_bytes() == b"original"
    # Original final-board still in place — archive is a copy, not move.
    assert (out_root / "final-board.jpg").read_bytes() == b"original"


def test_archive_ts_format_is_strict_iso_basic(tmp_path):
    """The ts must match `YYYYMMDDTHHMMSSZ` exactly so the route's regex
    `\\d{8}T\\d{6}Z` accepts it for restore.
    """
    import re

    out_root = tmp_path / "out"
    _write_final(out_root)
    ts = render_executor._archive_existing_final_board(out_root)
    assert re.fullmatch(r"\d{8}T\d{6}Z", ts) is not None


def test_archive_prunes_to_max_versions(tmp_path, monkeypatch):
    """Set MAX=3, archive 5 times → only the 3 newest kept."""
    out_root = tmp_path / "out"
    monkeypatch.setattr(render_executor, "RENDER_HISTORY_MAX_VERSIONS", 3)

    history = out_root / ".history"
    history.mkdir(parents=True, exist_ok=True)
    # Pre-populate older snapshots with monotonically-increasing names so
    # sorted(..., reverse=True) gives a deterministic prune order.
    for i in range(5):
        ts = f"2026010{i+1}T120000Z"
        (history / f"{ts}.jpg").write_bytes(b"old")

    _write_final(out_root, b"new")
    new_ts = render_executor._archive_existing_final_board(out_root)
    assert new_ts is not None

    remaining = sorted(p.name for p in history.iterdir())
    # 5 old + 1 new = 6 total before prune → keep top 3 by name desc
    assert len(remaining) == 3


# ----------------------------------------------------------------------
# restore_archived_final_board
# ----------------------------------------------------------------------


def test_restore_404_when_snapshot_missing(tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir()
    with pytest.raises(FileNotFoundError):
        render_executor.restore_archived_final_board(out_root, "20260101T120000Z")


def test_restore_copies_snapshot_back_to_final(tmp_path):
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    snapshot = history / "20260101T120000Z.jpg"
    snapshot.write_bytes(b"archived-content")

    result = render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    assert result["restored_from"] == "20260101T120000Z"
    assert (out_root / "final-board.jpg").read_bytes() == b"archived-content"


def test_restore_archives_existing_final_first(tmp_path):
    """Restore is reversible: before overwriting final-board.jpg, the current
    one must be archived too.
    """
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    (history / "20260101T120000Z.jpg").write_bytes(b"old")
    _write_final(out_root, b"current")

    result = render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    # previous_archived_at must reflect the just-archived current final
    assert result["previous_archived_at"] is not None
    archived = history / f"{result['previous_archived_at']}.jpg"
    assert archived.read_bytes() == b"current"
    # final-board now holds the restored snapshot
    assert (out_root / "final-board.jpg").read_bytes() == b"old"


def test_restore_previous_archived_at_none_when_no_current_final(tmp_path):
    """No current final → previous_archived_at is None (nothing to archive)."""
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    (history / "20260101T120000Z.jpg").write_bytes(b"snapshot")

    result = render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    assert result["previous_archived_at"] is None


def test_restore_uses_copy_not_copy2_so_mtime_is_now(tmp_path):
    """`copy` (not `copy2`) makes the restored final-board's mtime reflect the
    restore moment, not the original archive time. The route relies on this
    to invalidate the frontend's <img src> cache.
    """
    out_root = tmp_path / "out"
    history = out_root / ".history"
    history.mkdir(parents=True)
    snapshot = history / "20260101T120000Z.jpg"
    snapshot.write_bytes(b"x")
    # Backdate snapshot to a known older time
    old_time = time.time() - 86400  # 1 day ago
    import os

    os.utime(snapshot, (old_time, old_time))

    before_restore = time.time()
    render_executor.restore_archived_final_board(out_root, "20260101T120000Z")
    final_mtime = (out_root / "final-board.jpg").stat().st_mtime
    # mtime reflects the restore, not the snapshot — must be near `now`
    assert final_mtime >= before_restore


def test_render_subprocess_env_bounds_semantic_helpers(monkeypatch):
    monkeypatch.delenv("CASE_LAYOUT_SCREEN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("CASE_LAYOUT_PAIR_REVIEW_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("CASE_LAYOUT_FINAL_QA_TIMEOUT_SEC", raising=False)

    env = render_executor._render_subprocess_env()

    assert env["CASE_LAYOUT_SCREEN_TIMEOUT_SEC"] == "3"
    assert env["CASE_LAYOUT_PAIR_REVIEW_TIMEOUT_SEC"] == "8"
    assert env["CASE_LAYOUT_FINAL_QA_TIMEOUT_SEC"] == "8"

    monkeypatch.setenv("CASE_LAYOUT_SCREEN_TIMEOUT_SEC", "11")
    assert render_executor._render_subprocess_env()["CASE_LAYOUT_SCREEN_TIMEOUT_SEC"] == "11"


def test_summarize_subprocess_error_prefers_traceback_over_mediapipe_noise():
    stderr = "\n".join(
        [
            "W0000 00:00:1 face_landmarker_graph.cc:180] Sets FaceBlendshapesGraph acceleration to xnnpack by default.",
            "I0000 00:00:1 gl_context.cc:407] GL version: 2.1",
            "Traceback (most recent call last):",
            '  File "<string>", line 1, in <module>',
            "ValueError: 没有可渲染的角度槽位",
        ]
    )

    summary = render_executor._summarize_subprocess_error(stderr)

    assert summary.startswith("Traceback (most recent call last):")
    assert "ValueError: 没有可渲染的角度槽位" in summary
    assert "gl_context.cc" not in summary


def test_run_render_returns_blocked_payload_when_no_renderable_slots(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
ANGLE_SLOTS = ["front", "oblique", "side"]

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def build_manifest(case_dir, brand, template, semantic_judge_mode="off"):
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": [],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": ["group：侧面 术前术后姿态差过大(yaw=60.00, pitch=8.00, roll=8.00, weighted=72.00)，人工配对已保留该角度，需人工复核"],
        "warning_count": 1,
        "groups": [
            {
                "name": "group",
                "render_slots": ["front"],
                "selected_slots": {},
                "entries": [],
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    raise AssertionError("render_from_manifest should not run without slots")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(case_dir, brand="fumei", template="tri-compare")

    assert result["output_path"] is None
    assert result["status"] == "error"
    assert result["blocking_issue_count"] == 1
    assert "没有可渲染的角度槽位" in result["render_error"]
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "error"
    assert "没有可渲染的角度槽位" in manifest["blocking_issues"][0]


def test_run_render_applies_manual_overrides_before_pairing(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "plain-a.jpg").write_bytes(b"a")
    (case_dir / "plain-b.jpg").write_bytes(b"b")
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["front", "oblique", "side"]

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=None):
    return {
        "name": Path(file_path).name,
        "path": str(Path(file_path).resolve()),
        "relative_path": Path(file_path).name,
        "group_relative_path": Path(file_path).name,
        "phase": None,
        "phase_source": None,
        "angle": None,
        "angle_source": None,
        "angle_confidence": 0,
        "direction": None,
        "view": {},
        "pose": {},
        "crop_box": None,
        "sharpness_score": 30,
        "sharpness_level": "clear",
        "profile_fallback": None,
        "semantic_screen": None,
        "semantic_applied_fields": [],
        "issues": ["文件名缺少术前/术后关键词"],
        "rejection_reason": "phase_missing",
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    entries = [
        analyze_image(path, Path(case_dir), {}, Path(case_dir))
        for path in sorted(Path(case_dir).glob("*.jpg"))
    ]
    before = next((item for item in entries if item.get("phase") == "before" and item.get("angle") == "front"), None)
    after = next((item for item in entries if item.get("phase") == "after" and item.get("angle") == "front"), None)
    selected = {}
    if before and after:
        selected["front"] = {
            "label": "正面",
            "direction": "center",
            "before": before,
            "after": after,
        }
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"] if selected else [],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": ["group：侧面 术前术后姿态差过大(yaw=60.00, pitch=8.00, roll=8.00, weighted=72.00)，人工配对已保留该角度，需人工复核"],
        "warning_count": 1,
        "groups": [
            {
                "name": "group",
                "render_slots": ["front"],
                "selected_slots": selected,
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="auto",
        manual_overrides={
            "plain-a.jpg": {"phase": "before", "view": "front"},
            "plain-b.jpg": {"phase": "after", "view": "front"},
        },
    )

    assert result["status"] == "ok"
    assert result["output_path"]
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    group = manifest["groups"][0]
    assert "front" in group["selected_slots"]
    assert group["selected_slots"]["front"]["before"]["phase_source"] == "manual"
    assert group["selected_slots"]["front"]["after"]["phase_source"] == "manual"
    assert group["selected_slots"]["front"]["before"]["rejection_reason"] is None


def test_run_render_applies_selection_plan_to_manifest_slots(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-selection-plan"
    case_dir.mkdir()
    for name in ("auto-before.jpg", "auto-after.jpg", "better-before.jpg", "better-after.jpg"):
        (case_dir / name).write_bytes(name.encode())
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["front", "oblique", "side"]
ANGLE_LABELS = {"front": "正面", "oblique": "45°侧", "side": "侧面"}

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def compute_pose_delta(before_pose, after_pose):
    return {"yaw": 1, "pitch": 1, "roll": 0, "weighted": 2}

def derive_effective_template(selected_slots, angle_priority_profile=None):
    return ("single-compare", ["front"]) if "front" in selected_slots else (None, [])

def analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=None):
    name = Path(file_path).name
    return {
        "name": name,
        "path": str(Path(file_path).resolve()),
        "relative_path": name,
        "group_relative_path": name,
        "phase": "before" if "before" in name else "after",
        "phase_source": "filename",
        "angle": "front",
        "angle_source": "filename",
        "angle_confidence": 0.7,
        "direction": "center",
        "view": {"bucket": "front", "confidence": 0.7},
        "pose": {"yaw": 0, "pitch": 0, "roll": 0},
        "crop_box": None,
        "sharpness_score": 30,
        "sharpness_level": "clear",
        "profile_fallback": None,
        "semantic_screen": None,
        "semantic_applied_fields": [],
        "issues": [],
        "rejection_reason": None,
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    entries = [
        analyze_image(path, Path(case_dir), {}, Path(case_dir))
        for path in sorted(Path(case_dir).glob("*.jpg"))
    ]
    selected = {
        "front": {
            "label": "正面",
            "direction": "center",
            "before": next(item for item in entries if item["name"] == "auto-before.jpg"),
            "after": next(item for item in entries if item["name"] == "auto-after.jpg"),
        }
    }
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": [],
        "warning_count": 0,
        "groups": [
            {
                "name": "group",
                "render_slots": ["front"],
                "selected_slots": selected,
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="off",
        selection_plan={
            "version": 1,
            "policy": "source_selection_v1",
            "slots": {
                "front": {
                    "before": {
                        "filename": "better-before.jpg",
                        "render_filename": "better-before.jpg",
                        "phase": "before",
                        "phase_source": "manual",
                        "view": "front",
                        "view_source": "manual",
                        "selection_score": 92,
                        "selection_reasons": ["人工复核可用"],
                        "risk_level": "ok",
                    },
                    "after": {
                        "filename": "better-after.jpg",
                        "render_filename": "better-after.jpg",
                        "phase": "after",
                        "phase_source": "manual",
                        "view": "front",
                        "view_source": "manual",
                        "selection_score": 91,
                        "selection_reasons": ["人工复核可用"],
                        "risk_level": "ok",
                    },
                    "pair_quality": {"score": 95, "label": "strong", "severity": "ok"},
                }
            },
        },
    )

    assert result["status"] == "ok"
    assert result["render_selection_audit"]["applied_slots"][0]["slot"] == "front"
    assert result["render_selection_audit"]["overrode"]
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    selection = manifest["groups"][0]["selected_slots"]["front"]
    assert selection["before"]["name"] == "better-before.jpg"
    assert selection["after"]["name"] == "better-after.jpg"
    assert selection["selection_source"] == "source_selection_v1"
    assert selection["pair_quality"]["score"] == 95


def test_run_render_drops_low_value_side_slot_from_manifest(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-selection-plan-dropped-side"
    case_dir.mkdir()
    for name in (
        "front-before.jpg",
        "front-after.jpg",
        "oblique-before.jpg",
        "oblique-after.jpg",
        "side-before.jpg",
        "side-after.jpg",
    ):
        (case_dir / name).write_bytes(name.encode())
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["front", "oblique", "side"]
ANGLE_LABELS = {"front": "正面", "oblique": "45°侧", "side": "侧面"}

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def compute_pose_delta(before_pose, after_pose):
    return {"yaw": 1, "pitch": 1, "roll": 0, "weighted": 2}

def derive_effective_template(selected_slots, angle_priority_profile=None):
    slots = [slot for slot in ["front", "oblique", "side"] if slot in selected_slots]
    if len(slots) >= 3:
        return ("tri-compare", slots[:3])
    if "front" in slots and len(slots) >= 2:
        return ("bi-compare", slots[:2])
    if "front" in slots:
        return ("single-compare", ["front"])
    return (None, [])

def analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=None):
    name = Path(file_path).name
    view = "front"
    if "oblique" in name:
        view = "oblique"
    if "side" in name:
        view = "side"
    return {
        "name": name,
        "path": str(Path(file_path).resolve()),
        "relative_path": name,
        "group_relative_path": name,
        "phase": "before" if "before" in name else "after",
        "phase_source": "filename",
        "angle": view,
        "angle_source": "filename",
        "angle_confidence": 0.7,
        "direction": "center",
        "view": {"bucket": view, "confidence": 0.7},
        "pose": {"yaw": 0, "pitch": 0, "roll": 0},
        "crop_box": None,
        "sharpness_score": 30,
        "sharpness_level": "clear",
        "profile_fallback": None,
        "semantic_screen": None,
        "semantic_applied_fields": [],
        "issues": [],
        "rejection_reason": None,
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    entries = [
        analyze_image(path, Path(case_dir), {}, Path(case_dir))
        for path in sorted(Path(case_dir).glob("*.jpg"))
    ]
    selected = {}
    for slot in ["front", "oblique", "side"]:
        selected[slot] = {
            "label": ANGLE_LABELS[slot],
            "direction": "center",
            "before": next(item for item in entries if item["name"] == f"{slot}-before.jpg"),
            "after": next(item for item in entries if item["name"] == f"{slot}-after.jpg"),
        }
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["tri-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": [
            "group：侧面 术前术后姿态差过大(yaw=60.00, pitch=8.00, roll=8.00, weighted=72.00)，人工配对已保留该角度，需人工复核"
        ],
        "warning_count": 1,
        "groups": [
            {
                "name": "group",
                "render_slots": ["front", "oblique", "side"],
                "effective_template": "tri-compare",
                "selected_slots": selected,
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="off",
        selection_plan={
            "version": 1,
            "policy": "source_selection_v1",
            "slots": {
                "front": {
                    "before": {"filename": "front-before.jpg", "render_filename": "front-before.jpg"},
                    "after": {"filename": "front-after.jpg", "render_filename": "front-after.jpg"},
                    "pair_quality": {"score": 95, "label": "strong", "severity": "ok"},
                },
                "oblique": {
                    "before": {"filename": "oblique-before.jpg", "render_filename": "oblique-before.jpg"},
                    "after": {"filename": "oblique-after.jpg", "render_filename": "oblique-after.jpg"},
                    "pair_quality": {"score": 91, "label": "strong", "severity": "ok"},
                },
            },
            "dropped_slots": [
                {
                    "view": "side",
                    "reason": {"code": "low_comparison_value", "message": "侧面术前术后姿态差过大"},
                }
            ],
        },
    )

    assert result["status"] == "ok"
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    group = manifest["groups"][0]
    assert set(group["selected_slots"]) == {"front", "oblique"}
    assert group["render_slots"] == ["front", "oblique"]
    assert group["effective_template"] == "bi-compare"
    assert manifest["effective_templates"] == ["bi-compare"]
    assert manifest["render_selection_audit"]["dropped_slots"][0]["view"] == "side"
    assert manifest["warning_layers"]["selected_actionable"] == []
    assert len(manifest["warning_layers"]["dropped_slot_noise"]) == 1


def test_run_render_normalizes_side_pose_delta_and_layers_warnings(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-side-normalize"
    case_dir.mkdir()
    for name in ("side-before.jpg", "side-after.jpg", "unused.jpg"):
        (case_dir / name).write_bytes(name.encode())
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["side"]
ANGLE_LABELS = {"side": "侧面"}

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def compute_pose_delta(before_pose, after_pose):
    yaw = abs(float(before_pose.get("yaw", 0)) - float(after_pose.get("yaw", 0)))
    pitch = abs(float(before_pose.get("pitch", 0)) - float(after_pose.get("pitch", 0)))
    roll = abs(float(before_pose.get("roll", 0)) - float(after_pose.get("roll", 0)))
    return {"yaw": yaw, "pitch": pitch, "roll": roll, "weighted": yaw + pitch + roll * 0.5}

def pose_delta_within_threshold(slot, pose_delta):
    return pose_delta.get("weighted", 99) <= 16

def derive_effective_template(selected_slots, angle_priority_profile=None):
    return ("single-compare", ["side"]) if "side" in selected_slots else (None, [])

def analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=None):
    name = Path(file_path).name
    pose = {"yaw": -45, "pitch": 0, "roll": 0} if "before" in name else {"yaw": 45, "pitch": 0, "roll": 0}
    return {
        "name": name,
        "path": str(Path(file_path).resolve()),
        "relative_path": name,
        "group_relative_path": name,
        "phase": "before" if "before" in name else "after",
        "phase_source": "filename",
        "angle": "side",
        "angle_source": "filename",
        "angle_confidence": 1.0,
        "direction": "right",
        "view": {"bucket": "side", "confidence": 1.0},
        "pose": pose,
        "issues": [],
        "rejection_reason": None,
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    entries = [
        analyze_image(path, Path(case_dir), {}, Path(case_dir))
        for path in sorted(Path(case_dir).glob("*.jpg"))
    ]
    selected = {
        "side": {
            "label": "侧面",
            "direction": "right",
            "pose_delta": {"yaw": 90, "pitch": 0, "roll": 0, "weighted": 90},
            "before": next(item for item in entries if item["name"] == "side-before.jpg"),
            "after": next(item for item in entries if item["name"] == "side-after.jpg"),
        }
    }
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": [
            "group：侧面 术前术后姿态差过大(yaw=90.00, pitch=0.00, roll=0.00, weighted=90.00)，人工配对已保留该角度，需人工复核",
            "group：unused.jpg - 面部检测失败: 未检测到面部",
            "group：side-before.jpg - 正脸检测失败，已使用侧脸检测兜底: 未检测到面部",
            "group：side-after.jpg - 面部检测失败: 未检测到面部",
        ],
        "warning_count": 4,
        "groups": [
            {
                "name": "group",
                "render_slots": ["side"],
                "selected_slots": selected,
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="off",
        selection_plan={
            "version": 1,
            "policy": "source_selection_v1",
            "slots": {
                "side": {
                    "before": {"filename": "side-before.jpg", "render_filename": "side-before.jpg", "phase": "before", "view": "side"},
                    "after": {"filename": "side-after.jpg", "render_filename": "side-after.jpg", "phase": "after", "view": "side"},
                    "pair_quality": {"score": 95, "label": "strong", "severity": "ok"},
                }
            },
        },
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    pose_delta = manifest["groups"][0]["selected_slots"]["side"]["pose_delta"]
    assert pose_delta["yaw"] == 0
    assert pose_delta["normalization"] == "profile_abs_yaw_same_direction"
    layers = manifest["warning_layers"]
    assert len(layers["selected_actionable"]) == 0
    assert len(layers["stale_pose_noise"]) == 1
    assert len(layers["candidate_noise"]) == 1
    assert len(layers["selected_expected_profile"]) == 2
    assert manifest["warning_display_layers"]["selected_actionable"] == []
    assert manifest["warning_audit"]["suppressed_counts"]["stale_pose_noise"] == 1
    assert manifest["warning_audit"]["suppressed_counts"]["candidate_noise"] == 1


def test_run_render_regenerates_pose_warning_for_current_oblique_selection(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-oblique-current-warning"
    case_dir.mkdir()
    for name in ("old-before.jpg", "old-after.jpg", "new-before.jpg", "new-after.jpg"):
        (case_dir / name).write_bytes(name.encode())
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["oblique"]
ANGLE_LABELS = {"oblique": "45°侧"}

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def compute_pose_delta(before_pose, after_pose):
    yaw = abs(float(before_pose.get("yaw", 0)) - float(after_pose.get("yaw", 0)))
    pitch = abs(float(before_pose.get("pitch", 0)) - float(after_pose.get("pitch", 0)))
    roll = abs(float(before_pose.get("roll", 0)) - float(after_pose.get("roll", 0)))
    return {"yaw": yaw, "pitch": pitch, "roll": roll, "weighted": yaw + pitch + roll * 0.5}

def pose_delta_within_threshold(slot, pose_delta):
    return pose_delta.get("weighted", 99) <= 14

def derive_effective_template(selected_slots, angle_priority_profile=None):
    return ("single-compare", ["oblique"]) if "oblique" in selected_slots else (None, [])

def analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=None):
    name = Path(file_path).name
    poses = {
        "old-before.jpg": {"yaw": -42, "pitch": 5, "roll": -94},
        "old-after.jpg": {"yaw": -47, "pitch": 0, "roll": -85},
        "new-before.jpg": {"yaw": 30, "pitch": 0, "roll": 0},
        "new-after.jpg": {"yaw": 50, "pitch": 0, "roll": 0},
    }
    return {
        "name": name,
        "path": str(Path(file_path).resolve()),
        "relative_path": name,
        "group_relative_path": name,
        "phase": "before" if "before" in name else "after",
        "phase_source": "filename",
        "angle": "oblique",
        "angle_source": "filename",
        "angle_confidence": 1.0,
        "direction": "right",
        "view": {"bucket": "oblique", "confidence": 1.0},
        "pose": poses[name],
        "issues": [],
        "rejection_reason": None,
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    entries = [
        analyze_image(path, Path(case_dir), {}, Path(case_dir))
        for path in sorted(Path(case_dir).glob("*.jpg"))
    ]
    selected = {
        "oblique": {
            "label": "45°侧",
            "direction": "left",
            "pose_delta": {"yaw": 4.89, "pitch": 5.45, "roll": 8.87, "weighted": 14.78},
            "before": next(item for item in entries if item["name"] == "old-before.jpg"),
            "after": next(item for item in entries if item["name"] == "old-after.jpg"),
        }
    }
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": [
            "group：45°侧 术前术后姿态差过大(yaw=4.89, pitch=5.45, roll=8.87, weighted=14.78)，人工配对已保留该角度，需人工复核"
        ],
        "warning_count": 1,
        "groups": [
            {
                "name": "group",
                "render_slots": ["oblique"],
                "selected_slots": selected,
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="off",
        selection_plan={
            "version": 1,
            "policy": "source_selection_v1",
            "slots": {
                "oblique": {
                    "before": {"filename": "new-before.jpg", "render_filename": "new-before.jpg", "phase": "before", "view": "oblique"},
                    "after": {"filename": "new-after.jpg", "render_filename": "new-after.jpg", "phase": "after", "view": "oblique"},
                    "pair_quality": {"score": 60, "label": "review", "severity": "review"},
                }
            },
        },
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    actionable = manifest["warning_layers"]["selected_actionable"]
    assert len(actionable) == 1
    assert "new-before.jpg" in actionable[0]
    assert "new-after.jpg" in actionable[0]
    assert "weighted=20.00" in actionable[0]
    assert "weighted=14.78" in manifest["warning_audit"]["suppressed_layers"]["stale_pose_noise"][0]


def test_run_render_moves_accepted_source_group_warning_to_audit_layer(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-accepted-warning"
    case_dir.mkdir()
    for name in ("side-before.jpg", "side-after.jpg"):
        (case_dir / name).write_bytes(name.encode())
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["side"]
ANGLE_LABELS = {"side": "侧面"}

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def compute_pose_delta(before_pose, after_pose):
    return {"yaw": 0, "pitch": 0, "roll": 0, "weighted": 0}

def pose_delta_within_threshold(slot, pose_delta):
    return True

def derive_effective_template(selected_slots, angle_priority_profile=None):
    return ("single-compare", ["side"]) if "side" in selected_slots else (None, [])

def analyze_image(file_path, group_dir, hint_map, case_root, semantic_context=None):
    name = Path(file_path).name
    return {
        "name": name,
        "path": str(Path(file_path).resolve()),
        "relative_path": name,
        "group_relative_path": name,
        "phase": "before" if "before" in name else "after",
        "phase_source": "filename",
        "angle": "side",
        "angle_source": "filename",
        "angle_confidence": 1.0,
        "direction": "right",
        "view": {"bucket": "side", "confidence": 1.0},
        "pose": {"yaw": 45, "pitch": 0, "roll": 0},
        "issues": [],
        "rejection_reason": None,
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    entries = [
        analyze_image(path, Path(case_dir), {}, Path(case_dir))
        for path in sorted(Path(case_dir).glob("*.jpg"))
    ]
    selected = {
        "side": {
            "label": "侧面",
            "direction": "right",
            "pose_delta": {"yaw": 0, "pitch": 0, "roll": 0, "weighted": 0},
            "before": next(item for item in entries if item["name"] == "side-before.jpg"),
            "after": next(item for item in entries if item["name"] == "side-after.jpg"),
        }
    }
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": [
            "group：侧面 术前术后方向不一致，已按人工选择保留该角度，需人工复核"
        ],
        "warning_count": 1,
        "groups": [
            {
                "name": "group",
                "render_slots": ["side"],
                "selected_slots": selected,
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="off",
        selection_plan={
            "version": 1,
            "policy": "source_selection_v1",
            "accepted_warnings": [
                {
                    "slot": "side",
                    "code": "direction_mismatch",
                    "message_contains": "方向不一致",
                    "reviewer": "test-review",
                    "note": "真实侧面轮廓可接受",
                }
            ],
            "slots": {
                "side": {
                    "before": {"filename": "side-before.jpg", "render_filename": "side-before.jpg", "phase": "before", "view": "side"},
                    "after": {"filename": "side-after.jpg", "render_filename": "side-after.jpg", "phase": "after", "view": "side"},
                    "pair_quality": {"score": 80, "label": "review", "severity": "review"},
                }
            },
        },
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    layers = manifest["warning_layers"]
    assert layers["selected_actionable"] == []
    assert len(layers["accepted_review"]) == 1
    assert manifest["warning_audit"]["suppressed_layers"]["accepted_review"][0]["reviewer"] == "test-review"

    mismatch_result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        semantic_judge="off",
        selection_plan={
            "version": 1,
            "policy": "source_selection_v1",
            "accepted_warnings": [
                {
                    "slot": "side",
                    "code": "direction_mismatch",
                    "message_contains": "方向不一致",
                    "selected_files": ["other-before.jpg", "other-after.jpg"],
                    "reviewer": "test-review",
                    "note": "另一组配对，不应吞掉当前 warning",
                }
            ],
            "slots": {
                "side": {
                    "before": {"filename": "side-before.jpg", "render_filename": "side-before.jpg", "phase": "before", "view": "side"},
                    "after": {"filename": "side-after.jpg", "render_filename": "side-after.jpg", "phase": "after", "view": "side"},
                    "pair_quality": {"score": 80, "label": "review", "severity": "review"},
                }
            },
        },
    )

    mismatch_manifest = json.loads(Path(mismatch_result["manifest_path"]).read_text(encoding="utf-8"))
    assert mismatch_manifest["warning_layers"]["accepted_review"] == []
    assert len(mismatch_manifest["warning_layers"]["selected_actionable"]) == 1


def test_run_render_ignores_case_workbench_preview_outputs(tmp_path, monkeypatch):
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    (case_dir / "术前-正面.jpg").write_bytes(b"before")
    (case_dir / "术后-正面.jpg").write_bytes(b"after")
    preview_dir = case_dir / ".case-workbench-preview" / "abc123"
    preview_dir.mkdir(parents=True)
    (preview_dir / "preview.jpg").write_bytes(b"preview")
    old_bound_staging = case_dir / ".case-workbench-bound-render" / "job-old"
    old_bound_staging.mkdir(parents=True)
    (old_bound_staging / "术前-正面-old.jpg").write_bytes(b"old-bound-before")
    (old_bound_staging / "术后-正面-old.jpg").write_bytes(b"old-bound-after")

    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["front"]

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def is_image_file(path):
    path = Path(path)
    return path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}

def is_generated_case_layout_path(path, scan_root=None):
    return False

def _entry(path, case_dir):
    phase = "before" if "术前" in path.name else "after" if "术后" in path.name else None
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "relative_path": str(path.relative_to(case_dir)),
        "group_relative_path": str(path.relative_to(case_dir)),
        "phase": phase,
        "phase_source": "filename" if phase else None,
        "angle": "front",
        "angle_source": "filename",
        "angle_confidence": 1,
        "direction": "center",
        "view": {"bucket": "front", "confidence": 1},
        "issues": [] if phase else ["文件名缺少术前/术后关键词"],
        "rejection_reason": None if phase else "phase_missing",
    }

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    case_dir = Path(case_dir)
    entries = []
    for path in sorted(case_dir.rglob("*")):
        if is_generated_case_layout_path(path, case_dir):
            continue
        if not is_image_file(path):
            continue
        entries.append(_entry(path, case_dir))
    warnings = [f"unexpected preview source: {item['relative_path']}" for item in entries if item["name"] == "preview.jpg"]
    before = next(item for item in entries if item["phase"] == "before")
    after = next(item for item in entries if item["phase"] == "after")
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": warnings,
        "warning_count": len(warnings),
        "groups": [
            {
                "name": "group",
                "render_slots": ["front"],
                "selected_slots": {"front": {"before": before, "after": after}},
                "entries": entries,
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
def render_from_manifest(manifest, final_path):
    final_path.write_bytes(b"jpeg")
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(case_dir, brand="fumei", template="tri-compare")

    assert result["status"] == "ok"
    assert result["warning_count"] == 0
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    names = [item["name"] for item in manifest["groups"][0]["entries"]]
    assert names == ["术前-正面.jpg", "术后-正面.jpg"]

    bound_render_dir = tmp_path / "source" / ".case-workbench-bound-render" / "job-1"
    bound_render_dir.mkdir(parents=True)
    linked_source = tmp_path / "linked-source"
    linked_source.mkdir()
    (linked_source / "before.jpg").write_bytes(b"before")
    (linked_source / "after.jpg").write_bytes(b"after")
    (bound_render_dir / "case1-术前-正面.jpg").symlink_to(linked_source / "before.jpg")
    (bound_render_dir / "case2-术后-正面.jpg").symlink_to(linked_source / "after.jpg")

    bound_result = render_executor.run_render(bound_render_dir, brand="fumei", template="tri-compare")

    assert bound_result["status"] == "ok"
    bound_manifest = json.loads(Path(bound_result["manifest_path"]).read_text(encoding="utf-8"))
    bound_names = [item["name"] for item in bound_manifest["groups"][0]["entries"]]
    assert bound_names == ["case1-术前-正面.jpg", "case2-术后-正面.jpg"]


def test_run_render_side_face_detection_failure_falls_back_to_contain(tmp_path, monkeypatch):
    case_dir = tmp_path / "case-side-fallback"
    case_dir.mkdir()
    before = case_dir / "术前-侧面.jpg"
    after = case_dir / "术后-侧面.jpg"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    skill_script = tmp_path / "case_layout_board.py"
    render_script = tmp_path / "render_brand_clean.py"

    skill_script.write_text(
        """
from pathlib import Path

ANGLE_SLOTS = ["side"]
ANGLE_LABELS = {"side": "侧面"}

def resolve_brand(token):
    return {"id": token, "brand_line": "Fake Brand"}

def build_manifest(case_dir, brand, template, semantic_judge_mode="auto"):
    case_dir = Path(case_dir)
    before = case_dir / "术前-侧面.jpg"
    after = case_dir / "术后-侧面.jpg"
    return {
        "status": "ok",
        "case_mode": "face",
        "brand": brand,
        "template": template,
        "effective_templates": ["single-compare"],
        "blocking_issues": [],
        "blocking_issue_count": 0,
        "warnings": [],
        "warning_count": 0,
        "groups": [
            {
                "name": "group",
                "render_slots": ["side"],
                "selected_slots": {
                    "side": {
                        "before": {"name": before.name, "path": str(before), "phase_source": "manual", "angle_source": "manual"},
                        "after": {"name": after.name, "path": str(after), "phase_source": "manual", "angle_source": "manual"},
                    }
                },
                "entries": [],
            }
        ],
    }
""",
        encoding="utf-8",
    )
    render_script.write_text(
        """
from pathlib import Path

class _FaceAlign:
    @staticmethod
    def harmonize_pair(before_arr, after_arr):
        return before_arr, after_arr

    @staticmethod
    def lift_face_shadows(arr, slot=None):
        return arr

class _CaseLayout:
    FACE_ALIGN = _FaceAlign()

    @staticmethod
    def cv_to_pil(arr):
        return {"image": arr}

CASE_LAYOUT = _CaseLayout()

def whiten_background(img):
    return img

def render_side_profile_contain_cell(image_path, size):
    return {"path": str(image_path), "size": size}

def render_aligned_pair(*args, **kwargs):
    raise ValueError("未检测到可用侧面人脸")

def render_from_manifest(manifest, final_path):
    records = []
    manifest["render_plan"] = {"version": 1, "slots": records}
    selection = manifest["groups"][0]["selected_slots"]["side"]
    render_aligned_pair(
        selection["before"]["path"],
        [selection["after"]["path"]],
        (120, 160),
        "side",
        allow_direction_mismatch=True,
        render_plan_records=records,
    )
    final_path.write_bytes(b"jpeg")
    return final_path
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(render_executor, "SKILL_SCRIPT", skill_script)
    monkeypatch.setattr(render_executor, "RENDER_SCRIPT", render_script)
    monkeypatch.setattr(render_executor, "SKILL_PYTHON", sys.executable)

    result = render_executor.run_render(case_dir, brand="fumei", template="tri-compare")

    assert result["status"] == "ok"
    assert result["output_path"]
    assert result["composition_alerts"][0]["code"] == "side_face_alignment_fallback"
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    record = manifest["render_plan"]["slots"][0]
    assert record["strategy"] == "side_profile_contain_after_face_detection_error"
    assert record["composition_diagnostic"]["alerts"][0]["recommended_action"] == "复核侧面轮廓和术前术后构图，必要时换片"

    accepted_result = render_executor.run_render(
        case_dir,
        brand="fumei",
        template="tri-compare",
        selection_plan={
            "accepted_warnings": [
                {
                    "slot": "side",
                    "code": "side_face_alignment_fallback",
                    "message_contains": "侧面人脸检测失败",
                    "selected_files": [before.name, after.name],
                    "reviewer": "test-review",
                    "note": "真实侧面构图可接受",
                }
            ]
        },
    )
    assert accepted_result["composition_alerts"] == []
    assert accepted_result["composition_audit"]["accepted_review"][0]["code"] == "side_face_alignment_fallback"
    assert accepted_result["warning_audit"]["suppressed_counts"]["accepted_review"] == 1
