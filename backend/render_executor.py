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
    result = run_render(case_dir, brand="fumei", template="tri-compare", semantic_judge="off")
    # result keys: output_path, manifest_path, status, blocking_issue_count, manifest_summary
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Same skill paths as skill_bridge — keep in sync.
SKILL_ROOT = Path.home() / "Desktop" / "飞书Claude" / "skills" / "case-layout-board"
SKILL_SCRIPT = SKILL_ROOT / "scripts" / "case_layout_board.py"
RENDER_SCRIPT = SKILL_ROOT / "scripts" / "render_brand_clean.py"
SKILL_PYTHON = os.environ.get("CASE_LAYOUT_SKILL_PYTHON") or shutil.which("python3") or "/usr/bin/python3"

DEFAULT_RENDER_TIMEOUT_SEC = 180

# Keep at most this many archived final-board.jpg snapshots per (case, brand, template).
# LRU evicts the oldest beyond the limit so the case directory doesn't grow unbounded.
RENDER_HISTORY_MAX_VERSIONS = int(os.environ.get("RENDER_HISTORY_MAX_VERSIONS", "10"))


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
    """
    return r"""
import importlib.util
import json
import sys
from pathlib import Path

skill_script_path = Path(sys.argv[1])
render_script_path = Path(sys.argv[2])
case_dir = sys.argv[3]
brand_token = sys.argv[4]
template = sys.argv[5]
semantic_judge_mode = sys.argv[6]
out_root = Path(sys.argv[7])

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

case_layout = _load("case_layout_board", skill_script_path)
render_module = _load("render_brand_clean", render_script_path)

brand_dict = case_layout.resolve_brand(brand_token)
manifest = case_layout.build_manifest(
    Path(case_dir),
    brand_dict,
    template,
    semantic_judge_mode=semantic_judge_mode,
)

out_root.mkdir(parents=True, exist_ok=True)
final_path = out_root / "final-board.jpg"
manifest_path = out_root / "manifest.final.json"

render_module.render_from_manifest(manifest, final_path)

manifest_path.write_text(
    json.dumps(manifest, ensure_ascii=False, default=str, indent=2),
    encoding="utf-8",
)

# Compact summary returned to parent.
result = {
    "output_path": str(final_path),
    "manifest_path": str(manifest_path),
    "status": str(manifest.get("status") or ""),
    "blocking_issue_count": int(manifest.get("blocking_issue_count") or 0),
    "warning_count": int(manifest.get("warning_count") or 0),
    "case_mode": str(manifest.get("case_mode") or ""),
    "effective_templates": manifest.get("effective_templates") or [],
}
sys.stdout.write(json.dumps(result, ensure_ascii=False))
"""


def run_render(
    case_dir: Path | str,
    brand: str = "fumei",
    template: str = "tri-compare",
    semantic_judge: str = "off",
    timeout: int = DEFAULT_RENDER_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Spawn system Python and run build_manifest + render_brand_clean.

    Returns a dict with output_path / manifest_path / status / blocking_issue_count /
    warning_count / case_mode / effective_templates.

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

    out_root = case_dir / ".case-layout-output" / brand / template / "render"

    _archive_existing_final_board(out_root)

    proc = subprocess.run(
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
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "PYTHONIOENCODING": "utf-8",
            # Disable any feishu-compact marker that might bleed into stdout.
            "CASE_LAYOUT_FEISHU_COMPACT": "0",
        },
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"render subprocess exit={proc.returncode}: {proc.stderr.strip()[:500]}"
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
