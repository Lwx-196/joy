# VLM+ComfyUI 优化方案真上线 — 设计文档

> **状态**: Draft — 经 brainstorming 阶段确认，待写 plan + execute Phase A/B。
> **日期**: 2026-05-28
> **作者**: Claude (with user 召毛 collaboration)
> **brainstorming HARD-GATE**: 本 doc 经 user 批准前不写任何实施代码。Phase A 启动需 user 再次签字。

---

## 1. Context

### 1.1 What's already shipped (basis)

本会话同日完成 11 wave 基础设施（commits `785a5ab → 9488050`，跨日 28 commit）：

| Wave | 主题 | 状态 |
|---|---|---|
| W1 | P0 治理（observability + fail-closed classifier + ops endpoint） | ✅ in main |
| W2 | P2.2/P2.3/P2.4 — SimulationDeliveryGate + Runtime Guard + SLO Monitor | ✅ in main |
| W3 | P2.4 收尾 — auto-rollback applier + eval methodology fix | ✅ in main |
| W4 | release deploy gate (W4-1 placeholder check + W4-2 paused stop-loss + W4-3 注释一致化) | ✅ in main |
| W5-W9 | followup polish (CLI ratchet `--baseline-stale-days` / `--paused-stale-days` symmetric + audit forensic flat fields 双 surface 防 None + helper defensive copy-on-write) | ✅ in main |

**这是基础设施层**，不是优化方案本身。等价于："给飞机装好了黑匣子 + 自动驾驶安全护栏 + 仪表盘" — 飞机还在地面。

### 1.2 What's NOT shipped (the gap)

实际"优化方案"全在 owner WIP worktree：

| WIP worktree | 状态 | 用户拥有 |
|---|---|---|
| `feat/best-pair-routing` | Phase 1 T1 schema 已在 main / Phase 2 T2 in worktree | ✅ 是 |
| `feat/vlm-judge-vigilant` | Phase 0 (consensus + Tier 1 hard veto + Mode C fail-closed) | ✅ 是 |
| `feat/vlm-phase2-tiered-prompt` | research（Tier 1/2/3 prompt v2 schema + parser） | ✅ 是 |
| `feat/vlm-calibration` | distribution collapse 检测库 + 单测 + CLI 报告 | ✅ 是 |

main 上 `promotion_state="shadow"`，`approver=null`，从未启动灰度。

### 1.3 Current production reality (main DB sqlite 调查)

- 419 cases / 975 simulation_jobs / 629 render_jobs / 797 vlm_usage_log
- VLM 使用：97% `Qwen3-VL-4B-Instruct-4bit` 本地推理（cost=0） / 3% `gemini-2.5-flash`
- ai 模式 render_jobs 626 个：done 13% / done_with_issues 34% / blocked 37% / failed 14% / cancelled 2%
- best-pair 模式仅 3 jobs（全 case_id=126，2026-05-10，全部 done_with_issues）
- vlm-judge purpose 调用 0（797 次 VLM 全是 classifier）
- ab_feedback 表 0 行

**baseline 现状**：约 70% jobs 不顺畅，但缺乏 A/B 对照数据。

---

## 2. Goal

让 4 个优化方案中**至少一个**真正上线 + 数据驱动证明比原方案更高效、出图质量更高。

**Goal 范围限定**（brainstorming 阶段经决策）：
- 本 spec 仅覆盖 **best-pair routing** 一个方向（Phase A + Phase B）
- vlm-judge / vlm-phase2 / vlm-calibration 三方向**延后**，留待独立 spec
- Phase C（真 p10 上线）作为决策门**留给下次 session**，本 spec 出 Phase A + B 结果后停下

理由：user time budget "比 1 周更快"，必须 pick 1 方向 + 极简 A/B。

---

## 3. Brainstorming 决策记录

### 3.1 5 个 framing 问题答案

