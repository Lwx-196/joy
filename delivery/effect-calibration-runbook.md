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
- 结论：**要让 Phase 3.3 校准有统计意义（N≥6），必须先扩循证库覆盖**——加 brand→project 映射 + 泪沟/法令纹/苹果肌/太阳穴/卧蚕 等 region 的 evidence_row。这是 anchored-sim 循证库内容工作；harness 正确地 fail-closed（反臆造，不给无循证依据的 case 编 effect）。
- 当前可立即真校验的样本 = 康巧佳（Phase 0 验过）+ 许楚楚（commit aa23b25 港鼻背后 1→2 pair）+ 蓝凤端（共 3）。

### Step 3 进展（commit aa23b25）+ 根因拆分（owner-gated）

- **已做（grounded，0-quota）**：把 effect-evidence-library §1 已有但未港的 4 个 HA 行（泪沟/苹果肌/鼻基底/鼻背）逐字转录进 `EFFECT_ROWS`。**许楚楚 1→2 pair**（海魅鼻子→鼻背），泪沟 Phase-0 锚点就绪待品牌。
- **🔴 N≥6 真瓶颈 = 品牌→项目注册（owner 权威数据，不可臆造）**：19/22 case 因品牌未注册落 `unknown_segments` → 反臆造 drop。需 owner 给 project+ingredient+time_anchor：
  - **泪沟 HA 候选**（最高优，泪沟全靠它）：弗缦 / 盈致 / 妮凯丽 / 柯芮琦 / 薇旖美 / 丰颜（"玻尿酸" 字面=HA，owner 确认即可注册）。
  - **乔雅登（极致/雅致/丰颜）**：HA，多 case（下巴/苹果肌/鼻）。
  - **需新建项目类型**：胶原（collagen，非 HA，库无 PROJECT_COLLAGEN）/ 普丽妍 T 童颜针（biostimulator）/ 塑公主 / 塑妍萃 / 吉士 — 各需新项目类型 + 自带 evidence row（更大工程）。
  - **次级门控（区域无 evidence_row）**：法令纹 / 卧蚕 / 下颌线 / 太阳穴 / 口角 / 印堂 — 即便品牌注册仍缺 region row。
- **Step 2 真校准**与上述独立，仍卡 `TUZI_*` key（shell 当前无）。

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
# ✅ 2026-06-01 Step 1.5 已修（commit 8af7626）：gate 加确定性 no-change 预检 → stub 重验 gate_pass 0/3（no_visible_change，judge 0 调用）。floor 已闭环。
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
#    ⚠️ 用 --env-file（不能 source）。gate floor 已于 Step 1.5（commit 8af7626）修复：no-change 确定性预检兜底，判官幻觉假 pass 不再污染结果。
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

- 0-quota 管线：**31 tests passed**（builder dry-run + 反臆造 drop + report floor/candidate-pass/baseline-held/judge-down-fail-closed + gate 4 个 no_visible_change 用例）
- dry-run 真实案例库：discover 22 → 3 projectable（康巧佳含 4 evidence pairs），fail-closed skip 正确；`--all-cases --n 3` 现选满 3 projectable（Step 1.5 修 builder 顺序前 = 1）
- 全量回归 **1268 passed / 3 skip / 0 regression**；ruff clean
- Step 1.5（commit 8af7626）：gate `effect_delivery_qa` 加 no-change 预检（mean_abs_delta<1.0 AND changed_fraction<0.001，PROMPT_VERSION effect-v1→v2）+ builder projectable 顺序修复

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

## Step 1.5 已完成（2026-06-01，commit 8af7626，0-quota 闭环）

floor 已闭环，下一步是 owner 解锁 gpt-image-2 quota 后的 B 段真校准（Step 2）。

