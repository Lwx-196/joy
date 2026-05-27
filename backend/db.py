"""SQLite connection + schema initialization."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback.
    fcntl = None

DB_PATH = Path(
    os.environ.get(
        "CASE_WORKBENCH_DB_PATH",
        str(Path(__file__).resolve().parent.parent / "case-workbench.db"),
    )
).expanduser().resolve()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


SQLITE_BUSY_TIMEOUT_MS = _env_int("CASE_WORKBENCH_SQLITE_BUSY_TIMEOUT_MS", 5000)
SCHEMA_LOCK_TIMEOUT_SEC = _env_int("CASE_WORKBENCH_SCHEMA_LOCK_TIMEOUT_SEC", 30)
SCHEMA_COMPONENT = "main"
SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions (
  component    TEXT PRIMARY KEY,
  version      INTEGER NOT NULL,
  applied_at   TIMESTAMP NOT NULL
);

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
  meta_json     TEXT,
  recovery_token TEXT,
  recovery_claimed_at TIMESTAMP
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
  meta_json     TEXT,
  recovery_token TEXT,
  recovery_claimed_at TIMESTAMP
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
  reason_json   TEXT,
  reviewer      TEXT,
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

-- ComfyUI 灰度 A/B 反馈。只记录运营/审核反馈证据，不解锁发布。
CREATE TABLE IF NOT EXISTS ab_feedback (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  render_job_id          INTEGER NOT NULL REFERENCES render_jobs(id) ON DELETE CASCADE,
  case_id                INTEGER REFERENCES cases(id) ON DELETE SET NULL,
  baseline_job_id        INTEGER,
  candidate_job_id       INTEGER,
  simulation_job_id      INTEGER REFERENCES simulation_jobs(id) ON DELETE SET NULL,
  workflow_profile       TEXT,
  verdict                TEXT NOT NULL,
  hard_defect_tags_json  TEXT NOT NULL DEFAULT '[]',
  reviewer               TEXT NOT NULL,
  note                   TEXT,
  source                 TEXT NOT NULL DEFAULT 'gray_rollout',
  created_at             TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ab_feedback_render_job ON ab_feedback(render_job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ab_feedback_case ON ab_feedback(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ab_feedback_workflow ON ab_feedback(workflow_profile, verdict, created_at);

-- T74: 统一人工复核队列。ticket 只记录阻断与人工决策证据；
-- 决策回写仍走 source_group_selection trace / accepted warnings / slot locks，
-- 不直接改 render_quality.can_publish。
CREATE TABLE IF NOT EXISTS review_tickets (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  render_job_id   INTEGER REFERENCES render_jobs(id) ON DELETE SET NULL,
  ticket_type     TEXT NOT NULL,
  stage           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open',
  blocks_render   INTEGER NOT NULL DEFAULT 0,
  blocks_publish  INTEGER NOT NULL DEFAULT 0,
  reason_code     TEXT NOT NULL,
  slot            TEXT,
  source_filename TEXT,
  message         TEXT,
  evidence_json   TEXT NOT NULL DEFAULT '{}',
  decision_json   TEXT NOT NULL DEFAULT '{}',
  dedupe_key      TEXT NOT NULL DEFAULT '',
  created_at      TIMESTAMP NOT NULL,
  updated_at      TIMESTAMP NOT NULL,
  resolved_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_review_tickets_status ON review_tickets(status, ticket_type, updated_at);
CREATE INDEX IF NOT EXISTS idx_review_tickets_case ON review_tickets(case_id, status, ticket_type);
CREATE INDEX IF NOT EXISTS idx_review_tickets_job ON review_tickets(render_job_id, status);

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

-- VLM 调用用量审计。只追加真实 provider 调用摘要，不存图像内容。
CREATE TABLE IF NOT EXISTS vlm_usage_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  purpose        TEXT NOT NULL,
  provider       TEXT NOT NULL,
  model          TEXT NOT NULL,
  case_id        INTEGER REFERENCES cases(id),
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL NOT NULL DEFAULT 0,
  cost_source    TEXT NOT NULL DEFAULT 'unknown',
  latency_ms     INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL,
  error_detail   TEXT,
  -- P0.1: 结构化 per-image 失败上下文 {provider, attempt, error_class,
  -- error_message, traceback}；与 error_detail 互补（后者保留 4000 char 摘要）
  error_json     TEXT,
  usage_raw_json TEXT NOT NULL DEFAULT '{}',
  created_at     TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vlm_usage_created ON vlm_usage_log(purpose, created_at);
CREATE INDEX IF NOT EXISTS idx_vlm_usage_case ON vlm_usage_log(case_id, created_at);

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

-- P1.2: ComfyUI / VLM 候选谱系。每次 simulation 尝试落一行，operator 决策
-- 通过 UPDATE 写入 operator_decision / operator_user / decided_at 三件套；
-- 失败链路用 failure_reason；VLM judge runner 完成时写 vlm_judge_result_json。
-- 与 simulation_jobs 是 N:1（一个 job 可多次 attempt），与 cases 是 N:1。
CREATE TABLE IF NOT EXISTS candidate_lineage (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  simulation_job_id       INTEGER REFERENCES simulation_jobs(id) ON DELETE SET NULL,
  case_id                 INTEGER REFERENCES cases(id) ON DELETE SET NULL,
  input_image_hash        TEXT,
  workflow_hash           TEXT,
  provider                TEXT,
  model_name              TEXT,
  attempt                 INTEGER NOT NULL DEFAULT 1,
  failure_reason          TEXT,
  vlm_judge_result_json   TEXT,
  operator_decision       TEXT,
  operator_user           TEXT,
  decided_at              TIMESTAMP,
  created_at              TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_lineage_case     ON candidate_lineage(case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_lineage_sim      ON candidate_lineage(simulation_job_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_lineage_decision ON candidate_lineage(operator_decision, decided_at DESC);

-- P1.1: ops API 审计日志。每次 /api/render/ops/* 调用必落一行，无论
-- dry_run 与否；payload_json / response_json 用 JSON 字符串落原始入参出参，
-- outcome ∈ {ok|partial|error|dry_run}，http_status 是真实返回码。
-- reviewer 取 X-Reviewer header（强制）；request_id 取 X-Request-Id 或生成。
CREATE TABLE IF NOT EXISTS ops_audit_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id       TEXT,
  endpoint         TEXT NOT NULL,
  reviewer         TEXT NOT NULL,
  reason           TEXT,
  payload_json     TEXT,
  response_json    TEXT,
  outcome          TEXT NOT NULL,
  http_status      INTEGER NOT NULL,
  created_at       TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ops_audit_request      ON ops_audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_ops_audit_endpoint_at  ON ops_audit_log(endpoint, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_audit_reviewer     ON ops_audit_log(reviewer, created_at DESC);
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

# P0.1: vlm_usage_log 加结构化 per-image 失败上下文（旧库 migration）。
VLM_USAGE_LOG_COLUMNS = [
    ("error_json", "TEXT"),
]

CASE_TRASH_COLUMNS = [
    ("original_abs_path", "TEXT"),
    ("trashed_at", "TIMESTAMP"),
    ("trash_reason", "TEXT"),
]

IMAGE_OVERRIDE_COLUMNS = [
    ("manual_transform_json", "TEXT"),
    ("reason_json", "TEXT"),
    ("reviewer", "TEXT"),
]

BEST_PAIR_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS case_best_pair_selections (
  id                          INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id                     INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  before_filename             TEXT NOT NULL,
  after_filename              TEXT NOT NULL,
  delta_deg                   REAL NOT NULL,
  candidates_fingerprint      TEXT,
  candidates_fingerprint_snapshot TEXT,
  before_override_before_json TEXT,
  after_override_before_json  TEXT,
  view                        TEXT,
  selected_at                 TIMESTAMP NOT NULL,
  selected_by                 TEXT,
  selection_reason            TEXT
);
CREATE INDEX IF NOT EXISTS idx_cbps_case_at ON case_best_pair_selections(case_id, selected_at DESC);
"""

