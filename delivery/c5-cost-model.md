# C5.3 Cost Model (Final) — TELEMETRY PLACEHOLDER

> Phase C5.3 deliverable。等 A 流 telemetry + `vlm_usage_log.cost_usd` 真实聚合后回填。
> Status: **PLACEHOLDER** — Risk/Finance（D4 A）签字前不对外披露。

## 1. 回填触发条件

- [ ] A 流 C4.5 后 ≥ 1 周 `vlm_usage_log` 真实聚合
- [ ] `render_jobs` GPU 时长统计完成
- [ ] ComfyUI 自托管 amortized cost 财务核算完成
- [ ] 人工 case review 工时统计

## 2. 成本结构

### 2.1 可变成本（per case）

| 项 | 单价 | 数据源 |
|---|---|---|
| VLM judge API | $`<<VLM_API_PENDING>>` | `vlm_usage_log.cost_usd` |
| ComfyUI GPU 时间（amortized） | $`<<GPU_AMORT_PENDING>>` | `render_jobs.duration_ms` × GPU hourly |
| 网络 / 存储 | $`<<INFRA_PENDING>>` | 云账单 |
| **小计** | $`<<VAR_TOTAL_PENDING>>` / case | — |

### 2.2 固定成本（月度，amortize 到 per case）

| 项 | 月度 | Per case（按 `<<MONTHLY_CASES_PENDING>>` 摊销） |
|---|---|---|
| GPU rental / hardware | $`<<GPU_MONTHLY_PENDING>>` | $`<<GPU_PER_CASE_PENDING>>` |
| 电力 / 散热 | $`<<POWER_PENDING>>` | $`<<POWER_PER_CASE_PENDING>>` |
| 人工 case review（C5.6 hypercare 期内） | $`<<REVIEW_PENDING>>` | $`<<REVIEW_PER_CASE_PENDING>>` |
| **小计** | $`<<FIX_TOTAL_PENDING>>` / 月 | $`<<FIX_PER_CASE_PENDING>>` / case |

### 2.3 端到端单 case 成本

> $`<<TOTAL_PER_CASE_PENDING>>` = 可变 + 固定（按当前月度 case 量摊销）

## 3. 敏感性分析

| 月度 case 量 | Fixed 摊销 | 总单价 |
|---|---|---|
| `<<LOW_VOLUME>>` | $`<<FIX_LOW_PENDING>>` | $`<<TOTAL_LOW_PENDING>>` |
| `<<MID_VOLUME>>` | $`<<FIX_MID_PENDING>>` | $`<<TOTAL_MID_PENDING>>` |
| `<<HIGH_VOLUME>>` | $`<<FIX_HIGH_PENDING>>` | $`<<TOTAL_HIGH_PENDING>>` |

## 4. 与 billing policy 衔接

> 见 `docs/commercial/billing-policy.md`。

| Billing policy | 单价对应 |
|---|---|
| Pass-through | 客户单 case fee ≥ 总单价 × `<<MARKUP_PENDING>>` |
| Absorb | 全部成本进我方毛利成本项 |
| Bundled tier multiplier | base × `<<MULTIPLIER_PENDING>>`（覆盖 case 量预测） |
| Tiered free | 前 N 次免费成本 = `<<TIERED_COST_PENDING>>` / 客户 / 月 |

## 5. Sign-off 顺序

1. A 流交付 `vlm_usage_log` + `render_jobs` 真实聚合脚本（可选 `backend/scripts/aggregate_cost_telemetry.py`，只 SELECT）
2. Finance 财务核算（GPU rental / 电力 / 人工）
3. 本 placeholder 全部回填 → 自检 0 残留
4. Risk/Finance（D4 A）签字
5. PR 合并 + `docs/commercial/billing-policy.md` 数字同步

## 6. 聚合脚本（已实现）

`backend/scripts/aggregate_cost_telemetry.py` — 只 SELECT，不写 DB。Stream C 已交付，等 A 流 C4.5 telemetry 落地后跑出真数据。

**用法**：

```bash
# 默认 7 天窗口，JSON 输出到 stdout
python -m backend.scripts.aggregate_cost_telemetry --window 7

# 30 天窗口，原子写入 delivery 目录
python -m backend.scripts.aggregate_cost_telemetry --window 30 \
    --output delivery/c5-cost-telemetry-$(date +%F).json

# 自定义 DB（测试 / staging）
CASE_WORKBENCH_DB_PATH=/tmp/staging.db \
    python -m backend.scripts.aggregate_cost_telemetry --window 7
```

**输出 schema**：见脚本顶部 docstring。关键字段：
- `vlm_usage.total_cost_usd` / `cost_usd_per_call_avg` / `latency_ms_p50|p95` / `by_purpose` / `by_provider_model`
- `render_jobs.duration_ms_p50|p95` / `by_status`（`done` + `done_with_issues` 合计 `total_finished`）
- `simulation_jobs.by_status` + `drill_excluded`（best-effort，C3.0.4 后改成 promotion_audit_log JOIN）
- `candidate_lineage.attempts_per_case_avg` / `failure_reasons`
- `cost_per_case.vlm_api_cost_usd` + `estimated_eligible_cases`（GPU / 电力 / 人工成本由 finance 在 DB 外补）

**已知 limitations**（脚本 `limitations` 字段同步输出）：
- promotion_audit_log JOIN 未落地（C3.0.4 deliverable）
- GPU rental / 电力 / 硬件 / 人工 review 成本在 DB 外
- drill 排除当前用 `simulation_jobs.audit_json` 字符串嗅探，C3.0.4 后改 JOIN
- `render_jobs.duration` 用 `julianday` 差，不含 queue wait time

**smoke test 结果（主 worktree DB，2026-05-28，window=30）**：
- 281 finished render_jobs（74 done + 207 done_with_issues）
- 56 unique 完成 case
- 797 VLM classifier 调用（cost_usd=0，本地 mlx 模型，符合 PII 边界）
- 467 simulation_jobs（436 done / 30 failed / 1 running，0 drill exclude）

## 7. References

- 数据源：`vlm_usage_log` / `render_jobs`（含 `meta_json.ai_usage` JSON 字段，非独立表）
- 关联：`docs/commercial/billing-policy.md` / `docs/customer/billing.md`
- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.3 / C5.3.1
- RACI: `docs/commercial/raci-matrix.md` D4
