---
phase: project-progress-roadmap
goal: T1 push-blocker (N1-N5) 登记，本次 commit 带 TODO 注释，T2 之前由独立 hardening 子任务收敛。
created_at: 2026-05-10T11:30:00+08:00
---

# T1 push-blocker followup

来自 nyquist-auditor 深扫（2026-05-10 T1 audit），按影响度排序。本次 commit 在对应位置加 TODO 注释 + 在此登记，push 后由独立 hardening 子任务（在 T2 启动前）逐项收敛。

## N1: SQLite WAL 未启用 + worker 并发写锁竞争

- **位置**：`backend/db.py:362` `connect()` 无 `PRAGMA journal_mode=WAL`；`backend/render_queue.py:1625-1690` `_finish_result` 单事务持有写连接
- **风险**：2 worker 并发 commit 时撞 `database is locked`；render_quality `INSERT...ON CONFLICT` 退避失败 → render_jobs 已写但 quality 漏写
- **缓解**：connect() 加 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`；`_finish_result` 拆 audit 写入到独立短事务
- **验收**：并发 2 worker 跑 30s + render_quality 完整性校验脚本

## N2: recover() 重复提交同一 job

- **位置**：`backend/render_queue.py:1893-1905` + `backend/upgrade_queue.py:343-353`
- **风险**：启动期 _job_pool 已活，recover() 把 running→queued 后 fetchall() 重提交，新 worker 可读到同一 status='queued' 行 → 同 job 同时执行两次
- **缓解**：recover() 先抢 advisory lock 或用 `UPDATE ... RETURNING` 原子化；或加 job-level claim token
- **验收**：模拟 crash + restart 测试看 job_id 是否唯一执行

## N3: bound staging dir 资源泄漏

- **位置**：`backend/render_queue.py:184-223` `_build_bound_source_staging`
- **风险**：写 `.case-workbench-bound-render/job-{job_id}/` symlink，run_render 完成后无清理；长期占盘 + 源目录被删/重命名后留 dangling symlink
- **缓解**：finally 块清理 staging dir；启动期扫并清理无主 job-* 目录
- **验收**：跑 10 job 后 `ls .case-workbench-bound-render/` 应为空

## N4: image_workbench transfer 端点 DB/磁盘状态分裂

- **位置**：`backend/routes/image_workbench.py:2589-2620` `for item in payload.items` `try/except HTTPException`
- **风险**：`scanner.rescan_one` 抛 ValueError 时 `shutil.copy2` 已写入目标目录，DB rollback 但物理文件遗留 → 磁盘有文件、DB 无 case_image_overrides 状态分裂
- **缓解**：transfer 改两阶段：先 scanner.rescan_one 验证 → 再 shutil.copy2 → 最后写 DB；任何一步失败 rollback 已落地的物理文件
- **验收**：注入 ValueError 看磁盘/DB 一致性

## N5: 无 schema_version 表 + 启动期并发 ALTER 风险

- **位置**：`backend/db.py:301` `IMAGE_OVERRIDE_COLUMNS` ALTER 加列
- **风险**：单连接内顺序正确，但多 backend 进程并发启动时第二进程在第一个 ALTER 落地前 SELECT 会缺列
- **缓解**：加 `schema_versions` 表 + `BEGIN EXCLUSIVE` 或文件锁；首次进程独占执行 migration
- **验收**：并发 2 进程启动 + SELECT 立刻测列存在性

## WARNING（commit 不 block，但下一阶段处理）

- **W1**: 14 个 FE TypeScript interface 无对应后端 Pydantic `response_model` → 字段重命名静默 break TS 调用方
- **W2**: 2 个孤儿后端端点（`/api/stress/status` + `/api/render/stream` SSE）无前端消费者
- **W3**: `_LOCAL_ANGLE_FEATURE_CACHE` 全局 dict 无上限 → 长跑 RSS 单调增长
- **W4**: `case_grouping.py:300` rebuild 中途异常 UI 看到空清单（DELETE+INSERT 非原子）
- **W5**: `subprocess.run(timeout=240)` 不带 `start_new_session` → Node 子孙进程僵尸残留
- **W6**: enqueue_batch `except ValueError: continue` 静默跳 missing case，用户不知哪些被跳

## hardening 子任务建议顺序（T2 启动前）

1. N1 (WAL + busy_timeout) — 最容易修，影响最大
2. N5 (schema_versions 表) — 修了能预防未来 migration 风险
3. N2 (recover 原子化) — 关键正确性
4. N3 (staging cleanup) — 长跑必修
5. N4 (transfer 两阶段) — 状态一致性
6. W1 (response_model) — 跨 FE/BE 契约（与 T2 best-pair plan 改 4 文件相关，建议 T2 前做）
7. W3-W6 — 渐进改善

每项独立 commit + 对应 backend pytest 用例。
