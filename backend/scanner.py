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

from . import source_images

# Directory roots to scan
DEFAULT_ROOTS = [
    Path("/Users/a1234/Desktop/飞书Claude/output"),
    Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)"),
]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp"}
SKIP_DIR_NAMES = {
    ".case-layout-classify",
    ".case-layout-pick",
    ".case-layout-organize",
    ".case-layout-render",
    ".case-workbench-trash",
}
SKIP_DIR_PREFIXES = (".case-layout-", ".case-workbench-", "_download-inbox", ".cache", ".DS_Store")

BODY_KEYWORDS = ["颈纹", "直角肩", "瘦肩", "身体", "颈部", "肩颈", "后背", "手背"]
LABELED_TOKENS = ("术前", "术后", "before", "after", "治疗前", "治疗后")
STAGE_DIR_TOKENS = (*LABELED_TOKENS, "术中", "pre", "post")
FRAGMENT_RE = re.compile(r"^frame_\d+", re.IGNORECASE)
DATE_TAIL_RE = re.compile(r"\d{4}[\.\-/]\d{1,2}[\.\-/]\d{1,2}.*$")
CASE_DATE_PROJECT_RE = re.compile(r"^\s*(\d{2,4})[\.\-/](\d{1,2})[\.\-/](\d{1,2})(.*)$")
EXIF_DATETIME_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime
PHASE_QA_LOW_CONFIDENCE_THRESHOLD = 0.65
PHASE_QA_SEQUENCE_CONFLICT_CAP = 0.5
PHASE_QA_VISUAL_DIFF_LOW_CAP = 0.5
PHASE_QA_VISUAL_DIFF_LOW_THRESHOLD = 0.003


@dataclass
class CandidateCaseDir:
    abs_path: Path
    image_files: list[str]
    last_modified: float


def _iter_image_files(case_dir: Path) -> list[str]:
    files: list[str] = []
    try:
        for entry in case_dir.iterdir():
            if (
                entry.is_file()
                and entry.suffix.lower() in IMAGE_EXTS
                and source_images.is_source_image_file(entry.name)
            ):
                files.append(entry.name)
    except (OSError, PermissionError):
        return []
    return sorted(files)


def _should_skip_dir(name: str) -> bool:
    if name in SKIP_DIR_NAMES:
        return True
    return any(name.startswith(p) for p in SKIP_DIR_PREFIXES)


def _is_stage_dir_name(name: str) -> bool:
    lowered = name.lower()
    return any(tok.lower() in lowered for tok in STAGE_DIR_TOKENS)


def _iter_case_image_files(case_dir: Path) -> list[str]:
    """Return source image paths relative to `case_dir`.

    Direct-image cases keep the old behaviour (`["术前.jpg"]`). Directories with
    immediate stage subdirectories (`术前/`, `术后/`, `before/`, `after/`) are
    treated as one case boundary, so files are returned as relative paths such
    as `术前/a.jpg`. Generated `.case-layout-*` trees are pruned.
    """
    direct = _iter_image_files(case_dir)
    out: list[str] = list(direct)
    try:
        stage_dirs = [
            entry
            for entry in case_dir.iterdir()
            if entry.is_dir() and not _should_skip_dir(entry.name) and _is_stage_dir_name(entry.name)
        ]
    except (OSError, PermissionError):
        return sorted(out)

    for stage_dir in stage_dirs:
        for current_dir, subdirs, files in os.walk(stage_dir, followlinks=False):
            subdirs[:] = [d for d in subdirs if not _should_skip_dir(d)]
            current_path = Path(current_dir)
            for filename in files:
                if Path(filename).suffix.lower() not in IMAGE_EXTS:
                    continue
                try:
                    rel = current_path.joinpath(filename).relative_to(case_dir)
                except ValueError:
                    continue
                if not source_images.is_source_image_file(str(rel)):
                    continue
                out.append(str(rel))
    return sorted({*out})


