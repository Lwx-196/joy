# Audit Retention Policy — `ops_audit_log`

> Phase **C3.0.4** of the ComfyUI/VLM GA plan (`.claude/plan/comfyui-vlm-ga.md`).
> Defines how long every audit row is kept, what payload fields are stripped
> at write time, and the archival/export pipeline that lets us answer
> "what happened on date X to case Y?" three years later.

---

## 1. Retention target

**3 years from `created_at`** for every row in `ops_audit_log`.

The 3-year horizon is the regulatory floor under the 中国医美互联网广告
compliance regime that applies to before/after image publication; legal sign-off
is **required** before this policy is treated as final (see Section 6 below).
Until that sign-off lands, the operational policy is "retain indefinitely, do
not delete anything", because mistakenly purging a row inside the retention
window is irreversible while keeping an extra month is cheap.

---

## 2. What is retained

Every row of `ops_audit_log` carries the following columns; **all** are retained
for the full 3 years:

| Column | Purpose | Retention notes |
|--------|---------|------------------|
| `id` | Monotonic PK | Keep — used as correlation anchor |
| `request_id` | Cross-row correlation (e.g. applier `started → completed`) | Keep |
| `endpoint` | Which ops surface fired | Keep |
| `reviewer` | Identity of the operator (or `system/slo_monitor` for autorun) | Keep — required for forensic accountability |
| `reason` | Free text reason supplied by the caller | Keep — usually short, but see PII rule below |
| `payload_json` | Full request body | Keep, **minimized at write time** (see Section 3) |
| `response_json` | Full response body | Keep, minimized in lockstep with payload |
| `outcome` | `ok / partial / error / dry_run / rollback_*` | Keep |
| `http_status` | Numeric HTTP status returned | Keep |
| `created_at` | Insertion timestamp (UTC, ISO-8601) | Keep — drives retention math |

Together those fields are enough to answer:

- Was a rollback fired? When, by whom, with which evidence?
- Which case_id was affected by ops command X?
- Was a given drill labeled `is_drill=true`? (Drill labeling lives in
  `payload_json` per C4.0; see `rollback-drill-spec.md`.)

---

## 3. PII minimization at write time

`ops_audit_log` is **not** a place to mirror the rest of the database. Writers
must reduce the payload before insertion:

- **Keep**: numeric IDs (`case_id`, `simulation_job_id`, `render_job_id`),
  workflow names, scope strings, dry-run / drill flags, reviewer identity,
  recommendation tokens, sample sizes, threshold values, violation arrays,
  timestamps, audit correlation IDs (`request_id`).
- **Strip before insertion**: customer raw names, file paths under the
  customer's directory, base64 images, full image hashes (sha256 of pixel
  data is fine; sha256 of customer-identifiable filenames is not), VLM raw
  judge transcripts, free-text customer notes, anything from
  `cases.meta_json` other than the structured fields the operator explicitly
  needs to audit.

When in doubt, the rule is **"would this leak customer identity if the audit
DB was exfiltrated?"** If yes, strip it before the `INSERT`.

The existing writer at `backend/routes/render.py::_write_ops_audit_log` and the
applier writer at `backend/services/promotion_rollback_applier.py::_insert_audit_row`
already constrain themselves to structured payloads; **new ops endpoints
must do the same before they merge**. This is checked at code-review time —
add a paragraph to the PR description naming the fields kept and stripped.

---

## 4. Archive + export pipeline

Three years of `ops_audit_log` is small (estimated <100 MB at projected
volume), but we still split storage into two tiers so the hot DB stays lean:

1. **Hot**: rows aged <90 days live in `case-workbench.db::ops_audit_log`,
   queryable by the running backend and the `/ops` console.
2. **Cold**: rows aged ≥90 days are exported nightly via
   `backend/scripts/export_ops_audit_log.py` (the helper introduced in this
   phase) to a partitioned JSONL archive under
   `case-workbench-archive/ops_audit/YYYY/MM.jsonl`. Cold rows are **not**
   deleted from the hot DB — the export is purely additive until legal
   reviews retention (see below) and we agree on a deletion threshold.

The exporter is idempotent: re-running it produces identical archive files
(same row ordering, same JSON formatting). It refuses to overwrite an
existing archive without an explicit `--overwrite` flag.

Concrete command (run nightly via cron once legal approves):

```bash
.venv/bin/python -m backend.scripts.export_ops_audit_log \
  --output-dir case-workbench-archive/ops_audit \
  --min-age-days 90
```

The exporter is committed in this same phase under
`backend/scripts/export_ops_audit_log.py`; see the file for flags.

---

## 5. Restoring a row

The archive is JSONL with the same column names as the table. To restore one
or many rows for an investigation:

```bash
.venv/bin/python -m backend.scripts.export_ops_audit_log --restore \
  --archive case-workbench-archive/ops_audit/2027/03.jsonl \
  --where 'request_id="req-abc123"'
```

This **does not** overwrite the hot row; it inserts a new row with the
archived columns and a fresh `id`, plus a synthetic `reason` prefix
`restored_from_archive:` so the audit trail of the restoration itself is
visible.

---

## 6. Legal sign-off checklist

Before this policy is treated as final (and before any deletion threshold is
set), the following must be recorded:

- [ ] Legal counsel reviewed the 3-year retention window against the latest
      医美互联网广告 compliance interpretation.
- [ ] Legal counsel reviewed the PII strip list (Section 3) for completeness.
- [ ] Legal counsel approved the cold-archive location (must be in-region
      and access-controlled at the OS level, no shared drives).
- [ ] Legal counsel signed off on the restoration procedure (Section 5) —
      who is allowed to invoke it, and what audit trail is required.

The sign-off is recorded as a PR diff to this file ("approved on YYYY-MM-DD
by <name>"), not as a Slack thread. Until then the operational rule remains:
**keep everything, delete nothing**.

---

## 7. Operator quick-reference

When you are about to write a new `ops_audit_log` row from a new endpoint:

1. Are the IDs you log structured integers, not customer names? ✅
2. Are file paths in the payload under `case-workbench-ai/` or
   `case-workbench-archive/` (system-owned), never under a customer's home
   directory? ✅
3. If the payload includes a hash, is it of pixel data (safe) or of a
   customer-identifying filename (unsafe — strip it)? ✅
4. Is the reviewer field a known identity (`system/slo_monitor`,
   `system/exporter`, a logged-in user, or the on-call name from
   `on-call-rotation.md`)? ✅
5. Did you add `is_drill=true` to the payload if this is a drill? ✅

If yes to all five, your `INSERT` is consistent with this policy. If no to
any, fix the writer **before** the row is committed.
