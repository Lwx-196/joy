# 客户常见问题 — AI 增强服务（Beta）

> 对客户公开版。变更前 CS（D6 R）+ Legal（D6 C）+ Product（D6 A）联审。

## 1. AI 增强是什么？

我们为符合条件的医美案例图像提供基于 AI 的局部精修（focal-region enhance）。处理通过 ComfyUI 工作流 + VLM 质量裁判完成，输出可直接用于客户的合规广告 / 内容场景。

- **工作流名称（GA 锁定）**：`portrait_focal_enhance_v1`
- **处理范围**：局部精修，不替换面部 identity，不构建虚拟形象
- **不处理**：未成年人案例、未取得肖像授权的素材、临床高敏感图像

## 2. 当前阶段

AI 增强目前处于 **Beta 阶段**，根据灰度计划逐步开放：

| 阶段 | 覆盖率 | 状态 |
|---|---|---|
| Shadow | 0% 客户可见 | A 流 C2 阶段 |
| p10 | 10% 符合条件 case | C3 灰度 |
| p25 / p50 | 25% / 50% | C3 阶段性 |
| p100 | 100% 符合条件 case | C4 |
| Soft launch | 限定客户 1 周 | C4.5 |
| GA | 全量 | C5+ |

实际开放比例随时通过 `<<STATUS_PAGE_URL_PENDING>>` 同步。

## 3. 我的图像数据怎么使用？

- **存储期限**：交付完成后 ≤ `<<RETENTION_DAYS_PENDING — Legal D5>>` 天，audit_log 保留 ≥ 3 年（合规）
- **PII**：人脸数据最小化处理，不用于第三方模型训练
- **跨境**：本期不跨境传输客户原始图像
- 详见客户合同 ai_enhancement clause（见您的主合同附件）

## 4. 增强后的图像合规吗？

我们做了三层合规保障：

1. **法务 review**：广告法 / PIPL / 医美专项《生成式 AI 服务管理办法》全清单（见 `docs/commercial/legal-signoff-checklist.md`）
2. **AI 标识**：所有增强后图像携带 [可选 A/B/C — 见法务最终决议] AI 标识，客户对外发布需按平台规则二次披露
3. **临床 review**：Clinical owner 抽检确保增强结果不夸大疗效

**客户责任**：肖像授权链 + 平台合规披露 + 内容真实性证明（合同 3.X.6）。

## 5. 如果增强失败怎么办？

详见 `failure-modes.md`。简短版：

- **任何"未实际拿到 AI 增强成品"的场景一律不计费**
- 我方提供 layout-only fallback 兜底，确保客户工作流不中断
- Auto-rollback 触发期内不计费 + status banner + CS 通知

## 6. 计费如何？

详见 `billing.md` + 合同 ai_enhancement clause。当前 Beta 阶段计费策略：

- Beta 期：absorb（我方承担，客户零增量）
- GA 后：按客户分层（pass-through / tiered / bundled）— Risk/Finance 决策中

## 7. 谁能联系？

| 场景 | 联系方式 |
|---|---|
| 日常使用 / 问题反馈 | CS（您专属 onboarding 联系人） |
| Incident / 服务异常 | `<<INCIDENT_HOTLINE_PENDING>>` |
| 合同 / 计费 | Sales / Finance 对接人 |
| 合规 / 数据请求 | 您的法务接口人 → 我方 Legal |

## 8. 数据删除 / 被遗忘权

可向 CS 提交删除请求，我方在 7 工作日内删除原始图像。audit_log 保留按合规要求 ≥ 3 年（脱敏后保留事件元数据，不含原图）。

## 9. 灰度阶段的 SLA 怎么看？

详见 `sla.md`。Beta 阶段 SLA 是 best-effort（不构成 contractual SLA），GA 后切到合约 SLA。SLA 数字会在 A 流 C4.5 后第一次公布真实承诺。

## 10. References（客户可见）

- `sla.md` — 服务等级承诺
- `billing.md` — 计费规则
- `failure-modes.md` — 失败模式与兜底
- `launch-announcement-template.md` — 即将上线公告（CS 转发）

## 11. References（内部，客户不可见）

- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.4
- `docs/commercial/raci-matrix.md` D6
