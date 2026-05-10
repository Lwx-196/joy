# Best-pair 自动选图路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 case-workbench 加 best-pair 自动选图独立路径：扫 case 源图 → MediaPipe 测 yaw/pitch/roll → 选 top-5 最接近的术前/术后对 → 用户确认后联动写 `case_image_overrides` → 走现有 render 拼板（与 AI 路径并存）。

**Architecture:** 新增 2 张表（`case_best_pairs` 缓存 top-5 + `case_best_pair_selections` 用户选定历史），新增 `services/` 和 `workers/` 包，抽出 `image_override_writer` 统一入口给所有 case_image_overrides 写入口带 dirty hook，新增 6 个 REST 端点走独立 compute_queue（共享 `_job_pool`，内部串行 MediaPipe），render_executor / render_queue 加 `render_mode` 透传但不分策略分支（best-pair render 只是 manual_overrides 锁定对 + 跳过 AI enhance — 本 codebase 里 AI enhance 在 `ai_generation_adapter.simulate-after` 是独立路径，`run_render` 本身不调 AI，所以 best-pair render 无需改 executor 逻辑，只加两列透传）。

**Tech Stack:** Python 3.11 + FastAPI + SQLite (better-sqlite 风格 direct sqlite3) + pytest + React 19 + Vite + react-i18next + Playwright + MediaPipe FaceLandmarker（skill 工具链已本地装）

**Spec ref:** `docs/superpowers/specs/2026-05-09-best-pair-routing-design.md`

**Workdir:** `/Users/a1234/Desktop/案例生成器/case-workbench`（main 分支）

---

## 文件结构

### 新建（5 后端 + 5 前端 + 3 pytest + 3 e2e + 2 locale = 18 文件）

```
backend/
├── services/
│   ├── __init__.py                         # 新包
│   ├── image_override_writer.py            # T2 核心
│   └── best_pair_service.py                # T3 核心
├── workers/
│   ├── __init__.py                         # 新包
│   └── best_pair_compute_queue.py          # T3 worker
└── routes/
    └── best_pair.py                        # T4 6 端点

backend/tests/
├── test_image_override_writer.py           # T2 pytest
├── test_best_pair_service.py               # T3 pytest
└── test_best_pair_routes.py                # T4 pytest

frontend/src/
├── components/BestPairPanel/
│   ├── BestPairPanel.tsx                   # T6 主组件
│   ├── BestPairCandidateCard.tsx           # T6 子组件
│   └── useBestPair.ts                      # T6 query hook
└── locales/zh/bestPair.json                # T6
    locales/en/bestPair.json                # T6

frontend/tests/e2e/
├── best-pair-basic.spec.ts                 # T7
├── best-pair-dirty.spec.ts                 # T7
└── best-pair-skipped.spec.ts               # T7
```

### 修改（8 文件）

```
backend/
├── db.py                           # T1: 加 2 CREATE TABLE + _ensure_render_job_columns
├── routes/cases.py                 # T2: 4 处 override 写调用改走 writer; T4 注册 best_pair router
├── routes/image_workbench.py       # T2: 1 处 _write_image_override + 1 处 trash DELETE 改走 writer
├── render_queue.py                 # T5: _execute_render 读 render_mode + best_pair_selection_id 透传；enqueue 加可选参
└── main.py                         # T4: include best_pair router

frontend/src/
├── api.ts                          # T4/T6: 6 个 best-pair API fn
├── hooks/queries.ts                # T6: useBestPair, useSelectBestPair, etc.
├── pages/CaseDetail.tsx            # T6: 挂 BestPairPanel
├── components/RenderHistoryDrawer.tsx  # T6: mode badge + best-pair 文件名对
└── i18n/{index.ts,types.ts}        # T6: 注册 bestPair namespace
```

---

## 预读（engineer 必看）

**关键 codebase 事实**（grep 验证过）：

1. `backend/db.py:351 init_schema()` 模式：`executescript(SCHEMA)` + 追加 `_ensure_*_columns` helpers。已有 4 个 helper 在 :322-348。
2. `backend/_job_pool.py` 是 `ThreadPoolExecutor(max_workers=2)`，`render_queue` 和 `upgrade_queue` 共享。best-pair compute 复用同一池子。
3. `backend/routes/cases.py:1222 _write_image_override` 是本文件私有 helper；签名 `(conn, case_id, filename, manual_phase, manual_view, updated_at, manual_transform=_UNSET)`。两个 call site: `:4883` PATCH 主入口，`:5013/:5022` 另一写入点（看起来是 bind/override 场景）。
4. `backend/routes/cases.py:1478` trash 场景的 `DELETE FROM case_image_overrides` 是独立 SQL（不走 helper）。
5. `backend/routes/image_workbench.py:2303 _write_image_override` 是**另一个**私有 helper（签名略不同：位置参数 + 无 `manual_transform`）。call sites: `:2428/:2502/:2601`。还有 `:2314` 内联 DELETE（trash）+ `:2319` INSERT — 这些在 helper 内部。
6. `backend/render_executor.py:1593 run_render(case_dir, brand, template, ..., manual_overrides, selection_plan)` 不含 AI enhance 调用。AI enhance 走独立 `ai_generation_adapter` + `/simulate-after`。所以 T5 **不需要** 在 run_render 加 `skip_enhance` 逻辑，spec 第 7 节描述的「跳过 enhance.js」在本 codebase 是 noop — best-pair render 仅修改 `manual_overrides` 以锁定选定的 before/after 文件名。
7. `backend/render_queue.py:1358` `_fetch_case_image_overrides(conn, case_id)` 已存在并返回 `{filename: {phase, view, transform?}}`，`_execute_render` 传给 `run_render`。best-pair selection 联动写到 `case_image_overrides` 后，这个现有流自然会读到正确 override。
8. `backend/routes/cases.py:1276 _resolve_existing_source(case_dir, filename)` 抛 `HTTPException(400/404)` 做路径安全校验（trash dir 拒绝 + 相对路径解析 + 存在性检查）。Service 层不该 import 路由层；复制函数到 `services/case_files.py`（T2 顺手抽出）。
9. i18n 模式：`locales/{zh,en}/<ns>.json` + 在 `i18n/index.ts` 和 `i18n/types.ts` 双注册。
10. E2E 在 `frontend/tests/e2e/*.spec.ts`，`testDir: "./tests/e2e"`，backend 同 workers 跑在 `start.sh` 启动。conftest.py 顶层 monkeypatch `DB_PATH` 到 placeholder 再 per-test 覆盖（spec 注释写得很明白）。

---

## 全局约束（所有 task 共用）

- **TDD 严格**：每个 task 先写失败测试 → 跑失败 → 最小实现 → 跑通过 → 提交。
- **Commit 粒度**：每个 task 至少 1 commit；tasks 内可多 commit 但最后一个 step 必须 push 前可停住。
- **commit message**：`feat(best-pair): ...` / `refactor(best-pair): ...` / `test(best-pair): ...`。
- **不引 mock 数据**：MediaPipe 测试默认 mock yaw/pitch/roll 函数；用户约束"不能 mock 数据"指业务数据；测试里 mock 第三方库函数是白名单例外（codex-dispatch + stub adapter 模式已在项目沿用）。
- **skip_dirty_mark 约定**：`write_image_override` / `delete_image_override` 参数 `skip_dirty_mark=False` 默认 — 只有 best-pair 自身 `select_best_pair` 在写联动 override 时传 `True`，避免立刻作废自己刚选的缓存。
- **任何 DB schema 变更后**：跑 `pytest backend/tests -x` 全绿才算通过。
- **每次 commit 前**：`python -m pyflakes backend/` + `npm --prefix frontend run lint` + `npm --prefix frontend run type-check` 三件套。

---

## Task 1: DB schema + migration helper

**Files:**
- Modify: `backend/db.py`（末尾追加 SCHEMA 段 + 2 个 helper；init_schema 调用新 helper）
- Test: `backend/tests/test_best_pair_migration.py`（新建）

估时：2h。依赖：无（首 task）。

- [ ] **Step 1: Write failing test — schema migration 幂等**

Create `backend/tests/test_best_pair_migration.py`:

```python
"""Best-pair 表 + render_jobs 扩列的 schema migration 幂等测试。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_case_best_pairs_table_created(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        cols = _columns(conn, "case_best_pairs")
    assert "case_id" in cols
    assert "status" in cols
    assert "skipped_reason" in cols
    assert "candidates_json" in cols
    assert "candidates_fingerprint" in cols
    assert "source_version" in cols
    assert "scanned_at" in cols
    assert "updated_at" in cols


def test_case_best_pair_selections_table_created(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        cols = _columns(conn, "case_best_pair_selections")
    assert "id" in cols
    assert "case_id" in cols
    assert "before_filename" in cols
    assert "after_filename" in cols
    assert "delta_deg" in cols
    assert "candidates_fingerprint" in cols
    assert "before_override_before_json" in cols
    assert "after_override_before_json" in cols
    assert "selected_at" in cols
    assert "selected_by" in cols
    # is_current 明确删除（codex W1）
    assert "is_current" not in cols


def test_render_jobs_has_render_mode_and_selection_id(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        cols = _columns(conn, "render_jobs")
    assert "render_mode" in cols
    assert "best_pair_selection_id" in cols


def test_render_mode_default_is_ai(temp_db: Path) -> None:
    # 现有 render_queue.enqueue 不传 render_mode 时默认 'ai'
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT INTO cases (abs_path, scan_id, category, status, first_seen_at, updated_at) "
            "VALUES ('/tmp/c', 1, 'A', 'active', '2026-05-09', '2026-05-09')"
        )
        conn.execute(
            "INSERT INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
            "VALUES (1, '2026-05-09', '2026-05-09', '/tmp', 1, 'test')"
        )
        conn.execute(
            "INSERT INTO render_jobs (case_id, brand, template, status, enqueued_at, semantic_judge) "
            "VALUES (1, 'fumei', 'tri-compare', 'queued', '2026-05-09', 'auto')"
        )
        row = conn.execute("SELECT render_mode, best_pair_selection_id FROM render_jobs").fetchone()
    assert row[0] == "ai"
    assert row[1] is None


def test_ensure_render_job_columns_idempotent(temp_db: Path) -> None:
    """重复跑 _ensure_render_job_columns 不报错（codex W3）。"""
    from backend import db as _db
    with _db.connect() as conn:
        _db._ensure_render_job_columns(conn)
        _db._ensure_render_job_columns(conn)
        _db._ensure_render_job_columns(conn)
    with sqlite3.connect(temp_db) as conn:
        cols = _columns(conn, "render_jobs")
    assert "render_mode" in cols
    assert "best_pair_selection_id" in cols


def test_case_best_pairs_status_index(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        idx = {row[1] for row in conn.execute("PRAGMA index_list('case_best_pairs')")}
    assert "idx_case_best_pairs_status" in idx


def test_case_best_pair_selections_case_at_index(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        idx = {row[1] for row in conn.execute("PRAGMA index_list('case_best_pair_selections')")}
    assert "idx_cbps_case_at" in idx
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && pytest backend/tests/test_best_pair_migration.py -v`

Expected: ALL FAIL — `case_best_pairs` 表不存在 / `_ensure_render_job_columns` AttributeError。

- [ ] **Step 3: Add SCHEMA + helper + init_schema hook in backend/db.py**

在 `backend/db.py` 的 `SCHEMA = """..."""` 字符串末尾（在 `"""` 闭合前）追加：

```sql

-- Wave 2: best-pair 自动选图缓存
-- 每个 case 一行，lazy 初始化（首次 compute 时 INSERT）。
-- source_version：写 case_image_overrides 时 ++；compute 写回时
--   WHERE source_version=observed_version 才成立，避免 stale ready 覆盖 dirty。
-- candidates_fingerprint：sha1(sorted filenames + sorted overrides + last mtime)；
--   UI 提交 select 时必须带，校验不匹配 → 409 stale_fingerprint。
CREATE TABLE IF NOT EXISTS case_best_pairs (
  case_id         INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
  status          TEXT NOT NULL,
  skipped_reason  TEXT,
  candidates_json TEXT NOT NULL DEFAULT '[]',
  candidates_fingerprint TEXT,
  source_version  INTEGER NOT NULL DEFAULT 0,
  scanned_at      TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_best_pairs_status ON case_best_pairs(status);

-- Wave 2: best-pair 用户选定历史（append-only）
-- 当前选定 = ORDER BY selected_at DESC, id DESC LIMIT 1（不用 is_current 列）。
-- before/after_override_before_json：选定前 case_image_overrides 原行 JSON 快照；
--   NULL 表示原本没有 override 行（为 Wave 3 revert flow 留口）。
CREATE TABLE IF NOT EXISTS case_best_pair_selections (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id                     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  before_filename             TEXT NOT NULL,
  after_filename              TEXT NOT NULL,
  delta_deg                   REAL NOT NULL,
  candidates_fingerprint      TEXT,
  before_override_before_json TEXT,
  after_override_before_json  TEXT,
  selected_at                 TIMESTAMP NOT NULL,
  selected_by                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_cbps_case_at ON case_best_pair_selections(case_id, selected_at DESC);
```

然后在 `backend/db.py` 文件末尾（`_ensure_image_override_columns` 之后、`init_schema` 之前）追加：

```python
def _ensure_render_job_columns(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(render_jobs)").fetchall()}
    if "render_mode" not in existing:
        conn.execute(
            "ALTER TABLE render_jobs ADD COLUMN render_mode TEXT NOT NULL DEFAULT 'ai'"
        )
    if "best_pair_selection_id" not in existing:
        conn.execute(
            "ALTER TABLE render_jobs ADD COLUMN best_pair_selection_id INTEGER "
            "REFERENCES case_best_pair_selections(id)"
        )
```

最后在 `init_schema()` 函数内 `_ensure_case_trash_columns(conn)` 之后新增一行：

```python
        _ensure_render_job_columns(conn)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && pytest backend/tests/test_best_pair_migration.py -v`

Expected: 7 passed。

- [ ] **Step 5: Run full pytest for regression**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && pytest backend/tests -x`

Expected: 全绿（既有测试不受影响，新增 7 个测试通过）。

- [ ] **Step 6: Commit**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git add backend/db.py backend/tests/test_best_pair_migration.py
git commit -m "feat(best-pair): T1 schema for case_best_pairs + selections + render_jobs extras"
```

---

## Task 2: image_override_writer 统一入口 + dirty hook

**Files:**
- Create: `backend/services/__init__.py`（空包）
- Create: `backend/services/image_override_writer.py`
- Create: `backend/services/case_files.py`（把 `_resolve_existing_source` 从 `routes/cases.py` 抽出来给 service 用）
- Modify: `backend/routes/cases.py`（4 call site 改走 writer + import 调整）
- Modify: `backend/routes/image_workbench.py`（3 call site + 1 trash DELETE 改走 writer）
- Test: `backend/tests/test_image_override_writer.py`（新建）

估时：4h。依赖：T1（`case_best_pairs` 表必须存在 — 因为 `mark_best_pair_dirty` 会 UPDATE 它）。

**关键约定**：`write_image_override` / `delete_image_override` 尾部调 `mark_best_pair_dirty(case_id)`，除非 `skip_dirty_mark=True`。`mark_best_pair_dirty` 本身放 `image_override_writer.py` 内部函数（避免循环 import — best_pair_service 在 T3 才建）。

- [ ] **Step 1: Write failing test for writer module**

Create `backend/tests/test_image_override_writer.py`:

