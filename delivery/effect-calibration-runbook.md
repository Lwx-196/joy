# Effect-projection 校准 runbook（anchored-sim Phase 3.3）

把 Phase 3.3「对 Phase 0 的图跑 judge 校准」+ 整线**首次真 AI 端到端 smoke** + Phase 0 多部位补验，合并成一个一键 harness。0-quota 部分已建好验过；真 AI 出图缩成 owner 解锁 quota 后的一条命令。

## 这套 harness 是什么

```
case → effect_pairs（parse_procedures + 反臆造 fail-closed）
     → run_ps_model_router_after_simulation(mode=effect_projection)  ← 出图（candidate）
     → effect_projection judge（4 criteria，循证库锚定）
     → EffectDeliveryQA gate（pass/held，fail-closed）
     → 校准报告（winner 分布 / gate pass-rate / per-case）
```

- `backend/scripts/effect_calibration_packet_builder.py` — discover case → 解析 effect_pairs → 出 candidate（real / stub）→ packet
- `backend/scripts/effect_calibration_report.py` — packet → EffectDeliveryQA gate → 聚合校准报告
- `backend/tests/test_effect_calibration_packet_builder.py` — 9 tests，0-quota 管线全验

**关键事实**：生产 simulation 路径支持 `effect_pairs`（Phase 2.2），但此前**没有任何 caller 真的组 effect_pairs 传进去**。`_resolve_effect_pairs` 是这个 harness 首建的组装器（将来生产 simulation 接 effect 时可直接复用）。这条链此前从未真 AI 端到端跑过。

## ⚠️ 校准样本约束（harness 暴露的真瓶颈，先看）

全库扫描（`--all-cases`）：**22 focus-eligible case 里仅 3 projectable**（康巧佳 / 许楚楚 / 蓝凤端），19 skip。

- 根因：循证库（`procedure_region_mappings`）的 `BRAND_TO_PROJECT` + `(project, region)` evidence_row 覆盖窄。
- **尤其「泪沟」**（Phase 0 锚点 + 案例库出现最多的部位）**全 skip**——泪沟相关品牌（弗缦/盈致/胶原填充/玻尿酸/妮凯丽…）没注册，或泪沟的 evidence_row 缺失。
- 结论：**要让 Phase 3.3 校准有统计意义（N≥6），必须先扩循证库覆盖**——加 brand→project 映射 + 泪沟/法令纹/苹果肌/太阳穴/卧蚕 等 region 的 evidence_row。这是 anchored-sim 循证库内容工作，不在本 harness 范围；harness 正确地 fail-closed（反臆造，不给无循证依据的 case 编 effect）。
- 当前可立即真校验的样本 = 康巧佳（Phase 0 验过）+ 许楚楚 + 蓝凤端（共 3）。

## 两类 quota（必须区分）

| 环节 | 后端 | 凭证状态 |
|---|---|---|
| 出图（candidate 生成）| gpt-image-2（tu-zi，`PS_ENHANCE_SCRIPT` + `TUZI_*`）| **owner-gated**（需解锁）|
| judge（评判）| Vertex ADC gemini | **已就绪**（`tasks/t54_vertex_adc.local.env`，5-29 打通）|

→ judge 端不需 owner 解锁；只有真出图需要 gpt-image-2 quota + PS env。

## A. 0-quota 预演（任何时候可跑，验管线不验结论）

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench-effect-calibration

# 1) stub 出图（candidate = raw copy），0 quota，验 effect_pairs 解析 + packet 形状
../case-workbench/.venv/bin/python -m backend.scripts.effect_calibration_packet_builder \
    --stub --all-cases --n 3 \
    --scratch-root /tmp/effect-cal --output-packet /tmp/effect-cal/stub-packet.json

# 2) 真 judge（t54）跑 stub packet —— floor 验证（candidate==baseline = no-change = failure）
#    ⚠️ 用 --env-file（t54 是无 export 的 VAR=val 格式；source 不导入子进程 → blocked_missing_vlm_provider_config）
../case-workbench/.venv/bin/python -m backend.scripts.effect_calibration_report \
    --packet-json /tmp/effect-cal/stub-packet.json \
    --env-file /Users/a1234/Desktop/案例生成器/case-workbench/tasks/t54_vertex_adc.local.env \
    --report-output /tmp/effect-cal/stub-report.md
