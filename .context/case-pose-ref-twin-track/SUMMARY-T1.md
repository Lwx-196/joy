---
task: T1-no-planner-flag
status: done
builder: T1
date: 2026-05-08
---

# T1 — `--no-planner` CLI 开关

## 结论

**任务完成。** 两个目标文件均已正确实现 `--no-planner`。

## 变更详情

### `skills/case-layout-board/scripts/case_layout_enhance.js`

**变更**：在 `parseArgv` 中补充 `--help` / `-h` 分支，打印完整用法说明（含 `--no-planner` 说明）。

```
scripts/case_layout_enhance.js | 15 +++++++++++++++
1 file changed, 15 insertions(+)
```

**已有逻辑（无需改动）**：
- `--no-planner` token → `out.usePlanner = false`
- `router.editPipeline({ usePlanner: args.usePlanner })` 透传
- JSON 输出包含 `plannerUsed` 和 `editPrompt`

### `scripts/model-router.js`

**无变更**（`git diff` 0 行）。  
已有实现：`usePlanner=false` 时跳过 edit-planner，`editPrompt = userDirection`，`plannerUsed = false`。

## 验证

```bash
# --help 包含 --no-planner 说明
node .../case_layout_enhance.js --help 2>&1 | grep -i "no-planner"
# 输出：  --no-planner         跳过 edit-planner 改写，完整 prompt 直达 Tuzi 模型  ✅

# git diff 行数
cd .../case-layout-board && git diff --stat
# scripts/case_layout_enhance.js | 15 +++++++++++++++  ✅（≤ 50 行）

# model-router.js 无变更
# 0 行  ✅
```

## 契约满足

| 条目 | 状态 |
|------|------|
| `--no-planner` 解析 | ✅ 已有 |
| `usePlanner` 透传到 `editPipeline` | ✅ 已有 |
| `editPipeline` 跳过 planner | ✅ 已有 |
| `plannerUsed: false` 输出 | ✅ 已有 |
| `editPrompt` = 完整 prompt | ✅ 已有 |
| `--help` 含 `--no-planner` 说明 | ✅ 本次补充 |
| 总 diff ≤ 50 行 | ✅ 15 行 |
| 无 planner 时零回归 | ✅ 原有路径不变 |