| Q | 答 | 含义 |
|---|---|---|
| Q1: WIP ownership | "我是 4 个全部 owner，Claude 可直接 commit/push" | 4-track 并行可控；Phase 2 T2 由 Claude 直接 merge |
| Q2: Win criterion | "人眼盲评为主（质量为金标准，效率为辅）" | 必须填 ab_feedback 表；render success rate 仅作 efficiency 辅助 |
| Q3: Time budget | "比 1 周更快" | Pick 1 方向（best-pair）；样本量可能 < 30 严谨性让步给时间 |
| Q4: Ramp strategy | 隐含答案：(A→B) 推荐路径 | Phase A 1-2 天看方向 → 赢了 Phase B 3-5 天扩量 |
| Q5: Risk acceptance | 隐含：Phase A/B 全在 shadow，不动 promotion_state | 0 production rollback 风险 |

### 3.2 三个 candidate approach + 选定

| Approach | 时间 | 选定？ |
|---|---|---|
| A: 极简 retrospective（≤6 对现有数据，1-2 天） | 1-2 天 | ✅ Phase A |
| B: shadow A/B with new renders（30-40 对，3-5 天） | 3-5 天 | ✅ Phase B（A 通过后） |
| C: 一气推到 p10 上线 | 5-7 天 | ❌ 不在本 spec |

**选定路径**：A → B → 报告 → **停下**（C 留下次 session 决策）

---

## 4. Phase A — Retrospective 盲评（1-2 天）

### 4.1 目标
用**现有数据**（0 新 render）获取 best-pair vs ai 的**质量方向信号**（directional signal）。

### 4.2 范围
- 数据源：`case_id=126` 的 3 条 best-pair render_jobs（id=164/165/166，2026-05-10）+ 同 case 的 ai render_jobs（≤6 条历史产物）
- 评估方式：用户人眼盲评配对图（A vs B，标 winner）
- 样本量：≤6 对（不扩，用尽现有）

### 4.3 Execution steps

| # | What | Owner | 时间 |
|---|---|---|---|
| 1 | 拉 case_id=126 best-pair + ai render 产物路径（from main DB） | Claude | 5 min |
| 2 | 生成盲评包：markdown 表 + A/B 列**乱序** image refs + 评分 column；filename 不暗示 mode | Claude | 10 min |
| 3 | 你打开盲评包看图 + 标 winner | User | 10-15 min |
| 4 | 解析评分 + 写 ab_feedback 表（写前先 `.schema ab_feedback` audit） + 计算胜负 | Claude | 5 min |
| 5 | 出 first-signal report（赢/输/平 + 方向建议） | Claude | 10 min |

### 4.4 Deliverables
- `delivery/phase-a-blind-review-package.md`（盲评包）
- `delivery/phase-a-signal-report.md`（first-signal report）
- `ab_feedback` 表新增 ≤6 行（Claude 写前 `.schema` audit）

### 4.5 Phase A → Phase B 决策门

| Phase A 结果 | 决策 |
|---|---|
| best-pair 赢 ≥ 4/6（67%+） | ✅ 进 Phase B 扩量到 30+ 样本 |
| 混合 3-3 / 2-2-2 | ⚠️ 进 Phase B with caution（加 case 到 10+ 后再决） |
| ai 赢 ≥ 4/6 | ⛔ STOP，不走 Phase B；进 retrospective debrief — best-pair 设计假设需要 revisit |

### 4.6 风险评估

| 风险 | 缓解 |
|---|---|
| 样本量 ≤6 不足统计严谨 | 接受（Phase A 设计目标是 directional 不是统计；统计交给 Phase B） |
| 评图 bias（user 知道哪个是新方案 → expectation bias） | image ref 真乱序 + filename hash 不暗示 mode + Claude 生成包时自检 |
| best-pair 仅 case_id=126 一个 case，可能不具代表性 | Phase A 只问"有没有 directional signal"，代表性不是这一步要解决的 |
| ab_feedback 表 schema 不熟可能写错 | step 4 写表前 Claude 先 `.schema ab_feedback` audit |

