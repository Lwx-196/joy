# 客户失败模式与兜底 — AI 增强服务

> 对客户公开版。每种失败模式必须有：客户体感 + 兜底动作 + 客户可见文案 + 计费规则。
> 来源：plan `.claude/plan/comfyui-vlm-ga.md` "Customer Failure Mode Surface" 5-mode 表。
>
> **⚠️ 实现状态（Beta / PLANNED-FOR-GA）**：本文档描述 **GA 目标行为**。下列客户可见信号
> 为 **GA deliverable，尚未出货**，对客户发布前必须由 Stream A（交付明细 flag）/ Stream B
> （后台 banner / status page）真实实现：
> - 交付明细 flag `ai_enhancement_failed` / `bucket_hit` / `vlm_gate_blocked`（F1/F2/F4，**planned**）
> - eligibility 百分比 banner（F2，**planned**）
> - status page（`<<STATUS_PAGE_URL_PENDING>>`，**planned**）
>
> 标 `<<*_PENDING>>` 的数字等 A 流 C4.5 telemetry 回填；标 **(planned)** 的信号在真实出货前
> 不得作为对客户的现状承诺。

## 总览

| Failure mode | Customer 体感 | 我方兜底 | 计费 |
|---|---|---|---|
| F1. ComfyUI dies mid-render | 收到 layout-only 成品（无 AI 增强） | 自动 fallback + status 标记 | **不计费** |
| F2. P10/P25 bucket miss | 部分 case 有增强 / 部分没有 | banner 显示 eligibility % | 走 base 价（不计 AI） |
| F3. Auto-rollback fires | AI 增强突然暂停 | status banner + CS broadcast | **不计费**（影响窗口内） |
| F4. VLM gate blocks publish | 成品已生成但无法交付 | stage / ETA / 人工 review 通道 | **不计费**（成品被拦） |
| F5. Queue saturation / GPU OOM | latency 几分钟 → 几小时 | queue position / ETA / SLA 时钟 | 视 breach 级别（见 SLA） |

---

## F1. ComfyUI dies mid-render（中途崩溃）

**客户体感**：提交的 case 最终拿到 layout-only 成品，没有 AI 增强。

**我方兜底**：
- 自动 fallback 到 layout-only path
- `silent_fail_count` metric 实时监控（C3.0.1 dashboard）
- 单 case 标记 `ai_enhancement_failed=true` 在交付明细 **(planned — GA deliverable，尚未出货)**
- CS 触达：当日累计 ≥ `<<THRESHOLD_PENDING>>` 时主动通知

**客户可见文案**（在交付物 / 后台明细）：
> "AI 增强本次不可用，已使用 layout-only 兜底交付，不计费。如需重试请联系 CS。"

**计费**：**不计费**（与 `billing.md` §2.2 同步）

---

## F2. P10/P25 bucket miss（灰度未命中）

**客户体感**：同一批 case 部分走了 AI 增强 / 部分没有。

**我方兜底**：
- 客户后台 banner 实时显示当前 eligibility 百分比（来自 manifest state）**(planned — GA deliverable)**
- 在 case 交付明细标记 `bucket_hit=true/false` **(planned — GA deliverable)**

**客户可见文案**（status banner / FAQ）：
> "AI 增强当前处于 Beta 阶段，约 {{eligibility_pct}}% 的符合条件的 case 会自动走 AI 增强路径。未命中的 case 使用 layout-only 标准交付，按 base 价计费。"

**计费**：未命中 → 走 base 价（不计 AI enhancement fee）

---

## F3. Auto-rollback fires（自动 demote / 服务保护暂停）

**客户体感**：之前能用 AI 增强的 case 突然全走 layout-only。

**我方兜底**：
- `promotion_rollback_applier` 自动 demote manifest state（W13 hardening 三层防御）
- C3.0 alerting → CS broadcast：邮件 / 飞书 + status page banner ≤ 30 分钟
- Post-mortem 48h 内交付

**客户可见文案**（CS broadcast template）：
> "AI 增强服务暂时降级到 layout-only 模式，incident id `<<INC_ID>>`，预计恢复时间 `<<ETA>>`。窗口内所有 case 不计费，影响窗口的 case 列表见附件。我们将在 48 小时内提交 post-mortem。"

**计费**：**不计费**（hard breach 100% credit，与 `sla.md` §4 / `billing.md` §4 同步）

---

## F4. VLM gate blocks publish（VLM 裁判拦截）

**客户体感**：增强成品已生成但未交付，case 卡在 review 状态。

**我方兜底**：
- 成品标记 `vlm_gate_blocked=true` + 拦截原因（quality_below_threshold / clinical_review_required / 等）**(planned — GA deliverable)**
- 客户后台 / 邮件通知：当前 stage / ETA / 是否进入人工 review
- 人工 review 通道：CS → Clinical owner 7 工作日内决议

**客户可见文案**（case 明细 / CS 工单）：
> "本 case 的 AI 增强成品未通过质量裁判，原因：{{reason}}。当前 stage：{{stage}}，预计决议 ETA：{{eta}}。决议结果：人工 review 通过 → 交付；不通过 → 走 layout-only 兜底不计费。"

**计费**：**不计费**（成品未交付）— 与 `billing.md` §2.2 同步

---

## F5. Queue saturation / GPU OOM（队列拥塞）

**客户体感**：原本 ~`<<TYPICAL_LATENCY_PENDING>>` 秒 → 拉长到分钟 / 小时级。

**我方兜底**：
- 客户后台显示 queue position + ETA + SLA breach 时钟
- 拥塞触发 W11 resize（≤1280 长边）自动 fallback + dynamic timeout
- `paused_state_write_failed` OSError 兜底（W13 H-5）→ stop_loss_halt 升级保护
- 持续 ≥ `<<THRESHOLD_PENDING>>` 分钟拥塞 → 主动 throttle 新 case + CS 通知

**客户可见文案**（后台 / status page）：
> "AI 增强队列当前拥塞，您的 case 排在第 {{position}} 位，预计完成时间 {{eta}}。SLA p95 阈值剩余 {{remaining}} 分钟，超过将触发 breach credit。如需立即交付，可联系 CS 切换 layout-only path（不计 AI fee）。"

**计费**：
- 最终成功交付 → 计费
- p95 breach → 触发 SLA credit（见 `sla.md` §4）
- 客户主动切 layout-only → 不计 AI fee

---

## 通用支持

- **Incident hotline**：`<<INCIDENT_HOTLINE_PENDING>>`（hypercare 期内 ≤ 1h 人工响应）
- **Status page**：`<<STATUS_PAGE_URL_PENDING>>`
- **数据 / audit 调阅**：合规请求 → 您的 Legal 接口人 → 我方 Legal
- **CS 工单**：单 case 异常请提供 `case_id` + 触发时间 + 期望成品 + 实际收到

## References

- `sla.md` — SLA 承诺与 breach credit
- `billing.md` — 计费规则
- 内部对照：plan `.claude/plan/comfyui-vlm-ga.md` "Customer Failure Mode Surface" 表 + `docs/commercial/sla-template.md`
- 工程兜底：W11 resize / W13 manifest hardening / C3.0 alerting / `promotion_rollback_applier`
