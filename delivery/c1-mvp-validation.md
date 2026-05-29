# C1.2 — MVP Smoke + W11 Real Validation (verdict record)

> Phase C1 deliverable (`~/.claude/plans/commercial-readiness.md` C1 / kickoff
> gate `cvga-c1-kickoff-gate.md`). Real ComfyUI SDXL focal-enhance, 2026-05-29.
> Code under test: main `ff283bc` (W11 PR #34 + W13 PR #35 + W11.4 in #37).

## Result: ✅ PASS (W11 + W11.4 validated on plan-named targets 79/129)

3-case real smoke via `run-c1.sh` (starts ComfyUI at `飞书Claude/ComfyUI` →
polls `/system_stats` → runs `smoke.py` → kills ComfyUI on exit, no orphan).
Workflow `portrait_focal_enhance_v1.json`. Local MPS, 0 API quota.

| Case | Source | Output | dt | byte_distinct | /history | Path tested |
|---|---|---|---|---|---|---|
| **79** | 1920×1280 | **1920×1280** ✅ | 236.7s | true | +1 | resize → SDXL → upscale-back |
| **129** *(plan target)* | 1920×1280 | **1920×1280** ✅ | 236.0s | true | +1 | resize → SDXL → upscale-back |
| 134 *(bonus)* | 853×1280 | 848×1280 🟡 | 236.2s | true | +1 | non-resize (long_edge=cap) |

- **C1.1 ComfyUI service**: started + reachable + cleaned up (run-c1.sh lifecycle).
- **C1.2 W11 (resize + dynamic timeout) + W11.4 (upscale-back)**: ✅ validated.
  Both >1280 cases (79 re-confirm, 129 new) restored to **exact** source dims
  through the `output_dir`-provided path — the exact path that returned
  working-res in Task #107 before W11.4 merged. All 3 cases rendered cleanly,
  dt≈236s ≪ W11 dynamic timeout.

## Finding → W11.5 (fixed in this PR)

case 134 (853×1280, long_edge=1280 = `_FOCAL_MAX_LONG_EDGE`, so **no** W11
resize) came back **848×1280** — SDXL/VAE snaps working dims to a multiple of 8
(853 → 848; 848=8×106, 853 not ÷8). Upscale-back was gated by `needs_upscale`,
so the non-resize path left the output 5px short. **Not** a W11.4 regression —
cases 79/129 are all-÷8 dims so the gap was invisible there; case 134 (non-÷8)
exposed it.

**W11.5** (this PR): conditions the dim-restoration on the *actual generated
dims ≠ source dims* instead of the `needs_upscale` flag, generalizing the W11.4
contract to every path. Unit-tested (`test_nonresize_non8_source_restored_to_exact_dims`:
853×1280 source + simulated 848 SDXL snap → restored to 853×1280). A live
re-render of case 134 post-W11.5 was not run (ComfyUI killed on smoke exit); the
fix is unit-verified.

## Blocked (not part of C1.2; need owner)

- **C1.3 MVP shadow real-fire**: `vlm_daily_shadow.py` defaults to `--dry-run`
  (real fire 501-gated); server-mediated.
- **C2.2 N=10 blind-eval**: needs a configured **paid VLM judge** —
  `vlm_provider` fail-closes `blocked_missing_vlm_provider_config` (0 judge
  credentials in env, 0 judge rows ever). Owner must supply provider +
  endpoint + api_key + model + cost budget before the win-rate gate can run.

## Evidence

- Smoke harness: `/tmp/g1-m3-smoke/run-c1.sh` + `smoke.py`; log `run.c1.log`.
- Outputs (ephemeral): `/tmp/g1-m3-smoke/output/case{79,134,129}/comfyui-generated.png`.
- W11.5 fix: `backend/ai_generation_adapter.py` `run_comfyui_focal_enhance`
  (dim-restore block) + `backend/tests/test_w11_focal_resize_and_timeout.py`.