---

## 5. Phase B — Shadow A/B with new renders（3-5 天）

**前提**：Phase A 通过决策门（best-pair 赢 ≥ 4/6）

### 5.1 目标
扩样本到 30-40 对，做**统计严谨判定**（不再是 directional signal），决定是否值得推 Phase C（真 p10 上线）。

### 5.2 范围
- 数据源：今天起新跑 15-20 个 case 的 best-pair render + 同 case 历史 ai render 配对
- 样本量：30-40 对（每 case 最多生成 2-3 对 — 不同 brand / template 配置）
- case 选择：cross category × template_tier × customer_raw 三维 coverage

### 5.3 Day 0 — best-pair Phase 2 T2 worktree audit + commit decision

| # | What | Owner | 验收 |
|---|---|---|---|
| 1 | 读 `docs/superpowers/plans/2026-05-09-best-pair-routing.md` checkbox 进度 | Claude | 65 `[ ]` / 0 `[x]` 已知 — 重新 audit 实际 commit 进度 |
| 2 | `cd case-workbench-best-pair` + `pytest backend/tests/test_best_pair*` | Claude | 看哪些 test 已绿 |
| 3 | `git log feat/best-pair-routing..main` + `git log main..feat/best-pair-routing` | Claude | 看 ahead/behind 状态 |
| 4 | Claude 生成 Phase 2 T2 state report：哪些功能可上、哪些有 blocker | Claude | 决策依据 |
| 5 | User 授权 + Claude 提 PR merge Phase 2 T2 到 main（如 ready）；如有 blocker → 谈是否 fall back Phase 1 only | User + Claude | main 上 best-pair 路径可跑 |

**Day 0 决策门**：
- ✅ Phase 2 T2 ready + 测试绿 → Claude commit + push + PR + user 批 merge → 进 Day 1
- ⚠️ 部分 ready（如 5/8 task done）→ 决策：(a) 用 partial Phase 2 / (b) 补全 missing tasks 再跑（延 1-2 天）/ (c) fall back Phase 1 only
- ⛔ blocker（如 schema 不一致 / Phase 2 没法跑）→ STOP Phase B；原因写报告，回头补 Phase 2 task

### 5.4 Day 1 — case 选择 + best-pair render enqueue

| # | What | Owner |
|---|---|---|
| 1 | SQL 拉候选 case：cross 3 维 + has-ai-render-history + has before/after source photos | Claude |
| 2 | 选 15-20 个 case（每维度至少 2-3 个 sample） — 输出 markdown 列表给 user 确认 | Claude |
| 3 | User review case 列表（5 分钟） | User |
| 4 | Claude enqueue 15-20 个 best-pair render_jobs via render_queue 接口 | Claude |

### 5.5 Day 2 — render queue drain + 失败处理

| # | What | Owner |
|---|---|---|
| 1 | 每 30 分钟 poll `render_jobs WHERE batch_id=<phase-b-batch>` status | Claude（low-effort） |
| 2 | 失败处理：失败 case 看 error_message + audit_json.failure → retry / skip / 加入分析 | Claude |
| 3 | Claude 配对：每 best-pair render 找同 case 同 brand 同 template 的 historical ai render → 生成配对清单 | Claude |
| 4 | 输出 Day 3 盲评包预览给 user | Claude |

### 5.6 Day 3 — 盲评 + 写 ab_feedback

| # | What | Owner | 时间 |
|---|---|---|---|
| 1 | Claude 生成盲评包（markdown 表 30-40 行 + A/B 列乱序 + filename 不暗示 mode） | Claude | 30 min |
| 2 | User 盲评 30-40 对 | User | 30-60 min（30 sec/对） |
| 3 | Claude 解析 + 写 ab_feedback 表 | Claude | 10 min |

### 5.7 Day 4 — 统计分析 + report + Phase C 决策

