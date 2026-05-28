# Billing Policy — AI Enhancement Cost Recovery

> Phase C5.3.1 deliverable. Pass-through vs absorb vs bundled vs tiered 决议。
> Status: **DRAFT** — Risk/Finance（D4 accountable）签字 + Legal（D7）审阅 + Sales（D7 R）执行。

## 1. 背景

ComfyUI + VLM 增强引入两类新成本：
- **可变成本**：VLM judge API（`vlm_usage_log.cost_usd`）+ ComfyUI GPU 时间（amortized）
- **固定成本**：GPU rental / electricity / hardware（自托管）+ 人工 case review（hypercare 期内）

端到端真单价：`$<<COST_PER_CASE_PENDING — 等 C5.3 cost 模型>>` / case

需决议：增量成本如何在我方 / 客户之间分摊。

## 2. 四种 billing policy 对比

### 2.1 Pass-through（透传）

> 客户为每次 AI 增强单独付费。

| 项 | 内容 |
|---|---|
| 计费公式 | base_case_fee + ai_enhancement_unit_fee × count |
| 客户体感 | 高透明 / 单价直观 / 但单价敏感 |
| 我方风险 | 低（成本完全转嫁） |
| 客户风险 | 高（成本不可预测） |
| 实现 | 现有 case 计费 + 新 `ai_enhancement_unit_fee` SKU |
| 适合 | 大客户、用量稳定、要求审计明细 |

### 2.2 Absorb（吸收）

> 我方承担全部 AI 增强成本，不向客户单独收费。

| 项 | 内容 |
|---|---|
| 计费公式 | base_case_fee（不变） |
| 客户体感 | 最佳（零增量） |
| 我方风险 | 极高（毛利率被压） |
| 客户风险 | 无 |
| 实现 | 不改计费，仅成本端核算 |
| 适合 | 早期 beta 阶段、低 volume、获客优先 |

### 2.3 Bundled（打包）

> 升级为更高定价 tier，AI 增强作为 tier 价值的一部分，不单独定价。

| 项 | 内容 |
|---|---|
| 计费公式 | premium_tier_fee = base × multiplier；多 case 月费包 |
| 客户体感 | 高价值 / "AI Premium" 包装 |
| 我方风险 | 中（取决于 volume 预测） |
| 客户风险 | 中（被迫升级） |
| 实现 | 新建 SaaS plan tier + 客户迁移流程 |
| 适合 | 中型客户、长期合作、品牌升级 |

### 2.4 Tiered（阶梯计量）

> 增强次数前 N 次免费 / 超量 pass-through。

| 项 | 内容 |
|---|---|
| 计费公式 | first_N_per_month=0；超过 → pass-through |
| 客户体感 | 平衡（轻度免费 / 重度可预算） |
| 我方风险 | 中（依赖客户分布尾部） |
| 客户风险 | 低-中 |
| 实现 | quota 计数器 + 月度重置 + 超量提醒 |
| 适合 | 长尾客户、用量不均 |

## 3. 决策矩阵（待 Risk/Finance 拍板）

| 客户分层 | 推荐 policy | 理由 |
|---|---|---|
| Beta 客户（C4.5 soft launch） | **Absorb** | 获客 + telemetry 采集优先 |
| 现有付费客户（C4 p100 后 1 季度） | **Tiered** | 平滑过渡 + 保留升级动力 |
| 新签约客户（GA 后） | **Bundled** | "AI Premium" 高价值定位 |
| 大客户 / 企业 | **Pass-through** + 量优惠 | 审计透明 + 按量阶梯 |

**最终决议**：`<<DECISION_PENDING — D4 Risk/Finance accountable>>`

## 4. Failure-mode → billing 规则

> 与 `docs/customer/failure-modes.md` 双向对齐。

| Failure mode | 是否计费 | 依据 |
|---|---|---|
| ComfyUI dies mid-render（layout fallback） | **不计费** | 客户未拿到 AI 增强成品 |
| Auto-rollback fires 窗口内 | **不计费** | 服务不可用 |
| VLM gate blocks publish | **不计费**（成品被阻拦） | 客户未实际使用 |
| Queue saturation 内但最终成功 | **计费** | 服务最终交付 |
| P10/P25 bucket miss（未被灰度命中） | **不计费**（未走 AI 路径） | 走 layout-only 默认价 |

SLA breach credit 见 `docs/commercial/sla-template.md` §4 与本文件同步条款。

## 5. 客户合同 clause

见 `docs/commercial/legal-signoff-checklist.md` §3 草案 3.X.4 / 3.X.5。

## 6. 内部财务批准流程

1. C5.3 cost 模型完成（A 流 + Risk/Finance 联合）→ 单价透明
2. 本 policy DRAFT → Risk/Finance（D4）+ Legal（D7 C）+ Sales（D7 R）三方会签
3. 决议落 `delivery/c5-billing-policy-YYYY-MM-DD.md`
4. SaaS 计费系统 SKU 更新（如 Pass-through / Tiered 需新 SKU）
5. 客户 onboarding 流程 + CS 培训（C5.6 hypercare）

## 7. References

- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.3.1
- 关联：`docs/commercial/raci-matrix.md` D4 / D7 / `docs/commercial/sla-template.md` / `docs/customer/billing.md` / `docs/customer/failure-modes.md`
- 数据源：C5.3 cost 模型（A 流 telemetry 回填后）