```python
"""image_override_writer 统一入口测试（T2 核心）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.services import image_override_writer


def _insert_case(conn: sqlite3.Connection, case_id: int = 1) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
        "VALUES (?, ?, ?, '/tmp', 1, 'test')",
        (1, now, now),
    )
    conn.execute(
        "INSERT INTO cases (id, abs_path, scan_id, category, status, first_seen_at, updated_at) "
        "VALUES (?, '/tmp/case-1', 1, 'A', 'active', ?, ?)",
        (case_id, now, now),
    )


def _seed_best_pair_row(conn: sqlite3.Connection, case_id: int, status: str = "ready", source_version: int = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO case_best_pairs (case_id, status, candidates_json, source_version, scanned_at, updated_at) "
        "VALUES (?, ?, '[]', ?, ?, ?)",
        (case_id, status, source_version, now, now),
    )


def test_write_creates_row_when_none(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        image_override_writer.write_image_override(
            conn,
            case_id=1,
            filename="a.jpg",
            manual_phase="before",
            manual_view=None,
            manual_transform_json=None,
            updated_at="2026-05-09T00:00:00Z",
        )
        row = conn.execute(
            "SELECT manual_phase, manual_view FROM case_image_overrides WHERE case_id=? AND filename=?",
            (1, "a.jpg"),
        ).fetchone()
    assert row["manual_phase"] == "before"
    assert row["manual_view"] is None


def test_write_upserts_existing_row(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase="before", manual_view=None, manual_transform_json=None,
            updated_at="2026-05-09T00:00:00Z",
        )
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase="after", manual_view="front", manual_transform_json=None,
            updated_at="2026-05-09T01:00:00Z",
        )
        row = conn.execute(
            "SELECT manual_phase, manual_view FROM case_image_overrides WHERE case_id=? AND filename=?",
            (1, "a.jpg"),
        ).fetchone()
    assert row["manual_phase"] == "after"
    assert row["manual_view"] == "front"


def test_write_all_null_deletes_row(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase="before", manual_view=None, manual_transform_json=None,
            updated_at="2026-05-09T00:00:00Z",
        )
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase=None, manual_view=None, manual_transform_json=None,
            updated_at="2026-05-09T01:00:00Z",
        )
        row = conn.execute(
            "SELECT * FROM case_image_overrides WHERE case_id=? AND filename=?",
            (1, "a.jpg"),
        ).fetchone()
    assert row is None


def test_write_marks_best_pair_dirty(temp_db: Path) -> None:
    """写 override 默认 bump source_version 并置 status=dirty。"""
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        _seed_best_pair_row(conn, case_id=1, status="ready", source_version=5)
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase="before", manual_view=None, manual_transform_json=None,
            updated_at="2026-05-09T00:00:00Z",
        )
        row = conn.execute(
            "SELECT status, source_version FROM case_best_pairs WHERE case_id=?", (1,)
        ).fetchone()
    assert row["status"] == "dirty"
    assert row["source_version"] == 6


def test_write_skip_dirty_flag_preserves_ready(temp_db: Path) -> None:
    """best-pair select 自己写 override 时必须 skip_dirty_mark=True。"""
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        _seed_best_pair_row(conn, case_id=1, status="ready", source_version=5)
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase="before", manual_view=None, manual_transform_json=None,
            updated_at="2026-05-09T00:00:00Z",
            skip_dirty_mark=True,
        )
        row = conn.execute(
            "SELECT status, source_version FROM case_best_pairs WHERE case_id=?", (1,)
        ).fetchone()
    assert row["status"] == "ready"
    assert row["source_version"] == 5


def test_write_no_best_pair_row_is_noop(temp_db: Path) -> None:
    """case 未 compute 过（无 case_best_pairs 行），写 override 不抛错。"""
    with sqlite3.connect(temp_db) as conn:
        _insert_case(conn)
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase="before", manual_view=None, manual_transform_json=None,
            updated_at="2026-05-09T00:00:00Z",
        )
        # 不报错即通过


def test_delete_removes_row_and_marks_dirty(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        _seed_best_pair_row(conn, case_id=1, status="ready", source_version=0)
        conn.execute(
            "INSERT INTO case_image_overrides (case_id, filename, manual_phase, updated_at) "
            "VALUES (1, 'a.jpg', 'before', '2026-05-09')"
        )
        image_override_writer.delete_image_override(conn, case_id=1, filename="a.jpg")
        row = conn.execute("SELECT * FROM case_image_overrides WHERE case_id=1").fetchone()
        bp = conn.execute("SELECT status FROM case_best_pairs WHERE case_id=1").fetchone()
    assert row is None
    assert bp["status"] == "dirty"


def test_write_with_manual_transform_json(temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _insert_case(conn)
        transform = json.dumps({"rotate_deg": 1.5})
        image_override_writer.write_image_override(
            conn, case_id=1, filename="a.jpg",
            manual_phase=None, manual_view=None,
            manual_transform_json=transform,
            updated_at="2026-05-09T00:00:00Z",
        )
        row = conn.execute(
            "SELECT manual_transform_json FROM case_image_overrides WHERE case_id=1"
        ).fetchone()
    assert row["manual_transform_json"] == transform
```

- [ ] **Step 2: Run tests to verify failure**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && pytest backend/tests/test_image_override_writer.py -v`

Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services'`。

- [ ] **Step 3: Create services package + writer module**

Create `backend/services/__init__.py`:

```python
"""Cross-route business services.

Services here are called by multiple routes or by non-route modules
(workers, render_queue). They never import anything from `backend.routes.*`
to avoid circular imports.
"""
```

Create `backend/services/image_override_writer.py`:

```python
"""Unified write/delete entrypoint for case_image_overrides.

Every place that writes to case_image_overrides MUST go through these
two functions so the best-pair dirty hook fires reliably.

The hook bumps case_best_pairs.source_version and flips status to 'dirty'
(only if a row already exists — compute lazily creates it). Callers that
must not trigger dirty (best-pair's own select flow) pass skip_dirty_mark=True.
"""
from __future__ import annotations

import sqlite3
from typing import Any


def write_image_override(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    manual_phase: str | None,
    manual_view: str | None,
    manual_transform_json: str | None,
    updated_at: str,
    skip_dirty_mark: bool = False,
) -> None:
    """UPSERT a case_image_overrides row.

    Semantics:
      - All three manual_* None → DELETE the row (clear).
      - Any non-None → INSERT OR UPDATE, preserving non-touched columns isn't
        needed since all three columns are always written together.
    """
    all_null = manual_phase is None and manual_view is None and manual_transform_json is None
    if all_null:
        conn.execute(
            "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        )
    else:
        conn.execute(
            """INSERT INTO case_image_overrides
                 (case_id, filename, manual_phase, manual_view, manual_transform_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(case_id, filename) DO UPDATE SET
                 manual_phase = excluded.manual_phase,
                 manual_view = excluded.manual_view,
                 manual_transform_json = excluded.manual_transform_json,
                 updated_at = excluded.updated_at""",
            (case_id, filename, manual_phase, manual_view, manual_transform_json, updated_at),
        )
    if not skip_dirty_mark:
        _mark_best_pair_dirty(conn, case_id, updated_at)


def delete_image_override(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    skip_dirty_mark: bool = False,
) -> None:
    """Explicitly remove a single override row (trash / image-move scenarios)."""
    from datetime import datetime, timezone

    conn.execute(
        "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
        (case_id, filename),
    )
    if not skip_dirty_mark:
        _mark_best_pair_dirty(conn, case_id, datetime.now(timezone.utc).isoformat())


def _mark_best_pair_dirty(conn: sqlite3.Connection, case_id: int, now_iso: str) -> None:
    """Idempotent: if case_best_pairs row exists, bump source_version + flip status.
    If no row, noop (lazy init — compute will create on next trigger).
    """
    conn.execute(
        """UPDATE case_best_pairs
              SET status = 'dirty',
                  source_version = source_version + 1,
                  updated_at = ?
            WHERE case_id = ?""",
        (now_iso, case_id),
    )
```

- [ ] **Step 4: Run writer tests to verify pass**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && pytest backend/tests/test_image_override_writer.py -v`

Expected: 8 passed。

- [ ] **Step 5: Extract `_resolve_existing_source` + trash helpers into `services/case_files.py`**

Create `backend/services/case_files.py`（复制，不删 routes/cases.py 里的 —— 路由层对同名函数保持就地可用，避免大范围 import 改动；服务层自己用的独立 copy）：

```python
"""Path safety helpers for case file operations (service-layer copy).

Mirrors the logic in backend.routes.cases._resolve_existing_source so service
modules (best_pair_service, etc.) don't need to import from the routes layer.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

_TRASH_DIR_NAME = ".trash"


def resolve_existing_source(case_dir: Path, filename: str | None) -> Path:
    if not filename or filename in {".", ".."}:
        raise HTTPException(400, "existing image filename is required")
    if _TRASH_DIR_NAME in Path(filename).parts:
        raise HTTPException(400, "trashed images cannot be used as active source images")
    target = (case_dir / filename).resolve()
    try:
        target.relative_to(case_dir)
    except ValueError:
        raise HTTPException(400, "invalid image path")
    if not target.is_file():
        raise HTTPException(404, f"image not found: {filename}")
    return target
```

- [ ] **Step 6: Wire routes/cases.py to writer**

在 `backend/routes/cases.py` 顶部 import 段（已有 `from .. import _upgrade_executor, ai_generation_adapter, ...`）追加：

```python
from ..services import image_override_writer as _override_writer
```

替换 `backend/routes/cases.py:4883` 调用点（`_write_image_override(conn, case_id, filename, new_phase, new_view, now, manual_transform=transform_arg)`）为：

```python
_override_writer.write_image_override(
    conn,
    case_id=case_id,
    filename=filename,
    manual_phase=new_phase,
    manual_view=new_view,
    manual_transform_json=_manual_transform_to_json(transform_arg) if transform_arg is not _UNSET else None,
    updated_at=now,
)
```

替换 `:5013` 和 `:5022` 两个 call site 同样模式（对应的 `new_phase` / `new_view` 变量按原上下文替换；`_UNSET` 分支若原调用没传 transform 则 `manual_transform_json=None`）。

替换 trash 场景 `backend/routes/cases.py:1478` 内联 DELETE：

```python
_override_writer.delete_image_override(conn, case_id=case_id, filename=str(rel))
```

**保留** `_write_image_override` 私有 helper 函数定义不删（向下兼容其它可能隐藏的 call site，engineer 跑 `grep -n "_write_image_override" backend/routes/cases.py` 确认只剩 def，无 call）。若确认 0 call，删除定义。

- [ ] **Step 7: Wire routes/image_workbench.py to writer**

在 `backend/routes/image_workbench.py` 顶部追加 import：

```python
from ..services import image_override_writer as _override_writer
```

`image_workbench.py` 有自己的私有 `_write_image_override`（签名无 transform 字段）。改造策略：**保留** 私有 helper，但把它改写成薄壳调 service helper：

替换 `backend/routes/image_workbench.py:2303-2328` 的 `_write_image_override` 函数体为：

```python
def _write_image_override(
    conn: sqlite3.Connection,
    *,
    case_id: int,
    filename: str,
    manual_phase: str | None,
    manual_view: str | None,
    updated_at: str,
) -> None:
    _override_writer.write_image_override(
        conn,
        case_id=case_id,
        filename=filename,
        manual_phase=manual_phase,
        manual_view=manual_view,
        manual_transform_json=None,
        updated_at=updated_at,
    )
```

注意：原签名是位置参数，call site `:2428 / :2502 / :2601` 也是位置参数。改成 keyword-only 会破坏调用。**解决**：保留位置参数签名 — 把 `*,` 去掉：

```python
def _write_image_override(
    conn: sqlite3.Connection,
    case_id: int,
    filename: str,
    manual_phase: str | None,
    manual_view: str | None,
    updated_at: str,
) -> None:
    _override_writer.write_image_override(
        conn,
        case_id=case_id,
        filename=filename,
        manual_phase=manual_phase,
        manual_view=manual_view,
        manual_transform_json=None,
        updated_at=updated_at,
    )
```

Trash 的内联 DELETE `image_workbench.py:2314`（在 `_write_image_override` 函数体内，all-null 分支）原来是：

```python
    if manual_phase is None and manual_view is None:
        conn.execute(
            "DELETE FROM case_image_overrides WHERE case_id = ? AND filename = ?",
            (case_id, filename),
        )
        return
```

这段已经被上面新函数体替换 — service writer 内部 all_null 自己 DELETE。不需要额外处理。

- [ ] **Step 8: Run full pytest regression**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && pytest backend/tests -x`

Expected: 全绿。尤其 `test_image_overrides_basic.py`（既有 16 测试）不应 regress。

- [ ] **Step 9: Lint check**

Run: `cd /Users/a1234/Desktop/案例生成器/case-workbench && python -m pyflakes backend/services backend/routes/cases.py backend/routes/image_workbench.py`

Expected: 无输出（0 issues）。

- [ ] **Step 10: Commit**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git add backend/services/ backend/routes/cases.py backend/routes/image_workbench.py backend/tests/test_image_override_writer.py
git commit -m "refactor(best-pair): T2 unify case_image_overrides writes via services/image_override_writer"
```

---

## Task 3: best_pair_service + compute_queue

**Files:**
- Create: `backend/services/best_pair_service.py`
- Create: `backend/workers/__init__.py`（空包）
- Create: `backend/workers/best_pair_compute_queue.py`
- Test: `backend/tests/test_best_pair_service.py`（新建）

估时：6h。依赖：T1（schema） + T2（writer 和 resolve helper）。

### Module design

`best_pair_service.py` 导出：

```python
def compute_best_pair(case_id: int, *, db_conn_factory=None) -> dict
def select_best_pair_for_case(
    case_id: int, before: str, after: str, fingerprint: str,
    *, db_conn_factory=None,
) -> int  # returns selection_id
def trigger_best_pair_render(case_id: int, *, db_conn_factory=None) -> int  # returns job_id
def list_best_pair(case_id: int, *, db_conn_factory=None) -> dict
def revert_selection_overrides(selection_id: int, *, db_conn_factory=None) -> None  # Wave 3 stub
```

`db_conn_factory` 参数为可选 callable，默认使用 `backend.db.connect`；测试可传 stub。

**compute 算法**（core logic — 展开）：

1. 开 connection，`SELECT source_version FROM case_best_pairs WHERE case_id=?` → observed。无行则 INSERT 一行 `status='pending', source_version=0, candidates_json='[]'` 并 observed=0。
2. `SELECT abs_path FROM cases WHERE id=?`; 若不存在 / 目录不存在 → 写 `status='skipped', skipped_reason='dir_missing'`。
3. 读 `case_image_overrides` + fallback keyword（见 `backend/source_images.py`）把目录里源图分 before/after 两组。两组有一组空 → `status='skipped', skipped_reason='no_phase_labels'`。
4. 对 before∪after 调 MediaPipe FaceLandmarker（复用 skill 工具链：`case_layout_board.analyze_image` 已有集成；为隔离，在 service 层直接 import）。任一图无脸跳过；所有图都无脸 → `status='skipped', skipped_reason='no_face_detected'`。
5. 计算全 pair delta：`delta_deg = sqrt(dyaw² + dpitch² + droll²)`。
6. top-5 升序取，生成 `candidates_json`。
7. 计算 `candidates_fingerprint = sha1(sorted_filenames + sorted_overrides + latest_mtime)`。
8. `UPDATE case_best_pairs SET status='ready', candidates_json=?, candidates_fingerprint=?, scanned_at=?, updated_at=? WHERE case_id=? AND source_version=?` with observed。
9. `cursor.rowcount == 0` → dirty 被中途 bump（竞争），**不写**（保持 dirty）；下次触发重算。
10. skipped 分支同样走 `WHERE source_version=?`（codex C1 race 保护）— 如果 skip 写时 bump 发生，则不覆盖 dirty；下次 compute 读到 observed 更大，会重算到 ready。

**fingerprint 函数**：

```python
def _compute_fingerprint(
    filenames: list[str],
    overrides: dict[str, dict[str, Any]],
    latest_mtime_ns: int,
) -> str:
    import hashlib
    import json as _json
    payload = _json.dumps(
        {
            "files": sorted(filenames),
            "overrides": {k: overrides[k] for k in sorted(overrides.keys())},
            "mtime": latest_mtime_ns,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 1: Write failing test for compute race protection**

Create `backend/tests/test_best_pair_service.py`（先写第一批 core race + basic tests；MediaPipe 部分 mock）:

```python
"""best_pair_service 核心逻辑 + compute race 保护测试。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_case(conn: sqlite3.Connection, case_id: int, abs_path: str) -> None:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
        "VALUES (1, ?, ?, '/tmp', 1, 'test')",
        (now, now),
    )
    conn.execute(
        "INSERT INTO cases (id, abs_path, scan_id, category, status, first_seen_at, updated_at) "
        "VALUES (?, ?, 1, 'A', 'active', ?, ?)",
        (case_id, abs_path, now, now),
    )


def test_compute_skipped_dir_missing(temp_db: Path) -> None:
    from backend.services import best_pair_service as svc
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, "/tmp/nope-does-not-exist-" + str(id({})))
    result = svc.compute_best_pair(1)
    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "dir_missing"


def test_compute_inits_pending_row_when_missing(temp_db: Path) -> None:
    """compute 前若无 case_best_pairs 行，应先 INSERT pending 再算。"""
    from backend.services import best_pair_service as svc
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        _mk_case(conn, 1, "/tmp/case-x")
    # compute 会 skip dir_missing，但应已 INSERT 行
    svc.compute_best_pair(1)
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT status, source_version FROM case_best_pairs WHERE case_id=1").fetchone()
    assert row is not None
    assert row[1] == 0


