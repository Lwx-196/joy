# P4 T4.0 — What produces the "layout-render" baseline? (investigation, 0 quota)

> Worktree `case-workbench-focal-p4` @ `fix/focal-p4-formal-gate` off origin/main `60a9f83`
> (PR #41 focal crop+composite merged). Owner WIP `feat/vlm-classifier` untouched.
> Date 2026-05-29. Evidence: code read on origin/main, no execution.

## TL;DR

**The layout render does NOT itself call any AI.** It is local PIL composition
(0 quota). AI enhancement (FOCAL/POLISH/ARCHIVE) is a *separate* pre-step that
mutates the staging "after" images in place; the layout pipeline then composes
whatever is in staging into the deliverable board.

→ The P4 A/B is **product-level**: same layout pipeline, two staging states.
- **Arm A (baseline)** = board composed from after-images **without** focal enhancement.
- **Arm B (candidate)** = board composed from after-images **with** focal crop+composite.

Both arms are fully local; the only quota question is **which** enhancement the
baseline arm uses (see Decision below).

## The concrete code path

1. **Enhancement (per-image, in `render_queue.py`)** — the dispatch fn (`~1605`)
   resolves the mode via `md_ai_mode_router.resolve_mode(...)` then, for each
   `after` image in staging, replaces it in place:
   - `FOCAL` → `ai_generation_adapter.run_comfyui_focal_enhance` (line 1732) —
     **local ComfyUI MPS, 0 quota compute** (the candidate path, PR #41).
   - `ARCHIVE` → `run_clinical_archive_pipeline` — PIL EXIF transpose + WB, **no AI, 0 quota**.
   - `POLISH` → `run_direct_clinical_enhancement` (line 2712) — shells `node PS_ENHANCE_SCRIPT`
     → **external paid API** (the current md_ai/meiji_ai production default).
   - `COMPOSITE` → returns immediately, "layout pipeline runs in render_executor" (line 1666-68).
   - `REJECTED` → fail-closed skip.
   - FOCAL falls back to **POLISH** when the G1.A.i rollout gate rejects (line 1683-96).

2. **Layout render (`render_executor.run_render`, line 1671)** — subprocess into
   the `case-layout-board` skill (`~/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_board.py`,
   **confirmed present**). Builds the multi-panel board on a PIL canvas
   (`Image.new`, line 1582) → writes `<case_dir>/.case-layout-output/<brand>/<template>/render/`.
   **No paid API. 0 quota. Local.** This is "the product the customer sees."

So "layout-render" = `run_render` output (one board PNG per case). The judge can
compare board-A vs board-B as two image files (same shape as the P2 packet).

## Quota analysis — three candidate baselines

| Baseline arm | What it is | Quota | Fidelity to real product |
|---|---|---|---|
| **raw-layout** | original after-images, no enhancement, laid into board | **0 (local)** | low (not what ships today) |
| **ARCHIVE-layout** | EXIF transpose + WB, no AI | **0 (local)** | medium |
| **POLISH-layout** | `run_direct_clinical_enhancement` (node → external paid API) | **PAID, N× external calls** | **high** (current md_ai/meiji_ai default; FOCAL's own fallback) |

Candidate (FOCAL) = local ComfyUI MPS, 0 quota compute, but **needs ComfyUI
running** (owner-gated, as in C1.2 / Task #107).

## Surfaced risk — case sourcing for N≥10 (真实数据, no mock)

The local production DB `case-workbench/case-workbench.db` has **419 cases but
only 3 with non-empty `tags_json`, and 0 with any `MD_ANATOMICAL_KEYWORDS`**
(苹果肌/法令纹/面颊/泪沟/下巴/…). `focus_targets` are derived by cross-referencing
those keywords against case tags → **as-is, almost no case routes to FOCAL.**

P2's cases 79/129/134 came from the antigravity delivery smoke set
(`/tmp/g1-m3-smoke/input/`), not from DB tag routing. So T4.2 must answer:
**where do N≥10 focus-eligible cases come from?** Options: (a) the antigravity
26-case delivery set (if it has before/after + focus targets), (b) inject
`focus_targets` via `render_job_meta_json` for selected DB cases, (c) manual
anatomical tagging of N real cases. **This is a real data-sourcing task, not a
code detail — flag for owner before building T4.2.**

## What T4.2 (packet builder) must do (pending decisions below)

1. Pick N≥10 focus-eligible case_dirs with before/after staging images (risk above).
2. Per case, render twice into separate output roots:
   - Arm A: layout render with the chosen baseline enhancement.
   - Arm B: focal-enhance staging → layout render.
3. Assemble a `t51_blind_judge_packet_v1` packet (template `/tmp/focal-p2/packet.json`)
   with baseline=board-A, candidate=board-B per case.
4. Run `comfyui_vlm_judge_runner.py` (Vertex ADC, `tasks/t54_vertex_adc.local.env`).
   **Gate: candidate win-rate ≥ 60%.**

## Owner decisions (2026-05-29)

1. **Baseline arm = POLISH-layout** (paid, true product comparison). The gate is
   focal vs the current md_ai/meiji_ai production default. Quota: N× external
   `PS_ENHANCE_SCRIPT` (node) calls — owner-authorized magnitude pennies/N.
2. **Case sourcing**: inventory local materials first (done below).

## Material inventory result (2026-05-29, read-only)

**SOLVED — 86 already-rendered focus-eligible cases** under
`incoming/无创案例库/无创注射案例库/<患者>/<日期+术式>/`. Each has:
- `术前.jpg` (before) + `术后.jpg` (after) — proven before/after pair.
- `.case-layout-output/` — **already rendered successfully** → phase resolution
  (术前/术后 → before/after) and layout render are proven working on these dirs.
- **focus_targets come from the FOLDER NAME** (the procedure description), e.g.
  `王嘉琦/25.6.26卧蚕，泪沟` → 卧蚕+泪沟; `林真呈/…面颊，下巴` → 面颊+下巴;
  `郭若煊/…泪沟` → 泪沟; `赵建芬/…法令纹…卧蚕` → 法令纹+卧蚕. Substring-match the
  folder name against `MD_ANATOMICAL_KEYWORDS`.

**Critical wiring note**: the DB (`case-workbench.db`, 419 cases) `tags_json` is
empty AND its `abs_path` points to a *different* copy
(`~/Desktop/飞书Claude/医美资料/…` / `output/…`), not the `incoming/` render set.
→ **The builder must NOT go through the DB for focus routing.** Source case_dirs
directly from `incoming/无创案例库/无创注射案例库/` and derive focus_targets from
the folder name. (DB-tag routing yields 0 focal cases — that's why P2 used the
smoke set, not DB routing.)

86 ≫ N≥10 → ample for a strong N≥10 (even N=20–30) gate, with diverse focus
regions (唇/泪沟/面颊/下巴/法令纹/卧蚕/下颌线) + 79-style large-union cases for
the residual stress test.

## T4.2 builder — BUILT (0 quota, 2026-05-29)

`backend/scripts/focal_p4_packet_builder.py` + `backend/tests/test_focal_p4_packet_builder.py`:
- `resolve_focus_targets(folder_name)` — substring-match the procedure folder name
  against `MD_ANATOMICAL_KEYWORDS` (DB-free, per the inventory wiring note).
- `discover_cases` — walk `incoming/无创案例库/无创注射案例库/` for dirs with a
  before(术前)+after(术后) pair AND a focus-eligible folder name; skip the rest.
- `select_cases` — focus-region diversity round-robin + multi-focus (79-style) preference, n cap.
- `build_arm` — copy the pair into an isolated scratch dir (**originals never mutated**),
  enhance the after copy (baseline→POLISH paid / candidate→FOCAL ComfyUI), real
  `render_executor.run_render` → board path.
- `build_packet` — both arms per case → `t51_blind_judge_packet_v1`.
- `--stub-enhance` (0-quota dry-run: skip enhancement, real render only) + `--list-only`.

**Verification (0 quota):**
- 14 new unit tests pass (mocked enhance + render: focus resolution, discovery
  filters, diversity, scratch isolation byte-check, packet shape).
- Full backend suite **1036 passed / 3 skipped — 0 regression** (1022 + 14).
- `ruff check` clean on both files.
- `--list-only` on real data: **25 focus-eligible cases discovered** → 12 selected
  with diverse focus (泪沟/下巴/法令纹/面颊/苹果肌/下颌线/卧蚕/鼻基底). 25 ≫ N≥10.
- Note: `唇` is NOT in production `MD_ANATOMICAL_KEYWORDS` → 丰唇 cases don't route
  to focal (P2's case 134 唇 was a hand-injected smoke target). Non-blocking.

## T4.2 stub-smoke (0-quota real-render dry-run, 2026-05-29)

Ran the builder with `--stub-enhance` (skip enhancement, **real** local layout
render) on the proven-renderable pool — caught a real constraint and validated
the full render+packet path on real images, no ComfyUI / no paid calls.

**Finding (the dry-run earned its keep):** the layout templates need angle-paired
slots (正面/45°/侧面) and reject blurry after images. A first run on the raw
discovered set hit `status:error` ("没有可渲染的角度槽位" / "术后 过糊 sharpness=6.18")
on a case whose after image was blurry + single-angle. Builder hardened in response:
- **discover_cases** now records `has_rendered_board` (existing `final-board.*`);
  `main` defaults to selecting **only proven-renderable** cases (`--all-cases` to override).
  Of 25 discovered, **12 are proven-renderable** (≥ N≥10).
- **_stage_arm** now copies **all** top-level images (every angle, both phases),
  not just one before/after pair, so the scratch render gets the same inputs the
  original successful render had.
- **build_arm** enhances **all** after-phase images (mirrors render_queue dispatch).
- **build_packet** is now resilient: a per-case render failure drops that case
  (logged + reported in `dropped`/`dropped_count`), never aborts the whole packet
  (no silent cap).

**Result (N=3 proven-renderable, `--stub-enhance`):** all 3 cases rendered both
arms successfully → `wrote packet (3 items)`, **0 dropped**. Real `final-board.jpg`
boards produced under `/tmp/focal-p4-stub/{baseline,candidate}/<case>/`. The full
discovery → selection → scratch-staging → real layout render → packet path is
**proven end-to-end at 0 quota** (only the enhancement step is deferred to T4.3).

## T4.3 — real gate run (next, owner-gated: PAID POLISH + ComfyUI)

1. Select N≥10 (propose ~12) cases from the 86, diverse focus regions + ≥2
   large-union (79-style) cases. Copy each to a scratch staging dir (never mutate
   the `incoming/` originals — 最小影响).
2. Per case, render twice into separate output roots:
   - **Arm A (baseline)**: POLISH-enhance `术后.jpg` (`run_direct_clinical_enhancement`,
     **PAID node API**) → `run_render` board.
   - **Arm B (candidate)**: FOCAL crop+composite `术后.jpg` (`run_comfyui_focal_enhance`,
     local ComfyUI MPS — **needs ComfyUI running, owner-gated**) → `run_render` board.
3. Assemble `t51_blind_judge_packet_v1` (baseline=board-A PNG, candidate=board-B PNG).
4. `comfyui_vlm_judge_runner.py` (Vertex ADC, `tasks/t54_vertex_adc.local.env`).
   **Gate: candidate win-rate ≥ 60%** over N≥10. 79-style residual → P3 multi-crop.

**Runtime prerequisites before T4.2 can execute** (both owner-gated):
- ComfyUI running on :8188 (as in C1.2 / Task #107).
- `PS_ENHANCE_SCRIPT` (node) reachable + external auth valid for POLISH baseline.
