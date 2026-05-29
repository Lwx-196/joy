# Commercial Decision Log

> Phase C5.0.1 deliverable. Append-only record of the **D1–D10** governance
> decisions defined in [`../raci-matrix.md`](../raci-matrix.md) §2. This
> directory + [`_TEMPLATE.md`](_TEMPLATE.md) discharge raci-matrix.md §5
> exit-criteria line "Decision log 模板 + 目录建好".

## How to use

1. A decision is finalized per the RACI flow (`../raci-matrix.md` §3).
2. Copy [`_TEMPLATE.md`](_TEMPLATE.md) to `D{N}-YYYY-MM-DD.md`.
3. Fill every field (no placeholders) and link the evidence.
4. Commit alongside the PR / approval ticket so the record is git-reviewable.

## Decision points (from `../raci-matrix.md` §2)

`A (accountable)` is the role-type fixed by the RACI matrix; the actual
sign-off person + outcome live in each `D{N}-*.md` record and start **pending**
until the business team signs off (raci-matrix.md §5).

| ID | 决策点 | A (accountable role) | Phase | Status |
|---|---|---|---|---|
| D1 | fumei brand 解锁 | Product | C5.1 | pending |
| D2 | SLA latency p95 commit | Risk/Finance | C5.2 | pending |
| D3 | SLA quality 胜率 commit | Product | C5.2 | pending |
| D4 | Billing policy 决议 | Risk/Finance | C5.3.1 | pending |
| D5 | AI 增强可见标识 | Legal | C5.0.3 | pending |
| D6 | 客户通知 timing & 口径 | Product | C5.5 | pending |
| D7 | 客户合同 clause 升级 | Sales | C5.3.1 | pending |
| D8 | p100 → 全量解禁 | Product | post-C4.5 | pending |
| D9 | Rollback 触发后 broadcast 决策 | CS | C3.0 | pending |
| D10 | Hypercare 首月解除 | Product | C5.6 | pending |

## Legal checklist ↔ RACI decision mapping

Bidirectional alignment with
[`../legal-signoff-checklist.md`](../legal-signoff-checklist.md)
(raci-matrix.md §5 exit-criteria line "与 legal-signoff-checklist.md 双向引用对齐").

| Legal item | RACI decision | Notes |
|---|---|---|
| L10 (AI 标识方案 A/B/C) | **D5** (AI 增强可见标识) | Legal accountable |
| L16 (客户合同 ai_enhancement clause) | **D7** (客户合同 clause 升级) | Sales A, Legal C |
| L13 (audit_log retention ≥ 3 年 / `RETENTION_DAYS`) | **⚠ gap** | legal-signoff §4 routes "retention 期限" to this decision log, but raci-matrix §2 has **no dedicated D-row** for data retention. Business team to decide: fold under D5 (Legal accountable) or add a D11. Flagged here, not resolved by engineering. |

## References

- [`../raci-matrix.md`](../raci-matrix.md) — decision points + RACI table + record template
- [`../legal-signoff-checklist.md`](../legal-signoff-checklist.md) — L1–L18 legal review
