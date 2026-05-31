"""Build standalone single-image closeup artifacts for shipped board cases."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend import source_images
from backend.scripts.focal_p4_packet_builder import (
    CaseSpec,
    resolve_focus_targets,
)
from backend.scripts.single_image_packet_builder import (
    _make_enhance_fn,
    build_item,
)
from backend.services.delivery_gate import DeliverableItem

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_FRONT_TOKENS = ("front", "frontal", "正面", "正脸", "0°", "0度")
_SIDE_TOKENS = ("side", "profile", "oblique", "45", "侧", "斜")

EnhanceFn = Callable[[Path, list[str], Path], Path]


class SingleImageBuildError(RuntimeError):
    """A case cannot produce a standalone closeup; hold it, do not block boards."""


@dataclass(frozen=True)
class EnhancedAfter:
    """Staged raw/enhanced pair for one shipped case."""

    case_id: int
    customer: str
    case_name: str
    quality_score: float
    source_after_path: Path
    raw_path: Path
    enhanced_path: Path
    mask_path: Path
    judge_baseline_path: Path
    judge_candidate_path: Path
    focus_targets: tuple[str, ...]
    probes: dict | None
    prescreen: dict | None


def closeup_filename(item: DeliverableItem) -> str:
    safe_name = item.case_name.replace("/", "_").replace("\\", "_")
    return f"{safe_name}__closeup_score{int(item.quality_score)}.png"


def build_enhanced_after(
    item: DeliverableItem,
    scratch_root: Path,
    conn: sqlite3.Connection,
    *,
    enhance_fn: EnhanceFn | None = None,
) -> EnhancedAfter:
    """Stage and clarity-enhance the representative source after for `item`.

    The returned `enhanced_path` is the full-resolution PNG deliverable candidate.
    Any failure is raised as `SingleImageBuildError` so callers can hold only
    this companion artifact while leaving the already-shipped board untouched.
    """

    case_dir = _case_dir(conn, item.case_id)
    spec, selected_after = _case_spec_for_delivery(case_dir)
    if enhance_fn is None:
        enhance_fn = _make_enhance_fn("classical", classical_preset="clarity")

    try:
        packet_item = build_item(
            spec,
            arm="classical",
            scratch_root=scratch_root,
            enhance_fn=enhance_fn,
            require_enhancement=True,
            after_name=selected_after.name,
            judge_view="focal",
        )
    except Exception as exc:  # noqa: BLE001 - per-case build failures are held
        raise SingleImageBuildError(str(exc)[:500]) from exc

    baseline = packet_item.get("baseline") if isinstance(packet_item.get("baseline"), dict) else {}
    candidate = packet_item.get("candidate") if isinstance(packet_item.get("candidate"), dict) else {}
    prescreen = packet_item.get("prescreen") if isinstance(packet_item.get("prescreen"), dict) else {}

    try:
        raw_path = Path(str(baseline["full_res_path"]))
        enhanced_path = Path(str(candidate["full_res_path"]))
        judge_baseline = Path(str(baseline["source_path"]))
        judge_candidate = Path(str(candidate["source_path"]))
    except KeyError as exc:
        raise SingleImageBuildError(f"single-image packet missing path: {exc}") from exc

    mask_path = raw_path.parent / "probe_mask.png"
    if not mask_path.is_file():
        raise SingleImageBuildError(f"focus mask missing: {mask_path}")
    if not enhanced_path.is_file():
        raise SingleImageBuildError(f"enhanced image missing: {enhanced_path}")

    return EnhancedAfter(
        case_id=item.case_id,
        customer=item.customer,
        case_name=item.case_name,
        quality_score=item.quality_score,
        source_after_path=selected_after,
        raw_path=raw_path,
        enhanced_path=enhanced_path,
        mask_path=mask_path,
        judge_baseline_path=judge_baseline,
        judge_candidate_path=judge_candidate,
        focus_targets=tuple(str(t) for t in packet_item.get("focus_targets") or spec.focus_targets),
        probes=prescreen.get("probes") if isinstance(prescreen, dict) else None,
        prescreen=prescreen.get("verdict") if isinstance(prescreen, dict) else None,
    )


def _case_dir(conn: sqlite3.Connection, case_id: int) -> Path:
    row = conn.execute("SELECT abs_path FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise SingleImageBuildError(f"case #{case_id} not found")
    case_dir = Path(str(row["abs_path"] if isinstance(row, sqlite3.Row) else row[0]))
    if not case_dir.is_dir():
        raise SingleImageBuildError(f"case source directory missing: {case_dir}")
    return case_dir


def _case_spec_for_delivery(case_dir: Path) -> tuple[CaseSpec, Path]:
    image_paths = _source_image_paths(case_dir)
    after_paths = [p for p in image_paths if _phase_for(case_dir, p) == "after"]
    before_paths = [p for p in image_paths if _phase_for(case_dir, p) == "before"]
    if not after_paths:
        raise SingleImageBuildError(f"no source after image found under {case_dir}")

    focus_targets = _focus_targets(case_dir)
    if not focus_targets:
        raise SingleImageBuildError(f"no focus targets resolved from {case_dir}")

    selected_after = sorted(after_paths, key=_after_sort_key)[0]
    spec = CaseSpec(
        case_dir=case_dir,
        before_path=before_paths[0] if before_paths else selected_after,
        after_path=selected_after,
        focus_targets=focus_targets,
        image_names=[p.name for p in image_paths],
        after_names=[p.name for p in sorted(after_paths, key=_after_sort_key)],
        has_rendered_board=True,
    )
    return spec, selected_after


def _source_image_paths(case_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for entry in sorted(case_dir.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if entry.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        contextual = str(Path(case_dir.name) / entry.name)
        if (
            source_images.is_source_image_file(entry.name)
            and source_images.is_source_image_file(contextual)
        ):
            paths.append(entry)
    return paths


def _phase_for(case_dir: Path, path: Path) -> str | None:
    contextual = str(Path(case_dir.name) / path.name)
    return source_images._phase_from_filename(path.name) or source_images._phase_from_filename(contextual)


def _focus_targets(case_dir: Path) -> list[str]:
    from backend import ai_generation_adapter

    found: list[str] = []
    for part in reversed(case_dir.parts[-4:]):
        for target in resolve_focus_targets(part, ai_generation_adapter.MD_ANATOMICAL_KEYWORDS):
            if target not in found:
                found.append(target)
    return found


def _after_sort_key(path: Path) -> tuple[int, str]:
    lowered = path.name.lower()
    if any(token.lower() in lowered for token in _FRONT_TOKENS):
        rank = 0
    elif any(token.lower() in lowered for token in _SIDE_TOKENS):
        rank = 2
    else:
        rank = 1
    return (rank, path.name)
