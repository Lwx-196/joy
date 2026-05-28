# C0.5.4 — GA Approved Workflow Scope

## Decision

> **GA approval scope is locked to `portrait_focal_enhance_v1`.**

Any sub-plan, evidence report, or promotion approval that references
`local_region_enhance_v1`, `local_region_enhance_v2`, or
`local_region_enhance_v3` as the *approved* workflow is **obsolete** and
must not block, gate, or trigger rollout.

## Why

The 2026-05-28 W11 release shipped `portrait_focal_enhance_v1` as the
production-path focal workflow (PR #34, `8e4edde`). The sub-plan
`~/.claude/plans/p10-deploy-gate-unlock.md` was written before W11 landed
and used `local_region_enhance_v1` as an illustrative example. That
example was never replaced when the production runtime moved on, so two
documents disagreed about *which workflow is being promoted*.

Without an authoritative answer, the gate's `approved_workflows` check
either:

- silently green-lights a workflow that production no longer runs
  (sub-plan example interpreted as ground truth), or
- false-blocks promotion because the approval scope was signed against the
  obsolete name (operator follows sub-plan literally).

C0.5.4 closes that drift: one constant in code
(`backend.ai_generation_adapter.GA_APPROVED_WORKFLOW`), one schema
constraint in the contract doc (`approved_workflows` must include this
value), one preflight assertion (`_comfyui_preflight()` defaults to it).

## Code surface

| Surface                                              | Lock                                                                                       |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `backend.ai_generation_adapter.GA_APPROVED_WORKFLOW` | `"portrait_focal_enhance_v1"`                                                              |
| `_comfyui_preflight(workflow_name=...)`              | Falls back to `GA_APPROVED_WORKFLOW` when no runtime override is supplied.                 |
| `_comfyui_ab_validation_gate(model_name=...)`        | Returns `workflow_not_approved_for_default` blocker iff `model_name` ∉ `approved_workflows`. |
| `validate_ab_validation_reports.py --required-workflow` | Defaults to `portrait_focal_enhance_v1`; the validator emits `approved_workflows_missing_ga_scope` when the AB report's signed scope drops the GA workflow. |
| `case-workbench-ai/ab_runs/samples/t47_comfyui_ab_report.sample.json` | Sample approval signs `["portrait_focal_enhance_v1"]`.                       |

## Two scopes — both required, do not confuse

The approval block carries **two independent scope fields**, each enforced
by a different blocker code. Operators must touch only the workflow
scope when widening or narrowing GA coverage.

| Field                                                       | Layer            | Value                                | What it gates                                                                |
| ----------------------------------------------------------- | ---------------- | ------------------------------------ | ---------------------------------------------------------------------------- |
| `promotion_approval.scope`                                  | Action scope     | `"comfyui_default_promotion_v1"`     | Identifies *which action is being approved* (the default-promotion action). Fixed for the lifetime of the v1 promotion pipeline; bumping requires a new contract revision. |
| `promotion_approval.approved_workflows`                     | Workflow scope   | `["portrait_focal_enhance_v1"]` (GA) | Identifies *which workflows that action covers*. This is the field operators edit when re-signing for additional workflows. |

**Common mistake**: writing the GA workflow name into `scope` ("oh, the
scope is the focal workflow"). The validator emits
`approval_scope_invalid` and the gate emits `promotion_approval_invalid`;
neither error message points at the right fix unless the operator already
knows the two-field model. If you only need to widen coverage to a second
workflow, **only `approved_workflows` changes** — keep `scope` literally
equal to `"comfyui_default_promotion_v1"`.

See `ab-validation-schema.md` "Approval block" for the canonical field
table; this section is the operator-facing summary.

## Rollback baseline bindings (write path)

`compute_manifest_hashes.py --write` populates only the live
`bindings.*` slots; it never touches `rollback_baseline.bindings.*`.
The rollback baseline is the **last known-healthy snapshot** captured
manually before each promotion advance (and automatically by
`promotion_rollback_applier` once C2.9 wires the soak path). After
C0.5.3 the snapshot has three additional slots
(`ab_validation_report_hash`, `vlm_guardrail_report_hash`,
`production_gate_report_hash`); they must be captured at the same time as
the live bindings or rollback will revert to a stale evidence set. C2.9
covers the cron + applier wiring that makes this automatic; until then
treat `rollback_baseline.bindings` as a manual checklist.

## Operator runbook (until C3.1 wires `sign_promotion_approval.py`)

1. Generate the three canonical reports (C2.3/C2.4/C2.5 deliverables).
2. Edit `t47_comfyui_ab_report.json` → `promotion_approval.approved_workflows`
   to `["portrait_focal_enhance_v1"]` (and only this value during GA).
3. Re-sign `approved_evidence_sha256` against the freshly-written
   `vlm_guardrail_report.json` and `comfyui_production_gate.json`.
4. Run `python -m backend.scripts.compute_manifest_hashes --write` so the
   manifest bindings mirror the just-signed evidence files.
5. Run `python -m backend.scripts.validate_ab_validation_reports` —
   expect `OK: 3 reports validate against the C0.5 contract`.
6. Only then edit `case-workbench-ai/promotion/manifest.json` to advance
   `promotion_state`.

## When the GA scope is allowed to widen

Adding a second approved workflow (e.g. `portrait_focal_enhance_v2`) is a
**plan-level decision**, not a docs edit. The change must:

1. land a new W-N hardening series that proves the second workflow under
   the same coverage as W11 did for v1;
2. update `GA_APPROVED_WORKFLOW` to a tuple or list, with the migration
   path documented in a follow-up to this contract;
3. ship a new sample report whose `approved_workflows` lists both names.

Do **not** widen the scope by editing this file alone — the gate would
keep enforcing the old single-string constant until the code is updated.

## See also

- `ab-validation-schema.md` — schema of the three evidence reports
- `~/.claude/plans/comfyui-vlm-ga.md` C0.5 Data Contract Freeze
- W11 PR #34 — `portrait_focal_enhance_v1` production runtime baseline