def test_compute_source_version_race_keeps_dirty(temp_db: Path, tmp_path: Path) -> None:
    """模拟 compute 中途 mark_dirty → 写回因 source_version 不匹配被阻止。"""
    from backend.services import best_pair_service as svc
    case_dir = tmp_path / "case-race"
    case_dir.mkdir()
    (case_dir / "before.jpg").write_bytes(b"fake")
    (case_dir / "after.jpg").write_bytes(b"fake")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, source_version, scanned_at, updated_at) "
            "VALUES (1, 'pending', '[]', 0, ?, ?)",
            (now, now),
        )
    # 让 compute 在分析阶段提前 → 期间 bump source_version
    def _fake_analyze(*args, **kwargs):
        with sqlite3.connect(temp_db) as conn:
            conn.execute(
                "UPDATE case_best_pairs SET source_version = source_version + 1, status='dirty' WHERE case_id=1"
            )
            conn.commit()
        return {
            "before.jpg": {"yaw": 1.0, "pitch": 2.0, "roll": 0.5},
            "after.jpg": {"yaw": 1.2, "pitch": 2.1, "roll": 0.4},
        }
    with patch("backend.services.best_pair_service._analyze_faces", side_effect=_fake_analyze):
        with patch("backend.services.best_pair_service._partition_phases", return_value=(["before.jpg"], ["after.jpg"])):
            svc.compute_best_pair(1)
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute("SELECT status FROM case_best_pairs WHERE case_id=1").fetchone()
    assert row[0] == "dirty"  # 未被 ready 覆盖


