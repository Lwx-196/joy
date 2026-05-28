# Legal Sign-off Checklist — ComfyUI + VLM GA

> Phase C5.0.3 deliverable。法务 review 必走清单。
> Status: **TEMPLATE** — 等法务（D5 accountable）逐条 sign-off。

## 1. 适用法规与背景

| 法规 / 政策 | 触发点 | Owner |
|---|---|---|
| 《中华人民共和国广告法》（含医美专项） | 增强后图像作为案例展示 / 引流 | Legal + Sales |
| 《互联网广告管理办法》（2023.5 施行） | "可能引发未成年人模仿" / "暗示疗效" | Legal |
| 医美广告专项治理（市场监管总局 2024+） | 治疗前/后对比图、术前术后展示 | Legal + Clinical |
| 《民法典》肖像权 / 隐私权（1018–1039 条） | 客户上传的真人肖像 | Legal |
| 《生成式 AI 服务管理暂行办法》（2023.8） | "AI 生成内容应当显著标识" | Legal + Product |
| 《个人信息保护法》（PIPL） | 处理人脸 / 治疗信息 | Legal + Engineering |
| 数据保留合规 | audit_log 至少 3 年（C3.0.4） | Legal + Engineering |

## 2. 检查清单

### 2.1 广告法 / 医美专项

- [ ] **L1**：所有客户可见的增强后图像，是否构成《广告法》第 8 条"广告"定义？（是 → 进 L2-L5；否 → 仍走 L6+）
- [ ] **L2**：增强后图像是否包含"治愈率 / 有效率 / 疗效保证 / 患者证言"等违禁内容？
- [ ] **L3**：是否包含"治疗前 / 治疗后"对比拼图、未成年人形象、不真实代言？
- [ ] **L4**：医美广告必备审查文号（医疗广告审查证明）是否覆盖 AI 增强后版本？
- [ ] **L5**：广告内容真实性证明（原图 + 增强参数 + 时间戳）是否归档可追溯？

### 2.2 肖像权 / 隐私

- [ ] **L6**：客户上传图像的知情同意书是否覆盖"AI 增强"用途？（旧客户合同需更新见 §3）
- [ ] **L7**：被拍摄主体（如客户的客户）的肖像授权链是否完整？
- [ ] **L8**：人脸数据存储、传输、销毁链路是否符合 PIPL 单独同意 + 最小必要原则？
- [ ] **L9**：跨境数据流（如 ComfyUI 模型权重来自境外）合规审查？

### 2.3 AI 生成标识（《生成式 AI 服务管理暂行办法》第 17 条）

- [ ] **L10**：增强后图像是否需要可见水印（如右下角"AI 增强"标签）？
  - 选项 A：始终可见水印（最保守）
  - 选项 B：交付物含元数据标识，可见标识仅 in-app preview（中等）
  - 选项 C：仅元数据 + 客户合同披露，无可见标识（最低保守）
  - **决策**：`<<DECISION_PENDING — D5 Legal accountable>>`
- [ ] **L11**：内嵌元数据格式（EXIF / IPTC / C2PA）选型 + 是否伪造防护？
- [ ] **L12**：客户对外发布前是否触发二次披露义务（小红书 / 抖音 / 朋友圈）？

### 2.4 数据保留 & 审计

- [ ] **L13**：`audit_log` retention ≥ 3 年（C3.0.4 落地）法务确认？
- [ ] **L14**：PII 最小化（payload 不存原始人脸 / 仅 hash + metadata）？
- [ ] **L15**：被遗忘权（数据删除请求）SOP + 影响 audit_log 的合规取舍？

### 2.5 合同 & 客户口径

- [ ] **L16**：客户主合同 `ai_enhancement` clause（见 `billing-policy.md` §5）法务起草？
- [ ] **L17**：客户隐私政策 / 服务条款 同步更新？
- [ ] **L18**：CS 对客户的统一答复口径（`docs/customer/faq.md`）法务审阅？

## 3. 客户合同 ai_enhancement clause 框架

> 草案（法务最终定稿）：

```
3.X AI 增强服务

3.X.1 服务描述：本服务通过 AI 模型对客户提供的医美案例图像进行
     focal-region 增强处理，输出供客户在合规广告 / 内容场景使用。

3.X.2 数据使用：客户授权我方在本协议有效期内对上传图像进行处理、
     存储 ≤ <<RETENTION_DAYS>>、用于服务交付。我方不用于训练第三方模型，
     不跨境传输 PII。

3.X.3 内容标识：所有 AI 增强后图像 [可选条款 A/B/C 见 §L10] 携带
     "AI 增强" 标识；客户对外发布需保留并按平台规则二次披露。

3.X.4 SLA：见附件 `customer-sla.md`；breach credit 见 `customer-billing.md`。

3.X.5 失败兜底：见附件 `customer-failure-modes.md`；layout-only 兜底
     交付不计费。

3.X.6 责任分担：客户对图像合规性 / 肖像授权链负主要责任；
     我方对 AI 增强结果不违反《广告法》第 8 条提供 best-effort 保证。

3.X.7 合规终止：监管要求暂停 / 法务 risk 评估升级时，我方有权
     ≤ 24h 暂停本服务，pro-rate 退款。
```

## 4. Sign-off 流程

1. C 流交付本 template → 内部 PM review
2. 法务（D5 accountable）逐条 L1–L18 sign-off / 提修订意见
3. 重大决策（L10 AI 标识方案 / L13 retention 期限 / L16 合同 clause）走商业 RACI 决策日志
4. 全部 sign-off 后落 `delivery/c5-legal-signoff-YYYY-MM-DD.md`，附法务正式回函
5. **GA gate**：L1–L18 任何一条未 sign-off → 不开 C4.5 soft launch（与 A 流并行 review，但 launch 阻塞）

## 5. References

- Plan: `.claude/plan/comfyui-vlm-ga.md` Phase C5.0.3
- 关联：`docs/commercial/raci-matrix.md` D5/D7 / `docs/customer/billing.md` / `docs/customer/sla.md`
- 外部参考：广告法 / PIPL / 《生成式 AI 服务管理暂行办法》原文（法务持有）
