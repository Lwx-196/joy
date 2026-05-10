"""SQLite connection + schema initialization."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "case-workbench.db"),
    )
).expanduser().resolve()

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at    TIMESTAMP NOT NULL,
  completed_at  TIMESTAMP,
  root_paths    TEXT NOT NULL,
  case_count    INTEGER,
  mode          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_id               INTEGER NOT NULL REFERENCES scans(id),
  abs_path              TEXT NOT NULL UNIQUE,
  customer_raw          TEXT,
  customer_id           INTEGER REFERENCES customers(id),
  category              TEXT NOT NULL,
  template_tier         TEXT,
  blocking_issues_json  TEXT,
  pose_delta_max        REAL,
  sharp_ratio_min       REAL,
  source_count          INTEGER,
  labeled_count         INTEGER,
  meta_json             TEXT,
  last_modified         TIMESTAMP NOT NULL,
  indexed_at            TIMESTAMP NOT NULL,
  original_abs_path     TEXT,
  trashed_at            TIMESTAMP,
  trash_reason          TEXT
);

CREATE INDEX IF NOT EXISTS idx_cases_customer ON cases(customer_id);
CREATE INDEX IF NOT EXISTS idx_cases_category ON cases(category);
CREATE INDEX IF NOT EXISTS idx_cases_tier     ON cases(template_tier);

CREATE TABLE IF NOT EXISTS customers (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_name  TEXT NOT NULL UNIQUE,
  aliases_json    TEXT NOT NULL DEFAULT '[]',
  notes           TEXT,
  created_at      TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP NOT NULL
);

-- B1: 操作历史 / 撤销窗口
-- 任何写动作（patch / batch / merge / rescan / upgrade）都在 _apply_update 之前
-- 通过 audit.record_revision 写一条 before/after 快照。撤销 = 把最新一条 revision
-- 反向 apply。范围只覆盖最近一条；不做时光机。
CREATE TABLE IF NOT EXISTS case_revisions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id      INTEGER NOT NULL REFERENCES cases(id),
  changed_at   TIMESTAMP NOT NULL,
  actor        TEXT,                    -- 'user' | 'scan' | 'skill_upgrade'
  op           TEXT NOT NULL,           -- 'patch' | 'batch' | 'rescan' | 'merge_customer' | 'rename' | 'upgrade' | 'undo'
  before_json  TEXT NOT NULL,           -- 受影响列的旧值快照
  after_json   TEXT NOT NULL,           -- 新值快照
  source_route TEXT,                    -- 触发的 API 路径
  undone_at    TIMESTAMP                -- NULL=有效；非 NULL=已被撤销
);
CREATE INDEX IF NOT EXISTS idx_revisions_case ON case_revisions(case_id, changed_at DESC);

-- Phase 3: 渲染任务队列
-- 单 case 入队由 POST /api/cases/{id}/render；批量由 POST /api/cases/render/batch
-- 同一批次共享 batch_id；单 case 任务 batch_id=NULL。
-- output_path / manifest_path 在 done 状态时填充，是 final-board.jpg / manifest.final.json 的绝对路径。
-- error_message 在 failed 状态填充。
-- semantic_judge 默认 'auto'：正式自动出图先用视觉补判筛选低置信/未标注图片。
CREATE TABLE IF NOT EXISTS render_jobs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id       INTEGER NOT NULL REFERENCES cases(id),
  brand         TEXT NOT NULL,
  template      TEXT NOT NULL DEFAULT 'tri-compare',
  status        TEXT NOT NULL,
  batch_id      TEXT,
  enqueued_at   TIMESTAMP NOT NULL,
  started_at    TIMESTAMP,
  finished_at   TIMESTAMP,
  output_path   TEXT,
  manifest_path TEXT,
  error_message TEXT,
  semantic_judge TEXT NOT NULL DEFAULT 'auto',
  meta_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_render_jobs_case   ON render_jobs(case_id, enqueued_at DESC);
CREATE INDEX IF NOT EXISTS idx_render_jobs_status ON render_jobs(status, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_render_jobs_batch  ON render_jobs(batch_id, status);

-- 阶段 2: v3 升级任务队列
-- 单 case 入队由 POST /api/cases/upgrade（同步路径仍走旧 /api/cases/{id}/upgrade）；
-- 批量由 POST /api/cases/upgrade/batch
-- 同一批次共享 batch_id；状态机与 render_jobs 完全对称。
-- meta_json 在 done 时存升级摘要（category / template_tier / blocking_count / skill_status）。
-- error_message 在 failed 状态填充。
CREATE TABLE IF NOT EXISTS upgrade_jobs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id       INTEGER NOT NULL REFERENCES cases(id),
  brand         TEXT NOT NULL,
  status        TEXT NOT NULL,
  batch_id      TEXT,
  enqueued_at   TIMESTAMP NOT NULL,
  started_at    TIMESTAMP,
  finished_at   TIMESTAMP,
  error_message TEXT,
  meta_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_upgrade_jobs_case   ON upgrade_jobs(case_id, enqueued_at DESC);
CREATE INDEX IF NOT EXISTS idx_upgrade_jobs_status ON upgrade_jobs(status, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_upgrade_jobs_batch  ON upgrade_jobs(batch_id, status);

-- 阶段 3: 评估台
-- 通用承载：subject_kind 'case' | 'render' + subject_id 多态外键。
-- verdict 三态与 review_status 对齐（approved / needs_recheck / rejected）；
-- 缺一个 active 行 = "待评"（用 NOT EXISTS 子查询过滤）。
-- 软删：undone_at IS NULL = active；最新 active 即"当前生效评估"。
-- 撤销 = 把最新 active 标 undone（非物理删除）；
-- render undo 联动会自动把对应 active evaluation 标 undone。
CREATE TABLE IF NOT EXISTS evaluations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_kind  TEXT NOT NULL,
  subject_id    INTEGER NOT NULL,
  verdict       TEXT NOT NULL,
  reviewer      TEXT NOT NULL,
  note          TEXT,
  source_route  TEXT,
  created_at    TIMESTAMP NOT NULL,
  undone_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_evaluations_subject        ON evaluations(subject_kind, subject_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evaluations_pending_lookup ON evaluations(subject_kind, undone_at);

-- Stage B: 单张图 phase / view 手动覆盖
-- 主键 (case_id, basename(filename))。值 NULL = 该维度不覆盖,沿用 skill 自动判读。
-- phase 取值受 backend.routes.cases 的 _ALLOWED_OVERRIDE_PHASES 校验,
-- view 取值受 _ALLOWED_OVERRIDE_VIEWS 校验。
CREATE TABLE IF NOT EXISTS case_image_overrides (
  case_id       INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  filename      TEXT NOT NULL,
  manual_phase  TEXT,
  manual_view   TEXT,
  manual_transform_json TEXT,
  updated_at    TIMESTAMP NOT NULL,
  PRIMARY KEY (case_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_image_overrides_case ON case_image_overrides(case_id);

-- 分类重构：真实案例边界。一个 group 可以关联一个或多个旧 cases 行，
-- 例如历史扫描中被拆开的「术前/术后」子目录。
CREATE TABLE IF NOT EXISTS case_groups (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  group_key       TEXT NOT NULL UNIQUE,
  primary_case_id INTEGER REFERENCES cases(id),
  customer_raw    TEXT,
  title           TEXT NOT NULL,
  root_path       TEXT NOT NULL,
  case_ids_json   TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL DEFAULT 'auto',
  diagnosis_json  TEXT NOT NULL DEFAULT '{}',
  created_at      TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_case_groups_primary_case ON case_groups(primary_case_id);
CREATE INDEX IF NOT EXISTS idx_case_groups_status ON case_groups(status);

-- 图片级观察结果。默认来自规则/本地 skill 结构化输出；低置信度时未来可追加 VLM 来源。
CREATE TABLE IF NOT EXISTS image_observations (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id       INTEGER NOT NULL REFERENCES case_groups(id) ON DELETE CASCADE,
  case_id        INTEGER REFERENCES cases(id) ON DELETE SET NULL,
  image_path     TEXT NOT NULL,
  phase          TEXT NOT NULL DEFAULT 'unknown',
  body_part      TEXT NOT NULL DEFAULT 'unknown',
  view           TEXT NOT NULL DEFAULT 'unknown',
  quality_json   TEXT NOT NULL DEFAULT '{}',
  confidence     REAL NOT NULL DEFAULT 0,
  source         TEXT NOT NULL DEFAULT 'rules',
  reasons_json   TEXT NOT NULL DEFAULT '[]',
  created_at     TIMESTAMP NOT NULL,
  updated_at     TIMESTAMP NOT NULL,
  UNIQUE(group_id, image_path)
);
CREATE INDEX IF NOT EXISTS idx_image_observations_group ON image_observations(group_id);
CREATE INDEX IF NOT EXISTS idx_image_observations_case ON image_observations(case_id);
CREATE INDEX IF NOT EXISTS idx_image_observations_confidence ON image_observations(confidence);

-- 术前/术后候选配对与模板推荐。
CREATE TABLE IF NOT EXISTS pair_candidates (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id           INTEGER NOT NULL REFERENCES case_groups(id) ON DELETE CASCADE,
  slot               TEXT NOT NULL,
  before_image_path  TEXT,
  after_image_path   TEXT,
  score              REAL NOT NULL DEFAULT 0,
  metrics_json       TEXT NOT NULL DEFAULT '{}',
  status             TEXT NOT NULL DEFAULT 'missing_pair',
  template_hint      TEXT,
  created_at         TIMESTAMP NOT NULL,
  updated_at         TIMESTAMP NOT NULL,
  UNIQUE(group_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_pair_candidates_group ON pair_candidates(group_id);
CREATE INDEX IF NOT EXISTS idx_pair_candidates_status ON pair_candidates(status);

-- 出图质量结果。render_jobs.status 表达任务生命周期；这里表达产物质量与发布资格。
CREATE TABLE IF NOT EXISTS render_quality (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  render_job_id         INTEGER NOT NULL UNIQUE REFERENCES render_jobs(id) ON DELETE CASCADE,
  quality_status        TEXT NOT NULL,
  quality_score         REAL NOT NULL DEFAULT 0,
  can_publish           INTEGER NOT NULL DEFAULT 0,
  artifact_mode         TEXT NOT NULL DEFAULT 'real_layout',
  manifest_status       TEXT,
  blocking_count        INTEGER NOT NULL DEFAULT 0,
  warning_count         INTEGER NOT NULL DEFAULT 0,
  metrics_json          TEXT NOT NULL DEFAULT '{}',
  review_verdict        TEXT,
  reviewer              TEXT,
  review_note           TEXT,
  reviewed_at           TIMESTAMP,
  created_at            TIMESTAMP NOT NULL,
  updated_at            TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_render_quality_status ON render_quality(quality_status);

-- AI 运行审计：默认只记录结构化摘要，不要求上传真实图像。
CREATE TABLE IF NOT EXISTS ai_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_kind    TEXT NOT NULL,
  subject_id      INTEGER NOT NULL,
  model_role      TEXT NOT NULL,
  provider        TEXT,
  model_name      TEXT,
  input_summary_json TEXT NOT NULL DEFAULT '{}',
  output_json     TEXT NOT NULL DEFAULT '{}',
  status          TEXT NOT NULL,
  error_message   TEXT,
  started_at      TIMESTAMP NOT NULL,
  finished_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_runs_subject ON ai_runs(subject_kind, subject_id);

-- 术后 AI 增强/模拟任务，与真实 render_jobs 隔离。
CREATE TABLE IF NOT EXISTS simulation_jobs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id          INTEGER REFERENCES case_groups(id) ON DELETE SET NULL,
  case_id           INTEGER REFERENCES cases(id) ON DELETE SET NULL,
  status            TEXT NOT NULL,
  focus_targets_json TEXT NOT NULL DEFAULT '[]',
  policy_json       TEXT NOT NULL DEFAULT '{}',
  model_plan_json   TEXT NOT NULL DEFAULT '{}',
  input_refs_json   TEXT NOT NULL DEFAULT '[]',
  output_refs_json  TEXT NOT NULL DEFAULT '[]',
  watermarked       INTEGER NOT NULL DEFAULT 1,
  audit_json        TEXT NOT NULL DEFAULT '{}',
  error_message     TEXT,
  review_status     TEXT,
  reviewer          TEXT,
  review_note       TEXT,
  reviewed_at       TIMESTAMP,
  can_publish       INTEGER NOT NULL DEFAULT 0,
  created_at        TIMESTAMP NOT NULL,
  updated_at        TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simulation_jobs_group ON simulation_jobs(group_id);
CREATE INDEX IF NOT EXISTS idx_simulation_jobs_status ON simulation_jobs(status);
"""


