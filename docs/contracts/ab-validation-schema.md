# C0.5 Data Contract Freeze — A/B Validation Evidence Schema

> Source-of-truth for the **three evidence reports** consumed by
> `_comfyui_ab_validation_gate` in `backend/ai_generation_adapter.py`.
> Any code that produces or reads these files must conform to this contract.

## Why this exists

The promotion gate refuses to flip `promote_to_default` unless **all three**
report files exist, their content satisfies the schema-level checks below,
**and** their on-disk sha256 matches:

1. the `approved_evidence_sha256` block signed into the AB report's
   `promotion_approval` (approval-side check), and
2. the corresponding `bindings.*` slot in
   `case-workbench-ai/promotion/manifest.json` (manifest-side check).

Before C0.5 the gate compared evidence hashes only on the approval side and
`compute_manifest_hashes.py` hashed the *latest* `summary.json` under
`case-workbench-ai/ab_runs/<timestamp>/`. Those two paths could (and did)
drift. C0.5.3 anchors both paths to the **canonical** filenames documented
below; this file is the schema authority for that anchor.

## Canonical filenames

| Report                | Default canonical path                                              | Env override                                              |
| --------------------- | ------------------------------------------------------------------- | --------------------------------------------------------- |
| AB validation         | `case-workbench-ai/ab_runs/t47_comfyui_ab_report.json`              | `CASE_WORKBENCH_COMFYUI_AB_REPORT_PATH`                   |
| VLM guardrail         | `case-workbench-ai/ab_runs/vlm_guardrail_report.json`               | `CASE_WORKBENCH_COMFYUI_VLM_GUARDRAIL_REPORT_PATH`        |
| Production gate       | `case-workbench-ai/ab_runs/comfyui_production_gate.json`            | `CASE_WORKBENCH_COMFYUI_PRODUCTION_GATE_REPORT_PATH`      |

The reference samples under `case-workbench-ai/ab_runs/samples/` are
intentionally **not** the canonical files — they are validator fixtures.
Production generators (`generate_ab_validation_report.py` and friends,
delivered in C2.3/C2.4/C2.5) write directly to the canonical paths.

> **State at C0.5 exit (intentional fail-closed).** The three canonical
> files do **not** exist yet — they are produced by the C2.3 / C2.4 / C2.5
> generators. Until then, `compute_manifest_hashes.py` emits the
> `_AB_REPORT_MISSING_SENTINEL` / `_VLM_GUARDRAIL_MISSING_SENTINEL` /
> `_PRODUCTION_GATE_MISSING_SENTINEL` markers for the three new binding
> slots and the operator MUST leave
> `case-workbench-ai/promotion/manifest.json:bindings.{ab_validation,
> vlm_guardrail, production_gate}_report_hash` as `null`. The promotion
> gate then fails closed via `manifest_binding_*_missing` blockers — that
> is the contract. Do **not** stamp the sample-fixture sha256 into the
> production manifest to "satisfy" the binding; it would lock the gate
> against a known-fake evidence state. Bindings transition from `null` to
> real hashes only after C2 produces real reports and the operator runs
> `python -m backend.scripts.compute_manifest_hashes --write`.

## Validator entry point

```bash
python -m backend.scripts.validate_ab_validation_reports \
    [--report t47_comfyui_ab_report.json] \
    [--vlm vlm_guardrail_report.json] \
    [--production comfyui_production_gate.json]
```

Without flags the script validates the three canonical paths. Each report
is checked **independently**; exit code is non-zero iff any issue is
emitted. The validator only inspects shape and value membership; it does
not recompute sha256 (that is the job of `compute_manifest_hashes.py
--validate`).

## Schema 1 — `t47_comfyui_ab_report.json`

| Field                              | Type   | Required | Notes                                                                                       |
| ---------------------------------- | ------ | -------- | ------------------------------------------------------------------------------------------- |
| `validation_status`                | string | yes      | Free-form; `"ready_for_human_review"` unlocks the gate.                                     |
| `ready_for_human_review`           | bool   | yes      | Mirrors `validation_status == "ready_for_human_review"`.                                    |
| `promote_to_default`               | bool   | yes      | Only set true after a signed approval block is present.                                     |
| `comparable_pair_count`            | int    | yes      | Non-negative.                                                                               |
| `winner_evidence_count`            | int    | yes      | Non-negative; `winner_evidence_count >= candidate_win_count`.                               |
| `candidate_win_count`              | int    | yes      | Non-negative.                                                                               |
| `promotion_approval`               | object | iff `promote_to_default == true` | See "Approval block" below.                                  |

