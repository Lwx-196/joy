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

## review gate 新增（eval-auditor + integration-checker，2026-05-10 12:30）

### N6: 测试评估自身需返工（eval-auditor 12/25）

eval-auditor 评分：抽样 2/5、对照 2/5、可证伪 2/5。**关键认知：N1-N5 任一 bug 真在生产触发，pytest 351 仍可全绿** —— 测试无法证伪 push-blocker。

**hardening 必须包含的硬证伪测试**（每项绑死对应 N，否则修了 bug 验不出）：

| push-blocker | 必加硬证伪测试 |
|---|---|
| N1 (WAL/并发锁) | `test_concurrent_finish_result_does_not_drop_render_quality` — multiprocessing.Process×2 同时调 `_finish_result` 写 50 row，断言 render_quality 行数 == render_jobs 行数 |
| N2 (recover 重复提交) | `test_recover_with_live_pool_does_not_double_submit` — 真起 ThreadPoolExecutor(max=2) + 计数 fn，让 recover() 在 fetchall 后 yield，测同 job_id 是否只跑 1 次 |
| N4 (transfer DB/磁盘分裂) | `test_transfer_rollbacks_disk_when_rescan_fails` — monkeypatch scanner.rescan_one 抛 ValueError，断言 target_dir 内**无**新文件 + DB 无 case_image_overrides 行 |
| N5 (schema 并发 ALTER) | `test_concurrent_init_schema_two_processes_no_missing_column` — subprocess×2 竞争 ALTER，断言列存在性 |

### N6.1: stress 测试 4 用例不在 subprocess

- **位置**：`backend/tests/test_stress_mode.py`
- **风险**：CASE_WORKBENCH_OUTPUT_ROOT env 设了但 conftest import-time 已定型 `db.DB_PATH`，`test_stress_status_reports_isolated_paths` / `test_audit_revision_is_tagged_in_stress_mode` 实际只测了 status_payload 函数对 env 的反射，**未测 stress mode 端到端隔离**
- **修复**：4 用例搬到 subprocess（参考 `test_db_path_can_be_configured_by_env`）
- **gaming risk**：`test_stress_mode_blocks_destructive_routes` 5 行用 case_id=1（不存在）→ block 未生效会 404 而非 403，断言区分不出 "block 生效" vs "case 不存在"。改成"先 seed 真 case + 关 stress 跑确认会动数据 + 开 stress 跑 403 + 断言 case 行未变"两段对照

### N6.2: 新模块 runtime 路径 0 真覆盖

- `simulation_quality.py` 只 monkeypatch `POLICY_PATH`，0 直接覆盖 → 加 5 unit test (policy 缺失 / score 阈值 / review 状态写回)
- `issue_translator.py` 0 测试 → 加 zh/en 入参穷举
- `source_images.py` 0 测试
- `ai_generation_adapter.py` build_after_enhancement_prompt + _create_difference_heatmap 是 pure-fn 测试，"真正调用 PS-router"路径全 stub → 加 contract-test：fake_run input shape vs 真实 `run_ps_model_router_after_simulation` 签名 assert（防 stub 漂移）

### N6.3: e2e fixture 空库下静默 PASS

- **位置**：`frontend/tests/e2e/source-blockers.spec.ts` / `stress-smoke.spec.ts`
- **风险**：`if (await firstSnapshot.count())` 守卫意味着 fixture 若空，spec 只跑顶层 visible 断言仍 PASS，"功能坏了 0 cards" vs "空库" 区分不出
- **修复**：beforeAll 显式 `expect(supplementTrigger).toBeVisible()` 或 `expect(cards.first()).toHaveCount({min:1})`

### N6.4: test_image_workbench_transfer_rejects_trashed_source_path 文案断言

- **位置**：`test_image_overrides_basic.py` line 883-1013 transfer 段
- **风险**：断言全是 `reason 文本相等 + body.copied==0 + skipped[0].reason` 文案断言；文案重写一行该测试就崩，且不验证 trashed 文件物理上没被 access 的业务不变量
- **修复**：补加文件系统 inode mtime 不变 / shutil.copy2 未被调用的断言

## integration-checker 新发现（2026-05-10 13:00）

### N7: SimulateAfterResponse 同名 schema 双 endpoint 不一致

- **位置**：`backend/routes/case_groups.py:188-196` ↔ `backend/routes/cases.py` simulate-after / `frontend/src/api.ts:866-878`
- **风险**：Group-level POST `/api/case-groups/{id}/simulate-after` 返回 `{simulation_job_id, group_id, case_id, status, focus_targets, policy, error_message}`；Case-level 用 `response_model=SimulateAfterResponse` 返回 `{input_refs, output_refs, audit, focus_regions, provider, model_name}`。FE 在 `api.ts:1441-1449` 用 inline type 处理 group 变体（正确），但任何 reuser 把 `SimulateAfterResponse` 复用到 group 路由就 break（缺 policy/group_id，多出 input_refs/audit）
- **修复**：rename group 返回到 `GroupSimulateAfterResponse` 并锁 `response_model=`；api.ts 加注释说明两个 endpoint 故意不互换
- **严重度**：HIGH（schema 漂移 footgun，T2 best-pair 引入新 simulate-after 调用前必修）

