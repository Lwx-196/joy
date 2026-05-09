# Best-pair 自动选图路径设计

**Project**: case-workbench
**Wave**: 2
**Plan ID**: best-pair-routing
**Workdir**: `/Users/a1234/Desktop/案例生成器/case-workbench`
**Created**: 2026-05-09
**Reviewers**: codex (后端 / 数据 / 架构)，gemini (UX / 前端 / 用户路径)
**Status**: brainstorming v2 — 已吸收 5 项 Critical + 12 项 Warning

## 1. 背景

Wave 1 双轨实施已完成：

- **Track 1 — 净 prompt 测试**：T2 跑 case 88 / 5 次 / `--no-planner` 全程 plannerUsed=false。视觉证据 `/tmp/case88_no_planner/compare.jpg`：5/5 输出全部以原术后图为基准重绘（3/4 侧脸 + 黑发披肩 + 与原术后像素级一致），**0/5 满足"角度对齐术前"**。**判定：净 prompt 输 — planner 不是元凶；模型偏置成立；prompt 路径死。**
- **Track 2 — 案例库角度分布**：T3 扫 123 case / ~87 秒 / 30 case 有效 / 93 跳过（缺 phase 标注或目录不存在）。`<5°` 占 93%，median 2.0°，P90 3.9°。**决策矩阵明确指向 best-pair 自动选图。**

数据产物：

- `/tmp/case_best_pair.json`：30 case top-1 best-pair（filename_before / filename_after / delta_deg）
- `/tmp/case88_no_planner/compare.jpg`：Wave 1 决策视觉证据
- `~/Desktop/案例生成器/case-workbench/.ccg/state.md`：Wave 1 完整执行状态

## 2. 锁定的 7 项决策（含联动决策）

| # | 决策点 | 锁定结果 |
|---|---|---|
| 1 | 流程性质 | best-pair 与 AI **并存独立**；新增 `/api/cases/{id}/best-pair` 路径，仅做选图，不调 AI；前端两个独立按钮 |
| 2 | 数据持久化 | **预计算存 DB**（新表 `case_best_pairs`），UI 实时读 DB |
| 3 | 候选数量 | **Top-5**，按 delta_deg 升序，UI 默认高亮首条 |
| 4 | 计算触发 | **手动 + 事件驱动**；不设 cron |
| 5 | 未标注 case | **显示提示 + 引导现有 ImageOverridePopover**；按 reason 区分 3 种文案 |
| 6 | 输出形态 | **走 render 拼出 final-board**（用 `case_layout_board.py`，与 AI 路径同输出格式） |
| 7 | 选定即标注 | 用户在 BestPairPanel 点"确认选用此对"按钮后**联动写 `case_image_overrides`**；切换候选高亮**不**写 DB |

## 3. 架构

```
                ┌───────────────┐
                │  cases 表     │
                └───────┬───────┘
                        │
        ┌───────────────┴────────────────┐
        ↓                                 ↓
 ┌──────────────────┐          ┌────────────────────────────┐
 │ AI 路径（保留）   │          │ best-pair 路径（新）         │
 │ simulate-after   │          │ /api/cases/{id}/best-pair    │
 │   ↓              │          │   ↓                          │
 │ ai_generation_   │          │ best_pair_service            │
 │   adapter        │          │   ↓                          │
 │ (enhance.js)     │          │ case_best_pairs DB           │
 └────────┬─────────┘          └──────────────┬───────────────┘
          │                                    │
          │           ┌────────────────────────┘
          │           │ 用户确认选定一对
          │           │   ↓ POST best-pair/select
          │           │ case_best_pair_selections
          │           │   ↓
          ↓           ↓ POST best-pair/render
       ┌───────────────────────────────────────┐
       │ RenderQueue.enqueue                    │
       │   render_mode='ai'  /  'best-pair'     │
       └────────────────┬──────────────────────┘
                        ↓
               ┌────────────────────┐
               │ render_executor.   │
               │   run_render()     │
               │     ├─ AI 路径：    │
               │     │    enhance.js │
               │     │    + board   │
               │     └─ best-pair：  │
               │          board only│
               │          (跳过 AI) │
               └────────┬───────────┘
                        ↓
               render_jobs + final-board.jpg + .history 归档
```

## 4. 数据模型

### 4.1 新表 `case_best_pairs`（计算结果缓存）

