# Best-pair 自动选图路径设计

**Project**: case-workbench
**Wave**: 2
**Plan ID**: best-pair-routing
**Workdir**: `/Users/a1234/Desktop/案例生成器/case-workbench`
**Created**: 2026-05-09
**Reviewers**: codex (后端 / 数据 / 架构)，gemini (UX / 前端 / 用户路径)
**Status**: brainstorming v2 — 已吸收 5 项 Critical + 12 项 Warning

> **v3** — 2026-05-09 Round 2 review absorption: A-C1/A-C2/A-I1/A-I2/A-I4, B-C1/B-C2, C-C1/C-C2/C-C3/C-I3 critical fixes applied. Important items moved to writing-plans acceptance criteria.

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

**修订说明（codex C2）**：抽 `image_override_writer` helper，统一 case_image_overrides 写入。所有现有写入入口（v3 修正：5 处唯一 Python 函数 — `backend/routes/cases.py` PATCH helper + trash DELETE、`backend/routes/image_workbench.py` upload / override / delete handler）改成走该 helper。helper 内部按 R1 dirty 触发白名单规则决定是否调 `mark_best_pair_dirty(case_id, filename)`。

**修订说明（codex C3）**：不复用 `RenderQueue.enqueue_batch`（它会插入 render_jobs 走正式渲染流程），新建 `best_pair_compute_queue` 模块。compute 提交到全局共享 `_job_pool`（backend/_job_pool.py:17-19，MAX_WORKERS=2），best-pair compute 内部串行（MediaPipe singleton 不能多线程）。

### 5.1 服务函数