def test_compute_ready_writes_top5(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    case_dir = tmp_path / "case-ok"
    case_dir.mkdir()
    for name in ["b1.jpg", "b2.jpg", "a1.jpg", "a2.jpg", "a3.jpg"]:
        (case_dir / name).write_bytes(b"fake")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
    poses = {
        "b1.jpg": {"yaw": 5.0, "pitch": 2.0, "roll": 1.0},
        "b2.jpg": {"yaw": 6.0, "pitch": 3.0, "roll": 0.5},
        "a1.jpg": {"yaw": 5.2, "pitch": 2.1, "roll": 1.1},
        "a2.jpg": {"yaw": 12.0, "pitch": 8.0, "roll": 3.0},
        "a3.jpg": {"yaw": 4.9, "pitch": 1.9, "roll": 0.9},
    }
    with patch("backend.services.best_pair_service._partition_phases", return_value=(["b1.jpg", "b2.jpg"], ["a1.jpg", "a2.jpg", "a3.jpg"])):
        with patch("backend.services.best_pair_service._analyze_faces", return_value=poses):
            result = svc.compute_best_pair(1)
    assert result["status"] == "ready"
    cands = result["candidates"]
    assert 1 <= len(cands) <= 5
    # 最小 delta 应是 b1↔a3 或 b1↔a1
    assert cands[0]["delta_deg"] <= cands[-1]["delta_deg"]


def test_compute_no_face_detected(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    case_dir = tmp_path / "case-noface"
    case_dir.mkdir()
    (case_dir / "x.jpg").write_bytes(b"fake")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
    with patch("backend.services.best_pair_service._partition_phases", return_value=(["x.jpg"], ["x.jpg"])):
        with patch("backend.services.best_pair_service._analyze_faces", return_value={}):
            result = svc.compute_best_pair(1)
    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "no_face_detected"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest backend/tests/test_best_pair_service.py -v`

Expected: `ModuleNotFoundError: backend.services.best_pair_service`。

- [ ] **Step 3: Implement best_pair_service minimal skeleton**

Create `backend/services/best_pair_service.py`:

```python
"""Best-pair compute + select + list service.

See docs/superpowers/specs/2026-05-09-best-pair-routing-design.md §5.1
for the full algorithm. Key invariants:
- source_version protects compute write-back against races (codex C1).
- candidates_fingerprint gates select; UI must echo the fingerprint it saw.
- MediaPipe analysis is swappable for tests via _analyze_faces.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import db as _db
from . import image_override_writer as _writer


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_conn_factory() -> sqlite3.Connection:
    return _db.get_conn()


def _ensure_row(conn: sqlite3.Connection, case_id: int) -> int:
    """Insert pending row if missing; return current source_version."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source_version FROM case_best_pairs WHERE case_id=?", (case_id,)
    ).fetchone()
    if row is not None:
        return int(row["source_version"])
    now = _now_iso()
    conn.execute(
        "INSERT INTO case_best_pairs (case_id, status, candidates_json, source_version, scanned_at, updated_at) "
        "VALUES (?, 'pending', '[]', 0, ?, ?)",
        (case_id, now, now),
    )
    conn.commit()
    return 0


def _case_dir(conn: sqlite3.Connection, case_id: int) -> Path | None:
    row = conn.execute(
        "SELECT abs_path FROM cases WHERE id=? AND trashed_at IS NULL", (case_id,)
    ).fetchone()
    if not row:
        return None
    path = Path(row[0])
    if not path.exists() or not path.is_dir():
        return None
    return path


def _partition_phases(case_dir: Path, overrides: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Split filenames into (before, after). Override takes priority over keyword fallback.
    Returns ([], []) if no classifiable images.
    """
    from .. import source_images  # reuse existing keyword logic
    before: list[str] = []
    after: list[str] = []
    for p in sorted(case_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        name = p.name
        ov = overrides.get(name, {})
        phase = ov.get("phase")
        if phase not in ("before", "after"):
            phase = source_images.guess_phase_from_filename(name)  # returns 'before'|'after'|None
        if phase == "before":
            before.append(name)
        elif phase == "after":
            after.append(name)
    return before, after


def _analyze_faces(case_dir: Path, filenames: list[str]) -> dict[str, dict[str, float]]:
    """Run MediaPipe FaceLandmarker; return {filename: {yaw, pitch, roll}} for
    images where a face was detected. Missing = no face.

    Uses skill toolchain via case_layout_board.analyze_image if available;
    otherwise raises RuntimeError (tests mock this function).
    """
    import importlib.util
    spec_path = Path("/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_board.py")
    if not spec_path.exists():
        raise RuntimeError(f"case-layout-board skill missing: {spec_path}")
    spec = importlib.util.spec_from_file_location("_clb", spec_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load skill module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out: dict[str, dict[str, float]] = {}
    for name in filenames:
        try:
            analysis = module.analyze_image(str(case_dir / name))
            pose = analysis.get("pose") or {}
            if pose:
                out[name] = {
                    "yaw": float(pose.get("yaw") or 0.0),
                    "pitch": float(pose.get("pitch") or 0.0),
                    "roll": float(pose.get("roll") or 0.0),
                }
        except Exception:
            continue
    return out


def _compute_fingerprint(filenames: list[str], overrides: dict[str, dict[str, Any]], latest_mtime_ns: int) -> str:
    payload = json.dumps(
        {
            "files": sorted(filenames),
            "overrides": {k: overrides[k] for k in sorted(overrides.keys())},
            "mtime": latest_mtime_ns,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
```

（继续附加 compute_best_pair 函数主体 — 见 Step 4）

- [ ] **Step 4: Implement compute_best_pair + list_best_pair**

在 `backend/services/best_pair_service.py` 末尾追加：

```python
def compute_best_pair(case_id: int, *, db_conn_factory: Callable[[], sqlite3.Connection] | None = None) -> dict:
    conn_factory = db_conn_factory or _default_conn_factory
    conn = conn_factory()
    conn.row_factory = sqlite3.Row
    try:
        observed = _ensure_row(conn, case_id)
        case_dir = _case_dir(conn, case_id)
        if case_dir is None:
            return _write_skip(conn, case_id, observed, "dir_missing")

        # fetch overrides
        ov_rows = conn.execute(
            "SELECT filename, manual_phase, manual_view, manual_transform_json "
            "FROM case_image_overrides WHERE case_id=?",
            (case_id,),
        ).fetchall()
        overrides = {
            r["filename"]: {
                "phase": r["manual_phase"],
                "view": r["manual_view"],
                "transform": r["manual_transform_json"],
            }
            for r in ov_rows
        }

        before, after = _partition_phases(case_dir, overrides)
        if not before or not after:
            return _write_skip(conn, case_id, observed, "no_phase_labels")

        all_files = list(set(before) | set(after))
        poses = _analyze_faces(case_dir, all_files)
        if not poses:
            return _write_skip(conn, case_id, observed, "no_face_detected")

        # filter to only images that got a pose
        before_ok = [n for n in before if n in poses]
        after_ok = [n for n in after if n in poses]
        if not before_ok or not after_ok:
            return _write_skip(conn, case_id, observed, "no_face_detected")

        pairs: list[dict[str, Any]] = []
        for b in before_ok:
            for a in after_ok:
                dy = poses[a]["yaw"] - poses[b]["yaw"]
                dp = poses[a]["pitch"] - poses[b]["pitch"]
                dr = poses[a]["roll"] - poses[b]["roll"]
                delta = (dy * dy + dp * dp + dr * dr) ** 0.5
                pairs.append({
                    "before": b,
                    "after": a,
                    "delta_deg": round(delta, 3),
                    "delta_yaw": round(dy, 3),
                    "delta_pitch": round(dp, 3),
                    "delta_roll": round(dr, 3),
                })
        pairs.sort(key=lambda p: p["delta_deg"])
        top5 = pairs[:5]

        mtime_ns = max((case_dir / n).stat().st_mtime_ns for n in all_files)
        fingerprint = _compute_fingerprint(all_files, overrides, mtime_ns)

        now = _now_iso()
        cur = conn.execute(
            "UPDATE case_best_pairs "
            "SET status='ready', skipped_reason=NULL, "
            "    candidates_json=?, candidates_fingerprint=?, "
            "    scanned_at=?, updated_at=? "
            "WHERE case_id=? AND source_version=?",
            (json.dumps(top5, ensure_ascii=False), fingerprint, now, now, case_id, observed),
        )
        conn.commit()
        if cur.rowcount == 0:
            # Lost the race — dirty bumped while we were computing.
            return {"status": "dirty", "candidates": [], "fingerprint": None}
        return {
            "status": "ready",
            "candidates": top5,
            "fingerprint": fingerprint,
            "scanned_at": now,
        }
    finally:
        conn.close()


def _write_skip(conn: sqlite3.Connection, case_id: int, observed: int, reason: str) -> dict:
    now = _now_iso()
    cur = conn.execute(
        "UPDATE case_best_pairs "
        "SET status='skipped', skipped_reason=?, candidates_json='[]', "
        "    candidates_fingerprint=NULL, scanned_at=?, updated_at=? "
        "WHERE case_id=? AND source_version=?",
        (reason, now, now, case_id, observed),
    )
    conn.commit()
    if cur.rowcount == 0:
        return {"status": "dirty", "candidates": [], "fingerprint": None}
    return {"status": "skipped", "skipped_reason": reason, "candidates": []}


def list_best_pair(case_id: int, *, db_conn_factory: Callable[[], sqlite3.Connection] | None = None) -> dict:
    conn_factory = db_conn_factory or _default_conn_factory
    conn = conn_factory()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status, skipped_reason, candidates_json, candidates_fingerprint, "
            "       source_version, scanned_at, updated_at "
            "FROM case_best_pairs WHERE case_id=?",
            (case_id,),
        ).fetchone()
        if row is None:
            return {"status": "pending", "candidates": [], "fingerprint": None, "current_selection": None}
        sel = conn.execute(
            "SELECT id, before_filename, after_filename, delta_deg, candidates_fingerprint, selected_at "
            "FROM case_best_pair_selections WHERE case_id=? "
            "ORDER BY selected_at DESC, id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return {
            "status": row["status"],
            "skipped_reason": row["skipped_reason"],
            "candidates": json.loads(row["candidates_json"] or "[]"),
            "fingerprint": row["candidates_fingerprint"],
            "scanned_at": row["scanned_at"],
            "updated_at": row["updated_at"],
            "current_selection": None if sel is None else {
                "id": sel["id"],
                "before": sel["before_filename"],
                "after": sel["after_filename"],
                "delta_deg": sel["delta_deg"],
                "fingerprint": sel["candidates_fingerprint"],
                "selected_at": sel["selected_at"],
            },
        }
    finally:
        conn.close()
```

- [ ] **Step 5: Run T3 compute tests to verify pass**

Run: `pytest backend/tests/test_best_pair_service.py -v`

Expected: 5 passed。

- [ ] **Step 6: Add select_best_pair_for_case + tests**

在测试文件末尾 append：

```python
def test_select_writes_overrides_and_selection(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    case_dir = tmp_path / "case-sel"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"fake")
    (case_dir / "a.jpg").write_bytes(b"fake")
    fp = "deadbeef"
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "  source_version, scanned_at, updated_at) "
            "VALUES (1, 'ready', ?, ?, 0, ?, ?)",
            (
                '[{"before":"b.jpg","after":"a.jpg","delta_deg":1.0,"delta_yaw":0.5,"delta_pitch":0.5,"delta_roll":0.5}]',
                fp,
                now,
                now,
            ),
        )
    sel_id = svc.select_best_pair_for_case(1, "b.jpg", "a.jpg", fp)
    assert sel_id > 0
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        sel = conn.execute("SELECT * FROM case_best_pair_selections WHERE id=?", (sel_id,)).fetchone()
        ov = conn.execute("SELECT filename, manual_phase FROM case_image_overrides WHERE case_id=1 ORDER BY filename").fetchall()
        bp = conn.execute("SELECT status, source_version FROM case_best_pairs WHERE case_id=1").fetchone()
    assert sel["before_filename"] == "b.jpg"
    assert sel["after_filename"] == "a.jpg"
    assert sel["candidates_fingerprint"] == fp
    assert [r["filename"] for r in ov] == ["a.jpg", "b.jpg"]
    assert {r["manual_phase"] for r in ov} == {"before", "after"}
    # skip_dirty_mark=True 保护：select 后 best-pair 仍 ready（未自动作废）
    assert bp["status"] == "ready"
    assert bp["source_version"] == 0


def test_select_rejects_stale_fingerprint(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    from fastapi import HTTPException
    case_dir = tmp_path / "case-stale"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"fake")
    (case_dir / "a.jpg").write_bytes(b"fake")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) VALUES (1, 'ready', '[]', 'correctfp', 0, ?, ?)",
            (now, now),
        )
    with pytest.raises(HTTPException) as exc_info:
        svc.select_best_pair_for_case(1, "b.jpg", "a.jpg", "wrongfp")
    assert exc_info.value.status_code == 409


def test_select_rejects_when_not_ready(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    from fastapi import HTTPException
    case_dir = tmp_path / "case-dirty"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"fake")
    (case_dir / "a.jpg").write_bytes(b"fake")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) VALUES (1, 'dirty', '[]', 'fp', 0, ?, ?)",
            (now, now),
        )
    with pytest.raises(HTTPException) as exc_info:
        svc.select_best_pair_for_case(1, "b.jpg", "a.jpg", "fp")
    assert exc_info.value.status_code == 409


def test_select_validates_file_exists(temp_db: Path, tmp_path: Path) -> None:
    from backend.services import best_pair_service as svc
    from fastapi import HTTPException
    case_dir = tmp_path / "case-missing"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"fake")  # only before exists
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) VALUES (1, 'ready', '[]', 'fp', 0, ?, ?)",
            (now, now),
        )
    with pytest.raises(HTTPException) as exc_info:
        svc.select_best_pair_for_case(1, "b.jpg", "nonexistent.jpg", "fp")
    assert exc_info.value.status_code == 404


def test_select_snapshots_prior_override(temp_db: Path, tmp_path: Path) -> None:
    """before_override_before_json 在原行存在时应落快照；不存在则 NULL。"""
    from backend.services import best_pair_service as svc
    case_dir = tmp_path / "case-snap"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"fake")
    (case_dir / "a.jpg").write_bytes(b"fake")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) VALUES (1, 'ready', '[]', 'fp', 0, ?, ?)",
            (now, now),
        )
        # 预置 b.jpg 有老 override，a.jpg 没有
        conn.execute(
            "INSERT INTO case_image_overrides (case_id, filename, manual_phase, manual_view, updated_at) "
            "VALUES (1, 'b.jpg', 'after', 'side', ?)",
            (now,),
        )
    sel_id = svc.select_best_pair_for_case(1, "b.jpg", "a.jpg", "fp")
    with sqlite3.connect(temp_db) as conn:
        conn.row_factory = sqlite3.Row
        sel = conn.execute("SELECT * FROM case_best_pair_selections WHERE id=?", (sel_id,)).fetchone()
    import json as _json
    before_snap = _json.loads(sel["before_override_before_json"])
    assert before_snap["manual_phase"] == "after"
    assert before_snap["manual_view"] == "side"
    assert sel["after_override_before_json"] is None
```

- [ ] **Step 7: Run select tests to verify fail**

Run: `pytest backend/tests/test_best_pair_service.py::test_select_writes_overrides_and_selection -v`

Expected: FAIL — `select_best_pair_for_case` 未定义。

- [ ] **Step 8: Implement select_best_pair_for_case + trigger_best_pair_render**

在 `backend/services/best_pair_service.py` 末尾继续追加：

```python
def select_best_pair_for_case(
    case_id: int,
    before: str,
    after: str,
    fingerprint: str,
    *,
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> int:
    from fastapi import HTTPException
    from .case_files import resolve_existing_source

    conn_factory = db_conn_factory or _default_conn_factory
    conn = conn_factory()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT abs_path FROM cases WHERE id=? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "case not found")
        case_dir = Path(row["abs_path"])
        resolve_existing_source(case_dir, before)
        resolve_existing_source(case_dir, after)

        bp = conn.execute(
            "SELECT status, candidates_fingerprint, candidates_json "
            "FROM case_best_pairs WHERE case_id=?",
            (case_id,),
        ).fetchone()
        if bp is None or bp["status"] != "ready":
            raise HTTPException(409, "cache_not_ready")
        if bp["candidates_fingerprint"] != fingerprint:
            raise HTTPException(409, "stale_fingerprint")

        cands = json.loads(bp["candidates_json"] or "[]")
        match = next((c for c in cands if c.get("before") == before and c.get("after") == after), None)
        if match is None:
            raise HTTPException(400, "pair not in top candidates")
        delta_deg = float(match.get("delta_deg") or 0.0)

        def _snapshot(filename: str) -> str | None:
            r = conn.execute(
                "SELECT manual_phase, manual_view, manual_transform_json, updated_at "
                "FROM case_image_overrides WHERE case_id=? AND filename=?",
                (case_id, filename),
            ).fetchone()
            return None if r is None else json.dumps(dict(r), ensure_ascii=False)

        before_snap = _snapshot(before)
        after_snap = _snapshot(after)

        now = _now_iso()
        cur = conn.execute(
            "INSERT INTO case_best_pair_selections "
            "(case_id, before_filename, after_filename, delta_deg, candidates_fingerprint, "
            " before_override_before_json, after_override_before_json, selected_at, selected_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'local')",
            (case_id, before, after, delta_deg, fingerprint, before_snap, after_snap, now),
        )
        selection_id = cur.lastrowid or 0

        # Write override — skip_dirty_mark=True so we don't invalidate the cache we just selected from.
        _writer.write_image_override(
            conn,
            case_id=case_id,
            filename=before,
            manual_phase="before",
            manual_view=None,
            manual_transform_json=None,
            updated_at=now,
            skip_dirty_mark=True,
        )
        _writer.write_image_override(
            conn,
            case_id=case_id,
            filename=after,
            manual_phase="after",
            manual_view=None,
            manual_transform_json=None,
            updated_at=now,
            skip_dirty_mark=True,
        )
        conn.commit()
        return selection_id
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def trigger_best_pair_render(
    case_id: int,
    *,
    brand: str = "fumei",
    template: str = "tri-compare",
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> int:
    from fastapi import HTTPException
    from ..render_queue import RENDER_QUEUE

    conn_factory = db_conn_factory or _default_conn_factory
    conn = conn_factory()
    conn.row_factory = sqlite3.Row
    try:
        sel = conn.execute(
            "SELECT id FROM case_best_pair_selections WHERE case_id=? "
            "ORDER BY selected_at DESC, id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if sel is None:
            raise HTTPException(400, "no_current_selection")
    finally:
        conn.close()
    # enqueue through existing queue; T5 adds render_mode + best_pair_selection_id
    return RENDER_QUEUE.enqueue(
        case_id, brand=brand, template=template,
        render_mode="best-pair", best_pair_selection_id=sel["id"],
    )


def revert_selection_overrides(
    selection_id: int,
    *,
    db_conn_factory: Callable[[], sqlite3.Connection] | None = None,
) -> None:
    """Wave 3 stub — schema stores snapshots but MVP does not expose a revert endpoint."""
    raise NotImplementedError("revert flow is out of MVP scope")
```

- [ ] **Step 9: Run select tests to verify pass**

Run: `pytest backend/tests/test_best_pair_service.py -v`

Expected: 10 passed。

- [ ] **Step 10: Implement compute_queue worker**

Create `backend/workers/__init__.py` (empty).

Create `backend/workers/best_pair_compute_queue.py`:

```python
"""Sequential best-pair compute queue — submits one future to _job_pool at a time.

MediaPipe FaceLandmarker is not thread-safe; we want compute to pin a single
worker slot rather than racing with itself. We still share _job_pool (max=2)
with render/upgrade so compute and render can run in parallel — just not
two computes at once.

Batch status is persisted in-memory via a simple dict keyed by batch_id.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future
from typing import Any

from .. import _job_pool
from ..services import best_pair_service


class _ComputeQueue:
    def __init__(self) -> None:
        self._batches: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._inflight: Future | None = None
        self._chain: list[tuple[str, int]] = []  # [(batch_id, case_id), ...]

    def submit_batch(self, case_ids: list[int]) -> str:
        batch_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._batches[batch_id] = {
                "batch_id": batch_id,
                "total": len(case_ids),
                "done": 0,
                "failed": 0,
                "errors": [],
                "case_ids": list(case_ids),
                "status": "queued",
            }
            for cid in case_ids:
                self._chain.append((batch_id, cid))
            if self._inflight is None or self._inflight.done():
                self._inflight = _job_pool.submit(self._drain)
        return batch_id

    def status(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            b = self._batches.get(batch_id)
            return None if b is None else dict(b)

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._chain:
                    return
                batch_id, case_id = self._chain.pop(0)
                self._batches[batch_id]["status"] = "running"
            try:
                best_pair_service.compute_best_pair(case_id)
                with self._lock:
                    self._batches[batch_id]["done"] += 1
            except Exception as e:
                with self._lock:
                    self._batches[batch_id]["failed"] += 1
                    self._batches[batch_id]["errors"].append({"case_id": case_id, "error": str(e)})
            with self._lock:
                b = self._batches[batch_id]
                if b["done"] + b["failed"] >= b["total"]:
                    b["status"] = "done"


COMPUTE_QUEUE = _ComputeQueue()
```

- [ ] **Step 11: Write + run compute_queue test**

Append to `backend/tests/test_best_pair_service.py`:

```python
def test_compute_queue_batch_runs_all(temp_db: Path, tmp_path: Path) -> None:
    from backend.workers.best_pair_compute_queue import COMPUTE_QUEUE
    from unittest.mock import patch

    case_dirs: list[Path] = []
    with sqlite3.connect(temp_db) as conn:
        for i in range(3):
            cd = tmp_path / f"case-{i}"
            cd.mkdir()
            case_dirs.append(cd)
            _mk_case(conn, i + 1, str(cd))

    # Mock compute to avoid MediaPipe
    with patch(
        "backend.services.best_pair_service.compute_best_pair",
        return_value={"status": "skipped", "skipped_reason": "no_face_detected", "candidates": []},
    ):
        batch_id = COMPUTE_QUEUE.submit_batch([1, 2, 3])
        # poll for up to 3s
        import time
        for _ in range(30):
            st = COMPUTE_QUEUE.status(batch_id)
            if st and st["status"] == "done":
                break
            time.sleep(0.1)
    st = COMPUTE_QUEUE.status(batch_id)
    assert st is not None
    assert st["status"] == "done"
    assert st["done"] == 3
    assert st["failed"] == 0
```

Run: `pytest backend/tests/test_best_pair_service.py -v`

Expected: 11 passed (all).

- [ ] **Step 12: Full regression + lint**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
pytest backend/tests -x
python -m pyflakes backend/services backend/workers
```

Expected: 全绿 + 无 lint warning。

- [ ] **Step 13: Commit**

```bash
git add backend/services/best_pair_service.py backend/services/case_files.py backend/workers/ backend/tests/test_best_pair_service.py
git commit -m "feat(best-pair): T3 service + compute_queue with source_version race guard"
```

---

## Task 4: 6 REST 端点

**Files:**
- Create: `backend/routes/best_pair.py`
- Modify: `backend/main.py`（register router）
- Test: `backend/tests/test_best_pair_routes.py`

估时：4h。依赖：T3。

**6 endpoints**（spec §6.1 规范）：

```
GET    /api/cases/{case_id}/best-pair
POST   /api/cases/{case_id}/best-pair/compute
POST   /api/cases/{case_id}/best-pair/select
POST   /api/cases/{case_id}/best-pair/render
POST   /api/cases/best-pair/batch-compute    (body: {case_ids: [int,...]})
GET    /api/cases/best-pair/batch-compute/{batch_id}
```

- [ ] **Step 1: Write failing route tests**

Create `backend/tests/test_best_pair_routes.py`:

```python
"""Best-pair REST endpoint tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_case(conn: sqlite3.Connection, case_id: int, abs_path: str) -> None:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
        "VALUES (1, ?, ?, '/tmp', 1, 'test')",
        (now, now),
    )
    conn.execute(
        "INSERT INTO cases (id, abs_path, scan_id, category, status, first_seen_at, updated_at) "
        "VALUES (?, ?, 1, 'A', 'active', ?, ?)",
        (case_id, abs_path, now, now),
    )


def test_get_best_pair_pending_when_no_row(client: TestClient, temp_db: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, "/tmp/x")
    r = client.get("/api/cases/1/best-pair")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["candidates"] == []
    assert body["current_selection"] is None


def test_get_best_pair_404_on_missing_case(client: TestClient) -> None:
    r = client.get("/api/cases/9999/best-pair")
    assert r.status_code == 404


def test_post_compute_returns_result(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    case_dir.mkdir()
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
    with patch(
        "backend.services.best_pair_service.compute_best_pair",
        return_value={"status": "skipped", "skipped_reason": "no_phase_labels", "candidates": []},
    ):
        r = client.post("/api/cases/1/best-pair/compute")
    assert r.status_code == 200
    assert r.json()["status"] == "skipped"


def test_post_select_writes_overrides(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    case_dir = tmp_path / "c"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"f")
    (case_dir / "a.jpg").write_bytes(b"f")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) "
            "VALUES (1, 'ready', ?, 'fp', 0, ?, ?)",
            (
                '[{"before":"b.jpg","after":"a.jpg","delta_deg":1.0,"delta_yaw":0,"delta_pitch":0,"delta_roll":1}]',
                now, now,
            ),
        )
    r = client.post(
        "/api/cases/1/best-pair/select",
        json={"before": "b.jpg", "after": "a.jpg", "fingerprint": "fp"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["selection_id"] > 0
    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute("SELECT filename, manual_phase FROM case_image_overrides WHERE case_id=1 ORDER BY filename").fetchall()
    assert len(rows) == 2


def test_post_select_409_on_stale_fingerprint(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    case_dir = tmp_path / "c2"
    case_dir.mkdir()
    (case_dir / "b.jpg").write_bytes(b"f")
    (case_dir / "a.jpg").write_bytes(b"f")
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) VALUES (1, 'ready', '[]', 'realfp', 0, ?, ?)",
            (now, now),
        )
    r = client.post("/api/cases/1/best-pair/select", json={"before": "b.jpg", "after": "a.jpg", "fingerprint": "wrongfp"})
    assert r.status_code == 409


def test_post_render_returns_job_id(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    case_dir = tmp_path / "c3"
    case_dir.mkdir()
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
        now = _now()
        conn.execute(
            "INSERT INTO case_best_pair_selections (case_id, before_filename, after_filename, delta_deg, "
            "candidates_fingerprint, selected_at) VALUES (1, 'b.jpg', 'a.jpg', 1.0, 'fp', ?)",
            (now,),
        )
    with patch("backend.render_queue.RENDER_QUEUE.enqueue", return_value=42) as mock:
        r = client.post("/api/cases/1/best-pair/render")
    assert r.status_code == 200
    assert r.json()["job_id"] == 42
    call_kwargs = mock.call_args.kwargs
    assert call_kwargs.get("render_mode") == "best-pair"
    assert call_kwargs.get("best_pair_selection_id") == 1


def test_post_render_400_when_no_selection(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    case_dir = tmp_path / "c4"
    case_dir.mkdir()
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn, 1, str(case_dir))
    r = client.post("/api/cases/1/best-pair/render")
    assert r.status_code == 400
    assert "no_current_selection" in r.json().get("detail", "")


def test_batch_compute_accepts_and_returns_batch_id(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    with sqlite3.connect(temp_db) as conn:
        for i in range(3):
            _mk_case(conn, i + 1, str(tmp_path / f"c{i}"))
            (tmp_path / f"c{i}").mkdir()
    with patch("backend.workers.best_pair_compute_queue.COMPUTE_QUEUE.submit_batch", return_value="abc123") as mock:
        r = client.post("/api/cases/best-pair/batch-compute", json={"case_ids": [1, 2, 3]})
    assert r.status_code == 202
    assert r.json()["batch_id"] == "abc123"
    mock.assert_called_once_with([1, 2, 3])


def test_batch_compute_status_returns_progress(client: TestClient, temp_db: Path) -> None:
    with patch(
        "backend.workers.best_pair_compute_queue.COMPUTE_QUEUE.status",
        return_value={"batch_id": "abc", "total": 3, "done": 1, "failed": 0, "errors": [], "status": "running"},
    ):
        r = client.get("/api/cases/best-pair/batch-compute/abc")
    assert r.status_code == 200
    body = r.json()
    assert body["done"] == 1
    assert body["status"] == "running"


def test_batch_status_404_on_unknown(client: TestClient) -> None:
    with patch(
        "backend.workers.best_pair_compute_queue.COMPUTE_QUEUE.status", return_value=None
    ):
        r = client.get("/api/cases/best-pair/batch-compute/nope")
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest backend/tests/test_best_pair_routes.py -v`

Expected: 404/422 across the board (router not registered).

- [ ] **Step 3: Create routes/best_pair.py**

Create `backend/routes/best_pair.py`:

```python
"""Best-pair routing endpoints (Wave 2)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db as _db
from ..services import best_pair_service
from ..workers.best_pair_compute_queue import COMPUTE_QUEUE


router = APIRouter()


class SelectPayload(BaseModel):
    before: str = Field(..., min_length=1)
    after: str = Field(..., min_length=1)
    fingerprint: str = Field(..., min_length=1)


class BatchComputePayload(BaseModel):
    case_ids: list[int] = Field(..., min_length=1, max_length=500)


def _assert_case_exists(case_id: int) -> None:
    with _db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE id=? AND trashed_at IS NULL", (case_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "case not found")


@router.get("/api/cases/{case_id}/best-pair")
def get_best_pair(case_id: int) -> dict[str, Any]:
    _assert_case_exists(case_id)
    return best_pair_service.list_best_pair(case_id)


@router.post("/api/cases/{case_id}/best-pair/compute")
def post_compute(case_id: int) -> dict[str, Any]:
    _assert_case_exists(case_id)
    return best_pair_service.compute_best_pair(case_id)


@router.post("/api/cases/{case_id}/best-pair/select")
def post_select(case_id: int, payload: SelectPayload) -> dict[str, Any]:
    _assert_case_exists(case_id)
    selection_id = best_pair_service.select_best_pair_for_case(
        case_id,
        payload.before,
        payload.after,
        payload.fingerprint,
    )
    return {"selection_id": selection_id}


@router.post("/api/cases/{case_id}/best-pair/render")
def post_render(case_id: int, brand: str = "fumei", template: str = "tri-compare") -> dict[str, Any]:
    _assert_case_exists(case_id)
    job_id = best_pair_service.trigger_best_pair_render(case_id, brand=brand, template=template)
    return {"job_id": job_id}


@router.post("/api/cases/best-pair/batch-compute", status_code=202)
def post_batch_compute(payload: BatchComputePayload) -> dict[str, Any]:
    batch_id = COMPUTE_QUEUE.submit_batch(payload.case_ids)
    return {"batch_id": batch_id, "queued": len(payload.case_ids)}


@router.get("/api/cases/best-pair/batch-compute/{batch_id}")
def get_batch_status(batch_id: str) -> dict[str, Any]:
    st = COMPUTE_QUEUE.status(batch_id)
    if st is None:
        raise HTTPException(404, "batch not found")
    return st
```

- [ ] **Step 4: Register router in main.py**

Read `backend/main.py` to find the existing `app.include_router` pattern (there's one per route file).

Add at the end of the `include_router` section:

```python
from .routes import best_pair as _best_pair_router
app.include_router(_best_pair_router.router)
```

- [ ] **Step 5: Run tests to verify pass**

Run: `pytest backend/tests/test_best_pair_routes.py -v`

Expected: 10 passed.

- [ ] **Step 6: Verify all backend tests still pass**

Run: `pytest backend/tests -x`

Expected: 全绿。

- [ ] **Step 7: Commit**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git add backend/routes/best_pair.py backend/main.py backend/tests/test_best_pair_routes.py
git commit -m "feat(best-pair): T4 six REST endpoints + batch compute queue hookup"
```

---

## Task 5: render_queue render_mode + best_pair_selection_id 透传

**Files:**
- Modify: `backend/render_queue.py`（`enqueue` 签名加 2 可选参；`_execute_render` 读列值写入 manifest meta）
- Test: `backend/tests/test_best_pair_render_path.py`（新建）

估时：3h。依赖：T1（两个列已建）+ T3（service 调 enqueue 会传 kwargs）。

**关键**：codex 评审明确本 codebase 的 `run_render` 不调 AI enhance — spec §7 所谓 `skip_enhance` 在这里是 noop。Best-pair render 的差异只有：

1. `render_jobs.render_mode='best-pair'` + `best_pair_selection_id` 写入（审计 / UI badge 用）。
2. render 走现有 `_fetch_case_image_overrides` 路径 — 因 T3 `select_best_pair_for_case` 已把 before/after filename 写 `case_image_overrides` 带 phase=before/after，`run_render` 会自然锁定到该对。

所以 T5 只做「加两列 + 透传」，不动 `render_executor.py`，不加策略分支。

- [ ] **Step 1: Write failing test**

Create `backend/tests/test_best_pair_render_path.py`:

```python
"""render_queue.enqueue 对 render_mode / best_pair_selection_id 的透传测试。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mk_case(conn: sqlite3.Connection, case_id: int = 1) -> None:
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
        "VALUES (1, ?, ?, '/tmp', 1, 'test')",
        (now, now),
    )
    conn.execute(
        "INSERT INTO cases (id, abs_path, scan_id, category, status, first_seen_at, updated_at) "
        "VALUES (?, '/tmp/c', 1, 'A', 'active', ?, ?)",
        (case_id, now, now),
    )


def test_enqueue_default_render_mode_ai(temp_db: Path) -> None:
    from backend.render_queue import RENDER_QUEUE
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn)
    job_id = RENDER_QUEUE.enqueue(1, brand="fumei")
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT render_mode, best_pair_selection_id FROM render_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row[0] == "ai"
    assert row[1] is None


def test_enqueue_best_pair_mode(temp_db: Path) -> None:
    from backend.render_queue import RENDER_QUEUE
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn)
        conn.execute(
            "INSERT INTO case_best_pair_selections (id, case_id, before_filename, after_filename, "
            "delta_deg, candidates_fingerprint, selected_at) "
            "VALUES (7, 1, 'b.jpg', 'a.jpg', 1.0, 'fp', ?)",
            (_now(),),
        )
    job_id = RENDER_QUEUE.enqueue(1, brand="fumei", render_mode="best-pair", best_pair_selection_id=7)
    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT render_mode, best_pair_selection_id FROM render_jobs WHERE id=?", (job_id,)
        ).fetchone()
    assert row[0] == "best-pair"
    assert row[1] == 7


def test_enqueue_rejects_invalid_render_mode(temp_db: Path) -> None:
    from backend.render_queue import RENDER_QUEUE
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn)
    with pytest.raises(ValueError):
        RENDER_QUEUE.enqueue(1, brand="fumei", render_mode="bogus")


def test_enqueue_best_pair_requires_selection_id(temp_db: Path) -> None:
    from backend.render_queue import RENDER_QUEUE
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn)
    with pytest.raises(ValueError):
        RENDER_QUEUE.enqueue(1, brand="fumei", render_mode="best-pair")


def test_existing_enqueue_sites_unaffected_regression(temp_db: Path) -> None:
    """Sanity check: no existing call site breaks when default mode is 'ai'."""
    from backend.render_queue import RENDER_QUEUE
    with sqlite3.connect(temp_db) as conn:
        _mk_case(conn)
    job_id = RENDER_QUEUE.enqueue(1, brand="fumei", template="tri-compare", semantic_judge="auto")
    assert job_id > 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest backend/tests/test_best_pair_render_path.py -v`

Expected: FAIL — `unexpected keyword argument 'render_mode'`。

- [ ] **Step 3: Modify render_queue.enqueue signature**

在 `backend/render_queue.py:1221` 改 `enqueue` 方法签名：

```python
    def enqueue(
        self,
        case_id: int,
        brand: str,
        template: str = DEFAULT_TEMPLATE,
        semantic_judge: str = DEFAULT_SEMANTIC_JUDGE,
        batch_id: str | None = None,
        *,
        render_mode: str = "ai",
        best_pair_selection_id: int | None = None,
    ) -> int:
```

在函数体 `if not brand:` 之后加：

```python
        if render_mode not in ("ai", "best-pair"):
            raise ValueError(f"invalid render_mode: {render_mode!r}")
        if render_mode == "best-pair" and best_pair_selection_id is None:
            raise ValueError("render_mode='best-pair' requires best_pair_selection_id")
```

然后修改 `INSERT INTO render_jobs` SQL 追加两列：

```python
            cur = conn.execute(
                """
                INSERT INTO render_jobs
                    (case_id, brand, template, status, batch_id, enqueued_at,
                     semantic_judge, meta_json, render_mode, best_pair_selection_id)
                VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    brand,
                    template,
                    batch_id,
                    _now_iso(),
                    semantic_judge,
                    json.dumps(stress.tag_payload({"enqueue_source": "render_queue"}), ensure_ascii=False),
                    render_mode,
                    best_pair_selection_id,
                ),
            )
```

同时在 `_publish` 消息里加：

```python
        self._publish(
            {
                "type": "job_update",
                "job_id": job_id,
                "case_id": case_id,
                "batch_id": batch_id,
                "brand": brand,
                "template": template,
                "status": "queued",
                "render_mode": render_mode,
            }
        )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `pytest backend/tests/test_best_pair_render_path.py -v`

Expected: 5 passed。

- [ ] **Step 5: Regression run — existing enqueue_batch + recover paths**

Run: `pytest backend/tests/test_render_endpoints.py backend/tests/test_render_batch_preview.py backend/tests/test_queue_recovery.py -v`

Expected: 全绿（既有 enqueue_batch 不传 render_mode → 默认 'ai' → 行为一致）。

- [ ] **Step 6: Optionally surface render_mode in render_history API**

RenderHistoryDrawer 需要 render_mode badge（T6 里用）。找到 `fetch_render_history` 返回体（`backend/api.ts:2011`），在 `backend/routes/render.py` 或任何返回 history 列表的 endpoint 添加 `render_mode` 字段到 SELECT 和序列化。

**定位**：

```bash
grep -n "render_history\|render_jobs" backend/routes/render.py | head -20
```

在该端点的 SELECT 里加上 `render_mode` 列；在响应序列化 dict 加 `"render_mode": row["render_mode"]`。

若现有 history endpoint 是扫 `.history/` 目录而非 `render_jobs` 表（根据 `backend/render_executor.py:90 _archive_existing_final_board` 逻辑），则改成 join `render_jobs` 表：用 `output_path` 或 `finished_at` 关联最近一个 done job。

**简化方案**：在 history 列表响应里只附 `render_mode` 时，优先用一个独立 endpoint `GET /api/cases/{id}/render/history?include_mode=1`，若工作量大则挪到 Wave 3（UI badge 在 T6 做 fallback：若无字段则不显示 badge）。

- [ ] **Step 7: Commit**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git add backend/render_queue.py backend/tests/test_best_pair_render_path.py
git commit -m "feat(best-pair): T5 render_queue.enqueue accepts render_mode + best_pair_selection_id"
```

---

## Task 6: 前端 BestPairPanel + RenderHistoryDrawer mode badge + i18n

**Files:**
- Create: `frontend/src/components/BestPairPanel/BestPairPanel.tsx`
- Create: `frontend/src/components/BestPairPanel/BestPairCandidateCard.tsx`
- Create: `frontend/src/components/BestPairPanel/useBestPair.ts`
- Create: `frontend/src/locales/{zh,en}/bestPair.json`
- Modify: `frontend/src/api.ts`（加 6 API fn）
- Modify: `frontend/src/hooks/queries.ts`（加 useBestPair / useComputeBestPair / useSelectBestPair / useRenderBestPair）
- Modify: `frontend/src/pages/CaseDetail.tsx`（挂 panel + 用 useBrand 拿 brand）
- Modify: `frontend/src/components/RenderHistoryDrawer.tsx`（mode badge + best-pair 模式显文件名对）
- Modify: `frontend/src/i18n/{index.ts,types.ts}`（注册 bestPair namespace）

估时：8h。依赖：T4（API 端点可用）。

### UI 契约（gemini C1 + C2 吸收）

- **pending**：显示 "尚未计算" + 「计算」按钮（空 case 的 pending empty state → I4）。
- **ready**：显示 top-5 候选，每条含 before 缩略图 + after 缩略图 + `delta_deg`。点击切换**本地高亮**（不写 DB）。底部「确认并写入 override」按钮只有选中本地候选时可用；按下才 POST `/select`。
- **dirty**：候选列表 visible 但**所有选中按钮 disabled** + 顶部 banner "数据已过期，请重算" + 「重新计算」按钮。
- **skipped**：根据 `skipped_reason` 分别展示文案 + 「重新扫描」按钮（调 POST compute）。
- **current_selection**：在顶部一行高亮显示：已选 `b.jpg` ↔ `a.jpg`（delta X°）+ 「进入渲染」按钮。

### i18n namespace

- [ ] **Step 1: Create locale files**

Create `frontend/src/locales/zh/bestPair.json`:

```json
{
  "panelTitle": "最接近姿态对",
  "statusPending": "尚未计算",
  "statusReady": "已就绪",
  "statusDirty": "数据已过期",
  "statusSkipped": "无法自动选图",
  "computeButton": "计算",
  "recomputeButton": "重新计算",
  "confirmSelectionButton": "确认并写入 override",
  "renderButton": "用此对渲染",
  "currentSelectionPrefix": "当前选定：",
  "currentSelectionDelta": "（Δ {{delta}}°）",
  "deltaLabel": "Δ {{delta}}°",
  "candidateBefore": "术前",
  "candidateAfter": "术后",
  "dirtyBannerBody": "源图或 override 改动后缓存已作废，需要重算。",
  "skippedReasons": {
    "no_face_detected": "未识别到人脸，无法自动选图。",
    "no_phase_labels": "无法区分术前 / 术后，请先手动标注 phase。",
    "dir_missing": "case 目录已丢失。"
  },
  "errorStale": "页面数据已过期，请刷新。",
  "errorGeneric": "操作失败：{{detail}}",
  "renderMode": {
    "ai": "AI 渲染",
    "best-pair": "Best-pair 渲染"
  }
}
```

Create `frontend/src/locales/en/bestPair.json`:

```json
{
  "panelTitle": "Closest pose pair",
  "statusPending": "Not computed yet",
  "statusReady": "Ready",
  "statusDirty": "Data stale",
  "statusSkipped": "Cannot auto-pair",
  "computeButton": "Compute",
  "recomputeButton": "Recompute",
  "confirmSelectionButton": "Confirm & write override",
  "renderButton": "Render this pair",
  "currentSelectionPrefix": "Current selection: ",
  "currentSelectionDelta": "(Δ {{delta}}°)",
  "deltaLabel": "Δ {{delta}}°",
  "candidateBefore": "Before",
  "candidateAfter": "After",
  "dirtyBannerBody": "Cache invalidated by source edits; recompute required.",
  "skippedReasons": {
    "no_face_detected": "No face detected; cannot auto-pair.",
    "no_phase_labels": "Cannot distinguish before/after; label phase first.",
    "dir_missing": "Case directory missing."
  },
  "errorStale": "Page data is stale, please refresh.",
  "errorGeneric": "Operation failed: {{detail}}",
  "renderMode": {
    "ai": "AI render",
    "best-pair": "Best-pair render"
  }
}
```

- [ ] **Step 2: Register namespace in i18n**

Edit `frontend/src/i18n/index.ts`:

1. 顶部 import 两个新文件：

```typescript
import zhBestPair from "../locales/zh/bestPair.json";
import enBestPair from "../locales/en/bestPair.json";
```

2. `resources.zh` 对象末尾加 `bestPair: zhBestPair,`。
3. `resources.en` 对象末尾加 `bestPair: enBestPair,`。
4. `ns` 数组追加 `"bestPair"`。

Edit `frontend/src/i18n/types.ts`:

1. 顶部 import: `import bestPair from "../locales/zh/bestPair.json";`
2. `resources` 对象末尾加 `bestPair: typeof bestPair;`

- [ ] **Step 3: Add API functions in api.ts**

在 `frontend/src/api.ts` 末尾（或接 `renderHistorySnapshotUrl` 之后）追加：

```typescript
export interface BestPairCandidate {
  before: string;
  after: string;
  delta_deg: number;
  delta_yaw: number;
  delta_pitch: number;
  delta_roll: number;
}

export interface BestPairCurrentSelection {
  id: number;
  before: string;
  after: string;
  delta_deg: number;
  fingerprint: string | null;
  selected_at: string;
}

export type BestPairStatus = "pending" | "ready" | "dirty" | "skipped";
export type BestPairSkippedReason =
  | "no_face_detected"
  | "no_phase_labels"
  | "dir_missing";

export interface BestPairResponse {
  status: BestPairStatus;
  skipped_reason?: BestPairSkippedReason | null;
  candidates: BestPairCandidate[];
  fingerprint: string | null;
  scanned_at?: string;
  updated_at?: string;
  current_selection: BestPairCurrentSelection | null;
}

export const fetchBestPair = (caseId: number) =>
  api.get<BestPairResponse>(`/api/cases/${caseId}/best-pair`).then((r) => r.data);

export const computeBestPair = (caseId: number) =>
  api.post<BestPairResponse>(`/api/cases/${caseId}/best-pair/compute`).then((r) => r.data);

export const selectBestPair = (
  caseId: number,
  payload: { before: string; after: string; fingerprint: string },
) =>
  api.post<{ selection_id: number }>(`/api/cases/${caseId}/best-pair/select`, payload).then((r) => r.data);

export const renderBestPair = (
  caseId: number,
  params: { brand?: string; template?: string } = {},
) =>
  api.post<{ job_id: number }>(
    `/api/cases/${caseId}/best-pair/render`,
    undefined,
    { params },
  ).then((r) => r.data);

export const batchComputeBestPair = (caseIds: number[]) =>
  api.post<{ batch_id: string; queued: number }>(
    `/api/cases/best-pair/batch-compute`,
    { case_ids: caseIds },
  ).then((r) => r.data);

export interface BestPairBatchStatus {
  batch_id: string;
  total: number;
  done: number;
  failed: number;
  errors: Array<{ case_id: number; error: string }>;
  status: "queued" | "running" | "done";
}

export const fetchBestPairBatchStatus = (batchId: string) =>
  api.get<BestPairBatchStatus>(`/api/cases/best-pair/batch-compute/${batchId}`).then((r) => r.data);

export const bestPairThumbnailUrl = (caseId: number, filename: string) =>
  `${apiBase}/api/cases/${caseId}/files/thumb?path=${encodeURIComponent(filename)}`;
```

> 注意：`bestPairThumbnailUrl` 依赖已有文件缩略图 endpoint。若路径不匹配，`grep -n "thumb" backend/routes/cases.py` 确认实际 URL 模板。

- [ ] **Step 4: Add query hooks in hooks/queries.ts**

在 `frontend/src/hooks/queries.ts` 顶部 import 段追加新 API fn：

```typescript
import {
  fetchBestPair,
  computeBestPair,
  selectBestPair,
  renderBestPair,
  batchComputeBestPair,
  fetchBestPairBatchStatus,
} from "../api";
```

在文件末尾追加：

```typescript
export function useBestPair(caseId: number, enabled = true) {
  return useQuery({
    queryKey: ["best-pair", caseId],
    queryFn: () => fetchBestPair(caseId),
    enabled: enabled && caseId > 0,
    refetchInterval: (q) => {
      const data = q.state.data as any;
      return data?.status === "pending" ? 3000 : false;
    },
  });
}

export function useComputeBestPair() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (caseId: number) => computeBestPair(caseId),
    onSuccess: (_data, caseId) => {
      qc.invalidateQueries({ queryKey: ["best-pair", caseId] });
    },
  });
}