- **(A) gate 确定性 no-change 预检**：`EffectDeliveryQA.assess` 在调 judge 前测整图差异——`mean_abs_delta < 1.0` **且** `changed_fraction < 0.001`（max 通道 |Δ| > 10 的像素占比）→ 直接判 `fail / hard_veto_reason=no_visible_change`，**不调 judge（0 quota）**，仍 held + 可 `clear_effect` 人工 override。AND 双条件刻意保守：真 mask-anchored 投影必改一块连续区域，`changed_fraction` 远超 0.001，不会误杀 subtle effect。解码/numpy 异常 → fail-open 落回判官（既有 fail-closed 路径）。`PROMPT_VERSION` effect-v1→v2（防旧缓存假 pass 经 hash cache 泄漏）。
- **(B) builder 顺序**：projectable 过滤前置到 `select_cases`/`[:n]` 之前 → `--n N` 从 projectable 池里选 N。实测 `--all-cases --n 3` 现选满 3（康巧佳/蓝凤端/许楚楚），修前只给 1。
- **重验**：同一 stub packet（3 items）+ 真 judge t54 → `gate_pass 0/3 (0.0%)`，`verdict={'fail':3}`，`winner={'(none)':3}`，全部 `no_visible_change`，**判官从未被调用**。对比 Step 1 的幻觉假 pass/0.85 = 决定性闭环。
- **测试**：gate +4 no_visible_change 用例 + report +1 floor 用例（report 聚合测试改用视觉不同 candidate 走判官路径）；全量 1268 passed / 3 skip / 0 regression / ruff clean。
- **判官 prompt 强化（原 B）= 不做**：判官已证实无视现有 no-change 指令，确定性预检（A）才是可靠兜底；强化 prompt 收益低，略。

## Step 2 真校准已跑（2026-06-01，N=3，Phase 3 Exit 达成）

> owner 解锁 gpt-image-2 key。出图通路有坑（见下），用 `--api-direct` 绕过，**首次真 effect-projection 端到端跑通**。报告存档 `delivery/effect-calibration-report-n3.md`。

### 出图通路坑 + 解法（`--api-direct`）
- **node undici 被本地 Clash 代理 socket-reset**（UND_ERR_SOCKET/ECONNRESET），凡携带图片的请求 TLS 层即崩；curl/Python urllib 能穿透（切 Vless/TCP 稳定节点后 Python 稳）。3 家 provider × 3 客户端 × 多尺寸交叉验证 = 环境级，非 key/provider/代码问题。
- **新增 `--api-direct`**：AI 出图改用 Python urllib（替掉坏的 node HTTP 那一跳，唯一改动），**prompt 与 `_apply_effect_mask_anchor` 仍是生产代码**。读同款 `TUZI_IMAGE_PRIMARY_*` env。默认关，生产 node 路径不变。
- 真校准命令：`--api-direct --all-cases --n 3`（出图）→ `effect_calibration_report --env-file t54`（判官）。

### N=3 结果（gate_pass 0/3，但这是诚实信号）
| case | winner | conf | 判官理由 |
|---|---|---|---|
| 康巧佳 | baseline | 0.85 | 下巴明显圆形 patch 拼接痕迹，违反无缝编辑 |
| 蓝凤端 | baseline | 0.80 | 额头可见 mask 边界 |
| 许楚楚 | tie | 0.85 | candidate 与 baseline 无可见变化（AI 没改出效果）|

- **Phase 3 Exit 达成**：判官 4-criteria **区分力确认**——精准分辨 mask 拼接缝 artifact vs AI 无效果，非 Step 1 幻觉橡皮章（floor 修复有效：许楚楚无变化被正确判 tie）。
- **🔴 真发现**：当前 `_apply_effect_mask_anchor` 的 separate-ellipse mask 合成留下**可见圆形/边界缝**（2/3 case 因此 fail）→ anchored-sim 下一轮真问题 = **羽化 mask 边缘**（feather），而非判官或出图。许楚楚的 tie = 下巴/鼻背 HA 效果 AI 没投出来（subtle，或 prompt 强度不足）。
- 凭证：tu-zi key 在 gitignored `tasks/tuzi_image.local.env`（跑完轮换）；判官 t54 ADC。

## mask 边缘羽化（2026-06-01，修判官抓出的拼接缝）

`_apply_effect_mask_anchor` 新增向内羽化（`_feather_mask_inward`：erode→GaussianBlur→乘回 binary 钳制外侧为 0，半径自适应短边 1.2% 钳 [4,40]px）。0-quota 对同批 AI 原图再锚定 + t54 重判（`report-feathered.md`）：

| case | 缝前 winner | 羽化后 winner | 解读 |
|---|---|---|---|
| 康巧佳 | baseline（圆形 patch 缝）| baseline（untreated region artifact）| 缝抱怨消除，AI 输出本身仍有 artifact |
| 蓝凤端 | baseline（额头 mask 边界）| **tie（无可见变化）** | 纯肉毒静止脸本就无可见效果（循证库标注 botox 即刻零变化）→ 缝消失露出真相 |
| 许楚楚 | tie | tie（未投出效果）| HA 下巴/鼻背 AI 没可靠投出填充 |