BEST_PAIR_SELECTION_COLUMNS = [
    ("view", "TEXT"),
    ("selection_reason", "TEXT"),
]

RENDER_JOB_BEST_PAIR_COLUMNS = [
    ("render_mode", "TEXT NOT NULL DEFAULT 'ai'"),
    ("best_pair_selection_id", "INTEGER REFERENCES case_best_pair_selections(id)"),
    ("candidates_fingerprint_snapshot", "TEXT"),
    ("draft_preview", "INTEGER NOT NULL DEFAULT 0"),
]

RENDER_JOB_RECOVERY_COLUMNS = [
    ("recovery_token", "TEXT"),
    ("recovery_claimed_at", "TIMESTAMP"),
]

UPGRADE_JOB_RECOVERY_COLUMNS = [
    ("recovery_token", "TEXT"),
    ("recovery_claimed_at", "TIMESTAMP"),
]


def _ensure_table_columns(conn, table: str, columns: list[tuple[str, str]]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, kind in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")


def _ensure_manual_columns(conn) -> None:
    _ensure_table_columns(conn, "cases", MANUAL_COLUMNS)


def _ensure_simulation_job_columns(conn) -> None:
    _ensure_table_columns(conn, "simulation_jobs", SIMULATION_JOB_COLUMNS)


def _ensure_vlm_usage_log_columns(conn) -> None:
    _ensure_table_columns(conn, "vlm_usage_log", VLM_USAGE_LOG_COLUMNS)


def _ensure_case_trash_columns(conn) -> None:
    _ensure_table_columns(conn, "cases", CASE_TRASH_COLUMNS)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_trashed ON cases(trashed_at)")


def _ensure_image_override_columns(conn) -> None:
    _ensure_table_columns(conn, "case_image_overrides", IMAGE_OVERRIDE_COLUMNS)


def _ensure_best_pair_tables(conn) -> None:
    _execute_schema_script(conn, BEST_PAIR_SCHEMA)
    _ensure_table_columns(conn, "case_best_pair_selections", BEST_PAIR_SELECTION_COLUMNS)


def _ensure_render_job_best_pair_columns(conn) -> None:
    _ensure_table_columns(conn, "render_jobs", RENDER_JOB_BEST_PAIR_COLUMNS)


def _ensure_job_recovery_columns(conn) -> None:
    _ensure_table_columns(conn, "render_jobs", RENDER_JOB_RECOVERY_COLUMNS)
    _ensure_table_columns(conn, "upgrade_jobs", UPGRADE_JOB_RECOVERY_COLUMNS)


def init_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _schema_file_lock():
        with connect() as conn:
            conn.execute("BEGIN EXCLUSIVE")
            _execute_schema_script(conn, SCHEMA)
            _ensure_best_pair_tables(conn)
            _ensure_render_job_best_pair_columns(conn)
            _ensure_job_recovery_columns(conn)
            _ensure_image_override_columns(conn)
            _ensure_manual_columns(conn)
            _ensure_simulation_job_columns(conn)
            _ensure_vlm_usage_log_columns(conn)
            _ensure_case_trash_columns(conn)
            _record_schema_version(conn)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=max(SQLITE_BUSY_TIMEOUT_MS / 1000, 5.0))
    _configure_connection(conn)
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
    conn = sqlite3.connect(DB_PATH, timeout=max(SQLITE_BUSY_TIMEOUT_MS / 1000, 5.0))
    _configure_connection(conn)
    return conn


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")


def _execute_schema_script(conn: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def _record_schema_version(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO schema_versions (component, version, applied_at)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
          version = excluded.version,
          applied_at = excluded.applied_at
        """,
        (SCHEMA_COMPONENT, SCHEMA_VERSION, now),
    )


@contextmanager
def _schema_file_lock():
    lock_path = DB_PATH.with_name(f"{DB_PATH.name}.schema.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is None:
            yield
            return
        deadline = time.monotonic() + max(SCHEMA_LOCK_TIMEOUT_SEC, 1)
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for schema lock: {lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
