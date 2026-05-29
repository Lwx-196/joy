---
phase: comfyui-vlm-ga-c0-5
plan: .claude/plan/comfyui-vlm-ga.md
parent_phase: comfyui-vlm-ga
sub_phase: C0.5 — Data Contract Freeze
provides:
  - ab_validation_units table (SCHEMA_VERSION 6)
  - canonical evidence-report schemas (3 reports + master doc)
  - manifest-binding double-check in _comfyui_ab_validation_gate
  - GA_APPROVED_WORKFLOW = portrait_focal_enhance_v1 constant
  - validate_ab_validation_reports.py CLI validator
affects:
  - backend/db.py
  - backend/ai_generation_adapter.py
  - backend/scripts/compute_manifest_hashes.py
  - case-workbench-ai/promotion/manifest.json
key_files:
  - backend/db.py
  - backend/ai_generation_adapter.py
  - backend/scripts/compute_manifest_hashes.py
  - backend/scripts/validate_ab_validation_reports.py
  - backend/tests/test_ab_validation_units_schema.py
  - backend/tests/test_ab_validation_reports_schema.py
  - backend/tests/test_comfyui_gate_manifest_binding.py
  - backend/tests/test_ga_workflow_scope_lock.py
  - backend/tests/test_manifest_hashes.py
  - case-workbench-ai/promotion/manifest.json
  - case-workbench-ai/ab_runs/samples/t47_comfyui_ab_report.sample.json
  - case-workbench-ai/ab_runs/samples/vlm_guardrail_report.sample.json
  - case-workbench-ai/ab_runs/samples/comfyui_production_gate.sample.json
  - docs/contracts/ab-validation-schema.md
  - docs/contracts/ga-workflow-scope.md
completed: true
completed_at: 2026-05-28T23:55:00+08:00
notes: |
  C0.5 Exit checklist 4/4 PASS (Verifier matrix: 8/9 PASS + 1 PARTIAL closed via doc lock).
  Multi-model audit: Critical 0 / Warning 4 (all closed in-session) / Info 5 (deferred).
  Test suite: 935 passed, 2 skipped, 0 regression (baseline ~889 + 46 new C0.5 tests).
  Changes staged (12 files, +1948 / -8), NOT committed — awaiting owner decision.
  Downstream C1+ entry condition (main HEAD ≥ 6e74247) satisfied: stream-a is on origin/main.
  Owner WIP non-interference verified: 0 modifications to case_grouping.py / vlm_consensus_judge.py / vlm-judge worktree.
---

# C0.5 — Data Contract Freeze (Done)

## Deliverables vs Plan Exit Checklist

| Exit item                                                          | Status                                  |
| ------------------------------------------------------------------ | --------------------------------------- |
| `ab_validation_units` 表迁移过 + 至少 1 行测试数据                    | ✅ PASS (table + 6 tests)                |
| 3 report schema doc + validation script git committed              | ✅ PASS (master doc + validator + 3 fixtures, CLI exit 0) |
| manifest binding hash 与 sample report 文件 hash 对齐                | ✅ PASS — sentinel + fail-closed contract documented; real bindings transition to non-null only after C2 generators run |
| approval scope 文档锁定 `portrait_focal_enhance_v1`                  | ✅ PASS (constant + doc + validator default) |

## Multi-model audit outcome

| Reviewer        | Verdict       | Findings                              |
| --------------- | ------------- | ------------------------------------- |
| verifier        | PASS 8/9 + 1 PARTIAL → closed via `ab-validation-schema.md` fail-closed note | E1/E2/E4 PASS, E3 PARTIAL (canonical files absent) — closed by documenting the sentinel + fail-closed contract |
| team-reviewer   | Critical 0 / Warning 4 / Info 5 — verdict: ✅ commit-ready | W-1 (docstring), W-2 (cross-ref doc), W-3 (hard import), W-4 (BC test) — **all 4 closed in-session** |

## Behavioural contract surfaces locked at C0.5

1. **`ab_validation_units` table** is the SoT feeding `generate_ab_validation_report.py` (C2.3). Schema is frozen; further additions require a new SCHEMA_VERSION bump.
2. **Three canonical evidence files** (`t47_comfyui_ab_report.json`, `vlm_guardrail_report.json`, `comfyui_production_gate.json`) — written by C2.3/C2.4/C2.5, consumed by `_comfyui_ab_validation_gate` and `compute_manifest_hashes.py`. Schema is documented in `docs/contracts/ab-validation-schema.md`.
3. **Hash boundary** is anchored to canonical filenames (no longer "latest timestamp dir"). `BINDING_NAMES` grew from 4 to 7. New bindings emit sentinel hashes when canonical files are absent — the gate's `_manifest_binding_blockers` then fails closed via `manifest_binding_*_missing`.
4. **GA workflow scope** = `portrait_focal_enhance_v1` (matches W11 production runtime). Widening to a second workflow requires a code change to `GA_APPROVED_WORKFLOW`, not just a docs edit.

## What's NOT in C0.5 (deferred to later phases)

- `sign_promotion_approval.py` — explicitly C3.1 per plan §C3.1.
- Three `generate_*_report.py` generators — C2.3 / C2.4 / C2.5.
- Real canonical report files at canonical paths — C2 (current state: sentinel + fail-closed by design).
- Schema v2 bump — deferred post-GA Wave 14 per plan §schema migration risk.
- Owner WIP integration (best-pair / vlm-judge / vlm-phase2 / vlm-calibration) — C2.1 dependency.

## Worktree / commit state

- Worktree: `/Users/a1234/Desktop/案例生成器/case-workbench-stream-a` on branch `feat/cvga-stream-a`.
- HEAD = `origin/main` = `6e74247` (PR #35 W13 hardening merge commit).
- 12 files staged, +1948 / -8. **Not committed** — owner must run `/wrap-up` or explicit `git commit` to seal.
- No interference with 4 owner WIP worktrees (`case-workbench-best-pair`, `-vlm-judge`, `-vlm-phase2`, `-calibration`).

## Next phase entry condition (C1 — MVP Smoke + W11 真验证)

- Owner must start ComfyUI service at `/Users/a1234/Desktop/飞书Claude/ComfyUI` on port 8188 (auto-mode classifier rejects cross-project spawn — explicit user action required per NOW.md).
- All other C1 entry conditions met: stream-a is on `6e74247` ≥ required main HEAD, schema contracts frozen, validator + gate hardened.

## Info-tier carryover (per team-reviewer)

Deferred to a future cleanup pass (not blocking C1):

- **I-1**: `ab_report_hash` ↔ `ab_validation_report_hash` deprecation question (legacy binding is a soft alias once canonical exists).
- **I-2**: `rollback_baseline.bindings` operator capture protocol — touched in `ga-workflow-scope.md` "Rollback baseline" but auto-capture wiring is C2.9.
- **I-3**: `test_init_schema_is_idempotent` could additionally assert `schema_versions.applied_at` was updated.
- **I-4**: `judge_result_id` may need a sibling `judge_source` column once C2.3 producers commit; deferred until generator design pins it.
- **I-5**: `_is_int(value)` style helper could be hoisted to `backend/utils/`.