export function useSelectBestPair() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      caseId: number;
      before: string;
      after: string;
      fingerprint: string;
    }) =>
      selectBestPair(vars.caseId, {
        before: vars.before,
        after: vars.after,
        fingerprint: vars.fingerprint,
      }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["best-pair", vars.caseId] });
      qc.invalidateQueries({ queryKey: ["case", vars.caseId] });
    },
  });
}

export function useRenderBestPair() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { caseId: number; brand?: string; template?: string }) =>
      renderBestPair(vars.caseId, { brand: vars.brand, template: vars.template }),
    onSuccess: (_data, vars) => {
      qc.invalidateQueries({ queryKey: ["render-history", vars.caseId] });
    },
  });
}

export function useBatchComputeBestPair() {
  return useMutation({
    mutationFn: (caseIds: number[]) => batchComputeBestPair(caseIds),
  });
}

export function useBestPairBatchStatus(batchId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["best-pair-batch", batchId],
    queryFn: () => (batchId ? fetchBestPairBatchStatus(batchId) : Promise.reject("no id")),
    enabled: enabled && !!batchId,
    refetchInterval: (q) => {
      const data = q.state.data as any;
      return data?.status === "done" ? false : 1500;
    },
  });
}
```

- [ ] **Step 5: Create BestPairCandidateCard component**

Create `frontend/src/components/BestPairPanel/BestPairCandidateCard.tsx`:

```typescript
import { useTranslation } from "react-i18next";
import type { BestPairCandidate } from "../../api";
import { bestPairThumbnailUrl } from "../../api";