- **羽化有效**：判官不再抱怨拼接缝（identity 铁线 outside_exact 仍保住，单测 `test_feather_mask_inward_*`）。
- **校准暴露更深 2 问题**（下一轮真工作，非缝）：① **纯肉毒 case 是坏校准样本**（静止脸无可见效果，保证 tie）→ 校准样本应排除 botox-only 或要求动态表情图；② **HA 填充效果 gpt-image-2 没可靠投出** → 需强化 prompt 强度 / 换 edit 端点 / 调 mask 覆盖。
- gate_pass 仍 0/3 = 诚实信号（判官区分力强），瓶颈从「缝」下移到「AI 投影质量 + 样本筛选」。

## ✅ 选定配方（2026-06-01，owner 拍板）

经 gpt-image-2 / gemini-3-pro / nano-banana 三模型 × 鼻背/下巴/泪沟多轮对比 + 判官 + owner 人眼裁决：

**主力配方 = gpt-image-2 `/images/edits` + raw-first（`--no-mask-anchor`）**
```bash
set -a; source tasks/tuzi_image.local.env; set +a   # gpt-image-2 + images_edit
python -m backend.scripts.effect_calibration_packet_builder \
    --api-direct --no-mask-anchor --all-cases --select <case> --n 1 \
    --output-packet /tmp/effect-cal/packet.json
python -m backend.scripts.effect_calibration_report --packet-json ... --env-file t54 ...
```

**owner 审美基准（选型理由）**：gpt-edit raw **最自然、保毛孔质感、微微红润的健康气色** → 医美案例展示要的「真实可信术后感」，不是磨皮塑料感。gemini-3-pro 眼下最干净、nano 居中，但 gpt-edit 的真实质感胜出。

**关键认知**：
- **raw-first > mask-anchor**：忠实编辑器（gpt-image-2 images_edit）的 raw 是「全脸协调精修」，mask-anchor 只保留孤立治疗区、把协调感抹掉 → 反而像 baseline。owner 人眼推翻了「整帧必漂移、必须 mask 锚定」的旧硬底线假设（gpt-image-2 edit 是忠实的，毛孔/瑕疵/身份都在）。
- **效果类型决定成败**：泪沟（填平+淡黑眼圈）是清晰可见效果 → 三模型全 PASS；鼻背/下巴在正脸上太微弱 → 难过判官。校准样本应优先清晰效果类型。
- **判官 vs 人眼**：effect_projection 判官对自然克制的好结果会判 tie（鼻/下巴），与 owner 审美不完全对齐 → 判官「可见效果」阈值需按 owner 审美再校准。

**泪沟 N=1 PASS 实证**：郭若煊（弗缦 HA）gpt-edit raw → judge pass 0.85。N≥6 需再注册泪沟 HA 品牌（盈致/妮凯丽/柯芮琦/薇旖美 owner 确认）。

## 三中转 image-2 质量对比（2026-06-01，3 案例 × 3 中转 = 9 张）

owner 问三家中转的 gpt-image-2 质量差异。同 3 个泪沟 case（郭若煊/稀饭/黄靖榕）× tu-zi/flashapi/77code，gpt-image-2 images_edit raw-first，t54 判官：

| case | tu-zi | flashapi | 77code |
|---|---|---|---|
| 郭若煊 | 0.85 | 0.90 | 0.90 |
| 稀饭 | 0.90 | 0.85 | 0.85 |
| 黄靖榕 | 0.80 | 0.85 | 0.85 |
| 均值 | 0.85 | 0.867 | 0.867 |

**结论：三家质量等价**（9/9 PASS，均值差 <0.02 在单次生成噪声内，无一致赢家——每家在不同 case 拿最高分=随机波动非质量差距；视觉每 case 三家几乎一样）。同一 gpt-image-2 后端。**中转选择看可靠性/成本/配额，不是质量。** owner 选 tu-zi（自然+红润）站得住，flashapi/77code 同档备份/降级。

**附带**：泪沟 + raw-first 配方 **N=3 案例全 PASS**（不只 N=1），定量坐实。