| # | What | Owner |
|---|---|---|
| 1 | 统计分析：binomial test（best-pair win rate vs 50%） + bootstrap 95% CI | Claude |
| 2 | 效率分析：latency（enqueued→finished p50/p95）vs ai baseline + success_rate vs ai baseline | Claude |
| 3 | Quality + efficiency 双维 final report | Claude |
| 4 | Phase B → Phase C 决策门评估 | Claude → User |

### 5.8 Phase B 决策门 — 进 Phase C？

| 维度 | Phase C 通过阈值 | Phase B 实测 |
|---|---|---|
| Quality | best-pair wins ≥ 60% + bootstrap 95% CI 下限 > 50% | 待测 |
| Efficiency: render success | success_rate（done + done_with_issues）within 5% of ai baseline | 待测 |
| Efficiency: latency | p50 enqueued→finished within 10% of ai baseline | 待测 |
| 关键 failure | 0 critical (e.g. data corruption / manifest drift) | 待测 |

**3 维全过 → 推 Phase C（p10 真上线，留下次 session）**
**2 维过 / 1 维 borderline → 加测一轮或调整再判**
**任一维 fail → STOP，回头看 best-pair 设计**

### 5.9 Deliverables（Phase B 全套）
- `delivery/phase-b-day0-audit-report.md`（Phase 2 T2 audit）
- main 上新增 1 PR（Phase 2 T2 merge，如 Day 0 决定 commit）
- `delivery/phase-b-case-selection.md`（Day 1 case 列表）
- `delivery/phase-b-blind-review-package.md`（Day 3）
- `delivery/phase-b-report.md`（Day 4 final）
- ab_feedback 表新增 30-40 行

### 5.10 风险评估

| 风险 | 缓解 |
|---|---|
| Day 0 发现 Phase 2 T2 有大 blocker → 延期 | 退路：fall back Phase 1 only（已在 main，best-pair 最小路径可跑） |
| ComfyUI render queue 跑不动（不到 15 个能 done） | 设 sample-size 下限 = 12，至少能做 binomial test；如低于 12 延 Day 2 |
| 同 case 找不到 historical ai render 配对（best-pair case 选得太"新"） | case 选择优先 has-ai-render-history 的 case |
| User Day 3 不可用 | 延 Day 4 到次日；不阻塞 |
| Phase B 决策门 borderline（2 维过 / 1 维 borderline） | 设 "加测 10 对再决" 子流程；或接受 borderline 进 Phase C 风险（auto-rollback 兜底） |

---

## 6. Out of scope（本 spec 不做）

### 6.1 本 spec 内 out of scope（Phase A/B 不做）
- ❌ vlm-judge worktree 收尾（独立 wave）
- ❌ vlm-phase2 tiered prompt 收尾（独立 wave）
- ❌ vlm-calibration distribution collapse 收尾（独立 wave）
- ❌ `promotion_state` 改 p10（Phase C，留下次 session 决策）
- ❌ `compute_manifest_hashes --write` / `calibrate_slo_baseline --apply`（Phase C 必跑）
- ❌ launchd cron + `promotion_slo_check.py` 真启用（Phase C）
- ❌ `approver` / `approved_at` 填（Phase C）

### 6.2 Phase C 是什么（仅为完整性 outline，不在本 spec 实施）
- Day 0: `calibrate_slo_baseline --window 48 --apply`（48h shadow 数据校准）
- Day 1: manifest `promotion_state: shadow → p10` + approver/approved_at 真实填
- Day 2: 挂 launchd 跑 `promotion_slo_check.py` 每 15 分钟
- Day 3+: 观察 24h 数据 + auto-rollback 兜底
- Total: 3-5 天（最短）

Phase C 启动条件：**Phase B 决策门 3 维全过 + user 明确批准**

---

## 7. Success criteria（本 spec 整体）

本 spec 视为**成功**的判定（按时间窗口）：

