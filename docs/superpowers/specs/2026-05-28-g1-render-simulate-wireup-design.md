# G1 Design: render flow 接通 simulate-after dispatcher（ComfyUI 自动出图）

> 2026-05-28 · 5 个 STOP 后的最终方向
> 前置 finding: PR #27 Phase A / PR #28 connectivity audit / closed PR #29 6.1 MVP / α audit
> User 目标 G1: case 标准 render 拼板后自动出 AI 增强图（不需手动 /simulate-after）

## 1. Context

### 1.1 实证生产现状（5/28 audit 综合 5 个 STOP 后）

| Flow | 当前实装 | 当前 production |
|---|---|---|
| **render flow** `/api/cases/{id}/render` | layout-only 拼板 + md_ai brand 走 Node.js PS direct (`run_direct_clinical_enhancement`) | render_jobs 表 / md_ai 172 jobs zero ComfyUI 触发 |
| **simulate-after flow** `/api/cases/{id}/simulate-after` 或 `/api/case-groups/{id}/simulate-after` | dispatcher (`run_after_simulation`) → comfyui_local 或 ps_model_router | simulation_jobs 表 975 row / `provider=comfyui_local done=625 + 26 done_with_issues + 60 failed + 1 running` = 712 ComfyUI 真触发 / 跨 5-2 to 5-23 / fumei + md_ai 双 brand |

simulation pipeline 已 production，但**render flow 不接它**。

### 1.2 5 个 STOP 实证链

