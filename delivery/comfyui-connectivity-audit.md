# ComfyUI Connectivity Audit — VLM+ComfyUI 优化未上线根因 3-gate chain

> 2026-05-28 · 续 swift-validation Phase A finding
> 实施：4 WIP worktree audit + main 分支 ComfyUI 代码 trace + 3 个 gate chain 实证
> 决策：A 路径（全链路 gate audit + 解锁）

## 1. TL;DR

**4 个 WIP worktree（best-pair-routing / vlm-judge / vlm-phase2 / vlm-calibration）全 15/100，都不是接通 ComfyUI 的关键**。Main 分支 `backend/ai_generation_adapter.py` 已实装完整 ComfyUI client（COMFYUI_BASE_URL + 4 workflow + queue_prompt + memory gate + production gate report machinery）。

**production "ai" render mode 从未真触发 ComfyUI 的根因 = 3 层 gate chain 全部 fail-closed：**

| Gate | 文件 / 行 | 当前状态 | 失败行为 |
|---|---|---|---|
| G1 brand gate | `render_queue.py:1914` `if brand in ("md_ai","meiji_ai"):` | fumei 不属于 → skip enhancement hook | 走 layout-only path（解释 Phase A byte-identical finding） |
| G2 promotion manifest gate | `services/promotion_manifest_loader.py` `load_manifest()` | `case-workbench-ai/promotion/manifest.json` **文件缺失** → `None` → `state="shadow"` → `should_promote(case_id)=False` | candidate_only=True（即使 md_ai brand 也只 shadow inference） |
| G3 ComfyUI 服务 gate | `127.0.0.1:8188` | lsof 空 / curl HTTP 000 / ps 无进程 | 服务未启 → connection refused（即使前两 gate 通过也无法 invoke） |

任意一个 gate 失败都阻 ComfyUI 真出图。**三个 gate 当前全部 fail**，所以 production rendered output = layout-only board，与 best-pair byte-identical（Phase A 已实证）。

## 2. 4 个 WIP worktree 不是关键的实证

| worktree | branch | ahead | 评分 | 设计定位 | ComfyUI 实装 |
|---|---|---|---|---|---|
| best-pair | feat/best-pair-routing | 6 commits | 15/100 | T1+T2 DB schema + 写入层 foundation；**设计是绕过 AI 用 MediaPipe 选图** | 0 grep 命中 |
| vlm-judge | feat/vlm-judge-vigilant | 1+2 WIP | 15/100 | Pro+Flash 共识判定（只评分不出图），离线校准工具 | 0 grep 命中 |
| vlm-phase2 | feat/vlm-phase2-tiered-prompt | 0（main ahead 56 commit）| 15/100 | 研究态 Tier 1/2/3 prompt schema + parser，**branch stale** | 0 grep 命中 |
| vlm-calibration | feat/vlm-calibration | 1 commit | 15/100 | distribution collapse 检测库 + 日报 CLI | 0 grep 命中 |

每个 worktree 都是辅助系统（选图 / 评分 / 校准），**没有任何一个负责"AI 出图"路径**。真接入 ComfyUI 的代码早已在 main 分支。

## 3. Main 分支 ComfyUI 实装实证

### 3.1 ai_generation_adapter.py 关键定义

```python
# Line 48-87
COMFYUI_PROVIDER = "comfyui_local"
COMFYUI_BASE_URL = os.environ.get("CASE_WORKBENCH_COMFYUI_BASE_URL",
    os.environ.get("COMFYUI_BASE_URL", "http://127.0.0.1:8188"))
COMFYUI_WORKFLOW_DIR = "comfyui-workflows"
COMFYUI_ALLOWED_CANDIDATE_WORKFLOWS = {
    "local_region_enhance_v1@conservative",
    "local_region_enhance_v2@conservative",
    ...
}
COMFYUI_MAX_CONCURRENCY = 1   # _COMFYUI_GATE BoundedSemaphore
COMFYUI_MIN_FREE_MEMORY_MB = 1024
COMFYUI_TIMEOUT_SEC = 300
COMFYUI_AB_VALIDATION_REPORT_PATH = "case-workbench-ai/ab_runs/t47_comfyui_ab_report.json"
COMFYUI_VLM_GUARDRAIL_REPORT_PATH = "case-workbench-ai/ab_runs/comfyui_vlm_guardrail.json"
COMFYUI_PRODUCTION_GATE_REPORT_PATH = "case-workbench-ai/ab_runs/comfyui_production_gate.json"
```

### 3.2 实际可用的 ComfyUI 函数（已实装）

