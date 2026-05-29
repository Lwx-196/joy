# Phase 2 — crop+composite validation (real ComfyUI + real Vertex judge)

> Validates `~/.claude/plans/crisp-focal-crop.md` P1 against the baseline pinned
> in `focal-crop-baseline.md`. Real ComfyUI SDXL re-render (local MPS, 0 quota) +
> real Vertex ADC gemini-3.5-flash judge (3 paid calls, ~3¢). 2026-05-29.
> Code under test: `fix/focal-crop-native-res` (commit `34dff10`).

## Result: regressions ELIMINATED; the ≥60%-win gate (vs raw input) NOT met

Same 3 cases + targets as the C2.2 **0/3** baseline (apples-to-apples).
baseline = focal input (pre-enhance); candidate = new crop+composite output.

| case | targets | crop | path | judge verdict | vs baseline run |
|---|---|---|---|---|---|
| 79 | 下巴+面颊+chin | 1920×1000 (1.92 MP) | ×0.884 | **baseline wins** ("candidate softer, slightly blurry") | still loss |
| 134 | 唇+lip | 511×347 (0.18 MP) | **NATIVE** | **TIE** ("completely identical") | 🟢 loss → tie (blue artifact gone) |
| 129 | 泪沟+法令纹+face | 1920×1280 (2.46 MP) | ×0.781 | **TIE** ("identical; only diff = red neck marks on baseline") | 🟢 loss → tie (candidate cleaned the marks) |

**Scorecard: candidate wins 0/3 (gate ≥60% NOT met) — but 3 losses → 1 loss + 2 ties, 0 new losses.**

## Region-sharpness self-check (0 quota, edge-variance proxy)

| case | path | face Δsharp | bg Δsharp |
|---|---|---|---|
| 79 | ×0.884 | −46.1% | +0.6% |
| 134 | NATIVE | **−7.2%** (meets ≳−10%) | +0.0% |
| 129 | ×0.781 | −34.9% | −0.0% |

## What is validated vs what remains

**Validated (the rewrite's core mechanism works):**
- **Pristine background** — bg Δsharp ≈ 0% on all 3; the composite leaves the
  unmasked region byte-near-identical (confirmed visually too: clean backgrounds).
- **Native small-crop preserves sharpness** — 134 face −7.2% (vs baseline −69%).
- **Artifact elimination** — the case-134 blue hair-edge and case-129 red neck
  marks sat OUTSIDE the focal crop → now pristine → the judge sees "identical"
  (they no longer cost a loss).

**Remaining (two distinct problems):**
1. **Large-target softening (79):** a broad union still regenerates most of the
   face. The −46% is mostly SDXL denoise=0.40 re-synthesising a large skin area
   (a ×0.884 LANCZOS round-trip alone loses ~15%), **not** primarily resolution —
   so raising the budget (T2.4) won't suffice. → P3 multi-crop (split into small
   per-target crops like 134) / lower denoise.
2. **Even the perfect case (134) only TIES the raw input:** the SDXL focal
   enhancement isn't a *judge-visible improvement* over a decent input photo.
   This is largely a **baseline-choice artifact** — the C2.2 mini uses the raw
   input as baseline (explicitly "NOT the formal gate"); the formal C2 gate is
   candidate vs **layout-render**, which the input typically beats. → P4
   methodology (build the layout-render baseline + T51 packet builder).

## Decision (owner, 2026-05-29)

- **Merge this fix** as the new focal baseline — it is a strict improvement over
  W11 (regressions eliminated, mechanism validated, 1022 tests, 0 regression).
  The ≥60%-win gate is deferred to the correct baseline.
- **Next: P4** — build the layout-render baseline + T51 packet builder and run
  the real N≥10 gate (candidate vs layout-render, not vs raw input).
- P3 multi-crop remains the lever for the genuine large-target residual (79).

## Evidence (ephemeral /tmp, recorded here)

- render: `/tmp/focal-p2/{smoke_p2.py,run-p2.sh,manifest.json,run.p2.log}`;
  outputs `/tmp/focal-p2/output/case{79,134,129}/comfyui-generated.png`.
- judge: packet `/tmp/focal-p2/packet.json`; results `/tmp/focal-p2/results.json`;
  report `/tmp/focal-p2/report.json` (provider=vertex_generate_content_adc,
  model=gemini-3.5-flash, location=global, paygo).
