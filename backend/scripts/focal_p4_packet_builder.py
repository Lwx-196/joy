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

    The scratch must replicate the original two-level ``<患者>/<术式>`` structure:
    the layout render derives the board header from BOTH the procedure dir name
    (术式 title) AND the parent dir name (the patient identifier). Two confounds
    the VLM judge penalised on the candidate (P4 N=1 first-signal, 2026-05-29):
      1. slug leaf ('…卧蚕_泪沟') → underscores/"raw system labels" in the title;
      2. flat dir → lost patient name → "generic placeholder" identifier.
    So mirror ``scratch/<arm>/<patient>/<procedure>`` to match the baseline board.
    """
    patient = spec.case_dir.parent.name or "case"
    procedure = spec.case_dir.name
    scratch_case = scratch_root / arm / patient / procedure
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


def find_existing_board(
    spec: CaseSpec, brand: str, template: str
) -> Path | None:
    """The case's already-shipped product board = the 0-quota baseline.

    Prefer the brand/template the candidate renders into; fall back to any
    existing final-board so a case rendered under a different brand still
    yields a baseline (the judge compares product-vs-product, not paths).
    """
    exact = list(
        spec.case_dir.glob(f".case-layout-output/{brand}/{template}/render/final-board.*")
    )
    if exact and exact[0].is_file():
        return exact[0]
    any_board = sorted(spec.case_dir.glob(".case-layout-output/*/*/render/final-board.*"))
    return any_board[0] if any_board and any_board[0].is_file() else None


def detect_existing_render(spec: CaseSpec) -> dict[str, Any] | None:
    """From the case's shipped board, recover (board, brand, template, the exact
    after images the render selected).

    The candidate MUST render with the SAME brand/template (single- vs
    tri-compare differ in layout → otherwise a confound) and need only focal the
    after images actually composed into the board (manifest ``selected_slots``),
    not all 5–10 — the others are never shown.
    """
    boards = sorted(spec.case_dir.glob(".case-layout-output/*/*/render/final-board.*"))
    board = next((b for b in boards if b.is_file()), None)
    if board is None:
        return None
    # path: .../.case-layout-output/<brand>/<template>/render/final-board.*
    parts = board.parts
    try:
        brand = parts[-4]
        template = parts[-3]
    except IndexError:
        return None
    after_names: list[str] = []
    manifest = board.parent / "manifest.final.json"
    if manifest.is_file():
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
            for group in m.get("groups", []) or []:
                for slot in (group.get("selected_slots") or {}).values():
                    after = (slot or {}).get("after") or {}
                    name = after.get("name") or after.get("render_filename")
                    if name and name not in after_names:
                        after_names.append(name)
        except (ValueError, OSError):
            after_names = []
    if not after_names:
        after_names = spec.after_names or [spec.after_path.name]
    return {"board": board, "brand": brand, "template": template, "after_names": after_names}


def build_arm(
    spec: CaseSpec,
    arm: str,
    scratch_root: Path,
    *,
    brand: str,
    template: str,
    enhance_fn: Callable[[Path, str, CaseSpec], Path] | None,
    render_fn: Callable[[Path, str, str], dict[str, Any]],
    require_enhancement: bool = False,
    after_names_override: list[str] | None = None,
) -> Path:
    """Stage → enhance the after image (unless stubbed) → real layout render.

    ``enhance_fn(after_path, arm, spec) -> enhanced_path`` mutates nothing in the
    originals; we copy its result over the scratch after image (mimicking the
    ``render_queue`` dispatch in-place replacement) before rendering.
    Returns the rendered board path.

    ``after_names_override``: enhance only these after images (the ones the
    shipped board actually composed, per manifest ``selected_slots``) instead of
    every after in the dir — avoids focal-ing 5–10 images when the board shows 1–3.

    ``require_enhancement``: if True (the candidate FOCAL arm), raise when the
    enhancement no-ops on EVERY after image — FOCAL's K-1 contract returns the
    input path on silent failure (e.g. ComfyUI down), which would otherwise
    silently produce a candidate board identical to the raw input and a
    meaningless gate. Fail loud so build_packet drops the case.
    """
    scratch_case = _stage_arm(spec, arm, scratch_root)

    if enhance_fn is not None:
        # Enhance only the after images the shipped board composed (or every
        # after as a fallback) — mirrors render_queue dispatch in-place replace.
        after_names = after_names_override or spec.after_names or [spec.after_path.name]
        enhanced_count = 0
        for name in after_names:
            after_in_scratch = scratch_case / name
            if not after_in_scratch.is_file():
                continue
            enhanced = enhance_fn(after_in_scratch, arm, spec)
            if enhanced != after_in_scratch and Path(enhanced).is_file():
                shutil.copyfile(enhanced, after_in_scratch)
                enhanced_count += 1
        if require_enhancement and enhanced_count == 0:
            raise RuntimeError(
                f"enhancement no-op for {spec.slug} arm={arm} on all "
                f"{len(after_names)} after image(s) — FOCAL silently failed "
                "(ComfyUI down / silent fail); candidate would equal raw input."
            )

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
    baseline_strategy: str = "existing-board",
) -> dict[str, Any]:
    """Build both arms for every spec and assemble the t51 judge packet.

    baseline_strategy:
      - ``existing-board`` (default, 0 quota): baseline = the case's
        already-shipped final-board (owner decision 2026-05-29; POLISH re-render
        proved unviable — gpt-image-2 downscales to ~1024 + 240s timeouts).
      - ``render``: baseline = fresh layout render (via enhance_fn/render_fn).

    candidate is always the FOCAL crop+composite re-render.

    Per-case failures (a render that returns no board — e.g. blurry after image
    or missing angle slots, or a missing existing board) are **non-fatal**: the
    case is dropped, logged, reported in ``dropped`` (no silent cap).
    """
    items: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for spec in specs:
        try:
            # Recover the shipped board's brand/template + the after images it
            # actually composed, so the candidate renders a COMPARABLE board
            # (same layout) and only focal-enhances the shown afters.
            detected = detect_existing_render(spec)
            if baseline_strategy == "existing-board":
                if detected is None:
                    raise RuntimeError("no existing final-board for baseline")
                baseline_board = detected["board"]
                baseline_note = "existing shipped final-board (current product, 0 quota)"
                arm_brand = detected["brand"]
                arm_template = detected["template"]
                selected_afters = detected["after_names"]
            else:
                arm_brand, arm_template, selected_afters = brand, template, None
                baseline_board = build_arm(
                    spec, "baseline", scratch_root,
                    brand=arm_brand, template=arm_template,
                    enhance_fn=enhance_fn, render_fn=render_fn,
                )
                baseline_note = "fresh layout-render baseline"
            candidate_board = build_arm(
                spec, "candidate", scratch_root,
                brand=arm_brand, template=arm_template,
                enhance_fn=enhance_fn, render_fn=render_fn,
                after_names_override=selected_afters,
                # A real candidate must actually apply FOCAL; a no-op (ComfyUI
                # down) must drop the case, not yield a raw-input candidate.
                require_enhancement=enhance_fn is not None and not stub,
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
                    "role_note": baseline_note,
                },
                "candidate": {
                    "source_path": str(candidate_board),
                    "role_note": "FOCAL crop+composite layout-render (PR #41)",
                },
            }
        )
    baseline_desc = (
        "existing shipped final-board" if baseline_strategy == "existing-board"
        else "fresh layout-render"
    )
    note = (
        f"P4 formal gate (crisp-focal-crop). baseline={baseline_desc}, "
        "candidate=FOCAL crop+composite layout-render. "
        + ("STUB-ENHANCE dry-run: candidate enhancement skipped." if stub
           else "Real candidate FOCAL render.")
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
    parser.add_argument(
        "--baseline", choices=["existing-board", "render"], default="existing-board",
        help="existing-board (default, 0 quota): baseline = the case's shipped "
             "final-board. render: fresh layout render (POLISH proved unviable).",
    )
    parser.add_argument(
        "--select", default=None,
        help="Comma-separated substrings; keep only cases whose dir path matches "
             "ANY (e.g. '林真呈,胡志超,黄婧'). Overrides diversity selection.",
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
    if args.select:
        needles = [t.strip() for t in args.select.split(",") if t.strip()]
        pool = [s for s in pool if any(t in str(s.case_dir) for t in needles)]
        print(f"  --select matched {len(pool)} cases", file=sys.stderr)
        selected = pool[: args.n]
    else:
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
        baseline_strategy=args.baseline,
    )
    args.output_packet.parent.mkdir(parents=True, exist_ok=True)
    args.output_packet.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote packet ({packet['judge_item_count']} items) → {args.output_packet}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