| 函数 | 行号 | 作用 |
|---|---|---|
| `_comfyui_preflight` | 987 | 启动前检查 |
| `_comfyui_upload_image` | 1054 | 上传图到 ComfyUI |
| `_comfyui_json` | 965 | HTTP wrapper |
| `_comfyui_free_memory` | 1351 | GPU 内存清理 |
| `_comfyui_interrupt` | 979 | 中断进行中 prompt |
| `_comfyui_ab_validation_gate` | 707 | AB validation 报告 gate |
| `run_comfyui_local_after_simulation` | 2686 | 真正的 ComfyUI 出图入口 |
| `run_direct_clinical_enhancement` | 2609 | 上层路由，调 `run_comfyui_local_after_simulation` |

### 3.3 comfyui-workflows/ 目录文件

```
background_cleanup_v1.json
local_region_enhance_v1.json
local_region_enhance_v2.json
local_region_enhance_v3.json
portrait_45_compare_v1.json
portrait_front_compare_v1.json
portrait_side_compare_v1.json
```

7 个 ComfyUI workflow JSON 模板齐全。

## 4. 3-gate chain 详细 trace

### G1 — Brand gate（render_queue.py:1914）

```python
# 1.5. MD-AI clinical enhancement (automation hook)
if brand in ("md_ai", "meiji_ai"):
    ...
    self._automate_md_ai_clinical_enhancements(...)
# else: skip — 不走 enhancement，直接进 step 2 subprocess render
```

**4 处 brand gate**（全显式 reject fumei）：
- `render_queue.py:1586` `_automate_md_ai_clinical_enhancements` `if brand not in (...) return`
- `render_queue.py:1748` `use_staging = bool(... or brand in (...))`
- `render_queue.py:1914` step 1.5 enhancement hook 入口
- `render_queue.py:1932` hook 调用

设计意图：MD-AI brand 是医美专用，需要医学增强（local region enhance）；fumei/shimei 是普通品牌不需要。**这是显式商业规则不是 bug**。

**实证**：DB 中 fumei brand 394 jobs / md_ai 172 jobs / shimei 64 jobs，case_id=126 是 fumei → 永远跳过 G1。

### G2 — Promotion manifest gate（promotion_manifest_loader.py）

```python
DEFAULT_MANIFEST_PATH = "case-workbench-ai/promotion/manifest.json"

def load_manifest(path: Path | None = None) -> dict[str, Any] | None:
    target = path if path is not None else DEFAULT_MANIFEST_PATH
    if not target.exists() or not target.is_file():
        return None      # ← fail-safe: 文件缺失返 None
    ...

def get_promotion_state(manifest=None) -> str:
    m = manifest if manifest is not None else load_manifest()
    if m is None:
        return FAIL_CLOSED_STATE   # ← "shadow"
    ...

def should_promote(case_id, *, manifest=None) -> bool:
    state = get_promotion_state(manifest)
    if state == "p100": return True
    if state in {"shadow", "rolled_back"}: return False   # ← 所有 case False
    if state == "p10": return _stable_bucket(case_id) < 10
    ...
```

**实证**：
- `case-workbench-ai/promotion/manifest.json` **文件不存在**
- → `load_manifest()` return None
- → `get_promotion_state()` return "shadow"
- → `should_promote(case_id=任意)` return False

在 `ai_generation_adapter.py:2672`：
```python
promoted = bool(case_id is not None and should_promote(int(case_id)))
return {
    "candidate_only": not promoted,       # ← True（shadow / no manifest）
    "mix_with_real_case": promoted,        # ← False
    "can_publish_default": promoted,       # ← False
    "promote_to_default": promoted,        # ← False
}
```

即使 brand=md_ai 通过 G1，G2 也会把 ComfyUI 强制设为 candidate_only=True（只跑 shadow inference 不写真出图替换原 layout）。

### G3 — ComfyUI 服务 gate（127.0.0.1:8188）

**实测（2026-05-28 10:50）**：
```bash
$ lsof -nP -iTCP:8188 -sTCP:LISTEN
# 空

$ curl -sm 3 -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8188/
HTTP 000  ← 连接失败

$ ps aux | grep -iE "comfy|python.*main\.py" | grep -v grep
# 空
```

**结论**：ComfyUI 服务从未在本机启动。即使前两层 gate 通过，HTTP call 会 connection refused → `_comfyui_preflight` 抛异常 → fallback 到 layout-only。

### G3 supplementary — md_ai history 同样 zero ComfyUI 痕迹

```sql
SELECT meta_json FROM render_jobs WHERE id=568;  -- 最新 done md_ai
-- ai_usage.used_after_enhancement: False
-- ai_usage.used_ai_padfill: False
```

`case-workbench-ai/ab_runs/comfyui_production_gate.json` 文件不存在 → 即使 G1+G2 通过，G3 也阻；md_ai 172 jobs 都没真触发过 ComfyUI。

## 5. 完整 production 上线还差多少（按 _comfyui_ab_validation_gate 实装）

读 `ai_generation_adapter.py:711-789` 函数 `_comfyui_ab_validation_gate`，**完整 production "promote_to_default" 需要 8 个 condition 全过**：

