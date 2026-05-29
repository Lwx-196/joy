# C5.2 SLA Commitment (Final) — TELEMETRY PLACEHOLDER

> Phase C5.2 deliverable。基于 C5.0.2 template，**等 A 流 C4.5 soft launch 真 telemetry 回填**。
> Status: **PLACEHOLDER** — Risk/Finance（D2 A）+ Product（D3 A）签字前不可对外发布。

## 1. 回填触发条件

A 流交付下列证据后，C 流回到此 PR 把 `<<TELEMETRY_PENDING>>` 替换为真数据：

- [ ] C3 p10/p25/p50/p100 累计 ≥ `<<MIN_SAMPLE_PENDING>>` 真客户 case
- [ ] C4.5 soft launch 1 周完整窗口 SLO 全绿
- [ ] `slo_thresholds.json` baseline 已 `calibrate_slo_baseline --apply` 写入
- [ ] N=10 + soft launch 胜率 ≥ 60%
- [ ] 5 天严格 SLO（`sample_size >= min` AND `recommendation=continue`）通过

## 2. 最终 SLA 承诺（placeholder）

### 2.1 Latency

| 指标 | 承诺 | 基线测量 |
|---|---|---|
| p50 | ≤ `<<P50_PENDING>>` s | C4.5 实测 |
| p95 | ≤ `<<P95_PENDING>>` s | C4.5 实测 |
| p99 | ≤ `<<P99_PENDING>>` s | C4.5 实测 |

### 2.2 Quality

| 指标 | 承诺 | 数据源 |
|---|---|---|
| VLM 胜率 | ≥ `<<WIN_RATE_PENDING>>`%（红线 60%） | `candidate_lineage.vlm_judge_result_json`（经 `promotion_slo_monitor`） |
| 月度样本量 | ≥ `<<SAMPLE_SIZE_PENDING>>` | `slo_thresholds.json` |

### 2.3 Availability

| 指标 | 承诺 |
|---|---|
| 增强成功率 | ≥ `<<AVAIL_PENDING>>`%（红线 99%） |
| Fallback 透明度 | 100% |

### 2.4 Error rate

| 指标 | 承诺 |
|---|---|
| Total errors / 1k case | ≤ `<<ERR_PENDING>>` |
| ComfyUI dies mid-render / 1k | ≤ `<<COMFY_DIE_PENDING>>` |
| Queue saturation / 1k | ≤ `<<QUEUE_PENDING>>` |
| VLM gate blocks / 1k | ≤ `<<VLM_BLOCK_PENDING>>` |

### 2.5 Breach credit policy

| Breach 级别 | Credit |
|---|---|
| Soft | `<<SOFT_CREDIT_PCT_PENDING>>`% 当月相关 case |
| Hard | 100% 影响窗口 case |
| Catastrophic | 暂停 + pro-rate 退款 |

## 3. Sign-off 顺序

1. A 流回填所有 placeholder → 自检 placeholder 0 残留
2. Risk/Finance（D2 A）latency / credit 签字
3. Product（D3 A）quality / availability 签字
4. Legal（D6 C）措辞 review
5. Sales（D6 C）合同 clause 一致性 review
6. CS（D6 R）客户口径 review
7. PR 合并 → `docs/customer/sla.md` 同步 `<<TELEMETRY_PENDING>>` 替换 → 客户 GA broadcast

## 4. References

- 内部 template：`docs/commercial/sla-template.md`
- 客户对外版：`docs/customer/sla.md`
- 数据源：`backend/services/promotion_slo_monitor.py` / `slo_thresholds.json` / `candidate_lineage.vlm_judge_result_json` / `simulation_jobs` / `ops_audit_log`
- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.0.2 / C5.2
- RACI: `docs/commercial/raci-matrix.md` D2 / D3