def _case_mtime(case_dir: Path, image_files: list[str]) -> float:
    mtimes: list[float] = []
    try:
        mtimes.append(case_dir.stat().st_mtime)
    except OSError:
        pass
    for rel in image_files:
        try:
            mtimes.append((case_dir / rel).stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0


def discover_case_dirs(roots: list[Path]) -> list[CandidateCaseDir]:
    """Find case directories.

    Old behaviour found only leaf directories with direct images. The revised
    scanner also promotes a parent directory to a case when its immediate
    children are stage folders such as `术前/术后`, preventing one clinical case
    from being split into multiple `non_labeled` rows.
    """
    candidates: list[CandidateCaseDir] = []
    seen: set[Path] = set()
    covered_stage_dirs: set[Path] = set()

    for root in roots:
        if not root.exists():
            continue
        for current_dir, subdirs, files in os.walk(root, followlinks=False):
            current_path = Path(current_dir)
            if any(current_path == p or p in current_path.parents for p in covered_stage_dirs):
                subdirs[:] = []
                continue

            # Prune: skip generated artefacts
            subdirs[:] = [d for d in subdirs if not _should_skip_dir(d)]

            # Check direct image files
            direct_images = [f for f in files if Path(f).suffix.lower() in IMAGE_EXTS]
            stage_subdirs = [d for d in subdirs if _is_stage_dir_name(d)]
            image_files = _iter_case_image_files(current_path) if (direct_images or stage_subdirs) else []
            if not image_files:
                continue
            if current_path in seen:
                continue
            mtime = _case_mtime(current_path, image_files)
            if not mtime:
                continue
            candidates.append(CandidateCaseDir(
                abs_path=current_path,
                image_files=image_files,
                last_modified=mtime,
            ))
            seen.add(current_path)
            for name in stage_subdirs:
                covered_stage_dirs.add(current_path / name)
            subdirs[:] = [d for d in subdirs if d not in stage_subdirs]
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


def _normalize_case_year(raw_year: str) -> int | None:
    try:
        year = int(raw_year)
    except ValueError:
        return None
    if year < 100:
        year += 2000
    if year < 1900 or year > 2100:
        return None
    return year


def _parse_case_date_project_segment(segment: str) -> tuple[str | None, str | None]:
    match = CASE_DATE_PROJECT_RE.match(segment)
    if not match:
        return None, None
    year = _normalize_case_year(match.group(1))
    if year is None:
        return None, None
    try:
        month = int(match.group(2))
        day = int(match.group(3))
    except ValueError:
        return None, None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None, None
    project = match.group(4).strip(" \t._-—－/，,、:：")
    return f"{year:04d}.{month:02d}.{day:02d}", project or None


def extract_case_date_project(case_dir: Path, roots: list[Path]) -> tuple[str | None, str | None]:
    """Return date/project from the segment directly below the customer folder.

    This mirrors `extract_customer_raw`: when a known root matches, the customer
    is rel.parts[0] and the date/project segment is rel.parts[1]. If the case is
    outside known roots, fall back to the current directory and then its parent,
    which covers imported libraries whose leaf dir is already the treatment
    segment.
    """
    segments: list[str] = []
    for root in roots:
        try:
            rel = case_dir.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) >= 2:
            segments.append(rel.parts[1])
        break
    if not segments:
        segments.extend([case_dir.name, case_dir.parent.name])
    for segment in segments:
        date, project = _parse_case_date_project_segment(segment)
        if date:
            return date, project
    return None, None


def _parse_exif_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except UnicodeError:
            return None
    text = str(value).strip().replace("\x00", "")
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def extract_exif_taken_at(image_path: Path) -> datetime | None:
    """Return image EXIF shooting time when available.

    This is a local deterministic signal only. Missing/unreadable EXIF must not
    fail scanning because many source archives contain stripped metadata.
    """
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            exif = image.getexif()
            for tag in EXIF_DATETIME_TAGS:
                parsed = _parse_exif_datetime(exif.get(tag))
                if parsed is not None:
                    return parsed
    except (OSError, ImportError, AttributeError, TypeError, ValueError):
        return None
    return None


def _visual_diff_ratio(before_path: Path, after_path: Path) -> float | None:
    try:
        from PIL import Image, ImageOps

        resampling = getattr(Image, "Resampling", None)
        resample = resampling.BILINEAR if resampling is not None else Image.BILINEAR
        pixels: list[list[int]] = []
        for path in (before_path, after_path):
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("L")
                image = ImageOps.fit(image, (32, 32), method=resample)
                pixels.append([int(v) for v in image.getdata()])
    except (OSError, ImportError, AttributeError, TypeError, ValueError):
        return None
    if len(pixels) != 2 or len(pixels[0]) != len(pixels[1]) or not pixels[0]:
        return None
    total = sum(abs(a - b) for a, b in zip(pixels[0], pixels[1]))
    return round(total / (len(pixels[0]) * 255.0), 6)


def _phase_qa_entry(out: dict[str, dict[str, Any]], image_path: str) -> dict[str, Any]:
    return out.setdefault(
        image_path,
        {
            "reasons": [],
            "confidence_cap": None,
            "evidence": {},
        },
    )