| # | Condition | 当前状态 | 文件 / 字段 |
|---|---|---|---|
| 1 | AB validation report 存在 | 缺失 | `case-workbench-ai/ab_runs/t47_comfyui_ab_report.json` |
| 2 | report `ready_for_human_review=true` | N/A（文件缺失）| 同上 |
| 3 | report `promote_to_default=true` | N/A | 同上 |
| 4 | promotion_approval.scope = `comfyui_default_promotion_v1` | 缺失 | 同上 |
| 5 | promotion_approval.decision = `approve_default_promotion` | 缺失 | 同上 |
| 6 | VLM guardrail report 通过 | 缺失 | `comfyui_vlm_guardrail.json` |
| 7 | Production gate report 通过 | 缺失 | `comfyui_production_gate.json` |
| 8 | promotion/manifest.json state ≥ p10 | 缺失 | `promotion/manifest.json` |

外加 G3：ComfyUI 服务自身要起。

## 6. 解锁路径推荐（按 ROI）

### 6.1 最小 connectivity test（MVP）— 验证"接通"可行性

**目标**：跑通 1 case，看 ComfyUI 真出图 vs layout 是否实际产出不同 sha256。

**Step**：
1. **启 ComfyUI 服务**：`cd <ComfyUI 安装目录> && python main.py --listen 127.0.0.1 --port 8188` → curl /system_stats 应 200
2. **写 shadow manifest**：`echo '{"promotion_state":"shadow","schema_version":1,"updated_at":"2026-05-28T..."}' > case-workbench-ai/promotion/manifest.json` → `should_promote()` 仍 False but `candidate_only=True` shadow inference 可跑
3. **跑 md_ai brand case**：enqueue 任意 md_ai brand case → 应触发 G1 + G3 ComfyUI invoke（candidate-only / shadow artifact 写 `case-workbench-ai/candidates/` 不替换 final-board）
4. **验证 sha256**：fresh md_ai render 的 final-board.jpg sha256 应与 fumei 不同（因为走了 candidate-only 路径但 layout 部分仍跑）+ 检查 `case-workbench-ai/candidates/` 有无 ComfyUI 产出

**预估时间**：0.5-1 天（取决于 ComfyUI 启动是否顺利 + 是否需要 model weights）
**Quota**：0（ComfyUI 是本地推理无 API cost）
**风险**：ComfyUI 服务可能没装 / model weights 没下载 / GPU 内存不够 → 起不来

### 6.2 P10 灰度上线（5-day plan）— 真 production 路径

需要按 `_comfyui_ab_validation_gate` 8 condition 全通过的方式：
1. Day 0：MVP step 1-3 跑通 + 收 baseline AB sample（候选 vs base 10-20 对）
2. Day 1：跑 AB validation → 写 `t47_comfyui_ab_report.json`（人工审 + promotion_approval）
3. Day 2：跑 VLM guardrail → 写 `comfyui_vlm_guardrail.json`
4. Day 3：跑 production gate → 写 `comfyui_production_gate.json`
5. Day 4：manifest state 推到 `p10` + monitor SLO（W4 已有 framework）

### 6.3 brand gate 解锁 fumei（独立小路径）

**仅当**：6.1 跑通后，想让 fumei brand 也走 enhancement → 改 `render_queue.py:1914` `if brand in ("md_ai","meiji_ai"):` 加 fumei
**风险**：fumei case 设计是普通品牌不需医学增强，强加可能视觉退化
**建议**：先 6.1 + 6.2，fumei 解锁不是 production gap 的 blocker，是后续 enhancement scope 决策

## 7. Recommendation（下个 plan 边界）

**强烈推荐路径 6.1 MVP 优先**：用 1-2 个 md_ai case + shadow manifest，验证 ComfyUI 端到端能不能跑通。这是所有后续工作的物理 prerequisite。

**不要**：
- 不要 merge 4 个 WIP worktree（与 ComfyUI 接通无关，浪费时间）
- 不要先改 brand gate（设计正确，等 6.2 完成后再决策）
- 不要直接跳 6.2 p10 灰度（前提 ComfyUI 服务自身要先在跑）

## 8. 关联文件

| 文件 | 是否进 PR | 备注 |
|---|---|---|
| `delivery/comfyui-connectivity-audit.md` | ✅ tracked | 本文 |
| 4-agent audit reports | (机外，已 inline) | best-pair / vlm-judge / vlm-phase2 / vlm-calibration |

**Reviewer 复核路径**：
1. `curl -sm 3 http://127.0.0.1:8188/system_stats` → 当前 HTTP 000
2. `ls case-workbench-ai/promotion/manifest.json` → No such file
3. `ls case-workbench-ai/ab_runs/comfyui_production_gate.json` → No such file
4. `grep -n "brand in.*md_ai" backend/render_queue.py` → 4 处命中
5. `grep -n "COMFYUI_BASE_URL\|run_comfyui_local_after_simulation" backend/ai_generation_adapter.py` → 5+ 处命中（证实代码已实装）