```sql
CREATE TABLE IF NOT EXISTS case_best_pairs (
  case_id         INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
  status          TEXT NOT NULL,
                  -- 'ready' | 'skipped' | 'pending' | 'dirty'
  skipped_reason  TEXT,
                  -- 'no_phase_labels' | 'no_face_detected' | 'dir_missing' | NULL
  candidates_json TEXT NOT NULL DEFAULT '[]',
                  -- top-5 数组：
                  -- [{"before":"a.jpg","after":"b.jpg",
                  --   "delta_deg":2.1,"delta_yaw":1.2,
                  --   "delta_pitch":1.5,"delta_roll":0.8}, ...]
  candidates_fingerprint TEXT,
                  -- sha1(sorted filenames + sorted overrides + last mtime)
                  -- 用于 selection 校验；判 selection 是否基于已过期 cache
  source_version  INTEGER NOT NULL DEFAULT 0,
                  -- 写 case_image_overrides 时 ++；compute 完成时
                  -- WHERE source_version=observed 才写回 ready，避免并发 stale ready
  scanned_at      TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_best_pairs_status ON case_best_pairs(status);
```

**修订说明（codex C1）**：删除原 `dirty INTEGER` 列（与 `status='dirty'` 双写冗余）。改用 `source_version` + `WHERE source_version=observed_version` 写回避免 stale ready。

### 4.2 新表 `case_best_pair_selections`（用户选定历史）

```sql
CREATE TABLE IF NOT EXISTS case_best_pair_selections (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id                     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  before_filename             TEXT NOT NULL,
  after_filename              TEXT NOT NULL,
  delta_deg                   REAL NOT NULL,
  candidates_fingerprint      TEXT,
                              -- 选定时 case_best_pairs.candidates_fingerprint 的快照
  before_override_before_json TEXT,
                              -- 选定前 (case_id, before_filename) 行 case_image_overrides 全字段 JSON 快照
                              -- NULL 表示选定时该行不存在
  after_override_before_json  TEXT,
                              -- 同上 (case_id, after_filename)
  selected_at                 TIMESTAMP NOT NULL,
  selected_by                 TEXT
                              -- 单用户先填 'local'，留口
);
CREATE INDEX IF NOT EXISTS idx_cbps_case_at ON case_best_pair_selections(case_id, selected_at DESC);
```

**修订说明（codex W1）**：删除 `is_current` 列。"当前选定"通过 `ORDER BY selected_at DESC, id DESC LIMIT 1` 取，避免 partial unique 约束的复杂性。

**修订说明（codex W4）**：加 `before_override_before_json` / `after_override_before_json` 快照，撤销 selection 时可回滚 override（gemini 5 号未决问题）。

### 4.3 现有表改动

```python
# backend/db.py 末尾加 _ensure_render_job_columns helper
def _ensure_render_job_columns(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(render_jobs)")}
    if "render_mode" not in cols:
        conn.execute(
            "ALTER TABLE render_jobs ADD COLUMN render_mode TEXT NOT NULL DEFAULT 'ai'"
        )
    if "best_pair_selection_id" not in cols:
        conn.execute(
            "ALTER TABLE render_jobs ADD COLUMN "
            "best_pair_selection_id INTEGER REFERENCES case_best_pair_selections(id)"
        )

# init_schema() 末尾追加：
_ensure_render_job_columns(conn)
```

**修订说明（codex W3）**：仿 `_ensure_*_columns` 现有 4 个 helper（backend/db.py:322-358）的模式显式补列，不依赖单纯 `ALTER TABLE` 在重复执行下报错。

### 4.4 case_image_overrides 不改 schema

`case_image_overrides` 已有的 `manual_phase` / `manual_view` / `manual_transform_json` 全部够用。来源标识用 cases.py 现有的 `phase_override_source` 计算逻辑扩展（增加第三值 `'best-pair'`）。

## 5. 后端模块拆分

```
backend/
├── routes/
│   └── best_pair.py              # 6 个 REST 端点
├── services/
│   ├── best_pair_service.py       # 核心业务逻辑
│   └── image_override_writer.py   # 统一 case_image_overrides 写入入口（含 dirty hook）
└── workers/
    └── best_pair_compute_queue.py # 单线程 compute queue（不复用 RenderQueue）
```