### N8: QualityReview renderIssueTarget 漏 "侧向"

- **位置**：`backend/source_selection.py:1011` (warning text "侧向术前术后方向不一致") ↔ `frontend/src/pages/QualityReview.tsx:122-124` (renderIssueTarget)
- **风险**：FE renderIssueTarget 按 `String.includes("侧面"|"侧脸"|"正面"|"45")` 算 slot，后端 direction_mismatch warning 用"侧向"（不是侧面/侧脸）→ slot=null → 整条 issue 在 FE drilldown **静默 drop**，用户看不到 direction_mismatch warning
- **修复（短期）**：QualityReview.tsx:122 includes 列表加 "侧向"
- **修复（长期，推荐）**：backend 改成 emit 结构化字段 `{code, slot, severity}` 替代 String.includes 中文 prose（这条沉 lessons：协议字符串解析跨 FE/BE 必须结构化）
- **严重度**：MEDIUM —— 不阻断 push（只是某些 warning 在 EN/ZH UI 都看不到，但底层数据正确）

### N9: image_workbench transfer 字节上限/自循环检查/跨 customer

- **位置**：`backend/routes/image_workbench.py:2543-2635` POST `/api/image-workbench/transfer`
- 已有：path-traversal 防御（relative_to + extension whitelist）OK
- **缺失 1**：无 per-request 字节上限。payload caps 100 items 但单 item 可大；shutil.copy2 会写 GB 级到 target case dir
- **缺失 2**：`target_case_id == source_case_id` 检查在 `_case_dir(target)` 之后，self-target 仍会 hit DB+filesystem 才返回 error
- **缺失 3**：`inherit_review` 写 `copied_from_case_id` 不验证 source/target 同 customer，跨 customer transfer 当前直通
- **修复**：
  - 加 env `IMAGE_WORKBENCH_TRANSFER_MAX_BYTES`（默认 500MB）loop 内累计校验
  - self-target 检查上移到 `_case_dir(target)` 之前
  - 跨 customer transfer 加 guard（业务侧确认是否允许）
- **严重度**：MEDIUM —— 不阻断 push；但跟 N4（transfer DB/磁盘分裂）同源，hardening 一起做

### N10: CaseGroups ai_generation_authorized 装饰 flag

- **位置**：`frontend/src/pages/CaseGroups.tsx:62-64` ↔ `backend/routes/case_groups.py:118-124`
- **风险**：FE 总发送 `ai_generation_authorized: true` + `provider: "ps_model_router"` 硬编码；UI "授权" gate 只是 `window.prompt(focus_targets)`。真正 gate 在后端 env `CASE_WORKBENCH_ENABLE_AI_GENERATION=1`。flag 是装饰用
- **修复**：移除 payload 里的 authorized flag（env-only gate），或加真正的 confirmation dialog
- **严重度**：INFO —— lessons 候选（"装饰性同意 flag" 反模式）

## hardening 子任务建议顺序（T2 启动前）

每项独立 commit + 对应硬证伪测试 + 文档更新。

1. **N1 (WAL + busy_timeout) + 硬证伪 test_concurrent_finish_result** — 最容易修，影响最大
2. **N5 (schema_versions 表) + 硬证伪 test_concurrent_init_schema** — 预防未来 migration 风险
3. **N2 (recover 原子化) + 硬证伪 test_recover_with_live_pool** — 关键正确性
4. **N3 (staging cleanup)** — 长跑必修（可挂 startup scan + finally 块）
5. **N4 (transfer 两阶段) + N9 (transfer 字节/self/跨customer guard) + 硬证伪 test_transfer_rollbacks_disk** — 状态一致性 + 防滥用
6. **N7 (rename GroupSimulateAfterResponse) + response_model lock** — T2 best-pair 引入新 simulate-after 前必修
7. **N8 (QualityReview includes 加 "侧向") + 长期改结构化字段** — UI 可见 bug fix
8. **N6.1-N6.4 测试返工** — 把 stress 4 用例搬 subprocess + 加 simulation_quality/issue_translator/source_images runtime 测试 + e2e fixture min count 守卫 + transfer 文件系统不变量断言
9. **W1 (Pydantic response_model 全覆盖)** — 跨 FE/BE 契约（与 T2 best-pair 相关）
10. **W3-W6** — 渐进改善

每项 commit message 用 `fix(case-workbench): hardening N<X> ...` 前缀。
