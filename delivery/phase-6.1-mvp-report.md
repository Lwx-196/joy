# Phase 6.1 MVP Finding Report — 3-gate 全过但仍未触发 production AI gen

> 2026-05-28 · plan `~/.claude/plans/comfyui-unlock.md` Phase 6.1 MVP
> 实施：ComfyUI 服务起 + shadow manifest + fresh md_ai render
> 决策：**Phase 6.1 STOP — 实证发现 production 有两条 AI 路径，原 connectivity-audit 假设需修订**

## 1. TL;DR

Phase 6.1 MVP **执行成功 + 3 个 gate 全通过**，但触发 fresh md_ai render job 634 后**ComfyUI 仍未被调用**。深入审计发现：

**production md_ai brand 走的是 Node.js PS script（外部 API），不是 ComfyUI 本地推理**。

connectivity-audit (PR #28) 描述的 3-gate chain 是正确的 — G1/G2/G3 都需要通过。但 audit 假设"过了 3-gate 就触发 ComfyUI"是错的：**production 实际只走 PS script 路径，ComfyUI 入口由独立的 simulation pipeline dispatcher 调用，不在 md_ai render 流程中**。

## 2. 6.1 实施轨迹

| Step | 状态 | 结果 |
|---|---|---|
| 6.1.1 probe ComfyUI 装在哪 | ✅ done | `/Users/a1234/Desktop/飞书Claude/ComfyUI`（image-workbench 项目共享），完整安装 + .venv/ + models/ + custom_nodes/ |
| 6.1.2 启动 ComfyUI 服务 | ✅ done | PID 91129 / mps device 34GB VRAM / py3.12.13 / pytorch 2.11 / ComfyUI 0.21.0 / curl /system_stats HTTP 200 |
| 6.1.3 ~~写~~ **验证** shadow manifest | ✅ done（**errata: 见 §9**）| 在 phase-a worktree 写 manifest 后实际发现 origin/main **已有 manifest.json**（schema_v1 + state="shadow" + scope="production" + approver=null placeholder，自某 wave 起 tracked）；恢复回 origin/main 内容后 `should_promote=False` 行为不变，所以 6.1.3 实际是"验证 G2 一直是过的"不是"新写" |
| 6.1.4 fresh md_ai render | ⚠️ done_with_issues | job 634 case_id=129 brand=md_ai 跑 489s (8 分钟) done_with_issues |
| 6.1.5 验证 ComfyUI 真触发 | ❌ **NOT TRIGGERED** | `ai_usage.used_after_enhancement=false` / ComfyUI `/history` 0 entries / `vlm_usage_log` 时间窗 0 行 / `case-workbench-ai/candidates/` 空 / `ab_runs/` 无新 artifact |

## 3. 实证发现 — 两条 AI 路径二分

通过 grep 调用链 trace 发现 `backend/ai_generation_adapter.py` 实际有**两条独立 AI image generation 路径**：

### 路径 P1 (direct enhancement) — 当前 production md_ai 走的

```
RENDER_QUEUE.enqueue(case_id=129, brand="md_ai")
  → _execute_render_impl
    → if brand in ("md_ai","meiji_ai"):       # G1 brand gate ✅
    → _automate_md_ai_clinical_enhancements
      → run_direct_clinical_enhancement(entry, brand, focus_targets)
        ↓
        subprocess.run(["node", PS_ENHANCE_SCRIPT, ...])  # ← Node.js + 外部 API
        ↓
        case_layout_enhance.js (在 ~/Desktop/飞书Claude/skills/case-layout-board/scripts/)
        ↓
        external API call (gpt-image-2-vip / gemini-3-pro-image-preview-4k / nano-banana via PS model router)
```

**特点**：
- 用 **付费外部 API** 出图（OpenAI / Gemini / Tuzi 等）
- 跟 ComfyUI 完全无关（ComfyUI 服务起着也用不上）
- 与 promotion_state 灰度无关
- 在 md_ai/meiji_ai brand 自动触发
- silent fail：if `returncode != 0` → return 原图（无 ai_usage flag）

### 路径 P2 (simulation ComfyUI) — connectivity-audit 假设的，但未真触发

```
[unknown simulation pipeline caller]
  → 调用 dispatch (ai_generation_adapter.py:2998)
    → if provider == COMFYUI_PROVIDER: run_comfyui_local_after_simulation(...)  # ← ComfyUI 本地
    → if provider == DEFAULT_PROVIDER: run_ps_model_router_after_simulation(...)  # ← Node.js
```

**特点**：
- 通过 dispatcher 按 `provider` 选 ComfyUI 或 PS Model Router
- 受 promotion_state + _comfyui_ab_validation_gate 8 condition 严格控制
- 入口函数 `run_comfyui_local_after_simulation` (L2686) 真实接 `127.0.0.1:8188`
- **但没找到 production render 流程中调用 dispatcher / `run_comfyui_local_after_simulation` 的入口**

### 实证

```sql
-- job 634 (md_ai brand, fresh fresh render, 489s done)
SELECT meta_json FROM render_jobs WHERE id=634;
-- ai_usage.used_after_enhancement: False
-- ai_usage.used_ai_padfill: False
-- ai_usage.generated_artifact_count: 0
```

```bash
# ComfyUI 服务起来后 /history
curl http://127.0.0.1:8188/history
# {} — 0 entries
```

```bash
# vlm_usage_log 时间窗
SELECT COUNT(*) FROM vlm_usage_log WHERE created_at >= '2026-05-28 03:44:00';
# 0
```

```bash
# 候选 artifact 路径
ls case-workbench-ai/candidates/   # empty
ls case-workbench-ai/ab_runs/      # 最新 5/18，无新增
```

### 8 分钟跑了什么

job 634 跑了 489 秒，但 ai_usage 全 False。最可能解释：
- `_automate_md_ai_clinical_enhancements` scan staging dir 找到 3 张 after image
- 对每张调 `run_direct_clinical_enhancement` → `subprocess.run(node, ...)` 走 Node.js script
- 每张 DEFAULT_TIMEOUT_SEC=240s timeout 或 silent fail (`returncode != 0` → return 原图)
- 3 × ~160s ≈ 8 min
- staging 中文件没被 enhanced 替换 → final-board 拼版仍用原图 → manifest 没 `enhancement.enhanced_path` → `used_after_enhancement=False`

## 4. 对原 connectivity-audit 的修订

PR #28 `delivery/comfyui-connectivity-audit.md` 的 3-gate chain **位置正确**，但**触发模型错**：

| Audit 原假设 | 6.1 实证 |
|---|---|
| 3-gate 全过 → 触发 ComfyUI | ❌ — 即使 3-gate 全过，production md_ai 仍只走 Node.js PS path |
| ComfyUI 通过 `_automate_md_ai_clinical_enhancements` 调 | ❌ — `_automate_md_ai_clinical_enhancements` 调的是 `run_direct_clinical_enhancement`（Node.js path），不是 ComfyUI 入口 |
| `_comfyui_ab_validation_gate` 8 condition 用于 production gate | ✅ 但这 8 condition 是给 P2 simulation ComfyUI 路径用的，P1 PS path 不走这套 |

## 5. 6.1 → 6.2 决策门

按原 plan `~/.claude/plans/comfyui-unlock.md` 4.5 决策门：

| 6.1 结果 | 决策 |
|---|---|
| ComfyUI 真触发，sha256 ≠ Phase A baseline | ✅ 进 6.2 |
| ComfyUI 没被调（本次结果） | ⚠️ **STOP** — 6.2 8-condition 前提不成立（8 condition 是给 P2 simulation 路径的） |

**决策**：6.2 暂停。下一步需要 user 决定：

### Option α: 接通 P2 simulation ComfyUI 路径
- 需要先理解 production simulation pipeline 当前怎么 trigger（看 `run_ps_model_router_after_simulation` 谁调）
- 把 case-workbench render flow 改为通过 dispatcher 而不是 direct PS script
- 这是真正的"ComfyUI 接入 production"，但是较大的架构改动
- 时间预算：3-5 天 + 需要 design doc

### Option β: 接通 P1 PS path（更新 connectivity-audit + 重新定义 "接通"）
- "接通"实际意思是让 P1 Node.js PS script 成功调外部 API + 写 enhancement.enhanced_path 进 manifest
- 需要：probe `case_layout_enhance.js` 失败原因（API key / endpoint / quota / silent error）
- 跑通后 production md_ai 有 enhancement，但**仍非本地 ComfyUI 推理**（依赖外部付费 API）
- 时间预算：0.5-1 天

### Option γ: 修订 audit + 关闭 comfyui-unlock plan
- 实证 finding 已足够定 "production AI gen 落地" 的真实路径不是 ComfyUI 而是 Node.js PS script
- 写 finding report + 关闭 plan
- 用户重新决定业务方向（如果业务期望是本地 ComfyUI 推理省 API cost，需起新 plan；如果业务期望是外部 API 真触发，走 β）

## 6. Side effects from 6.1 实施

| 改动 | 类型 | 后果 |
|---|---|---|
| ComfyUI 服务起来（PID 91129） | runtime state | 占 mps 内存，user 可手动 kill |
| `case-workbench-ai/promotion/manifest.json` 写入 | file added | shadow state + `should_promote=False`，BC 与原 missing 行为相同（fail-closed），不影响 production |
| job 634 已写 DB (case_id=129 brand=md_ai, status=done_with_issues, 489s, output 文件已 cleanup) | DB row | 历史 render_jobs 加 1 row，可选 cancel/delete |
| fresh render 消耗 PS API quota | 外部 cost | 3 张 image × Node.js attempt，但 silent fail 可能 0 quota 真消耗（API call 都 fail），需 user 查 PS provider billing |

## 7. 关联文件

- 本 finding: `delivery/phase-6.1-mvp-report.md`
- audit ref: `delivery/comfyui-connectivity-audit.md` (PR #28 `2b53f47`)
- Phase A ref: `delivery/phase-a-evidence-report.md` (PR #27 `6756e2a`)
- 当前 manifest: `case-workbench-ai/promotion/manifest.json`（worktree-only，未进 git 因 sidecar runtime state）

## 9. Errata — 修订 PR #28 audit 关于 G2 manifest 的描述

**Audit (PR #28) 错误描述**："`case-workbench-ai/promotion/manifest.json` 文件缺失"

**实证修正**：origin/main 上**已经 tracked**（commit hash `f03ba2e`，schema_v1 + state="shadow" + scope="production" + approver/approved_at/expires_at=null + bindings 4 项 sha256 + rollback_baseline placeholder + 详细 notes 字段）。`.gitignore:35-37` 显式 `!case-workbench-ai/promotion/` exception 让 manifest 进 git。

**原 audit 在 phase-a worktree 上跑 `ls` 时**worktree 落后 main 56 commits（W9 之前的 vlm-phase2 fork point），那时 manifest 还没 commit 进 main。Audit 应该在 fresh worktree 从 origin/main 拉再 ls。

**对 6.1 finding 的影响**：G2 manifest gate **一直是过的**（state=shadow → fail-closed shadow path 工作正常）。**问题完全不在 G2**，**全在 P1/P2 二分**（production md_ai 走 Node.js PS path，不走 ComfyUI）。修正后 audit 3-gate chain 简化为：

| Gate | 修正状态 |
|---|---|
| G1 brand gate | 仍未过 fumei（设计正确） |
| G2 promotion manifest | ✅ **一直在场** state=shadow，与 P2 ComfyUI 触发模型无关 |
| G3 ComfyUI 服务 | 6.1.2 之前未起，现已起（PID 91129） |

**真正的根因始终是 production 不调 ComfyUI 入口（P2 path），不是任何 gate**。

## 10. Reviewer 复核路径（原 §8）

1. `curl -sm 3 http://127.0.0.1:8188/system_stats | jq .system.comfyui_version` → "0.21.0"
2. `git show origin/main:case-workbench-ai/promotion/manifest.json | jq .promotion_state` → "shadow"（验证 manifest 在 main，errata §9）
3. `sqlite3 case-workbench.db "SELECT meta_json FROM render_jobs WHERE id=634;" | jq .ai_usage.used_after_enhancement` → false
4. `curl http://127.0.0.1:8188/history` → `{}` (0 entries)
5. `grep -n "run_direct_clinical_enhancement\|run_comfyui_local_after_simulation" backend/ai_generation_adapter.py` → 实证两 entry 独立
6. `grep -rn "run_direct_clinical_enhancement\b" backend/` → only render_queue.py:1629 (P1) 调
7. `grep -rn "run_comfyui_local_after_simulation\b" backend/` → only ai_generation_adapter.py:2998 dispatcher (P2) 调
