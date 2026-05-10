"""No-planner 5-shot test for case 88.

Bypasses the edit-planner so that the full _POSE_ALIGNMENT_CLAUSE reaches
Tuzi directly.  If angle alignment improves AND detail copy-over disappears
the planner is the culprit.

Usage:
  python3 backend/scripts/run_no_planner_test.py

Outputs:
  /tmp/case88_no_planner/run_1/ ... /run_5/
    generated.png
    audit.json
  /tmp/case88_no_planner/compare.jpg   (6-panel: before / after / ref-board / run1..3)
  /tmp/case88_no_planner/summary.json
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

ROOT = Path("/Users/a1234/Desktop/案例生成器/case-workbench")
VENV_PY = ROOT / ".venv" / "bin" / "python"

ENHANCE_SCRIPT = Path(
    "/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_enhance.js"
)
PS_ENV_FILE = Path(
    "/Users/a1234/Desktop/飞书Claude/claude-feishu-bridge/.env"
)

CASE_DIR = Path(
    "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/小绿"
    "/25.7.2隆鼻，卧蚕，泪沟，唇"
)
AFTER_IMAGE  = CASE_DIR / "07041738_29.jpg"
BEFORE_IMAGE = CASE_DIR / "07041738_04.jpg"

MODEL   = "gpt-image-2-vip"
QUALITY = "4k"
TIMEOUT_SEC = 600

NUM_RUNS = 5

FOCUS_TARGETS = [
    "鼻山根明显抬高 5-7mm，鼻梁线条立体清晰，侧光下可见明显高光带",
    "卧蚕宽度增加 60%，呈饱满立体弧形，笑起来能看到光影段",
    "泪沟凹陷减少 80%，眼下区域几乎平整，无明显阴影",
    "唇部体积增加 50%，唇珠唇峰饱满立体，唇形更圆润，保持原唇色",
]

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers (mirrored from run_path1.py to avoid circular-import issues)
# ---------------------------------------------------------------------------


def load_env_for_subprocess() -> dict[str, str]:
    """Load .env into a subprocess-safe env dict (does not overwrite existing vars)."""
    env = os.environ.copy()
    if PS_ENV_FILE.is_file():
        for raw in PS_ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    return env


def run_enhance(
    *,
    label: str,
    image_path: Path,
    pose_refs: list[Path],
    prompt: str,
    out_dir: Path,
    no_planner: bool = False,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Call case_layout_enhance.js and persist the result.

    Adds --no-planner when *no_planner* is True.
    Returns a summary dict including 'plannerUsed' from the enhance output.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "node",
        str(ENHANCE_SCRIPT),
        "--image",   str(image_path),
        "--prompt",  prompt,
        "--quality", QUALITY,
        "--model",   MODEL,
    ]
    for ref in pose_refs:
        cmd.extend(["--pose-ref", str(ref)])
    if no_planner:
        cmd.append("--no-planner")

    last_raw: dict[str, Any] | None = None
    elapsed = 0.0

    for attempt in range(1, max_attempts + 1):
        t0 = time.time()
        logger.info("[%s] enhance.js attempt %d/%d (refs=%d, no_planner=%s)",
                    label, attempt, max_attempts, len(pose_refs), no_planner)
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            env=load_env_for_subprocess(),
            timeout=TIMEOUT_SEC,
            check=False,
        )
        elapsed = time.time() - t0
        logger.info("[%s] attempt %d done in %.1fs rc=%d",
                    label, attempt, elapsed, proc.returncode)

        if proc.returncode != 0:
            logger.error("stderr tail:\n%s", proc.stderr[-1500:])
            if attempt == max_attempts:
                raise RuntimeError(
                    f"{label}: enhance.js exit {proc.returncode} after {attempt} attempts"
                )
            time.sleep(5 * attempt)
            continue

        text = proc.stdout.strip()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            s, e = text.find("{"), text.rfind("}")
            if s < 0 or e <= s:
                if attempt == max_attempts:
                    raise
                time.sleep(5 * attempt)
                continue
            raw = json.loads(text[s : e + 1])

        last_raw = raw

        if raw.get("success") and raw.get("imagePath"):
            break

        deg = raw.get("degradations") or []
        deg_msg = ", ".join(
            f"{d.get('step')}:{d.get('error', '')}"
            for d in deg
            if isinstance(d, dict)
        )
        logger.warning("  attempt %d success=false  degradations=%s",
                       attempt, deg_msg or "n/a")
        if attempt == max_attempts:
            logger.error(json.dumps(raw, ensure_ascii=False, indent=2)[:1500])
            raise RuntimeError(
                f"{label}: enhance returned success=false after {attempt} attempts"
            )
        time.sleep(8 * attempt)

    raw = last_raw
    assert raw is not None and raw.get("success")

    src_path = Path(raw["imagePath"])
    if not src_path.is_file():
        raise RuntimeError(f"{label}: imagePath not on disk: {src_path}")

    saved = out_dir / f"generated{src_path.suffix or '.png'}"
    saved.write_bytes(src_path.read_bytes())

    planner_used_val = raw.get("plannerUsed")

    summary: dict[str, Any] = {
        "label":         label,
        "elapsed_sec":   round(elapsed, 1),
        "imagePath":     str(src_path),
        "savedPath":     str(saved),
        "model":         raw.get("model"),
        "quality":       raw.get("quality"),
        "poseRefCount":  raw.get("poseRefCount"),
        "stabilization": raw.get("stabilization"),
        "elapsedSeconds": raw.get("elapsedSeconds"),
        "plannerUsed":   planner_used_val,
        "no_planner_flag": no_planner,
    }
    (out_dir / "audit.json").write_text(
        json.dumps(
            {
                **summary,
                "editPrompt":   raw.get("editPrompt"),
                "plannedTasks": raw.get("plannedTasks"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return summary


def build_compare_image(
    *,
    panels: list[tuple[Path, str]],
    out_path: Path,
    target_h: int = 1200,
    band_h: int = 60,
    jpeg_quality: int = 90,
) -> None:
    """Build an N-panel comparison image with labelled top bands.

    *panels* is a list of (image_path, label) tuples.
    """
    import cv2
    import numpy as np

    loaded: list[Any] = []
    for img_path, lbl in panels:
        img = cv2.imread(str(img_path))
        if img is None:
            raise RuntimeError(f"compare: cannot read {img_path}")
        loaded.append((img, lbl))

    resized = []
    for img, lbl in loaded:
        h, w = img.shape[:2]
        scale = target_h / h
        nw = int(round(w * scale))
        resized.append((cv2.resize(img, (nw, target_h), interpolation=cv2.INTER_AREA), lbl))

    composed = []
    for img, lbl in resized:
        h, w = img.shape[:2]
        canvas = np.zeros((h + band_h, w, 3), dtype=np.uint8)
        canvas[band_h:, :] = img
        cv2.rectangle(canvas, (0, 0), (w, band_h), (40, 40, 40), -1)
        cv2.putText(
            canvas, lbl, (16, band_h - 18),
            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA,
        )
        composed.append(canvas)

    total_w = sum(p.shape[1] for p in composed)
    out = np.zeros((composed[0].shape[0], total_w, 3), dtype=np.uint8)
    x = 0
    for p in composed:
        w = p.shape[1]
        out[:, x : x + w] = p
        x += w

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), out, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    logger.info("Compare image saved: %s", out_path)


def get_base_prompt_from_backend() -> str:
    """Import build_after_enhancement_prompt via a subprocess to avoid path gymnastics."""
    inline = (
        "import sys, json\n"
        f"sys.path.insert(0, '{ROOT}')\n"
        "from backend.ai_generation_adapter import build_after_enhancement_prompt\n"
        f"focus = {json.dumps(FOCUS_TARGETS, ensure_ascii=False)}\n"
        f"print(build_after_enhancement_prompt(focus, [], '{MODEL}'))\n"
    )
    proc = subprocess.run(
        [str(VENV_PY), "-c", inline],
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return proc.stdout.rstrip("\n")


def find_reference_final_board() -> Path | None:
    """Find the earliest successful render_job final-board.jpg for case 88.

    Strategy:
      1. Scan simulation_jobs/ directories, read audit.json, look for
         final_board_path (populated by future render pipeline versions).
      2. Fall back to querying render_jobs DB for case_id=88 with output_path set.
      3. Fall back to the known stable path if DB is unavailable.
    """
    sim_jobs_dir = ROOT / "case-workbench-ai" / "simulation_jobs"

    # --- Strategy 1: scan simulation_jobs audit.json for final_board_path ---
    if sim_jobs_dir.is_dir():
        dirs = sorted(
            (d for d in sim_jobs_dir.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: int(d.name),
        )
        for d in dirs:
            audit_file = d / "audit.json"
            if not audit_file.is_file():
                continue
            try:
                data = json.loads(audit_file.read_text(encoding="utf-8"))
                fb = data.get("final_board_path")
                if fb and Path(fb).is_file():
                    logger.info("Found final_board_path in sim_job %s: %s", d.name, fb)
                    return Path(fb)
            except Exception:
                continue

    # --- Strategy 2: query render_jobs DB ---
    db_path = ROOT / "case-workbench.db"
    if db_path.is_file():
        try:
            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            cur.execute(
                """
                SELECT output_path FROM render_jobs
                WHERE case_id=88
                  AND status IN ('done', 'done_with_issues')
                  AND output_path IS NOT NULL
                ORDER BY id
                LIMIT 20
                """
            )
            for row in cur.fetchall():
                p = Path(row["output_path"])
                if p.is_file():
                    logger.info("Found final_board from render_jobs DB: %s", p)
                    return p
        except Exception as exc:
            logger.warning("DB query failed: %s", exc)

    # --- Strategy 3: hardcoded fallback ---
    fallback = (
        CASE_DIR
        / ".case-layout-output"
        / "fumei"
        / "tri-compare"
        / "render"
        / "final-board.jpg"
    )
    if fallback.is_file():
        logger.info("Using hardcoded fallback final-board: %s", fallback)
        return fallback

    logger.warning("Could not locate reference final-board.jpg for case 88")
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    out_root = Path("/tmp/case88_no_planner")
    out_root.mkdir(parents=True, exist_ok=True)

    logger.info("=== Case 88 no-planner 5-shot test ===")
    logger.info("AFTER  = %s", AFTER_IMAGE)
    logger.info("BEFORE = %s", BEFORE_IMAGE)

    # Verify inputs exist
    for p in (AFTER_IMAGE, BEFORE_IMAGE, ENHANCE_SCRIPT):
        if not p.exists():
            logger.error("Required path missing: %s", p)
            sys.exit(1)

    base_prompt = get_base_prompt_from_backend()
    logger.info("Base prompt length: %d chars", len(base_prompt))

    # ------------------------------------------------------------------
    # Run 5 generations with --no-planner
    # ------------------------------------------------------------------
    runs: list[dict[str, Any]] = []
    for run_idx in range(1, NUM_RUNS + 1):
        run_dir = out_root / f"run_{run_idx}"
        label = f"no-planner-run{run_idx}"
        logger.info("\n>>> Starting run %d/%d <<<", run_idx, NUM_RUNS)

        try:
            result = run_enhance(
                label=label,
                image_path=AFTER_IMAGE,
                pose_refs=[BEFORE_IMAGE],
                prompt=base_prompt,
                out_dir=run_dir,
                no_planner=True,
                max_attempts=3,
            )
        except Exception as exc:
            logger.error("Run %d failed: %s", run_idx, exc)
            runs.append({
                "run": run_idx,
                "success": False,
                "error": str(exc),
            })
            continue

        # --- Validate plannerUsed must be False ---
        planner_used = result.get("plannerUsed")
        if planner_used is True:
            logger.error(
                "FATAL: run %d returned plannerUsed=True despite --no-planner flag. "
                "Aborting with exit code 2.",
                run_idx,
            )
            sys.exit(2)

        elapsed = result.get("elapsed_sec", 0.0)
        stab = result.get("stabilization")
        logger.info(
            "Run %d done: elapsed=%.1fs  stabilization=%s  plannerUsed=%s",
            run_idx, elapsed, stab, planner_used,
        )

        runs.append({
            "run":          run_idx,
            "success":      True,
            "elapsed_sec":  elapsed,
            "stabilization": stab,
            "plannerUsed":  planner_used,
            "savedPath":    result.get("savedPath"),
            "audit_path":   str(run_dir / "audit.json"),
        })

    successful_runs = [r for r in runs if r.get("success")]
    if not successful_runs:
        logger.error("All %d runs failed. Exiting.", NUM_RUNS)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Select fastest 3 for compare panel
    # ------------------------------------------------------------------
    fastest3 = sorted(successful_runs, key=lambda r: r.get("elapsed_sec", 9999))[:3]
    fastest3_sorted_by_run = sorted(fastest3, key=lambda r: r["run"])
    logger.info(
        "Fastest 3 runs (by elapsed): %s",
        [r["run"] for r in fastest3_sorted_by_run],
    )

    # ------------------------------------------------------------------
    # Find reference final-board (job 19 baseline)
    # ------------------------------------------------------------------
    ref_board = find_reference_final_board()

    # ------------------------------------------------------------------
    # 6-panel compare
    # ------------------------------------------------------------------
    panels: list[tuple[Path, str]] = [
        (BEFORE_IMAGE, "术前 (target angle)"),
        (AFTER_IMAGE,  "原术后 (detail truth)"),
    ]

    if ref_board is not None and ref_board.is_file():
        panels.append((ref_board, "job19 final-board (planner on)"))
    else:
        logger.warning("Reference final-board not found; compare will have 5 panels instead of 6")

    for rank, run in enumerate(fastest3_sorted_by_run, start=1):
        saved = run.get("savedPath")
        if saved and Path(saved).is_file():
            label = f"no-planner run{run['run']} ({run['elapsed_sec']:.0f}s)"
            panels.append((Path(saved), label))

    compare_path = out_root / "compare.jpg"
    if len(panels) >= 2:
        try:
            build_compare_image(panels=panels, out_path=compare_path)
        except Exception as exc:
            logger.error("build_compare_image failed: %s", exc)
            compare_path = None  # type: ignore[assignment]
    else:
        logger.warning("Not enough panels to build compare image")
        compare_path = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Write summary.json
    # ------------------------------------------------------------------
    summary = {
        "case_id":     88,
        "after":       str(AFTER_IMAGE),
        "before":      str(BEFORE_IMAGE),
        "model":       MODEL,
        "quality":     QUALITY,
        "test":        "5-shot no-planner",
        "num_runs":    NUM_RUNS,
        "runs":        runs,
        "fastest3_run_indices": [r["run"] for r in fastest3_sorted_by_run],
        "reference_board": str(ref_board) if ref_board else None,
        "compare":     str(compare_path) if compare_path else None,
    }
    summary_path = out_root / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("\nDone. Summary written to: %s", summary_path)
    if compare_path:
        logger.info("Compare image: %s", compare_path)


if __name__ == "__main__":
    main()
