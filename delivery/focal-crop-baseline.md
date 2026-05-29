# Phase 0 — focal-crop baseline (regression reference)

> Pins the **pre-fix** state of the focal-enhance quality path so the
> crop-at-native-res rewrite (`~/.claude/plans/crisp-focal-crop.md`) can be
> measured against it. Source-of-truth: real Vertex VLM judge run
> `2026-05-29T07:38:04Z` (`/tmp/c2-mini`, ephemeral) + region-sharpness
> diagnostic (journal `2026-05-29` §14). Code under test: main `044a34d`
> (W11 + W11.4 + W11.5), `run_comfyui_focal_enhance` whole-image-resize path.

## Judge verdict (real Vertex ADC gemini-3.5-flash, location=global, paygo): **0/3**

The focal output (`candidate`) lost to its own un-enhanced input (`baseline`)
on all three cases. C2 gate is win-rate ≥ 60% → this is a hard fail.

| case | source | path | winner | conf | judge rationale (verbatim, trimmed) |
|---|---|---|---|---|---|
| 79 | 1920×1280 | resize round-trip | **baseline** | 0.85 | "candidate slightly softer + minor head tilt to viewer's right, flatter lighting; baseline crisper focus, more neutral upright alignment" |
| 134 | 853×1280 | NO resize (long_edge=cap) | **baseline** | 0.85 | "nearly identical; candidate has a small **blue/cyan light artifact** on right side of hair near ear" |
| 129 | 1920×1280 | resize round-trip | **baseline** | 0.85 | (softening; framing/alignment criterion) |

criterion scores consistently `baseline 4 / candidate 3` on sharpness/quality.

## Region-sharpness diagnostic (the smoking gun)

Edge-variance sharpness proxy, split center (face/focal) vs corners (bg):

| case | path | **face Δsharp** | bg (corners) Δsharp |
|---|---|---|---|
| 79 | resize round-trip | **−69%** | −16% |
| 129 | resize round-trip | **−47%** | −13% |
| 134 | NO resize (≤1280) | **−9%** | −3% |

Softening **tracks the whole-image resize round-trip** and concentrates on the
high-detail **face** (= the focal region). The `_FOCAL_MAX_LONG_EDGE=1280` cap
(added in W11 to fix timeouts) trades away quality on exactly the large images.
Mode-B secondary: SDXL inpaint artifacts at denoise=0.40 (case 134 blue
hair-edge decided that case; case 79 head tilt).

## Acceptance target (Phase 2 gate)

Re-render 79/134/129 through the crop+composite pipeline, re-judge with the
SAME Vertex judge. PASS = win-rate improves toward ≥ 60% (≥ 2/3 on this smoke)
**and** the sharpness/softening complaints disappear; face Δsharp ≳ −10%, bg
unchanged (now pristine by construction).

## Evidence (ephemeral, copied here because /tmp is transient)

- packet: `/tmp/c2-mini/packet.json` (baseline=`/tmp/g1-m3-smoke/input/case{79,134,129}.jpg`,
  candidate=`/tmp/g1-m3-smoke/output/case{N}/comfyui-generated.png`)
- judge results: `/tmp/c2-mini/results.json` · report: `/tmp/c2-mini/report.json`
- judge config: `case-workbench/tasks/t54_vertex_adc.local.env` (ADC, no secret keys)
