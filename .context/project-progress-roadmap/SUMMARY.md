---
phase: project-progress-roadmap
plan: .claude/plan/project-progress-roadmap.md
provides: [t1_audit_report, t1_blocker_list, t1_commit_split_proposal]
affects: [.gitignore, frontend/src/pages/CaseGroups.tsx, frontend/src/pages/ImageWorkbench.tsx, frontend/src/pages/QualityReview.tsx, frontend/src/pages/SourceBlockers.tsx, frontend/src/locales/zh, frontend/src/locales/en]
key_files:
  - .context/project-progress-roadmap/SUMMARY.md
completed: false
completed_at: 2026-05-10T10:30:00+08:00
notes: T1 audit-only session per user choice. 3 audit agents (interface-auditor / integration-checker / nyquist-auditor) ran in parallel. T1 fix + commit + push deferred to next session. T2 (best-pair) and T3 (UAT) untouched.
---

# T1 Audit Summary — wave-1 21981 行未提交代码

## 执行范围（本会话）

按用户选择：**只做 T1 audit + 风险清单**，不修代码不 commit 不 push。

实际工作量：3 audit agent 并行扫 51 modified + 22 untracked = 21981 insertions（plan 估算 11188 行 = 实际 2x）。

## 现状关键事实

- 工作目录：`/Users/a1234/Desktop/案例生成器/case-workbench/`
- 分支：`main`（ahead 3 commit vs `origin/main`）
- worktree 警示：`feat/best-pair-routing` 在 `case-workbench-best-pair/` 已有 commit `9f3bb26`（T2 第 2 步已实施）→ 用户选择 **T1 先 commit main，T2 worktree 之后 rebase**
- DB 文件：`case_workbench.db` + `cases.db` 在工作区未 ignore（plan 已识别）

## BLOCKER 清单（must fix before main commit）

### B1: DB 二进制文件 ignore 规则不匹配
- **来源**：interface-auditor + plan 已识别
- **证据**：`.gitignore` 现有 `case-workbench.db`（连字符），实际文件 `case_workbench.db`（下划线）+ `cases.db`
- **修复**：补 `.gitignore` 加 `case_workbench.db` + `cases.db`（或改为 `*.db` 通配但需 grep 确认无误伤）
- **次序**：必须**第一个 commit**，否则 DB 二进制会泄到 wave-1 主体 commit

### B2: i18n 完整性归零（最严重）
- **来源**：interface-auditor 验证通过
- **证据**：4 个新页 `CaseGroups.tsx` (274 LOC) / `ImageWorkbench.tsx` (1662 LOC) / `QualityReview.tsx` (1387 LOC) / `SourceBlockers.tsx` (357 LOC) — **3680 LOC、0 个 `useTranslation` import、含硬编码中文** (`已确认 / 低置信 / 自动 / 由整理诊断页创建 / 创建失败 / 暂无...`)
- **影响**：直接破坏阶段 21 的 14 namespace 0 mismatch / 495 t() 全 resolve 基线
- **修复**：4 页全部加 `useTranslation` + 抽 string 到 i18n
- **量化**：保守估计 4 页 × 30~80 strings ≈ 200-300 个新 i18n key

### B3: 4 个新 namespace JSON 缺失
- **来源**：interface-auditor + 主线验证（`ls frontend/src/locales/zh/` 无 caseGroups/sourceBlockers/qualityReview/imageWorkbench）
- **证据**：现有 14 namespace（caseDetail / cases / common / customerDetail / customers / dashboard / dict / evaluations / hotkeys / importCsv / jobBatch / render / renderHistory / revisions），4 新 namespace 未创建
- **修复**：在 `locales/{zh,en}/` 各创建 4 个 JSON + 同步注册 `frontend/src/i18n/index.ts`（运行时）+ `frontend/src/i18n/types.ts`（类型）— 阶段 11 lesson #11.2 已沉过

## 高优 push-blocker（B 级，commit 可带 TODO 但 push 前必修）

来自 nyquist-auditor 的并发 / 数据完整性深扫（8 项中筛 5 项最高影响）：