**修订说明（codex C2）**：抽 `image_override_writer` helper，统一 case_image_overrides 写入。所有现有写入入口（`backend/routes/cases.py` PATCH 主入口 + trash 行 :1476-1479 + `backend/routes/image_workbench.py` 三处 :2428-2435 / :2502-2509 / :2601-2608）改成走该 helper。helper 内部尾部统一调 `mark_best_pair_dirty(case_id)`。

**修订说明（codex C3）**：不复用 `RenderQueue.enqueue_batch`（它会插入 render_jobs 走正式渲染流程），新建 `best_pair_compute_queue` 模块。compute 提交到全局共享 `_job_pool`（backend/_job_pool.py:17-19，MAX_WORKERS=2），best-pair compute 内部串行（MediaPipe singleton 不能多线程）。

### 5.1 服务函数

```python
# best_pair_service.py
def compute_best_pair(case_id: int, db) -> dict:
    """
    单 case 同步算（~1s）：
      1. 读 case_best_pairs 当前 source_version 作为 observed_version
      2. 分类术前/术后图（case_image_overrides 优先 → 关键字 fallback）
      3. MediaPipe FaceLandmarker 提 yaw/pitch/roll
      4. 全配对算 delta_deg → top-5 升序
      5. UPDATE case_best_pairs SET status='ready', candidates_json=..., 
         candidates_fingerprint=..., scanned_at=now, updated_at=now
         WHERE case_id=? AND source_version=observed_version
      6. affected=0 → 不写（说明 mark_dirty 在中间插队），保持 dirty 状态
    """

def select_best_pair(
    case_id: int, before: str, after: str, fingerprint: str, db
) -> int:
    """
    用户选定。单 transaction 完成：
      1. _resolve_existing_source(case_dir, before) + (case_dir, after) 校验存在
         （复用 backend/routes/cases.py:1276 现有函数，不只 basename guard）
      2. 读 case_best_pairs：status 必须='ready'，
         candidates_fingerprint 必须等于传入 fingerprint
         否则 → 409 'stale'
      3. 读 (before, after) 当前 case_image_overrides 行做快照
      4. INSERT case_best_pair_selections，含 fingerprint + 2 个 before_json
      5. UPSERT case_image_overrides (case_id, before, manual_phase='before', updated_at=now)
      6. UPSERT case_image_overrides (case_id, after,  manual_phase='after',  updated_at=now)
      7. 通过 image_override_writer 入口（自动触发 mark_dirty）— 但因为这是
         best-pair 自身的 select，本次 mark_dirty 设 skip flag 避免立即作废自己刚选的
      8. 返回 selection_id
    校验失败 → 400/404/409；任意失败 → 全 rollback
    """

def trigger_best_pair_render(case_id: int, db) -> int:
    """
    取最新 selection（ORDER BY selected_at DESC, id DESC LIMIT 1）。
    无 selection → 400。
    enqueue 到 RenderQueue（render_mode='best-pair', best_pair_selection_id=sel.id）。
    返回 job_id。
    """

def mark_best_pair_dirty(case_id: int, db) -> None:
    """
    幂等：UPDATE case_best_pairs SET status='dirty', source_version=source_version+1, 
    updated_at=now WHERE case_id=?
    case 不存在该行 → noop（lazy 初始化）
    """

def revert_selection_overrides(selection_id: int, db) -> None:
    """
    撤销选定（未在 MVP 范围，但 schema 已留口）。
    读 selection 的 before_override_before_json / after_override_before_json，
    回写到 case_image_overrides；为 NULL 则 DELETE override 行。
    """

def list_best_pair(case_id: int, db) -> dict:
    """
    GET /best-pair 的实现：
      返回 {status, skipped_reason, candidates, current_selection, scanned_at, fingerprint}
    """
```

### 5.2 image_override_writer helper（核心 dirty hook）

```python
# services/image_override_writer.py
def write_image_override(
    case_id: int, filename: str, *,
    manual_phase: str | None = ...,
    manual_view: str | None = ...,
    manual_transform_json: str | None = ...,
    db,
    skip_dirty_mark: bool = False,
) -> None:
    """
    统一 case_image_overrides 写入入口。
    - UPSERT 逻辑（case_id, filename PK）
    - 全清三个字段 → DELETE 行
    - 尾部调 best_pair_service.mark_best_pair_dirty(case_id)（除非 skip_dirty_mark=True）
    """

def delete_image_override(case_id: int, filename: str, *, db, skip_dirty_mark=False) -> None:
    """删除单行；尾部 mark_dirty"""
```

