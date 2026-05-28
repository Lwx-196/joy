# Phase A Finding Report — VLM+ComfyUI 优化落地状态：未上线（实证）

> 2026-05-28 · swift-validation plan Phase A closure
> 实施：~/.claude/plans/swift-validation.md 4.1-4.6 (pivoted 2026-05-28)
> 决策：User P1 — Phase A finding 已 deliver，Phase B/C CLOSED；后续开新 plan 接通 ComfyUI。

## 1. TL;DR

在 `case_id=126 brand=fumei template=tri-compare` 上：
1. 备份 best-pair job 166 当前磁盘产物
2. 触发 fresh ai render → job 633（2026-05-28 02:40:11 finished, status=done_with_issues, 32s）
3. **两次 render 的 `final-board.jpg` byte-identical**（sha256 `e39bd3c28a05c040e28eaa6acf596c21d6f5c82a04ac5b44c5f028cd820a7680`，501897 bytes）

这是 production "ai" render mode **根本没调 AI 图像生成 pipeline** 的实证铁证。VLM+ComfyUI 优化方案落地状态 = **未上线**。

## 2. Evidence chain（可复现）

| # | 证据 | 数值 |
|---|---|---|
| E1 | `delivery/phase-a/best-pair-fumei-job166.jpg` sha256 | `e39bd3c28a05c040e28eaa6acf596c21d6f5c82a04ac5b44c5f028cd820a7680` |
| E2 | `delivery/phase-a/ai-fumei-fresh-job633.jpg` sha256 | `e39bd3c28a05c040e28eaa6acf596c21d6f5c82a04ac5b44c5f028cd820a7680`（同 E1） |
| E3 | best-pair job 166 写入时间 | 2026-05-10 21:54:38 |
| E4 | fresh ai job 633 写入时间 | 2026-05-28 10:40:11（mtime, 18 天后） |
| E5 | job 633 `meta_json.ai_usage.used_after_enhancement` | `false` |
| E6 | job 633 `meta_json.ai_usage.used_ai_padfill` | `false` |
| E7 | job 633 `meta_json.ai_usage.semantic_judge_effective` | `"off"` |
| E8 | job 633 `manifest.final.json.semantic_summary.pair_calls` | `0` |
| E9 | job 633 `manifest.final.json.semantic_summary.final_calls` | `0` |
| E10 | `vlm_usage_log` 表在 2026-05-28 02:39:00 - 02:41:00 区间行数 | `0` |
| E11 | best-pair job 166 `render_selection_plan` 序列化 sha | `765ffefb21ffc825...` |
| E12 | ai job 633 `render_selection_plan` 序列化 sha | `4319caaa143c9e88...`（与 E11 不同） |
| E13 | job 166 `best_pair_selection_id` / job 633 `best_pair_selection_id` | `5` / `None` |

## 3. 推理链

**前提**：
- E11 ≠ E12 → 两 mode 选了不同源图集（select 算法存在差异）
- E13 → best-pair mode 绑定 best_pair_selection_id=5, ai mode 不绑定（DB 层 schema 差异确实存在）

**但**：
- E1 = E2 → 最终像素输出完全相同
- E5-E10 → fresh ai render 在 32 秒内完成，**零 AI 调用 / 零 VLM 调用 / 零 ComfyUI 调用**

**结论**：
- production 上 `render_mode` 字段（`ai` vs `best-pair`）+ `best_pair_selection_id` 字段是 metadata，对最终图像 byte 级输出**零影响**
- 实际跑的是同一套 **layout-only pipeline**（PIL 拼图板子），没有 AI 图像生成参与
- 即使两边 source selection 算法不同，因 layout 算法 deterministic 且 source images 大致一致，输出仍 byte-identical

## 4. Phase A → Phase B 决策门（按 design doc 4.5）

| Phase A 结果（design doc 4.5 表） | 决策 |
|---|---|
| best-pair 赢 1/1 | ✅ 进 Phase B |
| 平 / 无明显差异 | ⚠️ 进 Phase B 扩量 |
| ai 赢 1/1 | ⚠️ Phase B 加强样本 |
| **两边 byte-identical（本 finding，原表未列）** | **⛔ CLOSED — 盲评不再有意义，Phase B/C 取消** |