### N1: SQLite WAL 未启用 + worker 并发写锁竞争
- **证据**：`backend/db.py:362` `connect()` 无 `PRAGMA journal_mode=WAL`；`backend/render_queue.py:1625-1690` `_finish_result` 单事务持有写连接同时 record_revision + UPDATE cases + UPDATE render_jobs + persist_render_quality
- **风险**：2 worker 并发 commit 时撞 `database is locked`；render_quality `INSERT...ON CONFLICT` 退避失败 → render_jobs 已写但 quality 漏写，审计断链

### N2: recover() 重复提交同一 job
- **证据**：`render_queue.py:1893-1905` + `upgrade_queue.py:343-353` recover() 把 running→queued 后 fetchall() 重提交；启动期 _job_pool 已活，新 worker 可读到同一 status='queued' 行
- **风险**：同 job 被同时执行两次

### N3: bound staging dir 资源泄漏
- **证据**：`render_queue.py:184-223` `_build_bound_source_staging` 写 `.case-workbench-bound-render/job-{job_id}/` symlink，run_render 完成后无清理
- **风险**：长期占盘；源目录被删/重命名后留 dangling symlink

### N4: image_workbench transfer 端点 DB/磁盘状态分裂
- **证据**：`backend/routes/image_workbench.py:2589-2620` `for item in payload.items` `try/except HTTPException`，但 `scanner.rescan_one` 抛 ValueError 时 `shutil.copy2` 已写入目标目录，DB rollback 但物理文件遗留
- **风险**：磁盘有文件、DB 无 case_image_overrides → 状态不一致

### N5: 无 schema_version 表 + 启动期并发 ALTER 风险
- **证据**：`backend/db.py:301` `IMAGE_OVERRIDE_COLUMNS` ALTER 加列，单连接内顺序正确，但**多 backend 进程并发启动**时第二进程在第一个 ALTER 落地前 SELECT 会缺列
- **风险**：极少触发但启动期窗口存在

## WARNING（验证步骤可暴露，commit 不 block）

来自 nyquist + integration-checker：
- **W1**: 14 个 FE TypeScript interface 无对应后端 Pydantic `response_model`，端点返回 `dict[str, Any]` → 字段重命名静默 break TS 调用方
- **W2**: 2 个孤儿后端端点（`/api/stress/status` + `/api/render/stream` SSE）无前端消费者
- **W3**: `_LOCAL_ANGLE_FEATURE_CACHE` 全局 dict 无上限 → 长跑 RSS 单调增长
- **W4**: `case_grouping.py:300` rebuild 中途异常 UI 看到空清单（DELETE+INSERT 非原子从 UI POV）
- **W5**: `subprocess.run(timeout=240)` 不带 `start_new_session` → Node 子孙进程僵尸残留
- **W6**: enqueue_batch `except ValueError: continue` 静默跳 missing case，用户不知哪些被跳

## INFO（lessons 候选 / 跨项目模式）

- nyquist：CDP 单 session → worker 必须串行（与 image-workbench Phase 2.5 lesson 同源）
- nyquist：错误 envelope 不能泄露绝对路径（image_workbench transfer 的 OSError str(e) 含完整源/目标路径）
- integration-checker：响应 schema 跨 FE/BE 必须 Pydantic `response_model` 锁定，避免 ad-hoc dict 漂移
- 跨项目模式：i18n 完整性必须每次新页加页时手动维护，不能等"以后再补"（阶段 21 已沉 lesson，wave-1 又踩）

## T1 commit 拆分草案（5 子 commit，比 plan 提案的 4 子多 1 个 .gitignore-first）

**关键调整**：plan 的 4-theme 提案需要把 `chore .gitignore` 从最后挪到**最前**，否则 DB 文件会污染前 3 个 commit。

