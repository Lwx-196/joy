# 上线公告模板 — AI 增强服务 GA

> Phase C5.5 deliverable。内部 + 客户双口径。
> 决策：Product（D6 A）+ CS（D6 R）+ Legal（D6 C）+ Sales（D6 C）联签。

---

## 一、内部公告模板（飞书 / 邮件 / 内网）

### 标题
> 【内部公告】ComfyUI + VLM AI 增强服务 GA — 生效日 `<<GA_DATE>>`

### 正文

```
团队：

经过 6-9 周的灰度（C0.5 → C5）+ 1 周 soft launch（C4.5）观察，ComfyUI + VLM
AI 增强服务（`portrait_focal_enhance_v1`）正式进入 GA。

【关键数据】
- 灰度阶段：shadow → p10 → p25 → p50 → p100 全绿（≥ 5 天严格 SLO 通过）
- N=10 + soft launch 真客户胜率：<<TELEMETRY_PENDING>>%（红线 60%）
- p95 latency：<<TELEMETRY_PENDING>>s
- 增强成功率：<<TELEMETRY_PENDING>>%（红线 99%）
- Rollback drill：staging 演练成功 + drill_lock + drill_id 隔离
- Audit retention：3 年合规（C3.0.4）
- Legal sign-off：L1-L18 全通过

【上线动作】
1. <<GA_DATE>> 00:00 关闭 RENDER_AUTO_AI_ENABLED env override（manifest 驱动）
2. 客户 broadcast（CS 推送）+ status page 公告
3. Hypercare 启动：首月值班表 + 1h 升级响应
4. 计费切换：Beta absorb → GA 阶段对应 tier（详见 billing-policy.md）

【关键角色】
- A 流（工程）on-call：<<ENG_ONCALL>>
- C 流（CS）one-stop：<<CS_LEAD>>
- Incident escalation：<<INC_HOTLINE>>
- Hypercare standup：每周 <<WEEKDAY>> <<TIME>>，首月每日 stand-up

【相关文档】
- 内部：docs/commercial/raci-matrix.md / sla-template.md / billing-policy.md / legal-signoff-checklist.md
- 客户：docs/customer/faq.md / sla.md / billing.md / failure-modes.md
- 运营：delivery/c5-sla-commitment.md / c5-cost-model.md / deploy-runbook.md

【已知风险与监控】
- ComfyUI 服务 OOM → W11 resize ≤1280 + auto-retry + alerting
- VLM gate quality 漂移 → C3.0 dashboard + monthly review
- 客户 latency 不满 → SLA breach credit policy 落地

请相关同学在 <<GA_DATE>> 前完成各自验收清单，hypercare 期间任何降级 / 异常
请第一时间到 <<INC_HOTLINE>>。

—— Product / Engineering / CS / Sales / Risk / Legal 联署
```

---

## 二、客户公告模板（邮件 / 后台 banner / status page）

### 邮件标题
> 【服务升级】AI 增强服务正式上线 — `<<GA_DATE>>`

### 邮件正文

```
尊敬的 <<CLIENT_NAME>>：

感谢您参与我们的 AI 增强服务 Beta 阶段（C4.5 soft launch）的测试与反馈。
基于多轮灰度验证 + 法务合规审查 + 真实客户案例评估，AI 增强服务
（portrait focal enhance v1）将于 <<GA_DATE>> 正式上线。

【您将获得什么】
✓ 符合条件的医美案例图像自动获得 AI 局部精修
✓ 端到端 p95 latency ≤ <<TELEMETRY_PENDING>>s
✓ 月度 SLA 报告主动推送
✓ 失败兜底：任何无法增强的 case 自动 layout-only 交付且不计费
✓ 完整的合规标识 / 审计日志 / 数据保留承诺

【需要您注意】
- AI 增强后图像携带 <<AI_LABEL_DECISION>> 标识，对外发布请按平台规则二次披露
- 您原合同需补签 ai_enhancement clause（Sales 接口人将单独联系）
- Hypercare 首月：CS 1h 响应 + 周度对账 + 每月 SLA 复盘

【计费变化】
- Beta 期：absorb（零增量，已经历）
- GA 后：按您的客户分层应用对应 policy（pass-through / tiered / bundled），
  具体方案 Sales 在合同附件中确认。

【关键文档（已同步至客户后台）】
- FAQ：常见问题
- SLA：服务等级承诺与 breach credit
- Billing：计费规则与不计费场景
- Failure modes：失败模式与兜底文案

【联系】
- 您的专属 CS：<<CS_CONTACT>>
- Incident hotline：<<INCIDENT_HOTLINE>>
- 合规 / 数据请求：您的 Legal 接口人 → 我方 Legal

期待与您共同推进 AI 增强服务的下一阶段。

—— <<COMPANY_NAME>> Product + CS 团队
<<GA_DATE>>
```

### 后台 banner（短版）

```
🎉 AI 增强服务 <<GA_DATE>> 正式上线
当前 eligibility：{{eligibility_pct}}%（实时） | SLA：p95 ≤ <<TELEMETRY_PENDING>>s
查看 → FAQ | SLA | Billing | 失败兜底
```

### Status page 摘要

```
Service: ComfyUI + VLM Portrait Focal Enhance v1
Status: Operational (GA)
Manifest state: <<MANIFEST_STATE>>  (eligibility: {{eligibility_pct}}%)
Latest incident: <<INCIDENT_OR_NONE>>
Last 30-day SLA:
  - Latency p95: <<TELEMETRY_PENDING>>s (target: <<TELEMETRY_PENDING>>s)
  - Quality win-rate: <<TELEMETRY_PENDING>>% (target: ≥60%)
  - Availability: <<TELEMETRY_PENDING>>% (target: ≥99%)
Next monthly SLA report: <<NEXT_REPORT_DATE>>
```

---

## 三、关闭 `RENDER_AUTO_AI_ENABLED` env override（工程上线动作）

> A 流（工程）在 GA 当日执行：

1. 确认 `case-workbench-ai/promotion/manifest.json` state = `p100`
2. PR：删除 / 注释 `RENDER_AUTO_AI_ENABLED` env override（manifest 驱动）
3. CI 全绿 + smoke 真客户 case 1 例验证
4. 关 env override commit message：
   ```
   chore: close RENDER_AUTO_AI_ENABLED env override for GA

   manifest p100 + C4.5 1-week soft launch passed.
   AI enhancement now fully driven by promotion_manifest_loader.

   Refs: plan C5.5
   ```

## 四、公告 timing 矩阵

| T-X 时间 | 受众 | 渠道 | Owner |
|---|---|---|---|
| T-30d | 全员 + Sales | 飞书 + 邮件 | Product |
| T-14d | 客户 KAM | 邮件 + 一对一 | Sales |
| T-7d | 全客户 | 邮件 | CS |
| T-3d | 全员 hypercare 名单 | 飞书 | CS lead |
| T-0 | 客户 + 内部 + status page | 全渠道 | CS + Eng on-call |
| T+1d | Hypercare standup 启动 | 飞书 | CS lead |
| T+7d | 第一周 SLA snapshot | 邮件 + 后台 | CS |
| T+30d | 首月 hypercare 解除评估 | 内部 + 客户简报 | Product + CS |

## References

- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.5 / C5.6
- `docs/commercial/raci-matrix.md` D6
- `docs/customer/faq.md` / `sla.md` / `billing.md` / `failure-modes.md`
- `delivery/c5-sla-commitment.md` / `delivery/c5-cost-model.md`
