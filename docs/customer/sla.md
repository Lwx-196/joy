# 客户 SLA — AI 增强服务（Beta）

> 对客户公开版。Telemetry 数字待 A 流 C4.5 soft launch 1 周窗口后回填。
> 内部对照版：`docs/commercial/sla-template.md`。

## 1. 适用与生效

- **服务名**：ComfyUI + VLM Portrait Focal Enhance v1
- **生效日期**：`<<GA_DATE_PENDING>>`（GA 公告日）
- **Beta 阶段**：当前服务为 Beta，承诺为 **best-effort**，不构成合约 SLA；GA 后切换为合约 SLA
- **适用 case 类型**：md_ai / meiji_ai（GA 后可能扩展，届时单独公告）
- **覆盖范围**：仅符合 `portrait_focal_enhance_v1` workflow scope 的 case

## 2. SLA 承诺

### 2.1 时延（Latency）

| 指标 | 承诺 |
|---|---|
| 单 case 增强 p50 | ≤ `<<TELEMETRY_PENDING>>` 秒 |
| 单 case 增强 p95 | ≤ `<<TELEMETRY_PENDING>>` 秒 |
| 极端尾延迟 p99 | ≤ `<<TELEMETRY_PENDING>>` 秒 |

> 时延从客户提交 case 进入 render queue 起，至 AI 增强成品落库为止。

### 2.2 质量

- **VLM 胜率（增强 vs layout-only）**：≥ `<<TELEMETRY_PENDING>>`%（最低红线 60%）
- 月度未达红线 → 自动 demote（触发 auto-rollback）+ credit policy 介入

### 2.3 可用性

- **增强成功率**：≥ `<<TELEMETRY_PENDING>>`%（最低 99%）
- **fallback 透明度**：100% — 任何无法增强的 case 都会拿到 layout-only 兜底成品且不计费

### 2.4 服务窗口

- **7×24 自动服务**
- **人工 incident 响应**：业务日 4 小时 / 非业务日 8 小时（见 §6 联系）
- **Hypercare 期（GA 后首月）**：人工响应升级到 1 小时（见 `launch-announcement-template.md`）

## 3. Beta scope 实时披露

我们会在客户后台 banner + `<<STATUS_PAGE_URL_PENDING>>` 同步当前灰度覆盖：

> "AI 增强当前处于 Beta 阶段，约 {{eligibility_pct}}% 的符合条件的 case 会自动走 AI 增强路径，其余暂使用 layout-only 交付。"

eligibility_pct 实时反映 `promotion_manifest_loader` 当前 state：
- Shadow：0%
- p10：10%
- p25：25%
- p50：50%
- p100：100%

## 4. Breach（违约）判定与 credit

> 与 `billing.md` §SLA breach credit 双向对齐；内部对照 `docs/commercial/sla-template.md` §4。

| 级别 | 判定 | Credit |
|---|---|---|
| **Soft** | 当月任一维度 < 目标 | 当月相关 case `<<CREDIT_PCT_PENDING>>`% 抵扣 |
| **Hard** | manifest 被 auto-rollback 在 7 天滚动窗口内 fire | 影响窗口内全部 case credit **100%** |
| **Catastrophic** | 连续 2 周 hard | 暂停服务 + 一对一升级 + 商务谈判 |

申请 credit：联系 CS，提供 incident id / 时间窗口。我方将基于 audit_log 自动核对，credit 在次月账单生效。

## 5. 客户责任

- 上传图像合规、肖像授权链完整（合同 3.X.6）
- 监控 status page，配合我方在 hard breach 期内暂停业务
- Incident 报告必须提供：case_id / 触发时间 / 期望成品 / 实际收到

## 6. 沟通与升级

| 级别 | 触发 | 渠道 | 响应 |
|---|---|---|---|
| L0 自助 | FAQ / status page 能查 | 自助 | 即时 |
| L1 CS | 单 case 异常 | CS 工单 | 业务日 4h |
| L2 Incident | 服务降级 / hard breach | `<<INCIDENT_HOTLINE_PENDING>>` | ≤ 1h（hypercare）/ 2h（GA 稳态） |
| L3 升级 | 连续 2 周 hard / catastrophic | Product / Sales / 客户 sponsor | ≤ 4h |

## 7. 透明度承诺

- 每月 SLA 报告：CS 主动推送 + 客户后台可下载
- 重大 incident（hard breach 以上）：48h 内 post-mortem
- SLA 调整（如新增维度 / 收紧阈值）：30 天提前通知 + 合同修订

## 8. References

- 内部对照：`docs/commercial/sla-template.md`
- 计费：`billing.md`
- 失败兜底：`failure-modes.md`
- 合规：合同 ai_enhancement clause 3.X.4