**改造范围**（grep 验证后逐个改）：
- `backend/routes/cases.py:1467-1480` PATCH 主入口（阶段 22 实施）
- `backend/routes/cases.py:1476-1479` trash 删除入口
- `backend/routes/image_workbench.py:2428-2435 / 2502-2509 / 2601-2608` 三处写
- 任何后续新增的 case_image_overrides 写入必须走该 helper

## 6. REST 端点（6 个）

| Method | Path | Body | 用途 |
|---|---|---|---|
| `GET` | `/api/cases/{id}/best-pair` | — | top-5 + status + current_selection + fingerprint |
| `POST` | `/api/cases/{id}/best-pair/recompute` | — | 单 case 同步重算（~1s） |
| `POST` | `/api/cases/best-pair/batch-recompute` | `{case_ids?: int[], only_dirty?: bool}` | 入队 compute_queue，返回 batch_id |
| `GET` | `/api/cases/best-pair/batch-status/{batch_id}` | — | 批量进度（前端轮询，不依赖 SSE） |
| `POST` | `/api/cases/{id}/best-pair/select` | `{before, after, fingerprint}` | 选定（联动写 override，校验 fingerprint） |
| `POST` | `/api/cases/{id}/best-pair/render` | — | 触发 RenderQueue（render_mode='best-pair'） |

**修订说明（gemini W4）**：批量进度用 GET 轮询，不依赖 SSE 连接，用户离开页再回来仍能看到。

**修订说明（codex Note + Info）**：select 和 render 不合并；保留独立 render 便于"只标注不出图"。

## 7. render_executor.py 改动

```python
# backend/render_executor.py 现有 run_render(...) 入口
# codex 引用 :1593-1600 - 真实签名是
# def run_render(*, case_id, db_path, manual_overrides=None, selection_plan=None, ...)
# 当前 RenderQueue._execute_render 调用见 backend/render_queue.py:1562-1569
def run_render(
    *, case_id, db_path,
    render_mode: str = 'ai',
    best_pair_selection_id: int | None = None,
    manual_overrides=None, selection_plan=None,
    ...
):
    if render_mode == 'best-pair':
        sel = load_selection(best_pair_selection_id, db_path)
        # 把 selection 的 (before, after) 转成现有的 manual_overrides + selection_plan 形式
        # 不需要 strategy pattern，1 个分支即可（codex W5）
        manual_overrides = _build_overrides_from_selection(sel)
        selection_plan = _build_selection_plan_from_selection(sel)
        # 关键：跳过 enhance.js 的 AI 调用
        skip_enhance = True
    else:
        skip_enhance = False
    ...
    # 现有逻辑：_run_enhance_if_not_skipped + case_layout_board.run(...)
```

`render_queue._execute_render` 入队时把 `render_mode` + `best_pair_selection_id` 一起读出来传给 `run_render`。

## 8. 前端

### 8.1 BestPairPanel 组件（CaseDetail 新增 tab 或 drawer）

**修订说明（gemini C1, W1, I3）**：交互拆为"切换候选" + "确认选定"两步。

```
┌─ Best-pair 选图 ──────────────────────────────────────────┐
│                                                           │
│   状态：✓ ready    数据指纹：a3f2b1...    已选：A↔B (1m前) │
│                                                           │
│   ┌──────────┬──────────┬──────────┬──────────┬──────────┐│
│   │ #1 2.1°  │ #2 2.3°  │ #3 3.0°  │ #4 4.1°  │ #5 4.8°  ││
│   │ before   │ before   │ before   │ before   │ before   ││
│   │ + after  │ + after  │ + after  │ + after  │ + after  ││
│   │ ▔▔▔▔▔▔ │   ▔▔▔▔   │   ▔▔▔▔   │   ▔▔▔▔   │   ▔▔▔▔   ││
│   │ (高亮)   │          │          │          │          ││
│   └──────────┴──────────┴──────────┴──────────┴──────────┘│
│                                                           │
│   即将选用：07041738_04.jpg ↔ 07041738_29.jpg              │
│                                                           │
│   [ 重算  ]   [ 确认选用此对  ]   [ 生成对比图  ]           │
│                                                           │
└───────────────────────────────────────────────────────────┘
```

**关键交互**：
- 点击候选缩略图 → 仅本地 state 切换高亮，**不**调 API
- "确认选用此对" → POST select（联动写 override），按钮显示 loading
- "生成对比图" → POST render，按钮 disabled 直到当前 selection 存在
- 候选每条显示 before + after 双缩略图 + delta_deg（gemini I1）