def _add_phase_qa_reason(
    out: dict[str, dict[str, Any]],
    image_path: str,
    reason: str,
    *,
    confidence_cap: float | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    entry = _phase_qa_entry(out, image_path)
    if reason not in entry["reasons"]:
        entry["reasons"].append(reason)
    if confidence_cap is not None:
        current = entry.get("confidence_cap")
        entry["confidence_cap"] = confidence_cap if current is None else min(float(current), confidence_cap)
    if evidence:
        entry["evidence"].update(evidence)


def assess_phase_quality(
    group_root: Path,
    observations: list[dict[str, Any]],
    *,
    low_confidence_threshold: float = PHASE_QA_LOW_CONFIDENCE_THRESHOLD,
    visual_diff_low_threshold: float = PHASE_QA_VISUAL_DIFF_LOW_THRESHOLD,
) -> dict[str, Any]:
    """Cross-check phase labels with EXIF order and before/after visual diff.

    The function only emits review signals. It never rewrites phase labels and
    never treats a clean signal as auto-approval.
    """
    by_image: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    exif_by_image: dict[str, datetime] = {}

    for obs in observations:
        image_path = str(obs.get("image_path") or "")
        phase = str(obs.get("phase") or "unknown")
        try:
            confidence = float(obs.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0

        if phase == "unknown" or confidence < low_confidence_threshold:
            _add_phase_qa_reason(
                by_image,
                image_path,
                "phase_review_required",
                evidence={"phase": phase, "confidence": round(confidence, 3)},
            )

        abs_path = group_root / image_path
        taken_at = extract_exif_taken_at(abs_path)
        if taken_at is not None:
            exif_by_image[image_path] = taken_at

    before = [obs for obs in observations if obs.get("phase") == "before"]
    after = [obs for obs in observations if obs.get("phase") == "after"]

    exif_pair_checked_count = 0
    exif_sequence_conflict_count = 0
    for before_obs in before:
        before_path = str(before_obs.get("image_path") or "")
        before_time = exif_by_image.get(before_path)
        if before_time is None:
            continue
        for after_obs in after:
            after_path = str(after_obs.get("image_path") or "")
            after_time = exif_by_image.get(after_path)
            if after_time is None:
                continue
            exif_pair_checked_count += 1
            if before_time <= after_time:
                continue
            exif_sequence_conflict_count += 1
            evidence = {
                "before_image_path": before_path,
                "after_image_path": after_path,
                "before_taken_at": before_time.isoformat(),
                "after_taken_at": after_time.isoformat(),
            }
            findings.append({"type": "phase_exif_sequence_conflict", **evidence})
            for image_path in (before_path, after_path):
                _add_phase_qa_reason(
                    by_image,
                    image_path,
                    "phase_exif_sequence_conflict",
                    confidence_cap=PHASE_QA_SEQUENCE_CONFLICT_CAP,
                    evidence=evidence,
                )

    visual_pair_checked_count = 0
    visual_diff_low_count = 0
    for before_obs in before:
        before_path = str(before_obs.get("image_path") or "")
        before_view = str(before_obs.get("view") or "unknown")
        if not before_path or before_view == "unknown":
            continue
        for after_obs in after:
            after_path = str(after_obs.get("image_path") or "")
            after_view = str(after_obs.get("view") or "unknown")
            if not after_path or before_view != after_view:
                continue
            diff = _visual_diff_ratio(group_root / before_path, group_root / after_path)
            if diff is None:
                continue
            visual_pair_checked_count += 1
            if diff >= visual_diff_low_threshold:
                continue
            visual_diff_low_count += 1
            evidence = {
                "before_image_path": before_path,
                "after_image_path": after_path,
                "view": before_view,
                "visual_diff_ratio": diff,
                "threshold": visual_diff_low_threshold,
            }
            findings.append({"type": "phase_visual_diff_too_low", **evidence})
            for image_path in (before_path, after_path):
                _add_phase_qa_reason(
                    by_image,
                    image_path,
                    "phase_visual_diff_too_low",
                    confidence_cap=PHASE_QA_VISUAL_DIFF_LOW_CAP,
                    evidence=evidence,
                )

    review_required_count = len(by_image)
    return {
        "by_image": by_image,
        "summary": {
            "image_count": len(observations),
            "review_required_count": review_required_count,
            "exif_checked_count": len(exif_by_image),
            "exif_pair_checked_count": exif_pair_checked_count,
            "exif_sequence_conflict_count": exif_sequence_conflict_count,
            "visual_pair_checked_count": visual_pair_checked_count,
            "visual_diff_low_count": visual_diff_low_count,
            "findings": findings[:50],
        },
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_case_payload(cand: CandidateCaseDir) -> dict[str, Any]:
    """Compute the inferred fields for one case directory.
    Pulled out of scan() so /api/cases/{id}/rescan can reuse it."""
    category, tier, blocking = infer_category(cand.abs_path, cand.image_files)
    from .services.procedure_region_mappings import has_immediate_visible_effect
    case_name = cand.abs_path.name
    has_effect, reason = has_immediate_visible_effect(case_name)
    if not has_effect:
        blocking.append({
            "code": "no_immediate_visible_effect",
            "files": [],
            "severity": "warn",
            "detail": reason,
        })
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

    # Re-collect source images for this case boundary. This supports both old
    # direct-image leaves and grouped stage subdirectories.
    image_files = _iter_case_image_files(case_dir)
    last_modified = _case_mtime(case_dir, image_files)
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