```python
# best_pair_service.py
def compute_best_pair(case_id: int, db) -> dict:
    """
    单 case 同步算（~1s）：
      1. 读 case_best_pairs 当前 source_version 作为 observed_version（行不存在则 observed_version=0）
      2. 分类术前/术后图（case_image_overrides 优先 → 关键字 fallback）
      3. MediaPipe FaceLandmarker 提 yaw/pitch/roll
      4. 全配对算 delta_deg → top-5 升序
      5. **UPSERT 写回 + observed_version 护卫**（v3 修正，解 Round 2 A-C1）：

         ```sql
         INSERT INTO case_best_pairs (
           case_id, status, source_version,
           candidates_json, candidates_fingerprint,
           scanned_at, updated_at
         ) VALUES (?, 'ready', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
         ON CONFLICT(case_id) DO UPDATE SET
           status = 'ready',
           source_version = excluded.source_version,
           candidates_json = excluded.candidates_json,
           candidates_fingerprint = excluded.candidates_fingerprint,
           scanned_at = excluded.scanned_at,
           updated_at = excluded.updated_at
         WHERE case_best_pairs.source_version = ?;  -- observed_version guard
         ```

         语义：
         - 首次 compute：INSERT 分支，seed `status='ready' / source_version=max(1, observed_version+1)`
         - 后续 compute：UPDATE 分支，仅当 `source_version == observed_version` 才写回
         - 并发冲突：ON CONFLICT DO UPDATE 但 WHERE 不命中 → 0 rows affected → compute 返回 `{recomputed: false, reason: 'superseded'}`，由调用方（endpoint / UI）决定重试或提示
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
      5. 通过 image_override_writer.write_image_override(skip_dirty_mark=True) 写两行：
         - (case_id, before, manual_phase='before')
         - (case_id, after,  manual_phase='after')
         `skip_dirty_mark=True` 保证 R1 白名单不会把自己刚选的 selection 标 dirty
      6. **事务内 bump source_version + 重确认 ready**（v3 新增，解 Round 2 A-C2）：

         ```sql
         UPDATE case_best_pairs
         SET status = 'ready',
             source_version = source_version + 1,
             candidates_fingerprint = ?,  -- 保持与 compute 写入的 fingerprint 一致
             updated_at = CURRENT_TIMESTAMP
         WHERE case_id = ?;
         ```

         作用：
         1. `source_version` 推进 → 并发中持旧 observed_version 的 compute 写回时
            `WHERE source_version=observed_version` 不命中 → 静默丢弃（§9 superseded 路径）
         2. `status='ready'` 重确认 → 防御 R1 白名单误判（哪怕 R1 mark 了 dirty，select 成功后覆盖回 ready）
         3. `candidates_fingerprint` 不变 → selection 引用的候选集仍有效
      7. 返回 selection_id
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
    """删除单行；尾部 mark_dirty（同样受白名单约束）"""
```

**dirty 触发白名单规则**（v3 新增，解 Round 2 B-C1）：

`image_override_writer` 写入 `case_image_overrides` 后，按以下规则决定是否调 `mark_best_pair_dirty`：

| 条件 | 是否 mark_dirty | 说明 |
|------|----------------|------|
| `filename` ∈ 当前 `case_best_pairs.candidates_json` 的 before/after 文件名并集 | ✅ 是 | 改到了候选图本身 |
| 字段改动涉及 `manual_phase` 或 `manual_view`（任意 filename） | ✅ 是 | 影响候选池分组 |
| 仅写 `manual_transform_json` / 裁剪 / 其他元数据 且 filename ∉ 候选并集 | ❌ 否 | 与 top-5 无关 |
| `case_best_pairs` 行不存在（首次） | ❌ 否（保持 lazy） | 由 compute 首次运行时 INSERT seed |
| 调用方显式传 `skip_dirty_mark=True`（仅 `select_best_pair` 内部使用） | ❌ 否 | 见 R2 |

`image_override_writer` 同一事务内 `SELECT candidates_json FROM case_best_pairs WHERE case_id=?`（行存在才判定）；若需 mark_dirty 则
```sql
UPDATE case_best_pairs
SET status='dirty', source_version=source_version+1, updated_at=CURRENT_TIMESTAMP
WHERE case_id=?;
```
未命中保持原状态（lazy）。

**避免 banner 长亮**：transform-only 编辑、无关图 phase/view 改动均不触发 dirty。解 B-C1 "任何 override 改动都 banner 长亮 + 禁用确认按钮" 的日常使用卡死问题。

**改造范围（v3 修正，解 Round 2 A-I1）**：5 处唯一调用点（以 Python 函数为单位，非 SQL 语句）：
- `backend/routes/cases.py` 2 处：
  - PATCH helper 内部 `_write_image_override`（阶段 22 实施的写入入口）
  - trash DELETE 路径 `:1478` 附近的删除入口
- `backend/routes/image_workbench.py` 3 处（upload / override / delete handler 函数各 1 处）
- 任何后续新增的 `case_image_overrides` 写入必须走该 helper

**T2 验收硬标准**（v3 新增）：

```bash
# 1) routes 层不应再有直接写 case_image_overrides 的 SQL
grep -rn "INSERT INTO case_image_overrides\|UPDATE case_image_overrides\|DELETE FROM case_image_overrides" backend/routes/
# 期望：0 行（全迁移到 image_override_writer）

# 2) 5 处唯一 Python 函数必须导入 writer
grep -rn "from backend.services.image_override_writer import" backend/routes/
# 期望：5 个唯一函数内导入
```

两条同时满足才算 T2 过。

### 5.x 模块拓扑（v3 新增，解 Round 2 A-I2 import 循环）

```
backend/services/
├── best_pair_dirty.py          # leaf: mark_best_pair_dirty(conn, case_id, filename)
├── image_override_writer.py    # 导入 best_pair_dirty（不反向导入 best_pair_service）
└── best_pair_service.py        # 导入 image_override_writer + best_pair_dirty
```

- `best_pair_dirty.py` 只含 `mark_best_pair_dirty(conn, case_id, filename)` + R1 白名单判定的纯函数实现，**不**导入任何其他 `backend.services.*` 模块 → 是依赖图的叶子
- `image_override_writer.py` 导入 `best_pair_dirty`，不再反向依赖 `best_pair_service`
- `best_pair_service.py` 内部自身的 `select_best_pair` 需要写 override 时，调 `image_override_writer.write_image_override(skip_dirty_mark=True)`；需要自己 bump 时直接写 SQL，不经过 `mark_best_pair_dirty`

这个拓扑避免 "writer ↔ service" 的循环导入，也让 pytest 可以只 import `best_pair_dirty` 测白名单判定无其他副作用。

### 5.y Compute pool 独立（v3 新增，解 Round 2 A-I4 MediaPipe 线程规约）

`best_pair_compute_queue` **不共享** `backend/_job_pool._job_pool`（后者 `max_workers=2`，是 render 用的）。

新增 `backend/_best_pair_compute_pool.py`：

```python
from concurrent.futures import ThreadPoolExecutor

# MediaPipe 模型不保证线程安全 → 串行
_best_pair_compute_pool = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix='best-pair-compute',
)
```

语义：
- 单 case `POST /recompute` 和 `POST /batch-recompute` 都走这个 pool
- compute 串行执行，避免 MediaPipe 竞态
- 不与 render 共享 pool → render 吞吐不受 compute 阻塞
- 单 worker + 无界 queue，回压由 endpoint 队长检测拒绝：§9 新增 429 `compute_pool_saturated`（threshold=100 待计算任务）

### 4.x schema 补列（v3，随 T1 migration 一起落地）

**`render_jobs` 表补列**：

```python
# backend/db.py _ensure_render_job_columns helper 内补：
if "render_mode" not in cols:
    conn.execute(
        "ALTER TABLE render_jobs ADD COLUMN render_mode TEXT NOT NULL DEFAULT 'ai'"
    )
    # enum: 'ai' | 'best_pair' | 'ai_fallback_from_best_pair'
if "best_pair_selection_id" not in cols:
    conn.execute(
        "ALTER TABLE render_jobs ADD COLUMN "
        "best_pair_selection_id INTEGER REFERENCES case_best_pair_selections(id)"
    )
if "candidates_fingerprint_snapshot" not in cols:
    conn.execute(
        "ALTER TABLE render_jobs ADD COLUMN candidates_fingerprint_snapshot TEXT NULL"
    )
```

**`case_best_pairs.status` 枚举新增 `'empty'`**（见 §7.4）：

```
'ready' | 'skipped' | 'pending' | 'dirty' | 'empty'
```

`empty` 与 `skipped` 的区别：`skipped` 是 compute 之前确定无需算（缺 phase / 目录不存在），`empty` 是 compute 实际跑完但候选池为空（例如所有图都检测不到人脸但又不是 100% 没脸的 skipped case）。MVP 二者 UI 文案可复用，但数据上保持分开便于后续分析。

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

## 7. render_executor.py 分支（v3 完整展开，解 Round 2 C-C1/C-C2/C-C3/C-I3）

### 7.1 函数签名（纯函数，test seam）

```python
# backend/render_executor.py

def _build_overrides_from_selection(selection_row: dict) -> dict[str, dict]:
    """Selection → {filename: {manual_phase, manual_view}}. 纯函数，不读 DB。

    形状：
      {
        "<before_filename>": {"manual_phase": "before", "manual_view": None},
        "<after_filename>":  {"manual_phase": "after",  "manual_view": None},
      }
    view 不由 selection 强制，None 表示沿用 DB override（见 §7.2 precedence）。
    """

def _build_selection_plan_from_selection(selection_row: dict, case_view: str) -> dict:
    """Selection → selection_plan（形状对齐 render_queue.py:443 _selection_plan_candidate）。

    仅填 selection 指定的 slot：
      selection_plan[case_view] = {"before": <before_filename>, "after": <after_filename>}
    其他 view slot = None（由 case_layout_board.py 按现有逻辑选或留空）。
    纯函数。
    """

def load_selection(selection_id: int, conn: sqlite3.Connection) -> dict | None:
    """模块级函数（非方法），pytest 可 monkeypatch。

    返回 {
      id, case_id, before_filename, after_filename,
      delta_deg, candidates_fingerprint,
      before_override_before_json, after_override_before_json,
      selected_at, selected_by
    } 或 None（selection_id 不存在）。
    """


class StaleBestPairSelection(Exception):
    """run_render 入口检测到 best_pair selection 已过期 / 不可用。

    reason: 'selection_not_found' | 'cache_not_ready' | 'fingerprint_mismatch'
    """
    def __init__(self, reason: str, **kwargs):
        self.reason = reason
        self.context = kwargs
        super().__init__(f"stale_best_pair_selection: {reason} {kwargs}")
```

### 7.2 Precedence 表（selection-synthesized vs DB manual_overrides）

```python
def run_render(
    *, case_id, db_path,
    render_mode: str = 'ai',
    best_pair_selection_id: int | None = None,
    manual_overrides=None, selection_plan=None,
    ...
):
    conn = sqlite3.connect(db_path)
    manual_overrides_db = _fetch_case_image_overrides(conn, case_id)
    #  ^^ 既有逻辑：从 case_image_overrides 读当前 overrides

    if render_mode == 'best_pair':
        # §7.3 入口漂移检测（见下）
        selection = load_selection(best_pair_selection_id, conn)
        _assert_best_pair_inputs_fresh(conn, case_id, selection)

        selection_overrides = _build_overrides_from_selection(selection)
        selection_plan = _build_selection_plan_from_selection(
            selection, case_view=_fetch_case_view(conn, case_id)
        )

        # selection 合成 override 优先覆盖同 (filename, field) 的 DB override
        manual_overrides = {**manual_overrides_db, **selection_overrides}
        skip_enhance = True  # best-pair 路径不跑 AI
    else:
        manual_overrides = manual_overrides_db
        selection_plan = selection_plan  # 保留 caller 传入
        skip_enhance = False

    # 下游既有逻辑：_run_enhance_if_not_skipped + case_layout_board.run(...)
    ...
```

**冲突规则**：

| (filename, field) 情况 | 取值来源 |
|---|---|
| 只在 DB manual_overrides 有 | DB override |
| 只在 selection_overrides 有 | selection（合成） |
| 两者都有，同 filename 同字段 | **selection 赢** |
| 两者都有，同 filename 不同字段 | 各自字段值并集（dict merge 语义） |
| 不同 filename | 互不影响（并集） |

MVP selection 只合成 `manual_phase`，不合成 `manual_view` / `manual_transform_json`，因此实际冲突仅在 `manual_phase` 字段发生。

### 7.3 Fingerprint 漂移检测（run_render 入口）

```python
def _assert_best_pair_inputs_fresh(conn, case_id: int, selection: dict | None) -> None:
    if selection is None:
        raise StaleBestPairSelection(
            'selection_not_found',
            selection_id='<unknown>',
        )

    row = conn.execute(
        "SELECT candidates_fingerprint, status "
        "FROM case_best_pairs WHERE case_id=?",
        (case_id,),
    ).fetchone()

    if row is None or row['status'] != 'ready':
        raise StaleBestPairSelection(
            'cache_not_ready',
            case_id=case_id,
            status=(row['status'] if row else None),
        )

    if row['candidates_fingerprint'] != selection['candidates_fingerprint']:
        raise StaleBestPairSelection(
            'fingerprint_mismatch',
            case_id=case_id,
            selection_fp=selection['candidates_fingerprint'],
            current_fp=row['candidates_fingerprint'],
        )
```

`render_queue._execute_render` 捕获 `StaleBestPairSelection` → 标记 job `failed`，`render_jobs.failure_reason` 写 `stale_best_pair_selection:<reason>`，不静默渲染。前端 RenderHistoryDrawer 该条目显示失败态 + reason i18n 文案。

### 7.4 Fallback 策略

- `compute_best_pair` 返回 empty（所有图均未检测到人脸或候选池为空）→ `case_best_pairs.status='empty'`（新增枚举值，见 §4.x）
- UI `BestPairPanel` 在 `status='empty'` 时显示 empty state，禁用"生成对比图"按钮
- URL hack 强行触发 render → endpoint 返回 422 `no_candidates`
- **不自动回退 AI 模式**，显式失败，用户自己决定走 AI 路径
- `render_mode` 枚举保留 `'ai_fallback_from_best_pair'` 空位，MVP 不走该路径；Phase 3 评估是否加自动回退开关

### 7.5 可观测性（render 全链路）

**render_jobs 行必须带**（随 T1 migration 补列，见 §4.x）：
- `render_mode`
- `best_pair_selection_id`（best-pair 路径必填，ai 路径 NULL）
- `candidates_fingerprint_snapshot`（render 排队时从 case_best_pairs 快照，对比实际 render 时是否漂移）

**run_render 结果 dict 新增**：
- `render_mode`
- `best_pair_selection_id`
- `selection_fingerprint_at_render`

**manifest.final.json 顶层新增 best_pair_context**（best-pair 路径必出，ai 路径省略）：

```json
{
  "selection_id": 123,
  "before_filename": "术前1.jpg",
  "after_filename": "术后3.jpg",
  "candidates_fingerprint": "sha256:...",
  "rendered_at_source_version": 5
}
```

方便后续回溯"这张 final-board 是基于哪次 selection、哪个 fingerprint 渲染的"。

### 7.6 Test seam 清单（T5 pytest）

| 用例 | seam | 覆盖 |
|---|---|---|
| (a) `render_mode='ai'`（既有路径） | 不调 `load_selection` | 既有路径 0 回归 |
| (b) best_pair 无 DB override | monkeypatch `load_selection` 返回 fixture selection + `_fetch_case_image_overrides` 返回 `{}` | selection_overrides 单独生效 |
| (c) best_pair + DB override 无冲突（不同 filename） | 同上，DB override 另外塞 `c.jpg` | 并集正确，两者都进 manual_overrides |
| (d) best_pair + DB override 冲突（同 filename 同 `manual_phase`） | 同上，DB override 给 `before_filename` 写 `manual_phase='after'` | selection 赢，最终 `manual_phase='before'` |
| (e) fingerprint 漂移 | `load_selection` 返回旧 fp + DB 查出新 fp | raise `StaleBestPairSelection('fingerprint_mismatch')` |
| (f) cache not ready | DB 查 `status='dirty'` | raise `StaleBestPairSelection('cache_not_ready')` |
| (g) `selection_id` 不存在 | `load_selection` 返回 `None` | raise `StaleBestPairSelection('selection_not_found')` |

所有测试用既有 `backend/tests/conftest.py` DB fixture，**不起 Chrome / 不跑 subprocess** — 在 subprocess 启动前 `run_render` 入口即可 raise 或 assert 合成 overrides 正确，覆盖核心分支无需真实 enhance / case_layout_board 执行。

### 7.7 T5 估时重估

原估 **3h**（"只加一个分支"），v3 实际工作量：
- `_build_overrides_from_selection` / `_build_selection_plan_from_selection` / `load_selection` 3 个纯函数 + 单测 ~1h
- `_assert_best_pair_inputs_fresh` + `StaleBestPairSelection` 异常 + run_render 分支接线 ~1.5h
- `render_queue._execute_render` 捕获异常 + failure_reason 写入 ~0.5h
- render_jobs 补列（补在 T1）+ manifest.final.json best_pair_context ~0.5h
- 7 个 pytest seam 用例 ~2.5h

**重估 6h**。其他切片（T1/T2/T3/T4/T6/T7）估时不变。

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

**Highlight 本地 state 生命周期规则**（v3 新增，解 Round 2 B-C2）：

1. `highlightIndex` 仅存在于 `BestPairPanel` mount 周期（React `useState`，**不**进 URL / localStorage / zustand global store）
2. 以下任一事件 → 重置为 0（top-1）：
   - 组件 unmount（drawer 关闭、切 case 详情页内其他 tab）
   - `caseId` prop 改变（父组件切 case）
   - recompute 完成（`status` `dirty` → `ready`，`candidates_fingerprint` 发生变化）
   - `candidates_json` 长度变化（列表条数发生变化）
3. 页面 reload / 浏览器导航 / 关浏览器 → 静默丢失，**不**弹"未保存"警告（MVP 简化；Phase 3 再评估是否加草稿保护）
4. **双 tab 竞争**：tab A 本地高亮 index=2 后 tab B 先点"确认选用此对" → tab A 下次 `GET /best-pair` 拿到新 fingerprint → 规则 2 触发重置。tab A 若此时点"确认选用此对" → 后端 409 `stale_fingerprint` → 前端 refetch + 重置 highlight + toast `bestPair.errors.staleFingerprint`

**键盘支持**：
- Tab 焦点进入候选缩略图列表（roving tabindex 或 arrow-key navigation）
- 方向键 `←` / `→` 切 `highlightIndex`（**不**确认）
- `Enter` = 把 `highlightIndex` 切到当前焦点项（等价鼠标点击）
- `Ctrl+Enter` 或点击"确认选用此对"按钮 = 写 DB（POST select）

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
| **T5** render_executor 分支 | `backend/render_executor.py` + `backend/render_queue.py` 加 render_mode 透传 + 2 列写入 | 6h (v3 重估，见 §7.7) |
| **T6** 前端 BestPairPanel + RenderHistoryDrawer mode badge + i18n + 3 状态机文案 | `frontend/src/components/BestPairPanel/*` + `frontend/src/components/RenderHistoryDrawer.tsx` + `frontend/src/locales/{zh,en}/bestPair.json` | 8h |
| **T7** Playwright E2E + pytest 端到端 | `e2e/best_pair_*.spec.ts` × 3 + `backend/tests/test_best_pair_*.py` 已含的端到端 | 4h |

**Wave 2 总估**：约 34h（v3 重估，T5 3h→6h）

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
