# RACI Matrix — ComfyUI + VLM GA Commercial Governance

> Phase C5.0.1 deliverable. Drives all C5.x commercial decisions.
> Status: **DRAFT** — needs商业团队 sign-off。

## 1. Roles

| Role | Owner type | 范围 |
|---|---|---|
| **Product** | 产品负责人 | 功能定义、客户价值口径、上线时间 |
| **Engineering** | 工程 lead（case-workbench owner） | 灰度 manifest、SLO 校准、rollback、observability |
| **Legal** | 法务 | 医美 AI 图像合规、广告法、肖像权、AI 标识、数据保留 |
| **Clinical** | 医美临床 reviewer | 增强后图像不违反疗效呈现红线 |
| **Customer Success (CS)** | CS lead | 客户沟通、失败模式 broadcast、credit policy 执行 |
| **Risk / Finance** | 财务 + 风控 | billing policy、成本核算、credit 拨款上限 |
| **Sales** | 销售 lead | 客户合同 ai_enhancement clause 升级 |

## 2. Decision points × RACI

> R = Responsible（执行），A = Accountable（最终拍板，唯一），C = Consulted（决策前必征询），I = Informed（决策后必通报）

| 决策点 | Product | Engineering | Legal | Clinical | CS | Risk/Finance | Sales |
|---|---|---|---|---|---|---|---|
| **D1. fumei brand 解锁** (C5.1) | A | R | C | C | I | C | I |
| **D2. SLA latency p95 commit** (C5.2) | C | R | I | I | C | A | C |
| **D3. SLA quality 胜率 commit** (C5.2) | A | R | I | C | C | I | I |
| **D4. Billing policy 决议** (C5.3.1) | C | I | C | I | C | A | C |
| **D5. AI 增强可见标识** (C5.0.3) | C | R | A | C | I | I | I |
| **D6. 客户通知 timing & 口径** (C5.5) | A | I | C | I | R | I | C |
| **D7. 客户合同 clause 升级** (C5.3.1) | I | I | C | I | I | C | A |
| **D8. p100 → 全量解禁** (post-C4.5) | A | R | I | I | C | C | I |
| **D9. Rollback 触发后 broadcast 决策** (C3.0) | C | R | I | I | A | I | I |
| **D10. Hypercare 首月解除** (C5.6) | A | R | I | I | C | C | I |

## 3. 流程约束

1. **每个 A 必须唯一** —— 上表已校验单点 accountable。
2. **任何 R 行动前**必须先向所有 C 发起 review 请求并留 audit trail（邮件 / 飞书审批单 / PR comment）。
3. **C 默认 SLA**：决策类 3 工作日 / 紧急 incident 4 小时。
4. **法务 review 异步并行**：D1/D4/D5/D7 法务 review 可与工程灰度并行，不阻塞 C3/C4 工程灰度本身。
5. **跨 R/A 冲突**升级到 product + 风控双签。

## 4. 决策记录模板

每个上表 D1–D10 决策落地必须留：

```
Decision: D{N}
Date: YYYY-MM-DD
A (sign-off): <name>
R (executed): <name>
C consulted: [<name1>, <name2>, ...]
I notified: [<name1>, ...]
Outcome: <一句话决议>
Evidence: <PR / 文档 / 邮件 / 审批单链接>
```

落到 `docs/commercial/decision-log/D{N}-YYYY-MM-DD.md`，与 git 一起 review。

## 5. Exit criteria

- [ ] 7 个 role owner 全部点名到具体人
- [ ] 10 个决策点 R/A/C/I 商业团队签字
- [ ] Decision log 模板 + 目录建好
- [ ] 与 `legal-signoff-checklist.md` 双向引用对齐

## 6. References

- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.0.1
- 关联文档：`docs/commercial/sla-template.md` / `docs/commercial/billing-policy.md` / `docs/commercial/legal-signoff-checklist.md`
