"""Case library scanner — Phase 1 lite version (no mediapipe).

Inference rules (filename + directory heuristics only):
- BODY_KEYWORDS in path → body
- All files match ^frame_\\d+ → fragment_only
- Has 术前/术后/before/after named files → standard_face
- Otherwise has images → non_labeled
- No images → unsupported

build_manifest (mediapipe-heavy) is invoked on-demand from /api/cases/{id} detail.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import customer_resolver

# Directory roots to scan
DEFAULT_ROOTS = [
    Path("/Users/a1234/Desktop/飞书Claude/output"),
    Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)"),
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp"}
SKIP_DIR_NAMES = {".case-layout-classify", ".case-layout-pick", ".case-layout-organize", ".case-layout-render"}
SKIP_DIR_PREFIXES = (".case-layout-", "_download-inbox", ".cache", ".DS_Store")

BODY_KEYWORDS = ["颈纹", "直角肩", "瘦肩", "身体", "颈部", "肩颈", "后背", "手背"]
LABELED_TOKENS = ("术前", "术后", "before", "after", "治疗前", "治疗后")
FRAGMENT_RE = re.compile(r"^frame_\d+", re.IGNORECASE)
DATE_TAIL_RE = re.compile(r"\d{4}[\.\-/]\d{1,2}[\.\-/]\d{1,2}.*$")


@dataclass
class CandidateCaseDir:
    abs_path: Path
    image_files: list[str]
    last_modified: float


def _iter_image_files(case_dir: Path) -> list[str]:
    files: list[str] = []
    try:
        for entry in case_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTS:
                files.append(entry.name)
    except (OSError, PermissionError):
        return []
    return sorted(files)


def _should_skip_dir(name: str) -> bool:
    if name in SKIP_DIR_NAMES:
        return True
    return any(name.startswith(p) for p in SKIP_DIR_PREFIXES)


def discover_case_dirs(roots: list[Path]) -> list[CandidateCaseDir]:
    """Find leaf directories that contain images directly."""
    candidates: list[CandidateCaseDir] = []
    seen: set[Path] = set()

    for root in roots:
        if not root.exists():
            continue
        for current_dir, subdirs, files in os.walk(root, followlinks=False):
            current_path = Path(current_dir)

            # Prune: skip generated artefacts
            subdirs[:] = [d for d in subdirs if not _should_skip_dir(d)]

            # Check direct image files
            direct_images = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
            if not direct_images:
                continue
            if current_path in seen:
                continue
            try:
                mtime = current_path.stat().st_mtime
            except OSError:
                continue
            candidates.append(CandidateCaseDir(
                abs_path=current_path,
                image_files=sorted(direct_images),
                last_modified=mtime,
            ))
            seen.add(current_path)
    return candidates


def infer_category(
    case_dir: Path, image_files: list[str]
) -> tuple[str, str | None, list[dict[str, Any]]]:
    """Return (category, template_tier_guess, blocking_issues_v2).

    blocking_issues_v2 entries are dicts: {code, files, severity}.
    Lite scanner has no per-image visual signals (no mediapipe), so `files` is
    populated only for codes whose semantics make file bindings useful — e.g.,
    `no_labeled_sources` binds to the first few unlabeled images so the UI can
    show "rename these files first". `missing_oblique` and others stay file-less.
    """
    path_str = str(case_dir)
    blocking: list[dict[str, Any]] = []

    def add(code: str, files: list[str] | None = None, severity: str = "block") -> None:
        blocking.append({"code": code, "files": files or [], "severity": severity})

    # Body / 颈纹 / 肩
    if any(kw in path_str for kw in BODY_KEYWORDS):
        return ("body", "body-dual-compare", blocking)

    if not image_files:
        add("no_images")
        return ("unsupported", None, blocking)

    # All files frame_xxx → fragment_only
    if all(FRAGMENT_RE.search(name) for name in image_files):
        # Bind to first 5 frames so the UI can show "rename these"
        add("no_labeled_sources", files=image_files[:5])
        return ("fragment_only", None, blocking)

    has_labeled = any(any(tok in name for tok in LABELED_TOKENS) for name in image_files)
    if not has_labeled:
        # Bind to first 5 unlabeled images for the rename suggestion UI.
        add("no_labeled_sources", files=image_files[:5])
        return ("non_labeled", None, blocking)

    # Has labeled → assume standard_face, tier guessed from labeled image count
    labeled_count = sum(1 for name in image_files if any(tok in name for tok in LABELED_TOKENS))
    if labeled_count >= 6:
        tier = "tri"
    elif labeled_count >= 4:
        tier = "bi"
    elif labeled_count >= 2:
        tier = "single"
    else:
        tier = "unsupported"
        add("missing_oblique")
    return ("standard_face", tier, blocking)


def extract_customer_raw(case_dir: Path, roots: list[Path]) -> str | None:
    """Customer is the first directory level under a known root."""
    for root in roots:
        try:
            rel = case_dir.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            continue
        return parts[0]
    # Fallback: use immediate parent
    parent = case_dir.parent.name
    return parent or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_case_payload(cand: CandidateCaseDir) -> dict[str, Any]:
    """Compute the inferred fields for one case directory.
    Pulled out of scan() so /api/cases/{id}/rescan can reuse it."""
    category, tier, blocking = infer_category(cand.abs_path, cand.image_files)
    labeled_count = sum(
        1 for name in cand.image_files if any(tok in name for tok in LABELED_TOKENS)
    )
    meta = {
        "image_files": cand.image_files[:50],
        "image_count_total": len(cand.image_files),
    }
    return {
        "category": category,
        "template_tier": tier,
        "blocking_issues_json": json.dumps(blocking, ensure_ascii=False),
        "source_count": len(cand.image_files),
        "labeled_count": labeled_count,
        "meta_json": json.dumps(meta, ensure_ascii=False),
    }


def rescan_one(conn: sqlite3.Connection, case_id: int) -> dict[str, Any]:
    """Re-run the lite scanner on a single case directory and update its row.
    Caller is responsible for audit snapshotting before/after this call.
    Raises ValueError if the case isn't found or its directory is gone."""
    row = conn.execute(
        "SELECT id, abs_path FROM cases WHERE id = ?", (case_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"case {case_id} not found")
    case_dir = Path(row["abs_path"])
    if not case_dir.exists() or not case_dir.is_dir():
        raise ValueError(f"directory missing: {case_dir}")

    # Re-collect image files for this dir only
    image_files: list[str] = []
    last_modified = case_dir.stat().st_mtime
    for entry in case_dir.iterdir():
        if entry.is_file() and entry.suffix.lower() in IMAGE_EXTS:
            image_files.append(entry.name)
    cand = CandidateCaseDir(
        abs_path=case_dir,
        image_files=sorted(image_files),
        last_modified=last_modified,
    )
    payload = _build_case_payload(cand)
    last_mtime_iso = datetime.fromtimestamp(last_modified, tz=timezone.utc).isoformat()
    conn.execute(
        """UPDATE cases SET category = ?, template_tier = ?, blocking_issues_json = ?,
               source_count = ?, labeled_count = ?, meta_json = ?,
               last_modified = ?, indexed_at = ? WHERE id = ?""",
        (
            payload["category"],
            payload["template_tier"],
            payload["blocking_issues_json"],
            payload["source_count"],
            payload["labeled_count"],
            payload["meta_json"],
            last_mtime_iso,
            _now_iso(),
            case_id,
        ),
    )
    return {**payload, "last_modified": last_mtime_iso, "case_id": case_id}


def scan(conn: sqlite3.Connection, roots: list[Path] | None = None, mode: str = "incremental") -> dict[str, Any]:
    """Scan all roots and upsert into cases table.

    Returns summary dict.
    """
    started = datetime.now(timezone.utc)
    roots = roots or DEFAULT_ROOTS

    cur = conn.execute(
        "INSERT INTO scans (started_at, root_paths, mode) VALUES (?, ?, ?)",
        (started.isoformat(), json.dumps([str(r) for r in roots], ensure_ascii=False), mode),
    )
    scan_id = cur.lastrowid

    candidates = discover_case_dirs(roots)
    new_count = 0
    updated_count = 0
    skipped_count = 0

    for cand in candidates:
        abs_path_str = str(cand.abs_path)
        last_mtime_iso = datetime.fromtimestamp(cand.last_modified, tz=timezone.utc).isoformat()

        existing = conn.execute(
            "SELECT id, last_modified FROM cases WHERE abs_path = ?",
            (abs_path_str,),
        ).fetchone()

        if existing and mode == "incremental" and existing["last_modified"] == last_mtime_iso:
            skipped_count += 1
            continue

        category, tier, blocking_codes = infer_category(cand.abs_path, cand.image_files)
        customer_raw = extract_customer_raw(cand.abs_path, roots)
        labeled_count = sum(1 for name in cand.image_files if any(tok in name for tok in LABELED_TOKENS))

        meta = {
            "image_files": cand.image_files[:50],  # cap for storage
            "image_count_total": len(cand.image_files),
        }

        if existing:
            conn.execute(
                """UPDATE cases SET scan_id = ?, customer_raw = ?, category = ?, template_tier = ?,
                       blocking_issues_json = ?, source_count = ?, labeled_count = ?, meta_json = ?,
                       last_modified = ?, indexed_at = ? WHERE id = ?""",
                (scan_id, customer_raw, category, tier, json.dumps(blocking_codes, ensure_ascii=False),
                 len(cand.image_files), labeled_count, json.dumps(meta, ensure_ascii=False),
                 last_mtime_iso, _now_iso(), existing["id"]),
            )
            updated_count += 1
        else:
            conn.execute(
                """INSERT INTO cases (scan_id, abs_path, customer_raw, category, template_tier,
                       blocking_issues_json, source_count, labeled_count, meta_json,
                       last_modified, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scan_id, abs_path_str, customer_raw, category, tier,
                 json.dumps(blocking_codes, ensure_ascii=False),
                 len(cand.image_files), labeled_count, json.dumps(meta, ensure_ascii=False),
                 last_mtime_iso, _now_iso()),
            )
            new_count += 1

    completed = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE scans SET completed_at = ?, case_count = ? WHERE id = ?",
        (completed.isoformat(), new_count + updated_count + skipped_count, scan_id),
    )

    duration_ms = int((completed - started).total_seconds() * 1000)
    return {
        "scan_id": scan_id,
        "case_count": new_count + updated_count + skipped_count,
        "new_count": new_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "duration_ms": duration_ms,
        "mode": mode,
    }
