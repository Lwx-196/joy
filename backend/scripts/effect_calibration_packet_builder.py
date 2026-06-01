"""Effect-projection calibration packet builder (anchored-simulation Phase 3.3).

The INVERSE of ``single_image_packet_builder`` (fidelity). Per effect-eligible
case, take the pre-effect (术前) photo as the baseline and produce an AI
post-procedure EFFECT projection (mask-anchored) as the candidate, then assemble
a judge packet for ``comfyui_vlm_judge_runner`` under
``judge_profile=effect_projection`` (4 evidence-anchored criteria).

This is the FIRST end-to-end wiring of
``case → effect_pairs → run_ps_model_router_after_simulation(effect) → judge``:
the production simulation path supports ``effect_pairs`` (Phase 2.2) but no
caller assembles them yet. ``_resolve_effect_pairs`` is that assembler — reused
by this harness and available to the production path later.

Two modes mirror the single-image builder:
  --stub (0-quota): candidate = raw copy of baseline. Validates packet shape +
    effect_pairs resolution + judge_profile wiring WITHOUT spending AI quota.
  real (owner gpt-image-2 quota + PS env): candidate = the production effect
    projection via run_ps_model_router_after_simulation(mode=effect_projection).

Usage:
    # 0-quota wiring dry-run (no AI, no PS env needed)
    python -m backend.scripts.effect_calibration_packet_builder \
        --stub --n 6 --output-packet /tmp/effect-cal/packet.json

    # real (owner unlocks quota): produces real effect projections
    python -m backend.scripts.effect_calibration_packet_builder \
        --n 6 --output-packet /tmp/effect-cal/packet.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from backend.scripts.focal_p4_packet_builder import (
    CaseSpec,
    discover_cases,
    select_cases,
)

DEFAULT_CASES_ROOT = (
    Path.home() / "Desktop" / "飞书Claude" / "医美资料" / "陈院案例(1)"
)
DEFAULT_BRAND = "fumei"
PACKET_SCOPE = "effect_calibration_packet_v1"
JUDGE_PROFILE = "effect_projection"

# Synthetic job_id base for scratch output dirs: negative so it never collides
# with a real DB simulation job. This harness does NOT touch the DB.
_SYNTHETIC_JOB_BASE = -920_000

# effect_projection 4 criteria (aligned with
# comfyui_vlm_judge_runner._effect_projection_prompt). The per-region do_right /
# red-line evidence rows are injected by the judge runner from the循证库 keyed by
# effect_pairs; these are only the judgment skeleton.
EFFECT_CRITERIA = [
    "effect_direction: every treated region moves toward its evidence-anchored do_right direction",
    "identity_preserved: unmistakably the SAME person — no face-shape / feature drift",
    "only_treated_regions: only treated regions changed; mask-outside == original (no smoothing/whitening elsewhere)",
    "natural_not_overdone: no over-distortion red-line from the evidence rows",
]


def _phase_fn(filename: str) -> str | None:
    from backend import source_images

    return source_images._phase_from_filename(filename)


def _anatomical_keywords() -> dict[str, Any]:
    from backend import ai_generation_adapter

    return ai_generation_adapter.MD_ANATOMICAL_KEYWORDS


def _resolve_effect_pairs(
    case_dir: Path, focus_targets: list[str]
) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    """Assemble evidence-anchored ``effect_pairs`` from the case folder name.

    ``parse_procedures`` maps the brand-tagged folder name to structured
    procedures; we keep only (project, region) pairs that have a registered
    evidence row (``effect_row``) — anything without evidence is dropped
    (反臆造 fail-closed: never invent an effect the循证库 doesn't anchor).
    """
    from backend.services import procedure_region_mappings as prm

    parsed = prm.parse_procedures(case_dir.name)
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for proc in parsed.get("procedures", []):
        project = str(proc.get("project") or "").strip()
        if not project:
            continue
        for region in proc.get("regions", []):
            key = (project, str(region))
            if key in seen:
                continue
            if prm.effect_row(project, str(region)) is not None:
                pairs.append(key)
                seen.add(key)
    return pairs, parsed


def _prepare_judge_image(
    src_path: Path, dst_path: Path, *, max_edge: int = 1536
) -> Path:
    """Bounded full-frame JPEG for the judge.

    Effect judging needs the WHOLE face: it must see both the treated-region
    effect AND mask-outside identity stability. So — unlike the fidelity focal
    crop — no crop is applied; only a long-edge bound + EXIF normalize.
    """
    from PIL import Image, ImageOps

    with Image.open(src_path) as _im:
        img = ImageOps.exif_transpose(_im).convert("RGB")
    w, h = img.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    img.save(dst_path, format="JPEG", quality=92)
    return dst_path


def _effect_project(
    baseline_path: Path,
    focus_targets: list[str],
    effect_pairs: list[tuple[str, str]],
    do_not_touch: list[str],
    *,
    job_id: int,
) -> Path:
    """Real effect projection via the production path (owner gpt-image-2 quota +
    PS env). Returns the mask-anchored result (identity locked outside treated
    regions). Raises on provider/PS failure (fail-closed — never a silent no-op).
    """
    from backend import ai_generation_adapter as aga

    result = aga.run_ps_model_router_after_simulation(
        job_id=job_id,
        after_image_path=baseline_path,
        before_image_path=None,
        focus_targets=focus_targets,
        brand=DEFAULT_BRAND,
        mode=aga.EFFECT_PROJECTION_MODE,
        effect_pairs=effect_pairs,
        do_not_touch=do_not_touch,
    )
    anchored = result.get("effect_anchored_path")
    if not anchored:
        raise RuntimeError(
            "effect projection returned no effect_anchored_path "
            f"(mode/effect_pairs not honoured?): keys={sorted(result)[:8]}"
        )
    return Path(anchored)


def build_item(
    spec: CaseSpec,
    *,
    scratch_root: Path,
    stub: bool,
    job_id: int,
) -> dict[str, Any]:
    """Stage baseline (术前) + produce an effect-projection candidate → judge item.

    Raises ``RuntimeError`` (→ build_packet drops the case) when:
      - no evidence-anchored effect_pairs resolve (反臆造: nothing to project), or
      - the real projection no-ops / fails (silent fail would yield a meaningless
        baseline==candidate judgment).
    """
    from PIL import Image, ImageOps

    effect_pairs, parsed = _resolve_effect_pairs(spec.case_dir, spec.focus_targets)
    if not effect_pairs:
        raise RuntimeError(
            f"no evidence-anchored effect_pairs for {spec.slug} "
            f"(needs_human_review={parsed.get('needs_human_review')}, "
            f"all_regions={parsed.get('all_regions')}) — refuse to project (反臆造)."
        )
    do_not_touch: list[str] = []

    stage_dir = scratch_root / spec.slug
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Baseline = pre-effect (术前) photo, EXIF-normalized once: the projection
    # emits display-oriented output, so baseline + candidate must share an
    # orientation or the "only_treated_regions" judgment is bogus.
    baseline_src = spec.before_path
    with Image.open(baseline_src) as _im:
        baseline_img = ImageOps.exif_transpose(_im).convert("RGB")
    baseline_full = stage_dir / f"baseline__{baseline_src.stem}.png"
    baseline_img.save(baseline_full, format="PNG")

    if stub:
        # 0-quota: candidate = raw copy (validates wiring/packet shape, no AI).
        candidate_full = stage_dir / f"candidate__{baseline_src.stem}.png"
        shutil.copyfile(baseline_full, candidate_full)
    else:
        produced = _effect_project(
            baseline_full, spec.focus_targets, effect_pairs, do_not_touch, job_id=job_id
        )
        if produced.read_bytes() == baseline_full.read_bytes():
            raise RuntimeError(
                f"effect projection no-op for {spec.slug} — candidate == baseline (silent fail)."
            )
        candidate_full = produced

    judge_baseline = _prepare_judge_image(baseline_full, stage_dir / "judge_baseline.jpg")
    judge_candidate = _prepare_judge_image(candidate_full, stage_dir / "judge_candidate.jpg")

    return {
        "ab_unit_id": spec.slug,
        "focus_targets": spec.focus_targets,
        "judge_profile": JUDGE_PROFILE,
        "criteria": EFFECT_CRITERIA,
        "effect_pairs": [list(p) for p in effect_pairs],
        "do_not_touch": do_not_touch,
        "view": "effect_projection_full",
        "procedures": parsed.get("procedures", []),
        "baseline": {
            "source_path": str(judge_baseline),
            "full_res_path": str(baseline_full),
            "role_note": "pre-effect (术前) original photo",
        },
        "candidate": {
            "source_path": str(judge_candidate),
            "full_res_path": str(candidate_full),
            "role_note": (
                "STUB raw copy (0-quota wiring)" if stub
                else "AI effect projection (mask-anchored, identity-locked)"
            ),
        },
    }


def build_packet(
    specs: list[CaseSpec],
    *,
    scratch_root: Path,
    stub: bool,
) -> dict[str, Any]:
    """Build one effect-projection judge item per spec; assemble the packet.

    Per-case failures (no effect_pairs / projection error) are non-fatal: the
    case is dropped + reported in ``dropped`` (no silent cap).
    """
    items: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for idx, spec in enumerate(specs):
        try:
            item = build_item(
                spec, scratch_root=scratch_root, stub=stub,
                job_id=_SYNTHETIC_JOB_BASE - idx,
            )
        except (RuntimeError, OSError) as exc:
            dropped.append({"ab_unit_id": spec.slug, "reason": str(exc)[:300]})
            print(f"  DROPPED {spec.slug}: {str(exc)[:160]}", file=sys.stderr)
            continue
        items.append(item)

    note = (
        "Effect-projection calibration (anchored-simulation Phase 3.3). "
        "baseline=术前 original, candidate="
        + (
            "STUB raw copy (0-quota wiring dry-run)." if stub
            else "AI effect projection (run_ps_model_router_after_simulation, mask-anchored)."
        )
        + f" {len(items)} items, {len(dropped)} dropped (no silent cap)."
    )
    return {
        "scope": PACKET_SCOPE,
        "judge_profile": JUDGE_PROFILE,
        "stub": stub,
        "note": note,
        "judge_item_count": len(items),
        "dropped_count": len(dropped),
        "dropped": dropped,
        "judge_items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--n", type=int, default=6)
    parser.add_argument("--scratch-root", type=Path, default=Path("/tmp/effect-cal"))
    parser.add_argument("--output-packet", type=Path, default=Path("/tmp/effect-cal/packet.json"))
    parser.add_argument(
        "--select", default=None,
        help="comma-separated substrings; keep only cases whose dir path matches ANY.",
    )
    parser.add_argument(
        "--all-cases", action="store_true",
        help="select from ALL discovered cases (default: only proven-renderable boards).",
    )
    parser.add_argument(
        "--stub", action="store_true",
        help="0-quota dry-run: candidate = raw copy. Validates effect_pairs + wiring without AI.",
    )
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args(argv)

    specs = discover_cases(args.cases_root, _anatomical_keywords(), _phase_fn)
    print(f"discovered {len(specs)} focus-eligible cases with before/after pairs", file=sys.stderr)
    pool = specs if args.all_cases else [s for s in specs if s.has_rendered_board]
    if args.select:
        needles = [t.strip() for t in args.select.split(",") if t.strip()]
        pool = [s for s in pool if any(t in str(s.case_dir) for t in needles)]
        selected = pool[: args.n]
    else:
        selected = select_cases(pool, args.n)

    # Pre-filter to evidence-anchored projectable cases (反臆造): the selected N
    # should all carry a real effect to project; report the rest (no silent cap).
    projectable: list[CaseSpec] = []
    skipped: list[dict[str, Any]] = []
    for s in selected:
        pairs, parsed = _resolve_effect_pairs(s.case_dir, s.focus_targets)
        if pairs:
            projectable.append(s)
        else:
            skipped.append({
                "slug": s.slug, "all_regions": parsed.get("all_regions"),
                "needs_human_review": parsed.get("needs_human_review"),
            })
    print(
        f"selected {len(projectable)} projectable cases "
        f"({len(skipped)} skipped: no evidence-anchored effect_pairs):",
        file=sys.stderr,
    )
    for s in projectable:
        pairs, _ = _resolve_effect_pairs(s.case_dir, s.focus_targets)
        print(f"  - {s.slug}  focus={s.focus_targets}  effect_pairs={pairs}", file=sys.stderr)
    for sk in skipped:
        print(f"  SKIP {sk['slug']}: regions={sk['all_regions']}", file=sys.stderr)

    if args.list_only:
        print(json.dumps(
            [
                {
                    "slug": s.slug, "case_dir": str(s.case_dir), "focus": s.focus_targets,
                    "effect_pairs": [list(p) for p in _resolve_effect_pairs(s.case_dir, s.focus_targets)[0]],
                }
                for s in projectable
            ],
            ensure_ascii=False, indent=2,
        ))
        return 0

    packet = build_packet(projectable, scratch_root=args.scratch_root, stub=args.stub)
    args.output_packet.parent.mkdir(parents=True, exist_ok=True)
    args.output_packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote packet ({packet['judge_item_count']} items, "
        f"{packet['dropped_count']} dropped) → {args.output_packet}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
