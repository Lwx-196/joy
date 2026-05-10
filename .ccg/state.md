# CCG Team Execution State

**Plan**: .claude/team-plan/case-pose-ref-twin-track.md
**Team**: case-pose-ref-twin-track-team
**Started**: 2026-05-08 22:32
**Last Updated**: 2026-05-09 12:23
**Current Wave**: 1 / 1（Wave 2 待人工触发）

## Wave 1 (completed)
- [x] T1: case_layout_enhance.js + model-router.js 加 --no-planner (builder-T1-opus) — completed @ 2026-05-08 23:35
- [x] T2: backend/scripts/run_no_planner_test.py (builder-T2) — completed @ 2026-05-08 22:41
- [x] T3: backend/scripts/scan_case_angle_distribution.py (builder-T3) — completed @ 2026-05-08 22:44

## Failed Tasks
（无）

## Skipped Tasks
（无）

## Notes

### T1 关键发现
- 起手 git diff 后发现：`case_layout_enhance.js:302` 已有 `--no-planner` token 解析（`out.usePlanner = false`），`router.editPipeline({ usePlanner: args.usePlanner })` 也已透传
- `model-router.js` 已原生支持 `usePlanner=false` 时跳过 edit-planner、`editPrompt = userDirection`、`plannerUsed = false`
- T1-opus 实际只补了 `--help`/`-h` 分支输出（15 行 insertions），让 `--help` 列出 `--no-planner`
- model-router.js: 0 行 diff（已有逻辑无需改动）
- 验收：`node case_layout_enhance.js --help | grep no-planner` ✅；总 diff ≤ 50 行 ✅

### T1 builder 编排注释
- 第一次 spawn 内部错误（builder-T1 注册了但 tmuxPaneId 空，未实际启动）
- 第二次 spawn `builder-T1-retry`（sonnet）成功启动，但用户中途要求改 opus 4.7 → shutdown
- 第三次 spawn `builder-T1-opus`（opus）写完 SUMMARY 但没自行 mark task completed → Lead 接管验收并 mark

### T2 关键产物
- `backend/scripts/run_no_planner_test.py` 创建（py_compile PASS）
- helper：`load_env_for_subprocess` / `run_enhance(no_planner=True)` / `build_compare_image` / `get_base_prompt_from_backend`
- plannerUsed 守护：每次 run 后检查，True 则 sys.exit(2)
- 5 次循环 + max_attempts=3 指数退避（5s/10s/15s）
- 6 联 compare：[术前, 原术后, job19 final-board (planner on), no-planner run fastest 1/2/3]
- `find_reference_final_board()` 三层策略（simulation_jobs → render_jobs DB → 硬编码回退）
- 真实执行验证留给主线（Wave 1 后人工跑）

### T3 关键发现（决策性数据）
- 实跑成功（123 cases，~87 秒）
- **30 case 有效 / 93 case 跳过**（大部分早期非医美案例无 before/after 分类标注；少数目录不存在或只有单边）
- **<5° 占 93%**（28/30），远超 70% 阈值
- 5-10°: 0%；>10°: 6%；median: 2.0°；P90: 3.9°
- **决策含义**：AI 角度对齐根本不需要，best-pair 自动选图就够（产品层 Gemini 推荐方案成立）
- 产物：`/tmp/case_best_pair.json`（30 条）+ `/tmp/case_angle_distribution.png`（直方图三桶）

### 文件改动清单
- `~/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_enhance.js`：+15 行（仅 --help 分支）
- `~/Desktop/飞书Claude/scripts/model-router.js`：0 行（已有 usePlanner 逻辑无需改）
- `~/Desktop/案例生成器/case-workbench/backend/scripts/run_no_planner_test.py`：新建
- `~/Desktop/案例生成器/case-workbench/backend/scripts/scan_case_angle_distribution.py`：新建
- `.context/case-pose-ref-twin-track/SUMMARY-T{1,2,3}.md`：3 个产物 frontmatter

### Wave 1 实测判定（2026-05-09 12:57）
- T2 实测 5/5 success，plannerUsed: false × 5 守护过，elapsed 35-45s
- **视觉评估 compare.jpg：5/5 输出全部以原术后图为基准重绘（3/4 侧脸 + 黑发披肩 + 与 panel 2 像素级一致）**
- **0/5 满足"角度对齐术前"** — pose-ref（术前正面 + 粉发箍）完全被模型无视
- **判定：Track 1 输 — 净 prompt 仍像素照搬。planner 不是元凶；模型偏置成立。prompt 路径死**

### Wave 2 决策（已锁定）
按 plan 决策矩阵：
- 净 prompt 输 ❌ + Track 2 `<5°` 占比 93% ✅ → **实施 best-pair 自动选图**
- 生成时优先选用最匹配 (before, after) 对（基于 T3 输出的 `/tmp/case_best_pair.json`）
- 降级到 AI 生成（保留现有路径作 fallback）
- 待用户确认后启动 Wave 2 plan