MANUAL_COLUMNS = [
    ("manual_category", "TEXT"),
    ("manual_template_tier", "TEXT"),
    ("manual_blocking_issues_json", "TEXT"),
    ("notes", "TEXT"),
    ("tags_json", "TEXT"),
    ("review_status", "TEXT"),  # 'pending' | 'reviewed' | 'needs_recheck' | None
    ("reviewed_at", "TIMESTAMP"),
    # 三态 manual UX 之 "挂起" — 用户主动暂时搁置该 case，工作队列默认隐藏。
    # held_until=NULL 表示未挂起；非 NULL 表示挂起到该时刻（也可设为遥远未来表示无限期）。
    ("held_until", "TIMESTAMP"),
    ("hold_reason", "TEXT"),
    # Stage A: skill_bridge 透传的逐图 metadata 与原始阻塞/警告字符串。
    # 这些列由 _upgrade_executor 写入,scanner 不写;UI 读取后渲染 view chip
    # 与 blocking 详情。值是 JSON 字符串(列表),空 case 为 NULL。
    ("skill_image_metadata_json", "TEXT"),
    ("skill_blocking_detail_json", "TEXT"),
    ("skill_warnings_json", "TEXT"),
]

SIMULATION_JOB_COLUMNS = [
    ("review_status", "TEXT"),
    ("reviewer", "TEXT"),
    ("review_note", "TEXT"),
    ("reviewed_at", "TIMESTAMP"),
    ("can_publish", "INTEGER NOT NULL DEFAULT 0"),
]

CASE_TRASH_COLUMNS = [
    ("original_abs_path", "TEXT"),
    ("trashed_at", "TIMESTAMP"),
    ("trash_reason", "TEXT"),
]

IMAGE_OVERRIDE_COLUMNS = [
    ("manual_transform_json", "TEXT"),
]


def _ensure_manual_columns(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
    for col, kind in MANUAL_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {kind}")


def _ensure_simulation_job_columns(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(simulation_jobs)").fetchall()}
    for col, kind in SIMULATION_JOB_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE simulation_jobs ADD COLUMN {col} {kind}")


def _ensure_case_trash_columns(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
    for col, kind in CASE_TRASH_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {col} {kind}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_trashed ON cases(trashed_at)")


def _ensure_image_override_columns(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(case_image_overrides)").fetchall()}
    for col, kind in IMAGE_OVERRIDE_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE case_image_overrides ADD COLUMN {col} {kind}")


def init_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _ensure_image_override_columns(conn)
        _ensure_manual_columns(conn)
        _ensure_simulation_job_columns(conn)
        _ensure_case_trash_columns(conn)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_conn() -> sqlite3.Connection:
    """For request-scoped use; caller must close."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