1. STOP-1 (Phase A): data corruption — best-pair 覆盖 ai render_jobs
2. STOP-2 (Phase A): byte-identical zero call — render flow 出 layout-only
3. STOP-3 (PR #28 audit): 4 WIP worktree 全 15/100，main 已实装 ComfyUI 但 audit 错以为 G2 manifest 缺失
4. STOP-4 (6.1): production md_ai 走 Node.js PS 不走 ComfyUI（修正 audit）
5. STOP-5 (α.1 audit): simulation-after pipeline 已 production 712 次 ComfyUI 真触发（修正 6.1）

## 2. Goal

让 render flow 完成 layout 拼板后**自动触发 simulate-after dispatcher 出 AI 增强图**，复用已 production 验证的 simulation pipeline 不重造轮子。

## 3. 三个 candidate sub-option

### Option G1.A — inline P1→P2 swap（最小改动）

**做法**：把 `_automate_md_ai_clinical_enhancements` 中调的 `run_direct_clinical_enhancement` (P1 Node.js PS) **swap 成 `run_after_simulation(provider="comfyui_local", ...)`** (P2 dispatcher)。

**保留**：inline 语义（staging dir 中 after image 被 enhance 后 step 2 subprocess 拼板用 enhanced 图）
**改动**：1 个函数 swap + adapter layer 处理 `dict` vs `Path` 返回类型不同
**复杂度**：~0.5 day
**风险**：低（隔离改动 / 不影响 simulate-after endpoint / 不动 simulation_jobs schema）
**缺点**：不写 simulation_jobs DB row，没 audit / no qa_scores / no watermark — render output 无 audit trail
**适合**：作为快速 ComfyUI 接入，省 PS API cost，不需 G1.B 的完整 audit

### Option G1.B — post-render auto-enqueue simulate-after job（完整 audit）

**做法**：render flow `_finish_result` 完成后，对满足条件的 case **自动调 `simulate_case_after` 函数（不是 HTTP）** 入 simulation_jobs 队列 → 独立 simulation_jobs row + 完整 P2 audit + ComfyUI 真触发 + watermark + qa_scores。

**保留**：render flow + simulate-after flow 两条独立 pipeline，render 干净不污染
**改动**：refactor `simulate_case_after` 抽 service layer（让 render_queue 可调）+ render_queue `_finish_result` 加 post-hook + brand/condition filter + 决策 manual_input 字段（focus_targets / focus_regions / model_name）
**复杂度**：~2-3 day
**风险**：中（refactor 拆 simulate_case_after / 决策 focus_targets 来源 / shadow gate 控制 / failure 处理）
**优点**：完整 audit / qa_scores / watermark / 复用 _comfyui_ab_validation_gate 控制
**缺点**：~2 倍 quota（每 case render → 1 ComfyUI inference，无人工 trigger 控制）

### Option G1.C — feature-flagged G1.A（hybrid）

**做法**：先做 G1.A 简单 swap，加 feature flag `RENDER_AUTO_AI_PROVIDER` 默认 `ps_model_router`，可切 `comfyui_local`。后续如真需要完整 audit 再升级 G1.B。

**复杂度**：~1 day（G1.A + flag layer）
**优点**：渐进 + 可回退 + flag 控制灰度
**缺点**：还是不写 simulation_jobs DB row，audit 有限

## 4. 关键决策点（user 待 confirm）

### 4.1 选哪个 sub-option？
G1.A (0.5d 快速 swap，无 audit) / G1.B (2-3d 完整 audit) / G1.C (1d hybrid)

### 4.2 brand scope?
- 只 md_ai/meiji_ai（保持当前 brand gate 语义）
- 所有 brand（包含 fumei shimei — 但 fumei 的 case 设计上不需医学增强）

### 4.3 focus_targets 来源?
- 现有：case_tags 与 MD_ANATOMICAL_KEYWORDS 交集（render 已实现，仍可复用）
- 替代：默认 ["面部"]（最宽泛）
- 替代：从 case manual_overrides / pre_render_gate 算

### 4.4 focus_regions 来源?
- 现有 simulate-after：user 在 endpoint payload 传（人工框）
- render flow 没人工输入 → 默认 full image / from manifest semantic detection

### 4.5 failure 处理？
- render done 但 ComfyUI fail → 整体 status: done (render OK) + ComfyUI fail audit only
- render done + ComfyUI fail → 整体 status: done_with_issues
- render done + ComfyUI fail → 整体 status: failed (退回 user)

### 4.6 灰度 / shadow 控制?
- 用 promotion_state (shadow=False / p10=10% / p100=All) — 已有 machinery
- 或新 flag `RENDER_AUTO_AI_ENABLED=false` 默认关
- 或两者结合

### 4.7 quota 预估?
- G1.A: 每 md_ai render = 1-3 ComfyUI inference（after image count），ComfyUI 本地不 cost
- G1.B: 每 md_ai render = 1 simulation_jobs row + 1 ComfyUI inference + watermark write
- G1.C: 同 G1.A，可 flag 切到 PS 模式（API cost）

## 5. Phase 拆分（按选 G1.A/B/C 决定）

### G1.A Phase
- Phase 1 (TDD): unit test 验证 `_automate_md_ai_clinical_enhancements` 调 dispatcher with `provider=comfyui_local`
- Phase 2: 实施 swap + adapter for `dict→Path` 返回
- Phase 3: 集成测试 跑 fresh md_ai case 看 ComfyUI history + ai_usage flag
- Phase 4: PR + 4-subagent review

### G1.B Phase
- Phase 1: refactor `simulate_case_after` 抽 service layer
- Phase 2: render_queue `_finish_result` 加 hook
- Phase 3: focus_targets / focus_regions / model_name 决策来源 + 测试
- Phase 4: shadow gate + failure 处理 + audit field 接入
- Phase 5: 集成测试 + 4-subagent review

### G1.C Phase
- Phase 1-2 同 G1.A
- Phase 3: 加 `RENDER_AUTO_AI_PROVIDER` env var + flag layer
- Phase 4: 集成测试 + PR

## 6. Out of scope（本 plan 不做）

- 不解锁 fumei brand 走 enhancement（仍尊重 brand gate 设计 — 等 G1.B 之后 user 决定）
- 不改 simulate-after endpoint behavior（已 production，不动）
- 不动 4 个 WIP worktree（实证不在主路径）
- 不动 promotion_state（保持 shadow 直到 user 明确 promote）
- 不写完整的 P10/P25/P50 灰度上线（那是后续 plan）

## 7. Success criteria

- `case_id=129 brand=md_ai` fresh render → render_jobs row done + simulation_jobs row done (G1.B) 或 ai_usage.used_after_enhancement=true (G1.A)
- ComfyUI /history 有 prompt_id
- 最终 output sha256 ≠ Phase A best-pair backup `e39bd3...`
- vlm_usage_log 时间窗有行（如果 dispatcher 包含 VLM judge）
- 单测 + 集成测试 + Playwright 全过 + CI 三 job 绿

## 8. 风险

| 风险 | 缓解 |
|---|---|
| ComfyUI 服务不稳定 / 超时 | timeout + retry + fallback to layout-only （不阻 render） |
| ComfyUI workflow 选错（local_region_enhance vs background_cleanup） | 默认 local_region_enhance_v1@conservative（已 production 验证） |
| focus_regions 不准 → 误增强 | 默认全图 / shadow mode 验证后切换 |
| simulation_jobs 表暴增（每 case render 都写） | G1.A 不写 / G1.B 加 conditional skip（如已有近期 done jobs 不重写） |
| MD-AI brand 之外的 case 误触发 | brand gate 保留 / shadow gate 控制 |

## 9. Decision log

- 2026-05-28 user 选 G1（render flow 接 ComfyUI），不是 G2/G3
- 2026-05-28 STOP-5 反转推翻 6.1 α/β/γ
- 待 confirm: G1.A vs G1.B vs G1.C 选哪个 + §4 6 个决策

## 10. References

- audit PR #28 `2b53f47`: connectivity-audit.md
- 6.1 closed PR #29 `99ffa24`: phase-6.1-mvp-report.md（finding 部分仍参考价值）
- Phase A PR #27 `6756e2a`: phase-a-evidence-report.md
- Initial design PR #26 `5094767`: 2026-05-28-vlm-comfyui-optimization-launch-design.md