export interface Props {
  caseId: number;
  candidate: BestPairCandidate;
  selected: boolean;
  disabled: boolean;
  onClick: () => void;
}

export function BestPairCandidateCard({ caseId, candidate, selected, disabled, onClick }: Props) {
  const { t } = useTranslation("bestPair");
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      aria-pressed={selected}
      data-testid={`best-pair-candidate-${candidate.before}-${candidate.after}`}
      className={`flex gap-3 p-2 border rounded transition ${
        selected ? "border-amber-500 bg-amber-50" : "border-gray-200 bg-white"
      } ${disabled ? "opacity-50 cursor-not-allowed" : "hover:border-amber-300"}`}
    >
      <div className="flex flex-col items-center">
        <img
          src={bestPairThumbnailUrl(caseId, candidate.before)}
          alt={candidate.before}
          className="w-20 h-20 object-cover rounded border"
        />
        <span className="text-xs mt-1">{t("candidateBefore")}</span>
      </div>
      <div className="flex flex-col items-center">
        <img
          src={bestPairThumbnailUrl(caseId, candidate.after)}
          alt={candidate.after}
          className="w-20 h-20 object-cover rounded border"
        />
        <span className="text-xs mt-1">{t("candidateAfter")}</span>
      </div>
      <div className="flex flex-col justify-center text-sm">
        <span>{t("deltaLabel", { delta: candidate.delta_deg.toFixed(2) })}</span>
        <span className="text-xs text-gray-500">
          yaw {candidate.delta_yaw.toFixed(1)} / pitch {candidate.delta_pitch.toFixed(1)} / roll {candidate.delta_roll.toFixed(1)}
        </span>
      </div>
    </button>
  );
}
```

- [ ] **Step 6: Create BestPairPanel main component**

Create `frontend/src/components/BestPairPanel/BestPairPanel.tsx`:

```typescript
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  useBestPair,
  useComputeBestPair,
  useSelectBestPair,
  useRenderBestPair,
} from "../../hooks/queries";
import { BestPairCandidateCard } from "./BestPairCandidateCard";

export interface Props {
  caseId: number;
  brand: string;
}

export function BestPairPanel({ caseId, brand }: Props) {
  const { t } = useTranslation("bestPair");
  const { data, isLoading, error } = useBestPair(caseId);
  const computeMut = useComputeBestPair();
  const selectMut = useSelectBestPair();
  const renderMut = useRenderBestPair();
  const [localPick, setLocalPick] = useState<{ before: string; after: string } | null>(null);

  const status = data?.status ?? "pending";
  const candidates = data?.candidates ?? [];
  const fingerprint = data?.fingerprint ?? null;
  const current = data?.current_selection;

  const canSelect = status === "ready" && localPick !== null && fingerprint !== null;
  const canRender = current !== null && current !== undefined;

  const skippedReason = data?.skipped_reason;
  const skippedText = useMemo(() => {
    if (!skippedReason) return "";
    return t(`skippedReasons.${skippedReason}`);
  }, [skippedReason, t]);

  if (isLoading) return <div className="p-4 text-sm text-gray-500">Loading...</div>;
  if (error) return <div className="p-4 text-sm text-red-600">{String(error)}</div>;

  return (
    <section data-testid="best-pair-panel" className="border rounded p-3 mb-4">
      <header className="flex items-center justify-between mb-2">
        <h3 className="font-medium text-sm">{t("panelTitle")}</h3>
        <span
          data-testid="best-pair-status"
          className={`text-xs px-2 py-0.5 rounded ${
            status === "ready" ? "bg-green-100 text-green-800" :
            status === "dirty" ? "bg-amber-100 text-amber-800" :
            status === "skipped" ? "bg-gray-100 text-gray-700" :
            "bg-blue-100 text-blue-800"
          }`}
        >
          {t(`status${status.charAt(0).toUpperCase() + status.slice(1)}` as any)}
        </span>
      </header>

      {current && (
        <div className="mb-3 p-2 bg-amber-50 border border-amber-200 rounded text-sm">
          <span>{t("currentSelectionPrefix")}</span>
          <code>{current.before}</code> ↔ <code>{current.after}</code>
          <span className="ml-1 text-gray-500">
            {t("currentSelectionDelta", { delta: current.delta_deg.toFixed(2) })}
          </span>
          <button
            type="button"
            data-testid="best-pair-render-button"
            disabled={!canRender || renderMut.isPending}
            onClick={() => renderMut.mutate({ caseId, brand })}
            className="ml-3 px-2 py-1 border rounded text-xs hover:bg-amber-100"
          >
            {t("renderButton")}
          </button>
        </div>
      )}

      {status === "dirty" && (
        <div data-testid="best-pair-dirty-banner" className="mb-2 p-2 bg-amber-100 border border-amber-300 rounded text-sm">
          <p>{t("dirtyBannerBody")}</p>
          <button
            type="button"
            data-testid="best-pair-recompute-button"
            onClick={() => computeMut.mutate(caseId)}
            disabled={computeMut.isPending}
            className="mt-1 px-2 py-1 border rounded text-xs hover:bg-amber-200"
          >
            {t("recomputeButton")}
          </button>
        </div>
      )}

      {status === "pending" && (
        <div className="py-4 text-center">
          <p className="text-sm text-gray-500 mb-2">{t("statusPending")}</p>
          <button
            type="button"
            data-testid="best-pair-compute-button"
            onClick={() => computeMut.mutate(caseId)}
            disabled={computeMut.isPending}
            className="px-3 py-1 border rounded text-sm hover:bg-gray-50"
          >
            {t("computeButton")}
          </button>
        </div>
      )}

      {status === "skipped" && (
        <div className="py-3">
          <p className="text-sm text-gray-600 mb-2">{skippedText}</p>
          <button
            type="button"
            data-testid="best-pair-recompute-button"
            onClick={() => computeMut.mutate(caseId)}
            disabled={computeMut.isPending}
            className="px-2 py-1 border rounded text-xs hover:bg-gray-50"
          >
            {t("recomputeButton")}
          </button>
        </div>
      )}

      {(status === "ready" || status === "dirty") && candidates.length > 0 && (
        <>
          <div className="flex flex-col gap-2">
            {candidates.map((c) => (
              <BestPairCandidateCard
                key={`${c.before}-${c.after}`}
                caseId={caseId}
                candidate={c}
                selected={localPick?.before === c.before && localPick?.after === c.after}
                disabled={status === "dirty" || selectMut.isPending}
                onClick={() => setLocalPick({ before: c.before, after: c.after })}
              />
            ))}
          </div>
          <button
            type="button"
            data-testid="best-pair-confirm-button"
            disabled={!canSelect || selectMut.isPending}
            onClick={() =>
              localPick && fingerprint &&
              selectMut.mutate({ caseId, before: localPick.before, after: localPick.after, fingerprint })
            }
            className="mt-3 px-3 py-1 bg-amber-500 text-white rounded disabled:bg-gray-300 text-sm"
          >
            {t("confirmSelectionButton")}
          </button>
        </>
      )}
    </section>
  );
}
```

Create `frontend/src/components/BestPairPanel/useBestPair.ts`（re-export barrel — 保证 CaseDetail 侧 import path 一致）:

```typescript
export { BestPairPanel } from "./BestPairPanel";
```

- [ ] **Step 7: Mount BestPairPanel in CaseDetail.tsx**

在 `frontend/src/pages/CaseDetail.tsx:49` 之后（`import { ImageOverridePopover }` 附近）加：

```typescript
import { BestPairPanel } from "../components/BestPairPanel/BestPairPanel";
```

在 CaseDetail 主体 JSX 渲染处（合适的位置，例如在 ImageOverridePopover 同级 / 图像网格上方 — engineer 实施时用 grep 看 caseDetail 现有组件插入位置）插入：

```tsx
<BestPairPanel caseId={caseId} brand={brand} />
```

注意 `brand` 已由 `const brand = useBrand();` 在 line 88 拿到，可直接使用。

- [ ] **Step 8: Add render_mode badge to RenderHistoryDrawer**

Modify `frontend/src/components/RenderHistoryDrawer.tsx` 列表项（`items.map`）里每条 snapshot 渲染加 badge。首先需要 snapshot 响应里带 render_mode — T5 Step 6 处理了 backend；若 backend 未 join，前端 fallback：不显示 badge。

在列表项 JSX 附近（点击进 lightbox 那段）前插入：

```tsx
{snapshot.render_mode === "best-pair" && (
  <span
    data-testid={`history-badge-${snapshot.archived_at}`}
    className="text-xs px-1.5 py-0.5 bg-amber-100 text-amber-800 rounded ml-2"
  >
    {t("renderMode.best-pair", { ns: "bestPair", defaultValue: "Best-pair" })}
  </span>
)}
```

若 render_mode === "best-pair" 同时有 `selection_before` / `selection_after` 字段（若 backend 附带），显示文件名对：

```tsx
{snapshot.render_mode === "best-pair" && snapshot.selection_before && (
  <span className="text-xs text-gray-500 ml-2">
    <code>{snapshot.selection_before}</code> ↔ <code>{snapshot.selection_after}</code>
  </span>
)}
```

在 `frontend/src/api.ts` 的 `RenderHistorySnapshot` 类型定义（grep 定位）里加可选字段：

```typescript
render_mode?: "ai" | "best-pair";
selection_before?: string;
selection_after?: string;
```

- [ ] **Step 9: Type-check + lint**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench/frontend
npm run type-check
npm run lint
```

Expected: 0 error（warning 与 baseline 一致）。

- [ ] **Step 10: Browser smoke**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
./start.sh  # backend on 5291 + frontend on 5292
```

打开 `http://127.0.0.1:5292/cases/<some-id>` 看 BestPairPanel 渲染：

- 首次无数据 → `statusPending` + compute 按钮可点。
- compute 后进 `ready`：展示候选，点候选高亮但 DB 不变（验证：浏览器 devtools 看 network 无 POST）。
- 点「确认并写入 override」→ POST `/select` 200 → panel 刷新 → 顶部 current selection 出现。
- 手动在 ImageOverridePopover 改任一 override → panel `status` 变 `dirty` → 确认按钮禁用 + banner 展示。
- 点「用此对渲染」→ render 任务入队 → RenderHistoryDrawer 新条目带 `Best-pair` badge。

- [ ] **Step 11: Commit**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git add frontend/src/components/BestPairPanel/ \
  frontend/src/locales/zh/bestPair.json frontend/src/locales/en/bestPair.json \
  frontend/src/i18n/ frontend/src/api.ts frontend/src/hooks/queries.ts \
  frontend/src/pages/CaseDetail.tsx frontend/src/components/RenderHistoryDrawer.tsx