# ⚠️ 2026-06-01 实测：当前 judge 把字节相同的图判 pass（见末尾「Step 1 实测结论」）→ floor 未过，需先修 gate
```

## B. 真校准（owner 解锁 gpt-image-2 quota + PS env 后，一键）

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench-effect-calibration

# Step 1 — 真 effect projection 出图（消耗 gpt-image-2 quota，走 PS_ENHANCE_SCRIPT）
#          这是 run_ps_model_router(effect) 第一次真 AI 端到端跑通
../case-workbench/.venv/bin/python -m backend.scripts.effect_calibration_packet_builder \
    --all-cases --n 6 \
    --scratch-root /tmp/effect-cal --output-packet /tmp/effect-cal/packet.json
#   no-evidence case 自动 skip（反臆造），projection no-op 自动 drop（不静默）

# Step 2 — effect_projection judge 校准（judge quota，t54 已就绪）
#    ⚠️ 用 --env-file（不能 source）；且需先修 gate floor（见末尾实测结论），否则 judge 假 pass 污染结果
../case-workbench/.venv/bin/python -m backend.scripts.effect_calibration_report \
    --packet-json /tmp/effect-cal/packet.json \
    --env-file /Users/a1234/Desktop/案例生成器/case-workbench/tasks/t54_vertex_adc.local.env \
    --report-output /tmp/effect-cal/report.md \
    --json-output /tmp/effect-cal/report.json

# Step 3 — 读 report.md：gate_pass_rate / winner 分布 / per-case 明细
```

**Phase 3 Exit** = report 产出 + effect_projection judge 的 4 criteria（effect_direction / identity_preserved / only_treated_regions / natural_not_overdone）在真 effect 图上区分力确认。

## 前置检查（真跑前）

- gpt-image-2：`PS_ENHANCE_SCRIPT`（node 脚本）存在 + `TUZI_*` key 配好 + quota 有额度
- judge：`tasks/t54_vertex_adc.local.env`（Vertex ADC，无密钥）—— 用 `--env-file` 传，**不要 source**（t54 无 export 前缀，source 不导入子进程）
- 样本：若想 N≥6，先确认/扩循证库（见上「校准样本约束」），否则当前只有 3 个真 projectable

## 验证状态

- 0-quota 管线：**9 tests passed**（builder dry-run + 反臆造 drop + report candidate-pass/baseline-held/judge-down-fail-closed）
- dry-run 真实案例库：discover 22 → 3 projectable（康巧佳含 4 evidence pairs），fail-closed skip 正确
- ruff clean / 纯新增 3 文件（零改现有代码）

## Step 1 实测结论（2026-06-01，0-quota floor 验证）

跑了 0-quota 预演（stub raw copy + 真 judge t54），**抓出一个 Phase 3.3 必须先修的 judge 缺陷**：

- **现象**：stub packet 的 baseline 与 candidate 是**字节完全相同**的图（sha256 都是 `826440f6...`，`BYTE-IDENTICAL: True`），judge 却判 `verdict=pass / winner=candidate / confidence=0.85`，rationale 幻觉出 "successfully projects the desired treatment effects for the lips..."。
- **根因**：`effect_projection` judge 被 item 里的 `effect_pairs`「预期效果」提示诱导，确认偏差地"看到"了不存在的效果，**无视了 prompt 里明写的 "no-change projection is a FAILURE, not a tie"**。
- **影响**：gate 的 floor 是漏的——任何 candidate（哪怕没变化/坏的）都可能被假 pass。**修复前，真校准（B 段）结果不可信。**
- **修复方向（下个会话 Step 1.5）**：
  - **(A 推荐) gate 层确定性 no-change 预检**：`EffectDeliveryQA.assess` 调 judge 前先测 baseline-vs-candidate 像素差异，差异极小 → 直接 fail（`no_visible_change`），不调 judge。确定性、不依赖 judge 自觉、顺带省 quota。**改 main 上 PR #59 的 `effect_delivery_qa.py`（owner 待批，阈值取保守值，勿误杀真 subtle effect）。**
  - (B) judge prompt 强化 no-change 检测——judge 已无视现有指令，不可靠，只作 A 的补充。
- **附带 builder bug**：`--n N` 在 projectable 过滤**之前**取 N（`select_cases(pool, n)` 砍早了），导致 `--n 3` 只给 1 projectable。真校准要 N≥6，需把 projectable 过滤前置到 `select_cases` 之前。

**新会话 Step 1.5 = 修 (A) gate floor 预检 + builder projectable 顺序 → 0-quota 重验 floor 应返回 fail（不再假 pass）→ 才进 B 段真校准。**
