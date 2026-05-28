# On-Call Rotation — ComfyUI/VLM Promotion Gate

> Phase **C3.0.5** of the ComfyUI/VLM GA plan (`.claude/plan/comfyui-vlm-ga.md`).
> Defines who is responsible when the promotion manifest enters
> `p10 → p25 → p50 → p100` and how an incident is escalated.

---

## 1. Scope

This rotation covers everything reachable from the **Ops Console** at `/ops`:

- Promotion manifest state transitions (`shadow ↔ p10 ↔ p25 ↔ p50 ↔ p100 ↔ rolled_back`)
- SLO monitor recommendation (`continue / rollback / insufficient_data / monitoring_paused / stop_loss_halt`)
- Rollback applier outcomes in `ops_audit_log`
- ComfyUI live-probe reachability and silent-fail counters
- Render latency P50/P95 outside the agreed-on SLO budget

It does **not** cover purely cosmetic or back-office concerns (review UI, customer naming,
data ingestion), which remain owned by the product team.

---

## 2. Roster

The rotation is staffed by **at least 2 on-call engineers** so coverage survives
illness, PTO, or unfamiliarity with a particular failure mode. Identities are kept in
this file so escalation paths are auditable.

| Slot | Identity (display name) | Contact | Active dates |
|------|------------------------|---------|--------------|
| Primary | _TBD — fill before C3 sign-off_ | _TBD_ | _TBD_ |
| Secondary | _TBD — fill before C3 sign-off_ | _TBD_ | _TBD_ |
| Tertiary (drill week) | _optional, used during real rollback drill_ | _TBD_ | _TBD_ |

**Update rule**: when the schedule changes, edit this table in the same PR that
changes the schedule (no shadow spreadsheet, no Slack-only changes). The git
blame is the source of truth.

---

## 3. Required preparation before being eligible

A person is **not** on-call until all of the following are recorded (training receipt
is the linked commit / journal entry, not "trust me"):

1. **Dashboard walkthrough** — sit through one demo of `/ops` and call out the
   meaning of every card: manifest_state, bucket_exposure_pct, slo_recommendation,
   sample_size vs minimum_sample_size, violations list, ComfyUI live probe,
   render latency P50/P95, silent_fail count, last applier event.
2. **Runbook dry-run** — execute `docs/operations/p10-soak-runbook.md` end-to-end
   in staging without help (see C3.5 exit criterion).
3. **Rollback drill participation** — observe or run one staging drill under
   `docs/operations/rollback-drill-spec.md` (see C4.0 exit criterion).
4. **Audit retention awareness** — read `docs/operations/audit-retention.md`
   and know which fields are kept 3 years vs which are minimized at write time.

The author of this document, the architect, and the primary on-call all sign
off on each new operator in the PR that adds them to the table above.

---

## 4. Standard cadence (no incident)

Even when nothing is on fire, the on-call performs the following every workday
during the promotion window (`p10` and beyond):

1. Open `/ops`, refresh once, screenshot the page and attach to the daily
   journal (`memory/journal/YYYY-MM-DD.md`).
2. Verify the **last applier event** card is either empty (no fires) or shows
   the most recent expected drill — never an unexplained `rollback_started`
   or `stop_loss_halt_alert`.
3. Read the latest row of `ops_audit_log` for `endpoint='promotion_rollback_applier.apply'`
   in the database and confirm it matches the `/ops` card (the dashboard is
   only a read of `ops_audit_log` ORDER BY id DESC).
4. Note in the journal: window sample vs minimum, recommendation, and any
   non-zero silent-fail count.

This is **not** a substitute for the runbook command in `p10-soak-runbook.md` —
it is the lightweight daily heartbeat between full SLO checks.

---

## 5. Escalation path

The path from "I see something weird" to "someone with full authority is acting"
must be at most **4 hops**, and every hop must be reachable inside one working
day. Skipping a hop is allowed only when the previous hop is unreachable; it
must be logged in `ops_audit_log` (reason field) when it happens.

```text
On-call (primary)
   │  no progress in 30 min
   ▼
Lead engineer
   │  no progress in 30 min
   ▼
Architect / project owner
   │  no progress in 30 min
   ▼
CEO / final decision maker
```

Examples of triggers per hop:

- **Primary on-call**: any single SLO violation in the rolling window, ComfyUI
  unreachable for >5 min during business hours, applier audit row with
  `outcome='rollback_aborted'`, silent_fail count >0 unexplained.
- **Lead engineer**: applier wrote `rollback_started` and primary can't confirm
  the recommendation that triggered it; primary is unsure whether the failure
  is hardware or logic.
- **Architect / project owner**: a real `rollback_completed` lands in
  production, or two consecutive windows recommend `rollback` after manual
  recovery, or the operator cannot find evidence proving whether `p10` is
  meeting its sample-size floor.
- **CEO / final decision maker**: customer-visible outage exceeding the
  documented SLO window for the current promotion state, OR a decision to
  abort the entire promotion (revert main and freeze).

---

## 6. Communication rules

- **Single channel of truth**: every incident gets one issue / one chat
  thread / one journal block. Cross-posting fragments context. If you must
  copy an excerpt to Slack, link back to the issue.
- **No private DMs for ops decisions**. If you need to ask the architect a
  question that influences whether a rollback fires, ask in the open thread.
- **State, evidence, decision**: every escalation message uses these three
  beats — "current manifest state and SLO output", "the audit row(s) +
  metric(s) I'm relying on", "what I want from you in the next 30 min".
- **Drill labeling**: any audit row whose `is_drill=true` (see C4.0) must
  also have its triggering message tagged `[DRILL]` so observers don't page
  the lead engineer.

---

## 7. Quarterly review

Every 90 days, or after any real `rollback_completed` event, the rotation
table and escalation path are re-reviewed:

1. Is every name still active and available?
2. Did any hop time out during the previous quarter? If yes, what slowed it?
3. Are the runbooks still describing the live system, or have new fields
   (e.g. C3.0.1 added 11) made them stale?
4. Do new on-call candidates need training before the next promotion phase?

The review is recorded as a journal entry plus a PR diff to this file. If no
diff is needed, the journal entry must still explicitly say "no change required".