### 8.2 状态机渲染

| status | UI |
|---|---|
| `pending` | empty state：📊 "尚未计算最优候选对" + [ 计算 ] 按钮（gemini I4） |
| `ready` | 主视图（top-5 + 选定按钮 + render 按钮） |
| `dirty` | 主视图，但"确认选定"按钮禁用 + 红色 banner "数据已过期，请先重算"（gemini C2） |
| `skipped (no_phase_labels)` | 提示 + [ 去标注 ] 按钮跳 ImageOverridePopover |
| `skipped (dir_missing)` | 告知性文案 "案例图片目录不存在 ({path})"，无按钮（gemini W3） |
| `skipped (no_face_detected)` | "未检测到人脸 — 该案例可能不适合 best-pair，建议走 AI 路径" |

### 8.3 RenderHistoryDrawer 改动（gemini W2）

每条历史显示：
- 现有 brand badge 保留
- 新增 `mode badge`：`AI` / `best-pair`
- best-pair 条目额外显示 `before: x.jpg / after: y.jpg`（来自 best_pair_selection_id JOIN）

### 8.4 cases 列表（不在 MVP）

不加 `has_best_pair` 列。理由：MVP 范围控制；用户可在 case 详情页内查看。

### 8.5 brand 体系兼容（gemini W5）

`case_layout_board.py` 接受 brand 参数（沿现有 render 路径），best-pair render 也透传。BestPairPanel 不放 brand 选择器，沿用 CaseDetail 的全局 brand state（与 RenderHistoryDrawer 一致）。

### 8.6 i18n 新增 namespace `bestPair`

```json
{
  "title": "Best-pair 选图",
  "status": { "ready": "就绪", "skipped": "跳过", "pending": "待计算", "dirty": "数据已过期" },
  "skippedReason": {
    "no_phase_labels": "缺少 phase 标注，请先标注术前/术后",
    "dir_missing": "案例图片目录不存在",
    "no_face_detected": "未检测到人脸，建议走 AI 路径"
  },
  "buttons": {
    "compute": "计算",
    "recompute": "重算",
    "select": "确认选用此对",
    "render": "生成对比图",
    "goAnnotate": "去标注"
  },
  "labels": {
    "currentSelection": "已选",
    "fingerprint": "数据指纹",
    "delta": "角度差",
    "before": "术前",
    "after": "术后",
    "willSelect": "即将选用"
  },
  "banners": {
    "dataStale": "数据已过期，请先重算"
  }
}
```

## 9. 错误处理矩阵

| 场景 | HTTP / 处理 |
|---|---|
| compute 时 case_dir 不存在 | 200，cache.status='skipped', skipped_reason='dir_missing' |
| compute 时所有图未检测到人脸 | 200，cache.status='skipped', skipped_reason='no_face_detected' |
| compute 时缺 phase 且关键字 fallback 不命中 | 200，cache.status='skipped', skipped_reason='no_phase_labels' |
| compute 写回时 source_version 不匹配 | 200，cache 保持 dirty（不报错，下次再算） |
| select 时 before/after 不在 case_dir / 文件不存在 | 400 + `_resolve_existing_source` 报错 |
| select 时 fingerprint 不匹配（cache 已变更） | 409 'stale_fingerprint' |
| select 时 cache.status != 'ready' | 409 'cache_not_ready' |
| render 时 case 无 selection | 400 'no_current_selection' |
| MediaPipe 加载失败 | 500，前端 toast '面部检测引擎不可用' |
| compute_queue 满（job_pool 队列满） | 429 + Retry-After |

## 10. 测试策略

### 10.1 pytest

- **migration**：`test_best_pair_migration.py` 覆盖 `_ensure_render_job_columns` 幂等
- **service**：`test_best_pair_service.py` 覆盖
  - compute 的 source_version race（mock mark_dirty 在中间触发）
  - select 的 fingerprint 校验、_resolve_existing_source、override 快照写入
  - mark_dirty 的所有 image_override_writer 入口（patch case / patch image / trash / image_workbench × 3）
  - revert_selection_overrides（即使不在 MVP 实施，schema 已支持，写测试预留）
- **routes**：`test_best_pair_routes.py` 6 个端点的 happy + 错误码

### 10.2 Playwright E2E