| 时间窗 | 成功定义 |
|---|---|
| Phase A 完（1-2 天） | 有 directional signal（赢 / 输 / 混合），任何方向都算成功（消除盲点） |
| Phase B 完（额外 3-5 天） | 有统计结论（best-pair 赢 / 输 / borderline 三选一），ab_feedback 表新增 ≥ 30 行 |
| Phase B → Phase C 决策（决策门评估） | 给出明确推 / 不推 / 加测 三选一建议 + 理由 |
| 本 spec 整体 done | Phase A + B 报告交付，下次 session 接 Phase C 或转其他方向 |

---

## 8. 时间预算 summary

| 阶段 | 时间 | 累计 |
|---|---|---|
| Phase A | 1-2 天 | 1-2 天 |
| Phase B Day 0 audit | 2-4 小时 | 1.5-2.5 天 |
| Phase B Day 1-2 render | 1-2 天 | 2.5-4.5 天 |
| Phase B Day 3 user 盲评 | 30-60 min user time | 3-5 天 |
| Phase B Day 4 report | 1-2 小时 | 3-5 天 |
| **本 spec 总** | **3-7 天**（best case A 通过即停 2 天） | — |

满足 user "比 1 周更快" 边界。

---

## 9. 风险接受度 summary

| 风险类别 | Phase A | Phase B |
|---|---|---|
| Production rollback 风险 | 无（不动 promotion_state） | 无（仍 shadow） |
| Data corruption | 无（只读 + 写 ab_feedback 表） | 极低（best-pair render 走已有 schema） |
| User time 投入 | 10-15 min | +30-60 min |
| WIP worktree 改动 | 无 | Day 0 可能 commit Phase 2 T2 到 main（user 授权） |
| Bias 风险 | image ref 乱序 + filename hash | 同 + 更严格（30+ 样本 + Day 3 batch 内一次性评） |

---

## 10. Decision log

- 2026-05-28 brainstorming: 5/5 framing 问题答完
- 2026-05-28 approach: A→B 推荐路径选定
- 2026-05-28 Phase A 设计: user 批准（"全 OK 进 Phase B 设计"）
- 2026-05-28 Phase B 设计: user 批准（"OK 进 design doc 落盘"）
- 待: Phase A 启动需 user 再次签字（实施 step 1 前）
- 待: Phase B Day 0 audit 后 Phase 2 T2 merge 决策需 user 授权
- 待: Phase B 决策门后 Phase C 是否启动需 user 明确批准（下次 session）

---

## 11. Spec self-review（brainstorming step 7）

检查项：

| 检查 | 结果 |
|---|---|
| Placeholders（如 `TBD` / `???`） | ✅ 无 |
| Contradictions | ✅ Phase A 决策门与 Phase B 前提一致；Out of scope 各处一致 |
| Ambiguity | ⚠️ "代表性"在 §4.6 说"不是这一步要解决"，需 Phase B §5.4 明确（cross 3 维 — 已写） |
| Scope | ✅ 严格限定 best-pair 一方向；其他 3 方向显式列 Out of scope |
| Success criteria 可测 | ✅ 每 phase 都有可量化决策门（赢/输/平 + 阈值） |
| 风险有缓解 | ✅ 每个风险都有 mitigation 列 |
| 时间预算与 user 约束一致 | ✅ 最长 5 天 < 1 周 |
| Out-of-scope 显式 | ✅ §6.1 + §6.2 两层显式 |

**Self-review 通过** — 准备 commit。

---

## 12. 引用

- brainstorming SKILL.md: `~/.claude/plugins/cache/superpowers-marketplace/superpowers/5.0.7/skills/brainstorming/SKILL.md`
- best-pair 原 plan: `docs/superpowers/plans/2026-05-09-best-pair-routing.md`
- best-pair 原 spec: `docs/superpowers/specs/2026-05-09-best-pair-routing-design.md`
- 本会话 11 wave 总结: `~/.claude/memory/journal/2026-05-28.md`
- case-workbench 时间线: `~/.claude/memory/projects/case-workbench.md`

