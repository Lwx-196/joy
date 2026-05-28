# SLA Template — ComfyUI + VLM 增强服务

> Phase C5.0.2 deliverable. 基于 C4.5 真 telemetry。
> Status: **TEMPLATE** — 所有 `<<TELEMETRY_PENDING>>` 数字在 A 流 C4.5 soft launch 1 周后由真实数据回填。
> Owner（A）：Risk/Finance（D2 / D3）

## 1. 适用范围

| 项 | 范围 |
|---|---|
| 服务名 | ComfyUI + VLM Portrait Focal Enhance v1 |
| Workflow scope | `portrait_focal_enhance_v1`（GA 锁定） |
| 适用 brand | <<BRAND_SCOPE_PENDING — 见 C5.1 fumei 解锁决策>> |
| 适用 case 类型 | md_ai / meiji_ai（C5.1 决策后可扩） |
| 测量窗口 | 滚动 7 天，DST-safe UTC |
| Beta 标识 | Beta — 灰度阶段（p25 / p50 / p100）按 manifest state 实时披露 |

## 2. SLA 维度

### 2.1 Latency（端到端：API → ComfyUI render → 交付）

| 指标 | 目标 | 测量 | 数据源 | 当前基线 |
|---|---|---|---|---|
| p50 | ≤ `<<TELEMETRY_PENDING>>` s | render_jobs.finished_at - started_at | `render_jobs` | `<<TELEMETRY_PENDING>>` |
| p95 | ≤ `<<TELEMETRY_PENDING>>` s | 同上 | 同上 | `<<TELEMETRY_PENDING>>` |
| p99 | ≤ `<<TELEMETRY_PENDING>>` s | 同上 | 同上 | `<<TELEMETRY_PENDING>>` |

**Breach handling**：连续 24h p95 超目标 ≥ 20% → 触发 SLO violation → C3.0 alerting → CS broadcast → credit policy（见 §4）

### 2.2 Quality（VLM 胜率 — 增强 vs layout-only）

| 指标 | 目标 | 测量 | 数据源 | 当前基线 |
|---|---|---|---|---|
| 胜率 | ≥ `<<TELEMETRY_PENDING>>` %（最低 60% 红线） | VLM judge `winner` == enhancement | `vlm_judgements` | N=10 + C4.5 真客户 case = `<<TELEMETRY_PENDING>>`% |
| 样本量 | ≥ `<<TELEMETRY_PENDING>>` / 7 天 | sample_size from `slo_thresholds.json` | `promotion_slo_check` audit | min 974（PR #31 calibrate） |

**Breach handling**：连续 7 天胜率 < 红线 → `promotion_rollback_applier` 自动 demote 一档 → CS broadcast。

### 2.3 Availability（增强成功率）

| 指标 | 目标 | 计算 | 数据源 |
|---|---|---|---|
| 增强成功率 | ≥ `<<TELEMETRY_PENDING>>` %（最低 99%） | 1 - (silent_fail_count + gpu_oom_count + paused_state_write_failed) / total_eligible | `ai_usage` / `promotion_audit_log` |
| Layout fallback 透明度 | 100% 客户可见 | failure-mode 文案 + billing not-charged | `docs/customer/failure-modes.md` |

**Breach handling**：详见 `docs/customer/failure-modes.md`。

### 2.4 Error rate（per 1k case）

| 指标 | 目标 | 数据源 |
|---|---|---|
| Total errors / 1k case | ≤ `<<TELEMETRY_PENDING>>` | promotion_audit_log + ops_audit_log |
| ComfyUI dies mid-render / 1k | ≤ `<<TELEMETRY_PENDING>>` | silent_fail_count metric |
| Queue saturation / 1k | ≤ `<<TELEMETRY_PENDING>>` | GPU OOM + retry count |
| VLM gate blocks / 1k | ≤ `<<TELEMETRY_PENDING>>` | violations count |

## 3. 测量与披露

- 内部 dashboard：`<<DASHBOARD_URL_PENDING>>`（C3.0 deliverable）
- 客户可见 status page：`<<STATUS_PAGE_URL_PENDING>>`（C5.5 deliverable）
- 每月 SLA 报告：`delivery/sla-monthly/YYYY-MM.md`（C5.6 hypercare 期内每周）

## 4. Breach & credit policy

> 由 Risk/Finance（D2 accountable）签字，见 `docs/commercial/billing-policy.md` 同步条款。

| Breach 级别 | 触发 | 客户 credit |
|---|---|---|
| Soft（单维度月度未达） | 当月 SLA 任一维度月度 < 目标 | 当月相关 case `<<CREDIT_PCT_PENDING>>`% 抵扣 |
| Hard（auto-rollback fire） | manifest 被自动 demote | 影响窗口内全部 case credit 100% |
| Catastrophic（连续 2 周 hard） | 任意 7 天 + 7 天串联 | 暂停服务 + 一对一升级 + 商务谈判 |

## 5. Beta scope 披露

> 灰度阶段必须对客户显式披露当前 eligibility 比例。

模板文案：
> "AI 增强当前处于 Beta 阶段，约 {{eligibility_pct}}% 的符合条件的 case 会自动走 AI 增强路径，其余暂使用 layout-only 交付。我们会在每周的客户简报中同步进展。"

eligibility_pct 来源：`promotion_manifest_loader` 当前 state（shadow=0 / p10=10 / p25=25 / p50=50 / p100=100）。

## 6. Telemetry 回填责任

| 项 | 占位 | 回填责任 | Trigger |
|---|---|---|---|
| latency p50/p95/p99 | `<<TELEMETRY_PENDING>>` | A 流 C4.5 后 | soft launch 1 周完整窗口 |
| 胜率 | `<<TELEMETRY_PENDING>>` | A 流 C2 + C4.5 | N=10 + 真客户 ≥ 50 case |
| availability | `<<TELEMETRY_PENDING>>` | A 流 C3.0 dashboard | 7 天滚动 |
| error rate | `<<TELEMETRY_PENDING>>` | A 流 C3.0 + C4.5 | 同上 |
| dashboard / status page URL | `<<URL_PENDING>>` | A 流 C3.0 部署后 | URL fix |
| credit % | `<<CREDIT_PCT_PENDING>>` | Risk/Finance | billing-policy 决策 |

## 7. References

- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.0.2 / C5.2
- 关联：`docs/commercial/raci-matrix.md` / `docs/commercial/billing-policy.md` / `docs/customer/sla.md`
- 数据源：`backend/services/promotion_slo_monitor.py` / `case-workbench-ai/promotion/manifest.json` / `slo_thresholds.json`