- `e2e/best_pair_basic.spec.ts`：
  - 进 case 详情 → 看到 BestPairPanel ready
  - 切换 top-5 高亮（不调 API，断言无 POST）
  - 点"确认选用此对" → POST select，断言 case_image_overrides 写入
  - 点"生成对比图" → POST render，等 RenderHistoryDrawer 出现新条目（mode='best-pair' badge）
- `e2e/best_pair_dirty.spec.ts`：
  - mock case 进入 dirty status → 断言"确认选定"按钮 disabled + banner 出现
  - 点"重算" → 断言按钮恢复
- `e2e/best_pair_skipped.spec.ts`：3 种 reason 的引导按钮分别可见

### 10.3 真实 MediaPipe 测试（gemini N2）

- 默认 mock yaw/pitch/roll
- `REAL_MEDIAPIPE=1` 环境变量启用真实 MediaPipe（与现有 `SMOKE_REAL_CHATGPT=1` 风格一致）
- CI 不跑真实，本地手动验证

## 11. 切片划分（MVP，6 + 1 重组）

**修订说明**：原 6 切片调整后变 7 切片（image_override_writer 改造单独切出，避免 T1 切片过厚）。

| Slice | 文件范围 | 估时 |
|---|---|---|
| **T1** DB schema + migration | `backend/db.py` 加 `_ensure_render_job_columns` + 2 个 CREATE TABLE | 2h |
| **T2** image_override_writer 抽离（dirty hook 公共依赖） | 新建 `backend/services/image_override_writer.py`；改 `cases.py` 4 处入口 + `image_workbench.py` 3 处 | 4h |
| **T3** best_pair_service + compute_queue | `backend/services/best_pair_service.py` + `backend/workers/best_pair_compute_queue.py` + pytest | 6h |
| **T4** REST 端点 | `backend/routes/best_pair.py` 6 端点 + supertest | 4h |
| **T5** render_executor 分支 | `backend/render_executor.py` + `backend/render_queue.py` 加 render_mode 透传 + 2 列写入 | 3h |
| **T6** 前端 BestPairPanel + RenderHistoryDrawer mode badge + i18n + 3 状态机文案 | `frontend/src/components/BestPairPanel/*` + `frontend/src/components/RenderHistoryDrawer.tsx` + `frontend/src/locales/{zh,en}/bestPair.json` | 8h |
| **T7** Playwright E2E + pytest 端到端 | `e2e/best_pair_*.spec.ts` × 3 + `backend/tests/test_best_pair_*.py` 已含的端到端 | 4h |

**Wave 2 总估**：约 31h（3-4 工作日）

## 12. 不在 MVP 的范围（明确收敛）

- cases 列表 `has_best_pair` 列与筛选
- 自动 cron 重扫
- top-5 上限可配
- 加权评分（delta_deg + 图片质量）
- `revert_selection_overrides` 真正实施（schema 已留口，actual revert flow 留给 Wave 3）
- batch 期间 SSE 实时推送（用 GET 轮询替代）

## 13. 已知风险与缓解

| 风险 | 缓解 |
|---|---|
| MediaPipe 在生产环境（非 dev workstation）部署 | 项目本地工具链（不部署），CI 不跑真实；继续用本地 brew 安装 |
| compute 长时间占用 _job_pool 影响 render | compute 提交到共享 _job_pool 上限 2，与 render 抢；如出现阻塞用 Wave 3 加专用 pool |
| 用户切换候选高亮 N 次的 UI state 复杂度 | React 局部 state，无远程同步，简单 |
| 撤销 selection 未在 MVP 实施但 schema 已留口 | Wave 3 实施时直接消费已写的 before_override_before_json |

## 14. 决策证据链

- Wave 1 视觉证据：`/tmp/case88_no_planner/compare.jpg`
- Wave 1 数据：`/tmp/case_best_pair.json` + `/tmp/case_angle_distribution.png`
- Wave 1 状态：`.ccg/state.md`
- Codex review：内联吸收，本 spec 标注 `codex C/W/Note` 引用
- Gemini review：内联吸收，本 spec 标注 `gemini C/W/Info/Note` 引用

## 15. 下一步

1. 用户审本 spec
2. 用户 OK 后调 `superpowers:writing-plans` 写实施 plan（每 slice 拆到 task 级 + 验收标准）
3. Plan OK 后 `/ccg:team-exec` 启动 Wave 2 并行实施