git commit -m "feat(best-pair): T6 BestPairPanel UI + history mode badge + bestPair i18n"
```

---

## Task 7: Playwright E2E + pytest 端到端

**Files:**
- Create: `frontend/tests/e2e/best-pair-basic.spec.ts`
- Create: `frontend/tests/e2e/best-pair-dirty.spec.ts`
- Create: `frontend/tests/e2e/best-pair-skipped.spec.ts`
- Create: `backend/tests/test_best_pair_e2e.py`（端到端 service → render_queue → 模拟 manifest 读 override）
- Test fixtures: 复用 existing `frontend/tests/e2e/fixtures/`（若无则新建 `best-pair-seed.sql`）

估时：4h。依赖：T1-T6 全部。

**E2E 策略**：MediaPipe 分析重量级，不跑真实模型。通过 **pytest fixture 直接 seed `case_best_pairs` + `case_image_overrides` + 预置 case 目录（含几个 dummy jpg）**，让 Playwright 走 compute → dirty → select → render 的**UI 动作路径**但 compute endpoint 返回 mock 结果（Playwright `page.route` 拦截 `/best-pair/compute` 返回 stub）。

- [ ] **Step 1: Write pytest end-to-end (without MediaPipe)**

Create `backend/tests/test_best_pair_e2e.py`:

```python
"""端到端：POST compute (mock 分析) → POST select → POST render → 验证 render_job
确实读 case_image_overrides 锁定到选定 pair。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_compute_select_render_round_trip(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    # 1. Seed case + 4 images
    case_dir = tmp_path / "case-e2e"
    case_dir.mkdir()
    for name in ["before-1.jpg", "before-2.jpg", "after-1.jpg", "after-2.jpg"]:
        (case_dir / name).write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)  # JPEG magic + padding
    now = _now()
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
            "VALUES (1, ?, ?, '/tmp', 1, 'test')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO cases (id, abs_path, scan_id, category, status, first_seen_at, updated_at) "
            "VALUES (1, ?, 1, 'A', 'active', ?, ?)",
            (str(case_dir), now, now),
        )
        # Pre-label via overrides so _partition_phases works without keyword guesser
        for name, phase in [
            ("before-1.jpg", "before"), ("before-2.jpg", "before"),
            ("after-1.jpg", "after"), ("after-2.jpg", "after"),
        ]:
            conn.execute(
                "INSERT INTO case_image_overrides (case_id, filename, manual_phase, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (name, phase, now),
            )

    # 2. Compute with mocked pose analysis
    poses = {
        "before-1.jpg": {"yaw": 3.0, "pitch": 1.0, "roll": 0.5},
        "before-2.jpg": {"yaw": 10.0, "pitch": 5.0, "roll": 2.0},
        "after-1.jpg": {"yaw": 3.1, "pitch": 1.05, "roll": 0.55},   # closest to before-1
        "after-2.jpg": {"yaw": 8.0, "pitch": 4.0, "roll": 1.5},
    }
    with patch("backend.services.best_pair_service._analyze_faces", return_value=poses):
        r = client.post("/api/cases/1/best-pair/compute")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    top1 = body["candidates"][0]
    assert top1["before"] == "before-1.jpg"
    assert top1["after"] == "after-1.jpg"
    fp = body["fingerprint"]

    # 3. Select top-1
    r = client.post(
        "/api/cases/1/best-pair/select",
        json={"before": top1["before"], "after": top1["after"], "fingerprint": fp},
    )
    assert r.status_code == 200

    # 4. Verify overrides now lock to selected pair (phase forced via writer skip_dirty)
    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT filename, manual_phase FROM case_image_overrides WHERE case_id=1 ORDER BY filename"
        ).fetchall()
    phases_by_file = {r[0]: r[1] for r in rows}
    # Selected pair has explicit before/after
    assert phases_by_file["before-1.jpg"] == "before"
    assert phases_by_file["after-1.jpg"] == "after"

    # 5. Verify best-pair cache still 'ready' (skip_dirty_mark=True)
    r = client.get("/api/cases/1/best-pair")
    assert r.json()["status"] == "ready"

    # 6. Trigger render with mocked RENDER_QUEUE (we only check the enqueue call args)
    with patch("backend.render_queue.RENDER_QUEUE.enqueue", return_value=99) as mock:
        r = client.post("/api/cases/1/best-pair/render")
    assert r.status_code == 200
    assert r.json()["job_id"] == 99
    kwargs = mock.call_args.kwargs
    assert kwargs["render_mode"] == "best-pair"
    assert kwargs["best_pair_selection_id"] is not None


def test_override_write_marks_best_pair_dirty_e2e(client: TestClient, temp_db: Path, tmp_path: Path) -> None:
    """PATCH override → best-pair cache 变 dirty。"""
    case_dir = tmp_path / "case-dirty"
    case_dir.mkdir()
    (case_dir / "img.jpg").write_bytes(b"\xff\xd8")
    now = _now()
    with sqlite3.connect(temp_db) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scans (id, started_at, completed_at, root_paths, case_count, mode) "
            "VALUES (1, ?, ?, '/tmp', 1, 'test')",
            (now, now),
        )
        conn.execute(
            "INSERT INTO cases (id, abs_path, scan_id, category, status, first_seen_at, updated_at) "
            "VALUES (1, ?, 1, 'A', 'active', ?, ?)",
            (str(case_dir), now, now),
        )
        # Pre-seed best-pair cache as ready
        conn.execute(
            "INSERT INTO case_best_pairs (case_id, status, candidates_json, candidates_fingerprint, "
            "source_version, scanned_at, updated_at) VALUES (1, 'ready', '[]', 'fp', 0, ?, ?)",
            (now, now),
        )
    # PATCH override via existing cases endpoint — goes through image_override_writer
    r = client.patch(
        "/api/cases/1/images/img.jpg",
        json={"manual_phase": "before", "manual_view": None},
    )
    # Should return 200 or 201 depending on endpoint; check either is fine.
    assert r.status_code in (200, 201)
    # Cache should now be dirty
    r = client.get("/api/cases/1/best-pair")
    assert r.json()["status"] == "dirty"
```

- [ ] **Step 2: Run pytest e2e**

Run: `pytest backend/tests/test_best_pair_e2e.py -v`

Expected: 2 passed.

- [ ] **Step 3: Write Playwright e2e — basic happy path**

Create `frontend/tests/e2e/best-pair-basic.spec.ts`:

```typescript
import { test, expect } from "@playwright/test";

test.describe("best-pair basic flow", () => {
  test("compute → select → render button appears", async ({ page }) => {
    // Use a seeded case ID — the fixture DB should have case-id 126 from existing setup.
    const caseId = 126;

    // Intercept compute endpoint to return a deterministic ready response
    await page.route(`**/api/cases/${caseId}/best-pair/compute`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ready",
          candidates: [
            { before: "术前1.jpeg", after: "术后1.jpeg", delta_deg: 1.5, delta_yaw: 0.5, delta_pitch: 1.0, delta_roll: 0.2 },
            { before: "术前1.jpeg", after: "术后2.jpeg", delta_deg: 3.0, delta_yaw: 1.5, delta_pitch: 2.0, delta_roll: 0.5 },
          ],
          fingerprint: "deadbeef",
          current_selection: null,
          scanned_at: "2026-05-09T10:00:00Z",
          updated_at: "2026-05-09T10:00:00Z",
        }),
      }),
    );

    // Intercept initial GET — start as pending
    await page.route(`**/api/cases/${caseId}/best-pair`, (route, request) => {
      if (request.method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "pending",
            candidates: [],
            fingerprint: null,
            current_selection: null,
          }),
        });
      }
      return route.continue();
    });

    // Intercept select
    await page.route(`**/api/cases/${caseId}/best-pair/select`, (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ selection_id: 42 }),
      }),
    );

    await page.goto(`/cases/${caseId}`);
    await expect(page.getByTestId("best-pair-panel")).toBeVisible();
    await expect(page.getByTestId("best-pair-compute-button")).toBeVisible();

    // After compute the server would be 'ready'; swap GET response
    let afterCompute = false;
    await page.unroute(`**/api/cases/${caseId}/best-pair`);
    await page.route(`**/api/cases/${caseId}/best-pair`, (route, request) => {
      if (request.method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(
            afterCompute
              ? {
                  status: "ready",
                  candidates: [
                    { before: "术前1.jpeg", after: "术后1.jpeg", delta_deg: 1.5, delta_yaw: 0.5, delta_pitch: 1.0, delta_roll: 0.2 },
                  ],
                  fingerprint: "deadbeef",
                  current_selection: null,
                }
              : {
                  status: "pending",
                  candidates: [],
                  fingerprint: null,
                  current_selection: null,
                },
          ),
        });
      }
      return route.continue();
    });

    await page.getByTestId("best-pair-compute-button").click();
    afterCompute = true;
    await expect(page.getByTestId("best-pair-status")).toHaveText(/ready|就绪/i);

    // Click the top candidate
    await page.getByTestId("best-pair-candidate-术前1.jpeg-术后1.jpeg").click();
    await expect(page.getByTestId("best-pair-confirm-button")).toBeEnabled();

    // Confirm
    await page.getByTestId("best-pair-confirm-button").click();
    // After select, the mock GET still returns ready without current_selection — that's fine for smoke
    await expect(page.getByTestId("best-pair-confirm-button")).toBeVisible();
  });
});
```

- [ ] **Step 4: Write e2e dirty flow**

Create `frontend/tests/e2e/best-pair-dirty.spec.ts`:

```typescript
import { test, expect } from "@playwright/test";

test("dirty state disables confirm and shows banner", async ({ page }) => {
  const caseId = 126;
  await page.route(`**/api/cases/${caseId}/best-pair`, (route, request) => {
    if (request.method() !== "GET") return route.continue();
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "dirty",
        candidates: [
          { before: "a.jpg", after: "b.jpg", delta_deg: 1.0, delta_yaw: 0, delta_pitch: 1, delta_roll: 0 },
        ],
        fingerprint: null,
        current_selection: null,
      }),
    });
  });

  await page.goto(`/cases/${caseId}`);
  await expect(page.getByTestId("best-pair-dirty-banner")).toBeVisible();
  await expect(page.getByTestId("best-pair-recompute-button")).toBeVisible();
  // Candidate cards should exist but be disabled
  const candidate = page.getByTestId("best-pair-candidate-a.jpg-b.jpg");
  await expect(candidate).toBeDisabled();
  // Confirm button not enabled (because localPick can't be set when disabled)
  await expect(page.getByTestId("best-pair-confirm-button")).toBeDisabled();
});
```

- [ ] **Step 5: Write e2e skipped flow**

Create `frontend/tests/e2e/best-pair-skipped.spec.ts`:

```typescript
import { test, expect } from "@playwright/test";

test.describe("skipped states render reason-specific copy", () => {
  const reasons: Array<[string, RegExp]> = [
    ["no_face_detected", /未识别到人脸|No face detected/i],
    ["no_phase_labels", /术前 \/ 术后|before\/after/i],
    ["dir_missing", /目录已丢失|directory missing/i],
  ];
  for (const [reason, pattern] of reasons) {
    test(`skipped: ${reason}`, async ({ page }) => {
      const caseId = 126;
      await page.route(`**/api/cases/${caseId}/best-pair`, (route) =>
        route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            status: "skipped",
            skipped_reason: reason,
            candidates: [],
            fingerprint: null,
            current_selection: null,
          }),
        }),
      );
      await page.goto(`/cases/${caseId}`);
      await expect(page.getByTestId("best-pair-panel")).toContainText(pattern);
    });
  }
});
```

- [ ] **Step 6: Run Playwright**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench/frontend
npx playwright test tests/e2e/best-pair-basic.spec.ts tests/e2e/best-pair-dirty.spec.ts tests/e2e/best-pair-skipped.spec.ts
```

Expected: 5 passed（basic 1 + dirty 1 + skipped 3 reasons = 5）。

- [ ] **Step 7: Full E2E regression**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench/frontend
npx playwright test
```

Expected: 既有 36 + 5 = 41 passed。若 route 拦截污染到其它测试（shared webServer + reuseExistingServer），确认 Playwright 的 `page.route` 是 per-page 不跨 spec。

- [ ] **Step 8: Full pytest regression**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
pytest backend/tests -v --tb=short
```

Expected: 既有 ~200 + 新增 ~28（T1 7 + T2 8 + T3 10 + T4 10 + T5 5 + T7 2）= 全绿。

- [ ] **Step 9: Commit**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git add backend/tests/test_best_pair_e2e.py \
  frontend/tests/e2e/best-pair-basic.spec.ts \
  frontend/tests/e2e/best-pair-dirty.spec.ts \
  frontend/tests/e2e/best-pair-skipped.spec.ts
git commit -m "test(best-pair): T7 Playwright 5 specs + pytest round-trip e2e"
```

---

## 收尾：阶段 commit + push

- [ ] **Final: full stack check**

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
pytest backend/tests -x
cd frontend
npm run type-check
npm run lint
npx playwright test
```

全绿则：

```bash
cd /Users/a1234/Desktop/案例生成器/case-workbench
git log --oneline origin/main..HEAD  # 应该是 7 commits（T1..T7）
git push origin main
```

---

## Spec 覆盖自检

对照 `docs/superpowers/specs/2026-05-09-best-pair-routing-design.md`：

| Spec 段 | 覆盖 task |
|---|---|
| §2 状态机（pending/ready/dirty/skipped） | T1 schema + T3 service |
| §3.1 `case_best_pairs` 表（无 dirty 列 / 无 is_current） | T1 |
| §3.2 `case_best_pair_selections` 表 + override_before snapshot | T1 + T3 select |
| §3.3 `render_jobs` 加 render_mode + best_pair_selection_id | T1 + T5 |
| §4.1 image_override_writer 统一入口 + skip_dirty_mark | T2 |
| §4.2 cases.py 4 call site 改造 | T2 Step 6 |
| §4.3 image_workbench.py 3 call site + trash 改造 | T2 Step 7 |
| §5.1 compute 算法 + source_version race | T3 Step 4 |
| §5.2 select + fingerprint 校验 + _resolve_existing_source | T3 Step 8 |
| §5.3 compute_queue shared _job_pool 串行 | T3 Step 10 |
| §6 6 endpoints | T4 |
| §7 render_executor 透传（不加策略分支） | T5（codebase 事实修正：无 enhance.js 调用可跳） |
| §8.1 BestPairPanel 两步交互（gemini C1） | T6 Step 6 |
| §8.2 dirty banner + disabled (gemini C2) | T6 Step 6 |
| §8.3 RenderHistoryDrawer mode badge (gemini W2) | T6 Step 8 |
| §8.4 3 种 skipped_reason 文案 (gemini W3) | T6 Step 1 locale |
| §8.5 batch 进度轮询 (gemini W4) | T6 Step 4 useBestPairBatchStatus |
| §9 测试策略 | T7 |
| §10 MVP 边界 | 全 plan 不触碰 cases 列表 has_best_pair / cron / top-5 可配 / 加权 / revert flow / SSE |

**无 gap**。

## No Placeholder 扫描

- T1-T7 每个 step 都含具体代码块或 exact commands — 无 TBD / "类似 Task N" / "add error handling"。
- DB schema SQL 完整写出 + 迁移 helper 代码完整。
- Service 层 compute_best_pair / select_best_pair_for_case 代码完整。
- Route 6 端点 handler 完整。
- 前端 BestPairPanel / BestPairCandidateCard 全 JSX 完整。
- E2E spec 3 个完整 fixture + 路由拦截。

## 类型一致性

- `render_mode: "ai" | "best-pair"` 统一字面量值（db 默认 `'ai'` / service `render_mode="best-pair"` / 前端 `BestPairCandidate` 之外没出现大小写或 underscore 版本）。
- `skipped_reason`: `"no_face_detected" | "no_phase_labels" | "dir_missing"` — db / service / API 响应 / 前端 locale key 一致。
- `BestPairCandidate` 字段 snake_case（`delta_deg` / `delta_yaw`）在前后端一致（前端 TS interface 写 snake_case 字段与 FastAPI 响应一致，避免 camelCase 转换 overhead）。
- Fingerprint 字段名 `candidates_fingerprint`（DB）/ `fingerprint`（API 响应 + 前端）— 映射固定在 `list_best_pair` 序列化层，前后一致。
- `selection_id` 返回字段（select endpoint）跟数据库 PK `id` 对齐。

---

**Plan 完成**。保存至 `docs/superpowers/plans/2026-05-09-best-pair-routing.md`。

---

# v3 Delta — Round 2 review absorption (binding override)

> **优先级声明**：本节以 spec v3（commit `f94df1d`）为唯一真实来源。当本节指令与 T1–T7 正文冲突时，以本节为准。engineer 按 T1–T7 执行过程中，遇到下列 delta 点必须按此节替换或追加，不得沿用正文。

spec v3 新增 6 Critical + 12 Important，本节把 Critical 全部绑定到具体 task step，把 Important 绑到 acceptance criteria。

## 全局 delta

- **R6 call-site 数量**：把正文"4 call sites"/"7 call sites"全部视为 **5 处唯一 Python 函数**——
  - `backend/routes/cases.py` 内 PATCH helper `_write_image_override` 函数体 + trash DELETE 路径（`:1478` 附近）的 2 个函数
  - `backend/routes/image_workbench.py` 内 upload / override / delete handler 3 个函数
- **R7 模块拓扑**：`mark_best_pair_dirty` 必须放在独立 leaf 模块 `backend/services/best_pair_dirty.py`，**不**再放 `image_override_writer.py` 内部（正文 T2 Step 4 的 `_mark_best_pair_dirty` 私有实现作废）。
- **R7 compute pool 独立**：不再共享 `backend/_job_pool._job_pool`。新增 `backend/_best_pair_compute_pool.py`（T3）：`ThreadPoolExecutor(max_workers=1, thread_name_prefix='best-pair-compute')`。
- **R7 `status` 枚举**：新增 `'empty'`（compute 实际跑完但候选池为空）；与既有 `'skipped'`（pre-compute 即知无需算）区分。
- **命名去歧义**：spec v3 里的 `select_best_pair` service 函数，在实现时重命名为 `select_best_pair_candidate`，避免与既有 `backend/source_selection.py:1078 select_best_pair`（AI 路径姿态配对函数）冲突。endpoint path 不变，只改 Python 函数名。

## Task 1 delta — `render_jobs` 加三列（spec §4.x）

原 Step 要求加 `render_mode` + `best_pair_selection_id`。v3 **多加一列** `candidates_fingerprint_snapshot`：

```python
# backend/db.py _ensure_render_job_columns 内追加：
if "candidates_fingerprint_snapshot" not in cols:
    conn.execute(
        "ALTER TABLE render_jobs ADD COLUMN candidates_fingerprint_snapshot TEXT"
    )
```

迁移测试（`test_best_pair_migration.py`）须断言三列都在。

## Task 2 delta — 三件套重构

### D2.1 R1 dirty 触发白名单（替换正文 Step 2 dirty 写法）

`write_image_override` / `delete_image_override` **不**再无条件调 `mark_best_pair_dirty`。改为：

1. 在 writer 内部收集 `changed_fields: tuple[str, ...]`（本次写入实际变动的字段，例如只写 `manual_phase` → `('manual_phase',)`；delete 一行 → `('manual_phase', 'manual_view', 'manual_transform_json')`）
2. 调 `mark_best_pair_dirty(conn, case_id=case_id, filename=filename, changed_fields=changed_fields)`（新签名，见 R7）
3. `mark_best_pair_dirty` 内部按以下表决定是否 UPDATE `case_best_pairs`：

| 条件 | 是否 mark_dirty |
|---|---|
| `manual_phase` 或 `manual_view` ∈ changed_fields | ✅ 是 |
| filename ∈ 当前 `case_best_pairs.candidates_json` 的 before/after 并集 且 changed_fields ≠ ∅ | ✅ 是 |
| 仅 `manual_transform_json` 改且 filename ∉ 候选并集 | ❌ 否 |
| `case_best_pairs` 行不存在 | ❌ 否（保持 lazy） |
| caller 传 `skip_dirty_mark=True` | ❌ 否 |

`_candidate_filenames(candidates_json: str) -> set[str]`：`json.loads` + 取每条 dict 的 `before` / `after` 值。坏 JSON → 空集。

dirty UPDATE 写法（保持正文已有精神）：
```sql
UPDATE case_best_pairs
SET status='dirty', source_version=source_version+1, updated_at=?
WHERE case_id=?;
```

### D2.2 R7 `best_pair_dirty.py` 独立 leaf

Create `backend/services/best_pair_dirty.py`（不导入任何 `backend.services.*`）：

```python
from __future__ import annotations
import json, sqlite3
from datetime import datetime, timezone
from typing import Iterable

_FIELDS_ALWAYS_DIRTY = frozenset({"manual_phase", "manual_view"})

def _candidate_filenames(candidates_json: str) -> set[str]:
    try:
        data = json.loads(candidates_json or "[]")
    except json.JSONDecodeError:
        return set()
    out: set[str] = set()
    for row in data:
        if isinstance(row, dict):
            for key in ("before", "after"):
                val = row.get(key)
                if isinstance(val, str):
                    out.add(val)
    return out

def _should_mark(conn, *, case_id: int, filename: str, changed_fields) -> bool:
    fields = set(changed_fields or ())
    if not fields:
        return False
    if fields & _FIELDS_ALWAYS_DIRTY:
        return True
    row = conn.execute(
        "SELECT candidates_json FROM case_best_pairs WHERE case_id=?",
        (case_id,),
    ).fetchone()
    if row is None:
        return False
    return filename in _candidate_filenames(row[0] if isinstance(row, tuple) else row["candidates_json"])

def mark_best_pair_dirty(conn: sqlite3.Connection, *, case_id: int, filename: str, changed_fields: Iterable[str]) -> bool:
    if not _should_mark(conn, case_id=case_id, filename=filename, changed_fields=changed_fields):
        return False
    cur = conn.execute(
        "UPDATE case_best_pairs SET status='dirty', source_version=source_version+1, updated_at=? WHERE case_id=?",
        (datetime.now(timezone.utc), case_id),
    )
    return cur.rowcount > 0
```

`image_override_writer.py` 顶部改导入：`from backend.services.best_pair_dirty import mark_best_pair_dirty`。正文 `_mark_best_pair_dirty` 私有函数删除。

### D2.3 R1/R7 新增测试（追加到 `test_best_pair_dirty.py` 新文件）

必须覆盖 6 条白名单用例：filename in candidates + transform-only、 manual_phase always、 manual_view always、unrelated transform-only = no dirty、no-row = noop、empty changed_fields = no dirty。

### D2.4 R6 T2 验收硬标准（追加到 Task 2 收尾 Step）

在 Task 2 最后一个 Step 前插入验收 Step：

```bash
# 1) routes 层不应再有直接写 case_image_overrides 的 SQL
grep -rn "INSERT INTO case_image_overrides\|UPDATE case_image_overrides\|DELETE FROM case_image_overrides" backend/routes/
# 期望输出：0 行（非 0 则 T2 未完成）

# 2) 5 个唯一 Python 函数都导入 writer
grep -rn "from backend.services.image_override_writer import" backend/routes/
# 期望：≥5 行命中（每个函数内至少一次导入）
```

两条同时过才算 T2 结束。

## Task 3 delta — compute / select 写 DB 三件套

### D3.1 R3 `compute_best_pair` UPSERT + observed_version guard

正文 Step 4 里的写回必须改为：

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

读 `observed_version`（行不存在时为 0）作为最后一个绑定；新 `source_version` 写 `max(1, observed_version + 1)`。ON CONFLICT WHERE 不命中 → `cursor.rowcount == 0` → 返回 `{"recomputed": False, "reason": "superseded"}`；调用方（endpoint / batch worker）决定重试与否。

空候选池的结果改为：`UPSERT ... status='empty', candidates_json='[]', candidates_fingerprint=hash("empty@<source_version>")`（枚举新增见全局 delta）。

### D3.2 R2 `select_best_pair_candidate` 事务内 bump source_version

正文 Step 8 原描述的 UPSERT 2 行 + INSERT selection 后，**追加** step（在同一事务内、commit 前）：

```sql
UPDATE case_best_pairs
SET status = 'ready',
    source_version = source_version + 1,
    candidates_fingerprint = ?,   -- 保持与选定时的 fingerprint 一致
    updated_at = CURRENT_TIMESTAMP
WHERE case_id = ?;
```

并发效果：任何持旧 observed_version 的 compute 在此之后写回时 WHERE 不命中 → 静默丢弃（superseded 路径）。

override 写必须走 `image_override_writer.write_image_override(conn, ..., skip_dirty_mark=True)` 而不是正文自写 SQL，保证 R1 白名单不误伤自己刚选的 selection。

### D3.3 R7 独立 compute pool

Create `backend/_best_pair_compute_pool.py`：

```python
from concurrent.futures import ThreadPoolExecutor
_best_pair_compute_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='best-pair-compute')
```

`best_pair_compute_queue` 在正文描述的 "shared `_job_pool`" 改为 **`from backend._best_pair_compute_pool import _best_pair_compute_pool`**。单 worker 串行，MediaPipe 不会竞态。

### D3.4 §9 429 `compute_pool_saturated`

endpoint `POST /api/cases/{id}/best-pair/recompute` / `POST /api/cases/best-pair/batch-recompute` 入队前检查当前 pending 任务数（用内存队列 size）。阈值 >100 → 返回 429 + `{"error": "compute_pool_saturated", "pending": <n>, "threshold": 100}`。

## Task 5 delta — render_executor 完整重写（解 C-C1/C-C2/C-C3）

**正文 T5 的"只加两列、不动 executor 逻辑"结论作废。** v3 §7 要求 render_executor 真加一条 best_pair 分支，含 3 个纯函数 + 1 个异常类 + fingerprint 漂移检测。估时由 3h 改为 **6h**（spec §7.7）。

### D5.1 `run_render` 签名与分支

```python
# backend/render_executor.py
from backend.render_queue import _fetch_case_image_overrides  # 或按既有导入路径

class StaleBestPairSelection(Exception):
    def __init__(self, reason: str, **ctx):
        self.reason = reason
        self.context = ctx
        super().__init__(f"stale_best_pair_selection:{reason} {ctx}")

def _build_overrides_from_selection(selection_row: dict) -> dict[str, dict]:
    return {
        selection_row["before_filename"]: {"manual_phase": "before", "manual_view": None},
        selection_row["after_filename"]:  {"manual_phase": "after",  "manual_view": None},
    }

def _build_selection_plan_from_selection(selection_row: dict, case_view: str) -> dict:
    return {
        case_view: {
            "before": selection_row["before_filename"],
            "after":  selection_row["after_filename"],
        }
    }

def load_selection(selection_id: int, conn) -> dict | None:
    cur = conn.execute(
        "SELECT id, case_id, before_filename, after_filename, delta_deg, "
        "candidates_fingerprint, before_override_before_json, after_override_before_json, "
        "selected_at, selected_by FROM case_best_pair_selections WHERE id=?",
        (selection_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))

def _assert_best_pair_inputs_fresh(conn, case_id: int, selection: dict | None) -> None:
    if selection is None:
        raise StaleBestPairSelection("selection_not_found")
    row = conn.execute(
        "SELECT candidates_fingerprint, status FROM case_best_pairs WHERE case_id=?", (case_id,)
    ).fetchone()
    if row is None or row[1] != "ready":
        raise StaleBestPairSelection("cache_not_ready", status=(row[1] if row else None))
    if row[0] != selection["candidates_fingerprint"]:
        raise StaleBestPairSelection("fingerprint_mismatch",
            selection_fp=selection["candidates_fingerprint"], current_fp=row[0])
```

`run_render` 入口（既有签名加 2 kwarg）：

```python
def run_render(*, case_id, db_path,
               render_mode: str = "ai",
               best_pair_selection_id: int | None = None,
               manual_overrides=None, selection_plan=None,
               ...):
    conn = sqlite3.connect(db_path)
    manual_overrides_db = _fetch_case_image_overrides(conn, case_id)
    if render_mode == "best_pair":
        selection = load_selection(best_pair_selection_id, conn)
        _assert_best_pair_inputs_fresh(conn, case_id, selection)
        selection_overrides = _build_overrides_from_selection(selection)
        case_view = conn.execute("SELECT view FROM cases WHERE id=?", (case_id,)).fetchone()[0]
        selection_plan = _build_selection_plan_from_selection(selection, case_view)
        manual_overrides = {**manual_overrides_db, **selection_overrides}  # selection wins on same (file, field)
        skip_enhance = True
    else:
        manual_overrides = manual_overrides_db
        skip_enhance = False
    # 下游保持既有逻辑不动
```

**Precedence 表**（C-C2）：同 `(filename, field)` selection 赢；不同 filename 并集；MVP selection 只合成 `manual_phase`，冲突实际只发生在该字段。

### D5.2 render_queue 捕获 Stale + failure_reason

`backend/render_queue.py::_execute_render` 在 subprocess/call `run_render` 的 try 块里 **捕获 `StaleBestPairSelection`** → 标 job failed + `failure_reason = f"stale_best_pair_selection:{exc.reason}"`。对应前端 RenderHistoryDrawer 显示失败态 + i18n key。

### D5.3 manifest.final.json `best_pair_context`（可观测性）

`run_render` 成功后写 `manifest.final.json` 顶层新增（best_pair 模式必出；ai 模式省略）：

```json
"best_pair_context": {
  "selection_id": 123,
  "before_filename": "术前1.jpg",
  "after_filename": "术后3.jpg",
  "candidates_fingerprint": "sha256:...",
  "rendered_at_source_version": 5
}
```

`source_version` 在 render 排队时从 `case_best_pairs` 读一次快照，写入 `render_jobs.candidates_fingerprint_snapshot`（D1.x 新列），同时传入 `run_render` 以便写 manifest。

### D5.4 T5 pytest 7 seam 用例（替换正文 T5 测试集）

用 `backend/tests/conftest.py` 的 `temp_db` + `seed_case` fixture，monkeypatch `backend.render_executor.load_selection` 与 `backend.render_executor._fetch_case_image_overrides` 作为 seam。**不**起 Chrome / **不**起 subprocess — 在 subprocess 启动前 raise 即可覆盖：

| 用例 id | seam 设置 | 期望 |
|---|---|---|
| (a) ai 路径 | `render_mode='ai'` | 不调 `load_selection`（spy 断言 `called == False`）；既有路径 0 回归 |
| (b) best_pair 无 DB override | monkeypatch load_selection → fixture selection + `_fetch_case_image_overrides` → `{}` | 返回 overrides 仅含 selection 合成 |
| (c) 无冲突 | (b) + DB override 塞 `c.jpg: {transform: ...}` | 结果并集 3 个 filename |
| (d) 冲突 | DB override 给 `before_filename` 写 `manual_phase='after'` | selection 赢，最终 `manual_phase='before'` |
| (e) fingerprint 漂移 | load_selection fp=v1，DB 查 fp=v2 | raise `StaleBestPairSelection('fingerprint_mismatch')` |
| (f) cache not ready | DB 写 status='dirty' | raise `StaleBestPairSelection('cache_not_ready')` |
| (g) selection not found | load_selection → None | raise `StaleBestPairSelection('selection_not_found')` |

完成后 `pytest backend/tests/test_render_executor_best_pair.py -v` 全绿才算 T5 过。

## Task 6 delta — highlight 本地 state 生命周期（R5）

正文 Step 6 BestPairPanel 的本地高亮 state 必须遵守 spec v3 §8.1 生命周期规则：

1. `highlightIndex: number` 仅存在于 `BestPairPanel` mount 周期（`useState`，**不**进 URL / localStorage / zustand）
2. 以下任一事件 → 重置为 0（top-1）：
   - 组件 unmount（drawer 关 / 切 case tab）
   - `caseId` prop 变化
   - `candidates_fingerprint` 变化（`useEffect([fingerprint])` 里 `setHighlightIndex(0)`）
   - `candidates_json.length` 变化
3. 页面 reload / 浏览器导航 / 关浏览器 → 静默丢失，**不**弹"未保存"警告（MVP；Phase 3 再评估）
4. 双 tab 竞争：tab A 若本地 highlightIndex=2 → tab B 先确认 → tab A 下次 `GET /best-pair` 拿新 fingerprint → 规则 2 触发重置。tab A 若此时仍点"确认"→ 后端 409 `stale_fingerprint` → 前端 refetch + 重置 highlight + toast `bestPair.errors.staleFingerprint`

**键盘**（新增 UX，追加到 BestPairPanel.tsx）：
- Tab 进候选列表（roving tabindex）
- `←` / `→` 切 highlightIndex（不确认）
- `Enter` = 切到焦点项
- `Ctrl+Enter` 或点击"确认"按钮 = 写 DB

Playwright（T7 新增 case）：
- `best-pair-highlight.spec.ts`：高亮 index 2 → 切 case → 切回 → highlightIndex 应为 0（unmount reset）
- 生命周期规则 3 默认不测（页面 reload 行为手测即可）

## Task 7 delta — e2e 补两个 spec

追加：
- `best-pair-highlight.spec.ts`（见上）
- `best-pair-stale-render.spec.ts`：mock `POST /render` 请求前，改 `case_best_pairs.candidates_fingerprint` → 应走 409 或 render 失败分支文案

## 估时重估

| Task | v2 | v3 |
|---|---|---|
| T1 | 2h | 2.5h (+fingerprint_snapshot 列) |
| T2 | 4h | 5h (+白名单判定 + leaf 拆分) |
| T3 | 8h | 9h (+独立 pool + UPSERT guard + empty 状态) |
| T4 | 4h | 4.5h (+429 saturated) |
| T5 | 3h | **6h** (+签名/异常/fingerprint/precedence/seam 七用例) |
| T6 | 6h | 7h (+highlight 生命周期 + 键盘) |
| T7 | 4h | 5h (+2 spec) |
| **合计** | 31h | **39h** |

## Spec 覆盖自检（v3 增量）

| Spec v3 段 | 覆盖 Task |
|---|---|
| §5.1 UPSERT + observed_version guard (R3) | T3 D3.1 |
| §5.1 空候选 → status='empty' (R7) | T3 D3.1 |
| §5.2 select bump source_version (R2) | T3 D3.2 |
| §5.2 dirty 触发白名单 (R1) | T2 D2.1 |
| §5.x best_pair_dirty leaf (R7) | T2 D2.2 |
| §5.y 独立 ThreadPoolExecutor(max_workers=1) (R7) | T3 D3.3 |
| §4.x render_jobs 补列 + status='empty' (R7) | T1 delta + T3 D3.1 |
| §7.1 render_executor 三纯函数 + Stale 异常 (R4/C-C1) | T5 D5.1 |
| §7.2 precedence 表 (C-C2) | T5 D5.1 |
| §7.3 fingerprint 漂移检测 (C-C3) | T5 D5.1 + D5.2 |
| §7.5 manifest best_pair_context + render_jobs snapshot | T5 D5.3 |
| §7.6 pytest 7 seam | T5 D5.4 |
| §8.1 highlight 生命周期 + 键盘 (R5/B-C2) | T6 delta |
| §9 429 compute_pool_saturated | T3 D3.4 |
| R6 call-site 5 处 + T2 硬标准 | T2 D2.4 |

**delta 覆盖完**。

## No-placeholder 扫描（delta）

- D2.1 白名单表 + `_candidate_filenames` 完整实现
- D2.2 `best_pair_dirty.py` 完整文件
- D3.1/D3.2 UPSERT / bump SQL 完整
- D5.1 `run_render` 分支完整伪代码 + 4 个纯函数 + 异常类
- D5.4 7 seam 用例表给 seam 设置 + 期望 expectation

无 TBD / "类似上面" / "add validation"。

## 类型一致性（v3 增量）

- `render_mode` 字面量：spec v3 §7 示例用 `'best_pair'`（下划线），v2 正文正文/ Task 5 用 `'best-pair'`（连字符）。**v3 绑定选下划线 `'best_pair'`**（Python 字面量 + DB 默认 + 前端 TS union 全改下划线）：
  - `backend/db.py` 的 CHECK / 默认值保留 `'ai'`
  - 正文 T5 里所有 `render_mode="best-pair"` → 改 `"best_pair"`
  - 前端 `api.ts` / `queries.ts` / 组件 props 相同替换
  - i18n key 不受影响（key 本来不带 mode 字面量）
- 新增 `render_mode` 枚举第 3 值 `'ai_fallback_from_best_pair'`（MVP 不触发，但 DB CHECK constraint 包含，防止 Phase 3 再改 schema）
- `case_best_pairs.status` 枚举扩到 5 值：`'ready' | 'skipped' | 'pending' | 'dirty' | 'empty'`
- `StaleBestPairSelection.reason` 字面量：`'selection_not_found' | 'cache_not_ready' | 'fingerprint_mismatch'`（前端 i18n key 对齐）
- `mark_best_pair_dirty` 新签名：`(conn, *, case_id, filename, changed_fields)`，返回 `bool`；**所有** caller 更新（正文 `_mark_best_pair_dirty(conn, case_id, now_iso)` 旧签名全废）

---

**v3 delta 完。engineer 执行时请同时读 T1–T7 正文 + 本 delta 节；delta 优先。**








