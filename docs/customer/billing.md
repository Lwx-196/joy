# 客户计费 — AI 增强服务

> 对客户公开版。内部对照：`docs/commercial/billing-policy.md`。
> Billing 决议由 Risk/Finance（D4）+ Legal（D7）+ Sales（D7 R）联签。

## 1. 当前阶段计费

| 阶段 | Billing 策略 | 客户体感 |
|---|---|---|
| Beta（C4.5 soft launch 以前） | **Absorb** — 我方承担，零增量 | 无新增收费 |
| GA 后过渡（首季度） | **Tiered** — 前 N 次免费 / 超量按量 | 免费配额 + 透明超量 |
| GA 稳态 | 按客户分层（见 §3） | 与销售商定 |

> 当前 Beta 阶段：**任何符合条件的 case 自动 AI 增强，本服务不向客户额外计费**。

## 2. 计费规则（GA 后）

### 2.1 单位与时间

- 计费单位：单 case AI 增强（成功交付为准）
- 计费周期：自然月，每月 1 日出账，5 个工作日内 reconcile
- 时区：UTC（与 SLA 测量窗口一致）

### 2.2 不计费的场景（与 `failure-modes.md` 双向对齐）

| 场景 | 不计费原因 |
|---|---|
| ComfyUI dies mid-render → layout-only fallback | 未拿到 AI 增强成品 |
| Auto-rollback fires 窗口内 | 服务暂停 |
| VLM gate blocks publish | 成品被拦截 |
| P10/P25 bucket miss | 未走 AI 路径 |
| `paused_state_write_failed` 触发的 stop-loss halt | 服务保护暂停 |

### 2.3 计费的场景

| 场景 | 计费 |
|---|---|
| Queue saturation 内但最终交付 | 计费（按交付） |
| 客户主动暂停后恢复 | 按恢复后交付计费 |
| 客户手动重渲（layout-only path） | 不计费 AI（按 base case 费用计） |

## 3. 客户分层（GA 稳态）

> 最终 policy 由 Risk/Finance 拍板。客户实际套餐与签约时锁定。

| 客户类型 | 策略 | 公式 |
|---|---|---|
| Beta 早签 | Absorb（不变） | base_case_fee |
| 现有付费客户（过渡） | **Tiered** | first `<<N_FREE_PENDING>>` cases/month=0；超出按 `$<<UNIT_FEE_PENDING>>`/case |
| 新签 SaaS 客户 | **Bundled "AI Premium" tier** | premium_tier_fee = base × `<<MULTIPLIER_PENDING>>` |
| 企业大客户 | **Pass-through + 量优惠** | base + ai_unit_fee × count，with volume tier |

## 4. SLA breach credit

> 与 `sla.md` §4 与 `docs/commercial/sla-template.md` §4 三处同步条款。

| Breach 级别 | Credit |
|---|---|
| Soft | 当月相关 case `<<CREDIT_PCT_PENDING>>`% 抵扣 |
| Hard | 影响窗口内全部 case **100%** credit |
| Catastrophic | 暂停 + pro-rate 退款 + 商务谈判 |

申请：联系 CS，提供 incident id / 时间窗口。

## 5. 账单 & reconcile

- 月度账单：按 case_id 明细 + 计费 / 不计费标识 + breach credit
- Reconcile 窗口：客户在账单送达后 10 个工作日内可申请复核
- 审计支持：`audit_log` ≥ 3 年保留，可按合规请求调阅

## 6. 退款与终止

- Beta 期任意时间：无理由退出 AI 增强 path（layout-only 默认）
- GA 后：按合同 ai_enhancement clause 3.X.7（合规终止条款 + pro-rate 退款）
- 数据删除：终止后 7 工作日内删除原始图像（audit_log 元数据合规保留）

## 7. References

- 客户合同 ai_enhancement clause（您主合同附件）
- `sla.md` — 服务等级（含 breach credit 同步条款）
- `failure-modes.md` — 不计费场景
- 内部对照：`docs/commercial/billing-policy.md`
