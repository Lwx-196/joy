---
phase: project-progress-roadmap
plan: .claude/plan/project-progress-roadmap.md
goal: 把 case-workbench 三条交错在途线（远端 ahead 3 commit / wave-1 11188 行未提交 / best-pair 64 步 plan 0/64 实施）按强依赖顺序收敛到 main 并跑通真实数据全链路验收
decisions:
  - T1 必须先于 T2（wave-1 与 best-pair plan 在 db.py / cases.py / render_queue.py / image_workbench.py 同改 4 文件）
  - T1 强制走 superpowers:subagent-driven-development，禁止主线 oneshot
  - T2 直接复用 docs/superpowers/plans/2026-05-09-best-pair-routing.md 64 step（不重新设计）
  - 远端 ahead 3 commit（纯文档）纳入 T1 收尾一起 push
  - case_workbench.db / cases.db 必须先补 .gitignore 再 commit（现有 .gitignore 命名不匹配）
  - 主线只做编排，全部实施动作下发 subagent
constraints:
  - 不能用 mock 业务数据
  - 每次 commit 前必须 pyflakes + lint + type-check 三件套全绿
  - 改动 > 30 行触发 /ccg:verify --gate=change
  - 跨前后端改动 > 500 行 / push main 前必须调 /review 或 superpowers:requesting-code-review
  - T2 实施时禁止重新设计 best-pair plan
files:
  - .claude/plan/project-progress-roadmap.md
  - docs/superpowers/plans/2026-05-09-best-pair-routing.md
  - docs/superpowers/specs/2026-05-09-best-pair-routing-design.md
  - .ccg/state.md
  - .gitignore
created_at: 2026-05-10T00:00:00+08:00
---

# Phase Context — project-progress-roadmap

## 任务三段式（强依赖）

T1 wave-1 收敛 + 远端 push → T2 best-pair 64 step 实施 → T3 真实数据全链路验收

## 关键事实（committed）

- 远端 `origin/main` 落后本地 `main` 3 commit：f94df1d / f79bd45 / e3d3497（纯文档）
- 工作区：51 modified + 30 untracked = 81 个未提交变更
- wave-1 后端新增量：6109 行（10 文件：7 模块 + 3 路由）
- wave-1 前端新增量：5079 行（5 文件：4 页 + 1 组件）
- main.py 已 include `stress` / `case_groups` / `image_workbench` 三 router（line 28/29/31）但未 commit
- best-pair plan：3662 行 / 64 step / 0 完成
- pose-ref Track 1（AI 角度对齐）已 prove dead（.ccg/state.md 2026-05-09 12:57 判定）

## SESSION_ID

无（本次未 spawn 外部模型，主线直接合成）
