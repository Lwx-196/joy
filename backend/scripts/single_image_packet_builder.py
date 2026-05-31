"""Single-image fidelity gate packet builder (L-140).

Unlike ``focal_p4_packet_builder`` (which renders each arm to a *board* and
judges the board — where the focal change is diluted to invisibility, P4 NO-GO),
this builds a **single-image** judge packet: per case, take ONE representative
after photo, enhance it at FULL resolution with a chosen fidelity arm, and emit a
``{baseline = raw after, candidate = enhanced after}`` pair for the保真-strict
VLM judge — evaluated at the photo's own presentation scale (L-139), not diluted
into a board.

Arms (``--arm``):
  ``classical``    — focal UnsharpMask (zero-AI, pure 保真).            [Phase 1]
  ``gpt-image-2``  — ``run_direct_clinical_enhancement`` (cloud, PAID). [Phase 2]
  ``sdxl``         — ``run_sdxl_light_enhance`` (local ComfyUI).        [Phase 3]

Each item carries objective fidelity probes
(``backend.services.fidelity_probes``) + a prescreen verdict, so a 磨皮/重绘 arm
is flagged (and judge quota saved) before the VLM judge runs.

Discovery / focus resolution / selection diversity are REUSED verbatim from
``focal_p4_packet_builder`` (same case library, same anatomical keywords). The
only thing dropped is the layout-render step.

Originals are never mutated: raw + enhanced both live under an isolated scratch
dir (最小影响).

CLI::

    python -m backend.scripts.single_image_packet_builder \
        --arm classical --n 12 \
        --scratch-root /tmp/single-image --output-packet /tmp/single-image/packet.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from backend.scripts.focal_p4_packet_builder import (
    CaseSpec,
    discover_cases,
    select_cases,
)

# Runbook-correct case library (the陈院案例 set the P4 gate actually used).
DEFAULT_CASES_ROOT = (
    Path.home() / "Desktop" / "飞书Claude" / "医美资料" / "陈院案例(1)"
)
DEFAULT_BRAND = "fumei"
PACKET_SCOPE = "single_image_fidelity_packet_v1"
JUDGE_PROFILE = "single_image_fidelity"
ARMS = ("classical", "gpt-image-2", "sdxl")

# 保真-strict criteria injected into every judge item (read by
# comfyui_vlm_judge_runner._judge_prompt under judge_profile=single_image_fidelity).
ARM_CRITERIA = [
    "sharpness_clarity: focal detail (pores, tear-trough, fine skin texture) is crisper and clearer",
    "texture_pore_preservation: real skin texture and pores are preserved, NOT smoothed to a plastic look",
    "blood_color_skin_tone_fidelity: natural blood-colour and skin tone preserved — no de-saturation, darkening, or colour-cast",
    "real_blemish_preservation: real blemishes / redness / marks are kept, not airbrushed away",
    "identity_preservation: unmistakably the SAME person, no AI-portrait drift",
]


def _phase_fn(filename: str):
    from backend import source_images

    return source_images._phase_from_filename(filename)


def _anatomical_keywords() -> dict[str, Any]:
    from backend import ai_generation_adapter

    return ai_generation_adapter.MD_ANATOMICAL_KEYWORDS


# enhance_fn signature: (after_path, focus_targets, out_dir) -> enhanced_path.
EnhanceFn = Callable[[Path, list[str], Path], Path]


# Judge-facing images are bounded so the VLM actually PERCEIVES the focal change.
# A full 12MP photo is internally downscaled by the judge to ~768-1024px, which
# erases native-pixel focal sharpening (the board-dilution lesson one scale up,
# L-139): the enhancement is imperceptible and judged a TIE. We therefore present
# the focus REGION (where the enhancement lives) at a size near the judge's own
# tiling resolution, so a real fidelity gain is visible. ``full`` keeps the whole
# (downscaled) face — the honest "full-face product" presentation scale.
JUDGE_MAX_EDGE = 1536


def _prepare_judge_image(
    src_path: Path, dst_path: Path, *, crop_bbox: tuple[int, int, int, int] | None, max_edge: int = JUDGE_MAX_EDGE
) -> Path:
    """Write a bounded JPEG the judge can actually perceive (optional focal crop)."""
    from PIL import Image, ImageOps

    with Image.open(src_path) as _im:
        img = ImageOps.exif_transpose(_im).convert("RGB")
    if crop_bbox is not None:
        img = img.crop(crop_bbox)
    w, h = img.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    img.save(dst_path, format="JPEG", quality=92)
    return dst_path


def _pick_after(spec: CaseSpec, after_name: str | None) -> Path:
    """Representative after image for the single-image judge.

    ``after_name`` (an exact filename) overrides; otherwise the first after the
    discovery sorted (deterministic). The focal mask is positioned by frontal
    facial ratios, so a frontal after is preferable — pass ``--after-name`` for a
    smoke when the first after is an oblique/side angle.
    """
    if after_name:
        for name in spec.image_names:
            if name == after_name:
                return spec.case_dir / name
        raise RuntimeError(f"--after-name {after_name!r} not found in {spec.slug}")
    return spec.after_path


def build_item(
    spec: CaseSpec,
    *,
    arm: str,
    scratch_root: Path,
    enhance_fn: EnhanceFn | None,
    require_enhancement: bool,
    after_name: str | None = None,
    judge_view: str = "focal",
) -> dict[str, Any]:
    """Stage raw + enhance one representative after → a single-image judge item.

    The full-resolution enhanced PNG is the eventual deliverable, but the judge
    is shown a BOUNDED image (``_prepare_judge_image``) so it can actually
    perceive the focal change:
      - ``judge_view="focal"`` (default): the focus-region crop near native res.
      - ``judge_view="full"``: the whole face downscaled (full-face product scale).
    Probes are always computed on the FULL-resolution raw/enhanced pair.

    Raises ``RuntimeError`` (→ build_packet drops the case) when the enhancement
    no-ops (silent fail) under ``require_enhancement`` — a no-op candidate would
    be byte-identical to the baseline and make a meaningless judgment.
    """
    from PIL import Image, ImageOps

    from backend.ai_generation_adapter import _focal_crop_bbox
    from backend.services.fidelity_probes import compute_fidelity_probes, prescreen_verdict
    from backend.services.focal_mask_generator import generate_focus_mask

    after_src = _pick_after(spec, after_name)
    stage_dir = scratch_root / arm / spec.slug
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    stage_dir.mkdir(parents=True, exist_ok=True)

    # Normalize EXIF orientation ONCE: the enhancement (classical_enhance) emits
    # display-oriented output, so the raw baseline, focus mask, probes, and judge
    # crops must all share that orientation. A phone photo with an EXIF rotation
    # would otherwise leave raw (stored) and enhanced (transposed) misaligned →
    # a bogus full-frame probe diff AND a misplaced focal judge crop. PNG keeps
    # the baseline lossless so probe deltas reflect the enhancement, not JPEG noise.
    with Image.open(after_src) as _im:
        raw_img = ImageOps.exif_transpose(_im).convert("RGB")
    raw_path = stage_dir / f"raw__{after_src.stem}.png"
    raw_img.save(raw_path, format="PNG")

    # Mask + focus bbox are always needed (probe region + focal judge crop).
    mask_path = generate_focus_mask(
        raw_path, spec.focus_targets, output_path=stage_dir / "probe_mask.png"
    )
    focal_bbox = _focal_crop_bbox(mask_path)

    probes: dict[str, Any] | None = None
    prescreen: dict[str, Any] | None = None

    if enhance_fn is None:
        # Stub: candidate = a raw copy (0-quota wiring dry-run). No probes.
        enhanced_path = stage_dir / f"enhanced__{after_src.stem}.png"
        shutil.copyfile(raw_path, enhanced_path)
    else:
        work_after = stage_dir / f"work__{after_src.stem}.png"
        shutil.copyfile(raw_path, work_after)
        enh_dir = stage_dir / "enh"
        enhanced = Path(enhance_fn(work_after, spec.focus_targets, enh_dir))
        no_op = enhanced == work_after or (
            enhanced.is_file()
            and work_after.is_file()
            and enhanced.read_bytes() == raw_path.read_bytes()
        )
        if require_enhancement and no_op:
            raise RuntimeError(
                f"enhancement no-op for {spec.slug} arm={arm} — candidate would "
                "equal the raw baseline (silent fail)."
            )
        enhanced_path = enhanced
        try:
            probes = compute_fidelity_probes(raw_path, enhanced_path, mask_path)
            prescreen = prescreen_verdict(probes)
        except Exception as exc:  # noqa: BLE001 — probes are advisory, never fatal
            prescreen = {"passed": None, "reasons": [f"probe error: {exc}"]}

    # Judge-facing (bounded) images at the chosen presentation scale.
    crop = focal_bbox if judge_view == "focal" else None
    judge_baseline = _prepare_judge_image(
        raw_path, stage_dir / "judge_baseline.jpg", crop_bbox=crop
    )
    judge_candidate = _prepare_judge_image(
        enhanced_path, stage_dir / "judge_candidate.jpg", crop_bbox=crop
    )

    return {
        "ab_unit_id": spec.slug,
        "focus_targets": spec.focus_targets,
        "judge_profile": JUDGE_PROFILE,
        "judge_view": judge_view,
        "criteria": ARM_CRITERIA,
        "view": f"single_image_after_{judge_view}",
        "workflow": arm,
        "focal_bbox": list(focal_bbox),
        "baseline": {
            "source_path": str(judge_baseline),
            "full_res_path": str(raw_path),
            "role_note": f"raw after photo (unenhanced original), {judge_view} view",
        },
        "candidate": {
            "source_path": str(judge_candidate),
            "full_res_path": str(enhanced_path),
            "role_note": f"{arm} fidelity-enhanced after (full-res deliverable), {judge_view} view",
        },
        "prescreen": {"probes": probes, "verdict": prescreen},
    }


def build_packet(
    specs: list[CaseSpec],
    *,
    arm: str,
    scratch_root: Path,
    enhance_fn: EnhanceFn | None,
    stub: bool,
    after_name: str | None = None,
    judge_view: str = "focal",
) -> dict[str, Any]:
    """Build a single-image judge item per spec; assemble the packet.

    Per-case failures (no-op enhancement / missing after) are non-fatal: the case
    is dropped + reported in ``dropped`` (no silent cap).
    """
    items: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for spec in specs:
        try:
            item = build_item(
                spec,
                arm=arm,
                scratch_root=scratch_root,
                enhance_fn=enhance_fn,
                require_enhancement=enhance_fn is not None and not stub,
                after_name=after_name,
                judge_view=judge_view,
            )
        except RuntimeError as exc:
            dropped.append({"ab_unit_id": spec.slug, "reason": str(exc)[:300]})
            print(f"  DROPPED {spec.slug}: {str(exc)[:160]}", file=sys.stderr)
            continue
        items.append(item)

    prescreen_pass = sum(
        1 for it in items if (it["prescreen"]["verdict"] or {}).get("passed") is True
    )
    prescreen_fail = sum(
        1 for it in items if (it["prescreen"]["verdict"] or {}).get("passed") is False
    )
    note = (
        f"Single-image fidelity gate (L-140). arm={arm}, baseline=raw after, "
        f"candidate={arm} enhanced after, judge_view={judge_view} (focal=crop near "
        f"native res so the change is perceptible; full=downscaled face). "
        f"prescreen pass/fail = {prescreen_pass}/{prescreen_fail}. "
        + ("STUB dry-run: candidate = raw copy." if stub else f"Real {arm} enhancement.")
    )
    return {
        "scope": PACKET_SCOPE,
        "arm": arm,
        "judge_view": judge_view,
        "judge_profile": JUDGE_PROFILE,
        "note": note,
        "judge_item_count": len(items),
        "dropped_count": len(dropped),
        "dropped": dropped,
        "prescreen_pass": prescreen_pass,
        "prescreen_fail": prescreen_fail,
        "judge_items": items,
    }


def _make_enhance_fn(arm: str, *, classical_preset: str = "fine") -> EnhanceFn:
    """Bind the real adapter for ``arm`` to the (after, focus, out_dir) shape."""
    if arm == "classical":
        from backend.services import classical_enhance

        def enhance(after_path: Path, focus: list[str], out_dir: Path) -> Path:
            return classical_enhance.unsharp_focal_enhance(
                after_path, focus_targets=focus, output_dir=out_dir, preset=classical_preset,
            )

        return enhance

    if arm == "gpt-image-2":
        from backend import ai_generation_adapter

        def enhance(after_path: Path, focus: list[str], out_dir: Path) -> Path:
            # POLISH path writes via the node PS script; out_dir is unused.
            return ai_generation_adapter.run_direct_clinical_enhancement(
                after_path, brand=DEFAULT_BRAND, focus_targets=focus,
            )

        return enhance

    if arm == "sdxl":
        from backend import ai_generation_adapter

        def enhance(after_path: Path, focus: list[str], out_dir: Path) -> Path:
            fn = getattr(ai_generation_adapter, "run_sdxl_light_enhance", None)
            if fn is None:
                raise RuntimeError("run_sdxl_light_enhance not implemented yet (Phase 3)")
            return fn(after_path, focus_targets=focus, output_dir=out_dir)

        return enhance

    raise ValueError(f"unknown arm: {arm}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=ARMS, default="classical")
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--n", type=int, default=12)
    parser.add_argument("--scratch-root", type=Path, default=Path("/tmp/single-image"))
    parser.add_argument("--output-packet", type=Path, default=Path("/tmp/single-image/packet.json"))
    parser.add_argument("--after-name", default=None, help="exact after filename to judge (override first-after default)")
    parser.add_argument(
        "--judge-view", choices=("focal", "full"), default="focal",
        help="focal (default): judge a near-native focus crop so the change is perceptible; "
             "full: judge the whole downscaled face (full-face product presentation scale).",
    )
    parser.add_argument(
        "--select", default=None,
        help="comma-separated substrings; keep only cases whose dir path matches ANY.",
    )
    parser.add_argument(
        "--all-cases", action="store_true",
        help="select from ALL discovered cases (default: only proven-renderable = existing board).",
    )
    parser.add_argument(
        "--stub", action="store_true",
        help="0-quota dry-run: candidate = raw copy (no enhancement). Validates wiring/packet shape.",
    )
    parser.add_argument(
        "--classical-preset", choices=("fine", "clarity"), default="fine",
        help="classical arm: fine = pore sharpen (imperceptible at downscale); "
             "clarity = scale-invariant local-contrast pop (perceptible, still 保真).",
    )
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args(argv)

    specs = discover_cases(args.cases_root, _anatomical_keywords(), _phase_fn)
    print(f"discovered {len(specs)} focus-eligible cases with before/after pairs", file=sys.stderr)
    pool = specs if args.all_cases else [s for s in specs if s.has_rendered_board]
    if args.select:
        needles = [t.strip() for t in args.select.split(",") if t.strip()]
        pool = [s for s in pool if any(t in str(s.case_dir) for t in needles)]
        print(f"  --select matched {len(pool)} cases", file=sys.stderr)
        selected = pool[: args.n]
    else:
        selected = select_cases(pool, args.n)
    print(f"selected {len(selected)} cases for arm={args.arm}:", file=sys.stderr)
    for s in selected:
        print(f"  - {s.slug}  focus={s.focus_targets}", file=sys.stderr)

    if args.list_only:
        print(json.dumps(
            [{"slug": s.slug, "case_dir": str(s.case_dir), "focus": s.focus_targets} for s in selected],
            ensure_ascii=False, indent=2,
        ))
        return 0

    enhance_fn = None if args.stub else _make_enhance_fn(args.arm, classical_preset=args.classical_preset)
    packet = build_packet(
        selected, arm=args.arm, scratch_root=args.scratch_root,
        enhance_fn=enhance_fn, stub=args.stub, after_name=args.after_name,
        judge_view=args.judge_view,
    )
    args.output_packet.parent.mkdir(parents=True, exist_ok=True)
    args.output_packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote packet ({packet['judge_item_count']} items, "
        f"prescreen {packet['prescreen_pass']}✓/{packet['prescreen_fail']}✗) → {args.output_packet}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