## 5. Phase A 5 step mapping

| step | status | note |
|---|---|---|
| 1a backup best-pair | ✅ done | sha256 e39bd3... captured 10:37 |
| 1b fresh ai render | ✅ done | job 633, 32s, status=done_with_issues |
| 1c capture ai output | ✅ done | sha256 e39bd3... (collision detected) |
| 2 blind review package | ⏸ skipped | finding 自证不需要盲评 |
| 3 user blind review | ⏸ skipped | 同上 |
| 4 write ab_feedback | ⏸ skipped | 同上（无 winner 可写） |
| 5 first-signal report | ✅ done | 本文 |

## 6. 对原疑问的回答（user 启动语）

> "我之前最少规划的 best-pair + VLM 这个性能优化方案是不是已经真正落地了，还有就是要确认核实他们两者是不是已经进入了真正的生产任务，并且能够比原来的方案更高效，出图质量更高"

**实证答案**：
- 落地状态：**没有**。production "ai" render mode 在 2026-05-28 实测中零 AI/VLM/ComfyUI 调用，与 best-pair 模式输出 byte-identical
- "比原方案更高效 / 质量更高"前提：**不成立**。当前没有"两种生成路径"可对比，两者实际都是 layout-only 拼图，无图像生成参与
- design doc 1.2 "What's NOT shipped" 列的 4 个 WIP worktree（best-pair-routing / vlm-judge / vlm-phase2 / vlm-calibration）依然是 production gap

## 7. Recommendation（下个 plan）

1. **优先**：审 4 个 WIP worktree（user owner），找哪一份代码最接近"AI gen 真接入"状态 → 用 `framework-selector` 评估 vs 现状
2. **次优先**：backend/ai_generation_adapter.py / render_executor.py grep + audit，定位 ai mode 代码路径是否本来就没接 image gen，或者有 feature flag 关闭了
3. **不必再**：跑 Phase B / 扩样本盲评（不解决根本问题，浪费 quota）

## 8. 关联文件

| 文件 | 是否进 PR | 备注 |
|---|---|---|
| `delivery/phase-a-evidence-report.md` | ✅ tracked | 本文 |
| `delivery/phase-a-render-pairs.json` | ✅ tracked | 机器可读 pair manifest（含全部 sha256 / size / DB id） |
| `delivery/phase-a/best-pair-fumei-job166.jpg` | ❌ gitignored | `.gitignore:51 delivery/*/` 规则屏蔽大 JPG；worktree-only，复核走 sha256 |
| `delivery/phase-a/ai-fumei-fresh-job633.jpg` | ❌ gitignored | 同上 |
| `delivery/phase-a/best-pair-fumei-job166.manifest.json` | ❌ gitignored | 同上（同子目录，被规则一起屏蔽；如未来需 review 可临时 `git add -f`） |
| `delivery/phase-a/ai-fumei-fresh-job633.manifest.json` | ❌ gitignored | 同上 |
| `docs/superpowers/specs/2026-05-28-vlm-comfyui-optimization-launch-design.md` §4.7 + §5/§6 | ✅ tracked | design doc closure record + Phase B/C CANCELLED 标注 |
| `~/.claude/plans/swift-validation.md` | (机外) | 个人 plan，标 Phase B/C CANCELLED + 引用本 finding |

**Reviewer 复核路径（不依赖 binary backup）**：
1. `git log -1 --format=%H` confirm HEAD
2. `sqlite3 case-workbench.db "SELECT id, status, render_mode, datetime(finished_at), meta_json FROM render_jobs WHERE id IN (166, 633);"`
3. `sqlite3 case-workbench.db "SELECT COUNT(*) FROM vlm_usage_log WHERE created_at BETWEEN '2026-05-28 02:39:00' AND '2026-05-28 02:41:00';"` → 应为 0
4. 文件 sha256 由 `delivery/phase-a-render-pairs.json` 字段提供，可与磁盘当前 `final-board.jpg` 对照（注：磁盘状态可能被后续 render 覆盖，sha256 比对仅在 backup 时刻有效）
