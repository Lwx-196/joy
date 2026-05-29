"""P4 formal-gate packet builder — focal (candidate) vs POLISH layout-render (baseline).

Builds a ``t51_blind_judge_packet_v1`` packet for the Vertex VLM judge by rendering
each case **twice** through the real layout pipeline (``render_executor.run_render``),
differing only in how the ``after`` image is enhanced beforehand:

- **Arm A (baseline)** = POLISH (``run_direct_clinical_enhancement``, external paid
  API) → layout board. This is the current md_ai/meiji_ai production default — the
  honest "what the customer gets today" product (owner decision 2026-05-29).
- **Arm B (candidate)** = FOCAL crop+composite (``run_comfyui_focal_enhance``, local
  ComfyUI MPS) → layout board. PR #41's region-aware native-res inpaint.

The judge then picks the better *product board* per case; gate = candidate
win-rate ≥ 60% over N≥10 (plan ``crisp-focal-crop.md`` §Phase 4 / §Gate).

Case sourcing (P4 T4.0 inventory): the local DB has empty ``tags_json`` and
``abs_path`` pointing at a different copy, so DB-tag routing yields 0 focal cases.
Instead we source case_dirs directly from ``incoming/无创案例库/无创注射案例库/``
(86 already-rendered before/after pairs) and derive ``focus_targets`` from the
**folder name** (the procedure description) via ``MD_ANATOMICAL_KEYWORDS``.

Originals are never mutated: each arm copies the before/after pair into an isolated
scratch case dir, enhances the copy, and renders from there (最小影响).

Runtime prerequisites for a *real* (non-stub) run, both owner-gated:
- ComfyUI on :8188 (candidate arm).
- ``PS_ENHANCE_SCRIPT`` (node) + external auth (baseline arm, PAID).

Use ``--stub-enhance`` for a 0-quota wiring dry-run: skips both enhancement calls
(copies the after image unchanged) but runs the real local layout render and
assembles a real packet — validating discovery / focus resolution / scratch
isolation / render invocation / packet shape without ComfyUI or paid calls.

CLI::

    python -m backend.scripts.focal_p4_packet_builder \
        --cases-root "incoming/无创案例库/无创注射案例库" \
        --n 12 --scratch-root /tmp/focal-p4 \
        --output-packet /tmp/focal-p4/packet.json [--stub-enhance]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Default case library root (P4 T4.0 inventory: 86 rendered before/after pairs).
DEFAULT_CASES_ROOT = (
    Path.home()
    / "Desktop"
    / "案例生成器"
    / "incoming"
    / "无创案例库"
    / "无创注射案例库"
)
DEFAULT_BRAND = "fumei"
DEFAULT_TEMPLATE = "single-compare"
PACKET_SCOPE = "t51_blind_judge_packet_v1"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


@dataclass
class CaseSpec:
    """A discovered, focus-eligible case with a before/after pair."""

    case_dir: Path
    before_path: Path
    after_path: Path
    focus_targets: list[str] = field(default_factory=list)
    # All top-level image files (every angle, both phases) so the scratch render
    # gets the same inputs the original successful render had — not just one pair.
    image_names: list[str] = field(default_factory=list)
    # The after-phase image filenames to enhance (mirrors render_queue dispatch).
    after_names: list[str] = field(default_factory=list)
    # True if the case already has a successful final-board → proven renderable.
    has_rendered_board: bool = False

    @property
    def slug(self) -> str:
        # Stable, filesystem-safe id from the two trailing path parts
        # (患者/术式) so per-arm scratch dirs don't collide.
        parts = self.case_dir.parts[-2:]
        raw = "__".join(parts) if parts else self.case_dir.name
        return "".join(c if c.isalnum() or c in "._-" else "_" for c in raw)


def resolve_focus_targets(folder_name: str, anatomical_keywords: dict[str, Any]) -> list[str]:
    """Substring-match the procedure folder name against the anatomical keywords.

    Order-preserving + deduped, matching ``render_queue``'s own derivation.
    """
    found: list[str] = []
    for key in anatomical_keywords:
        if key in folder_name and key not in found:
            found.append(key)
    return found


def _classify_phase(filename: str, phase_fn: Callable[[str], str | None]) -> str | None:
    return phase_fn(filename)


def discover_cases(
    cases_root: Path,
    anatomical_keywords: dict[str, Any],
    phase_fn: Callable[[str], str | None],
) -> list[CaseSpec]:
    """Walk ``cases_root`` for dirs holding a before+after image AND a focus-
    eligible folder name. Returns sorted, deterministic specs.
    """
    specs: list[CaseSpec] = []
    if not cases_root.is_dir():
        return specs

    for case_dir in sorted(p for p in cases_root.rglob("*") if p.is_dir()):
        # Skip render-output / hidden dirs.
        if case_dir.name.startswith(".") or any(
            part.startswith(".case-layout") for part in case_dir.parts
        ):
            continue

        before: Path | None = None
        after: Path | None = None
        image_names: list[str] = []
        after_names: list[str] = []
        for entry in sorted(case_dir.iterdir()):
            if not entry.is_file() or entry.name.startswith("."):
                continue
            if entry.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            image_names.append(entry.name)
            phase = _classify_phase(entry.name, phase_fn)
            if phase == "before" and before is None:
                before = entry
            elif phase == "after":
                after_names.append(entry.name)
                if after is None:
                    after = entry
        if before is None or after is None:
            continue

        focus = resolve_focus_targets(case_dir.name, anatomical_keywords)
        if not focus:
            continue

        has_board = bool(
            list(case_dir.glob(".case-layout-output/*/*/render/final-board.*"))
        )

        specs.append(
            CaseSpec(
                case_dir=case_dir,
                before_path=before,
                after_path=after,
                focus_targets=focus,
                image_names=image_names,
                after_names=after_names or [after.name],
                has_rendered_board=has_board,
            )
        )
    return specs


def select_cases(specs: list[CaseSpec], n: int) -> list[CaseSpec]:
    """Pick ``n`` cases maximising focus-region diversity, preferring multi-focus
    (79-style broad-union) cases that stress the residual.

    Greedy round-robin over primary focus regions so a single popular region
    (e.g. 泪沟) can't dominate the sample.
    """
    if n <= 0 or not specs:
        return []
    # Bucket by primary focus region; within a bucket, multi-focus cases first.
    buckets: dict[str, list[CaseSpec]] = {}
    for spec in specs:
        buckets.setdefault(spec.focus_targets[0], []).append(spec)
    for region in buckets:
        buckets[region].sort(key=lambda s: (-len(s.focus_targets), s.slug))

    ordered_regions = sorted(buckets, key=lambda r: (-len(buckets[r]), r))
    selected: list[CaseSpec] = []
    idx = 0
    while len(selected) < n and any(buckets.values()):
        region = ordered_regions[idx % len(ordered_regions)]
        if buckets[region]:
            selected.append(buckets[region].pop(0))
        idx += 1
        # Stop if every bucket drained.
        if not any(buckets.values()):
            break
    return selected[:n]


def _stage_arm(spec: CaseSpec, arm: str, scratch_root: Path) -> Path:
    """Copy the before/after pair into an isolated scratch case dir for ``arm``.

    Returns the scratch case dir. Originals are never touched.
    """
    scratch_case = scratch_root / arm / spec.slug
    if scratch_case.exists():
        shutil.rmtree(scratch_case)
    scratch_case.mkdir(parents=True, exist_ok=True)
    # Copy EVERY top-level image (all angles, both phases) so the render gets the
    # same inputs the original successful render had — not just one before/after.
    names = spec.image_names or [spec.before_path.name, spec.after_path.name]
    for name in names:
        src = spec.case_dir / name
        if src.is_file():
            shutil.copyfile(src, scratch_case / name)
    return scratch_case


def build_arm(
    spec: CaseSpec,
    arm: str,
    scratch_root: Path,
    *,
    brand: str,
    template: str,
    enhance_fn: Callable[[Path, str, CaseSpec], Path] | None,
    render_fn: Callable[[Path, str, str], dict[str, Any]],
) -> Path:
    """Stage → enhance the after image (unless stubbed) → real layout render.

    ``enhance_fn(after_path, arm, spec) -> enhanced_path`` mutates nothing in the
    originals; we copy its result over the scratch after image (mimicking the
    ``render_queue`` dispatch in-place replacement) before rendering.
    Returns the rendered board path.
    """
    scratch_case = _stage_arm(spec, arm, scratch_root)

    if enhance_fn is not None:
        # Enhance every after-phase image in scratch (mirrors render_queue
        # dispatch, which replaces each 'after' image in place pre-render).
        after_names = spec.after_names or [spec.after_path.name]
        for name in after_names:
            after_in_scratch = scratch_case / name
            if not after_in_scratch.is_file():
                continue
            enhanced = enhance_fn(after_in_scratch, arm, spec)
            if enhanced != after_in_scratch and Path(enhanced).is_file():
                shutil.copyfile(enhanced, after_in_scratch)

    result = render_fn(scratch_case, brand, template)
    board = result.get("output_path")
    if not board:
        raise RuntimeError(
            f"render returned no output_path for {spec.slug} arm={arm}: {result}"
        )
    board_path = Path(board)
    if not board_path.is_file():
        raise RuntimeError(
            f"render output_path does not exist for {spec.slug} arm={arm}: {board_path}"
        )
    return board_path


def build_packet(
    specs: list[CaseSpec],
    scratch_root: Path,
    *,
    brand: str,
    template: str,
    enhance_fn: Callable[[Path, str, CaseSpec], Path] | None,
    render_fn: Callable[[Path, str, str], dict[str, Any]],
    stub: bool,
) -> dict[str, Any]:
    """Render both arms for every spec and assemble the t51 judge packet.

    Per-case failures (a render that returns no board — e.g. blurry after image
    or missing angle slots) are **non-fatal**: the case is dropped, logged, and
    the build continues. Dropped cases are reported in the packet (no silent cap).
    """
    items: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for spec in specs:
        try:
            baseline_board = build_arm(
                spec, "baseline", scratch_root,
                brand=brand, template=template,
                enhance_fn=enhance_fn, render_fn=render_fn,
            )
            candidate_board = build_arm(
                spec, "candidate", scratch_root,
                brand=brand, template=template,
                enhance_fn=enhance_fn, render_fn=render_fn,
            )
        except RuntimeError as exc:
            reason = str(exc)
            # Surface the render's own error string if present.
            if "render_error" in reason:
                reason = reason.split("render_error':", 1)[-1][:200]
            dropped.append({"ab_unit_id": spec.slug, "reason": reason[:300]})
            print(f"  DROPPED {spec.slug}: {reason[:160]}", file=sys.stderr)
            continue
        items.append(
            {
                "ab_unit_id": spec.slug,
                "focus_targets": spec.focus_targets,
                "baseline": {
                    "source_path": str(baseline_board),
                    "role_note": "POLISH layout-render (current production default)",
                },
                "candidate": {
                    "source_path": str(candidate_board),
                    "role_note": "FOCAL crop+composite layout-render (PR #41)",
                },
            }
        )
    note = (
        "P4 formal gate (crisp-focal-crop). baseline=POLISH layout-render, "
        "candidate=FOCAL crop+composite layout-render. "
        + ("STUB-ENHANCE dry-run: enhancement skipped, boards from raw after." if stub
           else "Real double-arm render.")
    )
    return {
        "scope": PACKET_SCOPE,
        "note": note,
        "judge_item_count": len(items),
        "dropped_count": len(dropped),
        "dropped": dropped,
        "judge_items": items,
    }


def _make_real_enhance_fn() -> Callable[[Path, str, CaseSpec], Path]:
    """Bind the real adapters: baseline→POLISH (paid), candidate→FOCAL (ComfyUI)."""
    from backend import ai_generation_adapter

    def enhance(after_path: Path, arm: str, spec: CaseSpec) -> Path:
        if arm == "baseline":
            return ai_generation_adapter.run_direct_clinical_enhancement(
                after_path, brand=DEFAULT_BRAND, focus_targets=spec.focus_targets,
            )
        return ai_generation_adapter.run_comfyui_focal_enhance(
            after_path, focus_targets=spec.focus_targets, brand=DEFAULT_BRAND,
        )

    return enhance


def _make_real_render_fn() -> Callable[[Path, str, str], dict[str, Any]]:
    from backend import render_executor

    def render(case_dir: Path, brand: str, template: str) -> dict[str, Any]:
        return render_executor.run_render(case_dir, brand=brand, template=template)

    return render


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES_ROOT)
    parser.add_argument("--n", type=int, default=12, help="number of cases to select")
    parser.add_argument("--scratch-root", type=Path, default=Path("/tmp/focal-p4"))
    parser.add_argument("--output-packet", type=Path, default=Path("/tmp/focal-p4/packet.json"))
    parser.add_argument("--brand", default=DEFAULT_BRAND)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--stub-enhance", action="store_true",
        help="0-quota dry-run: skip enhancement (no ComfyUI / no paid API), real render only.",
    )
    parser.add_argument(
        "--list-only", action="store_true",
        help="Discover + select cases and print them; do not render.",
    )
    parser.add_argument(
        "--all-cases", action="store_true",
        help="Select from ALL discovered cases (default: only those with an "
             "existing final-board = proven renderable, which avoids blurry/"
             "missing-angle render failures).",
    )
    args = parser.parse_args(argv)

    from backend import ai_generation_adapter, source_images

    specs = discover_cases(
        args.cases_root,
        ai_generation_adapter.MD_ANATOMICAL_KEYWORDS,
        source_images._phase_from_filename,
    )
    print(f"discovered {len(specs)} focus-eligible cases with before/after pairs", file=sys.stderr)
    pool = specs if args.all_cases else [s for s in specs if s.has_rendered_board]
    if not args.all_cases:
        print(f"  {len(pool)} proven-renderable (existing final-board); "
              f"{len(specs) - len(pool)} unrendered excluded", file=sys.stderr)
    selected = select_cases(pool, args.n)
    print(f"selected {len(selected)} cases:", file=sys.stderr)
    for s in selected:
        print(f"  - {s.slug}  focus={s.focus_targets}", file=sys.stderr)

    if args.list_only:
        print(json.dumps(
            [{"slug": s.slug, "case_dir": str(s.case_dir), "focus": s.focus_targets} for s in selected],
            ensure_ascii=False, indent=2,
        ))
        return 0

    enhance_fn = None if args.stub_enhance else _make_real_enhance_fn()
    render_fn = _make_real_render_fn()

    packet = build_packet(
        selected, args.scratch_root,
        brand=args.brand, template=args.template,
        enhance_fn=enhance_fn, render_fn=render_fn, stub=args.stub_enhance,
    )
    args.output_packet.parent.mkdir(parents=True, exist_ok=True)
    args.output_packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote packet ({packet['judge_item_count']} items) → {args.output_packet}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