| # | message 前缀 | 涵盖文件 | 估算行数 |
|---|------------|---------|---------|
| **C1** | `chore(case-workbench): wave-1 .gitignore 修正 + DB 排除` | `.gitignore` | < 10 行 |
| **C2** | `feat(case-workbench): wave-1 backend 新模块 + 路由接线` | `backend/{ai_generation_adapter,case_grouping,render_quality,simulation_quality,source_images,source_selection,stress}.py` + `backend/routes/{case_groups,image_workbench,stress}.py` + `backend/{audit,db,issue_translator,main,models,render_executor,render_queue,scanner,skill_bridge,upgrade_queue}.py` + `backend/routes/{cases,customers,evaluations,render}.py` + `backend/scripts/` | ~10000 行 |
| **C3** | `feat(case-workbench): wave-1 frontend 跨 case 工作台 + 4 新页 + i18n 补齐` | `frontend/src/pages/{CaseGroups,ImageWorkbench,QualityReview,SourceBlockers,CaseDetail,Cases,Dashboard,JobBatch}.tsx` + `frontend/src/components/ManualRenderPicker.tsx` + `frontend/src/components/{Layout,RenderStatusCard}.tsx` + `frontend/src/{App,api,index.css}` + `frontend/src/hooks/queries.ts` + `frontend/src/locales/{zh,en}/{caseDetail,cases,common,importCsv,jobBatch,render,caseGroups,imageWorkbench,qualityReview,sourceBlockers}.json` + `frontend/src/i18n/{index,types}.ts` | ~8000 行（含 i18n 修复） |
| **C4** | `test(case-workbench): wave-1 pytest + e2e 全套` | `backend/tests/test_{case_groups,render_quality,render_background_policy_real,source_selection,stress_mode}.py` + 8 修改的测试 + `frontend/tests/e2e/{critical-flows,source-blockers,stress-smoke,source-image-override}.spec.ts` | ~3000 行 |
| **C5** | `chore(case-workbench): wave-1 配置 + ccg state + 文档` | `start.sh` + `frontend/{playwright,vite}.config.ts` + `frontend/package.json` + `.ccg/state.md` + `.context/{case-pose-ref-twin-track,debug,project-progress-roadmap}/` + `docs/superpowers/plans/2026-05-09-best-pair-routing.md` + `docs/superpowers/specs/2026-05-09-best-pair-routing-design.md` | ~4000 行 |

**B2 + B3 i18n 修复必须落在 C3**（不能拆为独立 commit，因为修复 = 改 4 页 .tsx + 新建 4 namespace JSON + i18n 注册，三件强耦合）。

## 下一会话 hand-off

**前置条件已满足**：
- `.context/project-progress-roadmap/SUMMARY.md` 已写（本文件）
- BLOCKER 清单清晰（3 个 commit-blocker + 5 个 push-blocker）
- commit 拆分顺序确定（5 子 commit，C1 必须最先）
- T2 worktree 处理策略确定（T1 push 后 rebase）

**下一会话指令**：

```
强制走 superpowers:subagent-driven-development（与 plan 一致）。

按 SUMMARY 的 BLOCKER 顺序：
1. 修 B1（.gitignore 补 case_workbench.db + cases.db）→ 验证 → C1 commit
2. spawn phase-runner 并行修 B2 + B3（4 新页加 useTranslation + 抽 string + 新建 4 namespace JSON + 注册 i18n/index.ts + types.ts）
3. 主线编排：跑 pytest backend/tests + npm --prefix frontend lint + type-check + build → 挂的 spawn debugger
4. 按 SUMMARY 的 5 子 commit 顺序提交
5. 调 /review 或 superpowers:requesting-code-review（push main 前强制 gate，>500 行 + push main 双重触发）
6. Critical/High finding 修完 → 追加修复 commit → 停下来等用户确认
7. 用户确认 → git push origin main（带远端落后 3 commit）

push-blocker（N1-N5）：在 commit 时加 TODO 注释 + 写 .context/project-progress-roadmap/followup.md，
push 前由 review gate 强制覆盖，或留作 T2/T3 之间的 hardening 子任务。
```

**T2/T3 状态**：未触及；待 T1 push 完成后启动 T2（best-pair worktree rebase + 64 step 实施）。