### Approval block (`promotion_approval`)

| Field                       | Type       | Required | Constraint                                                            |
| --------------------------- | ---------- | -------- | --------------------------------------------------------------------- |
| `status`                    | string     | yes      | Must be `"approved"`.                                                 |
| `approved`                  | bool       | yes      | Must be true.                                                         |
| `scope`                     | string     | yes      | Must be `"comfyui_default_promotion_v1"` (action scope, not workflow).|
| `decision`                  | string     | yes      | Must be `"approve_default_promotion"`.                                |
| `approver` *or* `approved_by` | string   | yes      | Non-empty.                                                            |
| `approved_at`               | ISO-8601   | yes      | Timezone-aware preferred; trailing `Z` accepted.                      |
| `approved_workflows`        | list[str]  | yes      | Non-empty. **GA scope MUST contain `"portrait_focal_enhance_v1"`** (see `ga-workflow-scope.md`). |
| `approved_evidence_sha256`  | object     | yes      | Keys: `vlm_guardrail_report`, `production_gate_report`. Values: `sha256:<64hex>`. |
| `expires_at`                | ISO-8601   | optional | If present, must parse.                                               |

## Schema 2 — `vlm_guardrail_report.json`

The gate consumes both top-level fields and a nested `vlm_guardrail`/
`candidate_promotion_guardrail` object. Producers must populate the
**top-level fields** as the canonical source; the nested forms are read
only as fallbacks.

| Field                                                | Type   | Required | Notes                                                                              |
| ---------------------------------------------------- | ------ | -------- | ---------------------------------------------------------------------------------- |
| `calibration_status`                                 | string | yes      | One of `calibrated_for_fail_closed_review`, `not_calibrated_fail_closed`, `pending`. |
| `accepted_judgment_count`                            | int    | yes      | Non-negative.                                                                      |
| `required_judgment_count_min`                        | int    | yes      | Non-negative; gate uses this directly.                                             |
| `agreement_rate`                                     | float  | yes      | In `[0.0, 1.0]`.                                                                   |
| `required_agreement_rate_min`                        | float  | yes      | In `[0.0, 1.0]`.                                                                   |
| `false_candidate_promotion_count`                    | int    | yes      | Non-negative.                                                                      |
| `candidate_promotion_guardrail.guardrail_status`     | string | yes      | One of `pass`, `manual_review_required`, `hard_veto`.                              |
| `candidate_promotion_guardrail.manual_review_required_count` | int | yes  | Non-negative.                                                                      |

## Schema 3 — `comfyui_production_gate.json`

The gate reads either the top-level fields or the nested `production_gate`
sub-object; producers must populate `production_gate` (canonical).

| Field                                       | Type      | Required | Notes                                                            |
| ------------------------------------------- | --------- | -------- | ---------------------------------------------------------------- |
| `production_gate.reason_code`               | string    | yes      | Must be `"promotion_approval_required"` to satisfy the gate.     |
| `production_gate.hard_defect_codes`         | list[str] | yes      | Empty list when no hard defects.                                 |
| `production_gate.candidate_win_count`       | int       | yes      | Non-negative; must match `t47_comfyui_ab_report.candidate_win_count` (validator emits `cross_report_drift` on mismatch). |
| `production_gate.required_candidate_wins_min` | int     | yes      | Non-negative.                                                    |

## Cross-report invariants

| Invariant                                                 | Issue code (validator)        |
| --------------------------------------------------------- | ----------------------------- |
| AB and production-gate `candidate_win_count` must match.  | `cross_report_drift`          |
| `winner_evidence_count >= candidate_win_count`.           | `winner_evidence_below_wins`  |
| `agreement_rate <= 1.0`.                                  | `agreement_rate_out_of_range` |

## See also

- `ga-workflow-scope.md` — pins approved workflow list to
  `portrait_focal_enhance_v1` for GA.
- `backend/scripts/validate_ab_validation_reports.py` — CLI validator.
- `backend/scripts/compute_manifest_hashes.py` — binds the canonical
  files into `manifest.json` (C0.5.3).
