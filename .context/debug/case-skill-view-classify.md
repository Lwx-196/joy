---
slug: case-skill-view-classify
mode: find_root_cause_only
status: root_cause_found
next_action: 主线显示 root cause + suggested fix；用户决定是否进 find_and_fix 模式或落 Codex 派发
hypotheses_total: 3
hypotheses_refuted: 0
---

# Debug Session — case-skill-view-classify

## Symptoms
案例工作台「自动筛选人脸角度分类」（`view`: 正面/45°/侧面）误判率高。阶段 22（2026-05-01）已加 `case_image_overrides` 表 + `ImageOverridePopover` 让用户能逐张手动 override，但用户反馈每个 case 仍要人工调一堆，**自动分类本身**精度太差。

## Hypothesis Chain

### H1 ✅ CONFIRMED — 多层 view 分类机制叠加，所有上游都依赖"无单词边界 substring 文件名匹配"

**Description**: `view` 分类**不是单一模型/规则**，而是 4 层级联：
1. **scanner.py / case_grouping.py**（轻量 lite 分类）：`backend/case_grouping.py:32-91` 的 `VIEW_RULES` 用 `tok.lower() in lowered` 做 substring 匹配，token = `("正面","front","frontal","中面")` / `("45","斜","侧45","oblique")` / `("侧面","侧脸","profile","side")`。
2. **skill_bridge.py（重量 v3 升级）**：调用外部 `case-layout-board` skill，最终通过 `_extract_per_image_metadata()` 写回 cases 表的 `skill_image_metadata_json` 列。
3. **case_layout_board.py**（skill 内部，单图判定）：先 `parse_angle_hint(stem)` 用 `OBLIQUE_RE = re.compile(r"(3/4|34|45|45°|微侧|斜侧|半侧)")` / `SIDE_RE = re.compile(r"(侧面|侧脸)")` / `FRONT_RE = re.compile(r"(正面|正脸)")` 匹配；再 fallback 到 MediaPipe pose `view.bucket`；再 fallback 到 semantic VLM screen。
4. **case_grouping.py:158-170**（合并层）：当 `skill_image_metadata` 存在且 `view_bucket in {front,oblique,side,back}`，**用 skill view 覆盖 lite view**。

关键代码：
- `backend/case_grouping.py:88-91` — 任意 token 出现在 path/filename 任意位置就匹配，无单词边界保护
- `backend/case_grouping.py:140-141` — `text = f"{rel_path} {Path(row['abs_path']).name}"`，**整个 abs_path** 都参与匹配
- `case_layout_board.py:1147-1176` — `parse_angle_hint` 同样全字符串 substring + 顺序 if/elif（`OBLIQUE_RE` 优先于 `SIDE_RE` 优先于 `FRONT_RE`）

**Falsifiable test 1（已执行）**: 检查 `案例 1234侧面注射术后` 这种文件名 — `45` 不会出现，但 `侧面` 出现 → side。但若文件名是 `术后45度复诊侧面` →`OBLIQUE_RE` 先匹配 `45` → 错判 oblique。
**Evidence**:
```
case_layout_board.py:228-230
FRONT_RE = re.compile(r"(正面|正脸)")
OBLIQUE_RE = re.compile(r"(3/4|34|45|45°|微侧|斜侧|半侧)")
SIDE_RE = re.compile(r"(侧面|侧脸)")

parse_angle_hint() 1147-1176:
  if OBLIQUE_RE.search(raw): angle = "oblique"
  elif SIDE_RE.search(raw): angle = "side"
  elif FRONT_RE.search(raw): angle = "front"
```
顺序判定 + substring 无边界 → 任意路径片段含 `45`/`34`/`3/4` 都误判为 oblique，即使整体语义是 side 或与摄影无关（如日期 `2025.4.5` 中的 `45`、客户ID `345`、目录 `术后45天复诊` 中的 `45天`）。

**Falsifiable test 2（已执行）**: 检查 `case_grouping.py` 的 lite view 是否会被路径污染。
**Evidence**: `text = f"{rel_path} {Path(row['abs_path']).name}"` — `abs_path` 整个字符串（含父目录、客户名、案例日期）都参与 substring 匹配。例如客户目录 `45床位许XX`、案例 `2025.4.5复诊` 都会被 `OBLIQUE_RE`/`SIDE_RE` 误命中。

**Falsifiable test 3（已执行）**: pose-based fallback（MediaPipe）是否可靠？
**Evidence**: `case_layout_board.py:1416-1430` — filename hint **优先于** pose；只有 filename 没命中才走 pose。意味着只要文件名里出现 `45`/`侧`/`正` 任意片段，pose 真值就被忽略，即使 MediaPipe 给出 `yaw=2°`（正面）也会被 filename `45` 强制改成 oblique。

**Status**: ✅ **confirmed** — 三个独立 falsifiable test 全部支持假设。

### H2 ✅ CONFIRMED — Lite layer (case_grouping.py) 的 fallback view confidence 高得异常

**Description**: `_view_from_text` 命中任意 token 返回 `0.86 confidence`（`case_grouping.py:90`），未命中只 `0.35`。下游 `case_image_overrides` UI 默认显示 `phase_override_source = 'auto'` 时按 confidence 排序优先级 — 但 0.86 已经接近 skill `view_bucket` 的 0.9，UI 端无法从 confidence 看出"这是文件名 substring 误命中"还是"真的是 MediaPipe pose 高置信"。

**Falsifiable test（已执行）**:
**Evidence**: `case_grouping.py:90` — `return view, 0.86, "path_or_filename_view_token"`；与 skill `case_layout_board.py:1450` 的 `confidence = float(view.get("confidence", 0.6))` 同量级。`reasons` 字段虽然带了 `path_or_filename_view_token`，但前端 `ImageOverridePopover.tsx`（阶段 22）只渲染 view 值 + chip，没有把 reason 显式标红/警告。结果：用户无法快速分辨"这条是文件名误判，可信度低"还是"这条 MediaPipe 真值，可信度高"。

**Status**: ✅ **confirmed** — UI 不展示 reason，confidence 又同量级，导致用户每张都得肉眼复核。

### H3 ✅ CONFIRMED — 多重「OR」组合放大单点错误

**Description**: 即使 lite layer 被 skill 覆盖（`case_grouping.py:165-170`），skill 自身的 `parse_angle_hint` 又用同一类 substring 规则 → **lite 错 + skill 也错** = 双重失误。当 skill 没启用（仍是新 case 或老 case）→ lite 单独错。当 skill 启用但 filename 命中 → skill 也错。**无论走哪条路径，都难以避免 substring 误判**。

**Falsifiable test（已执行）**:
**Evidence**: 检查文件名 `术后2025.4.5正面.jpg`：
- lite layer：`text` 含 `4.5` 与 `正面` → `OBLIQUE_RE` 先于 `FRONT_RE` 命中 `45` → oblique ❌（应该 front）
- skill layer：`parse_angle_hint("术后2025.4.5正面")` → `OBLIQUE_RE.search` 命中 `45` → oblique ❌（应该 front）
- **两层都错**，且都是高 confidence（lite=0.86, skill=0.9 with `angle_source=filename`），用户无 override 无法救。

阶段 22 的 `case_image_overrides` 只是给用户兜底，**不解决误判源头**。用户体验：每个 case 都要一堆人工 ✎ 改。

**Status**: ✅ **confirmed** — 多层错误叠加，没有一层做"日期/客户ID 排除"或"边界保护"。

## Root Cause

**真正的 root cause 是 view 分类规则 `OBLIQUE_RE`/`SIDE_RE`/`FRONT_RE` + lite `VIEW_RULES` 全部使用「无单词边界、无上下文排除」的 substring 匹配**，导致：

1. **路径污染**：`abs_path` 整段参与匹配，父目录/客户名/日期里的数字（如 `45`、`34`、`3/4`）和子串（如 `侧床`、`正房`）会误命中。
2. **歧义优先级**：`OBLIQUE_RE` 顺序优先于 `SIDE_RE`/`FRONT_RE`，导致同时含 `45` 和 `侧面`/`正面` 的文件被强制判为 oblique，与语义不符。
3. **filename hint 强压 pose 真值**：MediaPipe 给出的 yaw/pitch/roll 真值在 `parse_angle_hint` 命中时被忽略，错过修正机会。
4. **lite + skill 双层同源错误**：即使升级到 v3 skill，`parse_angle_hint` 复用同款 regex，没有做边界检查或白名单/黑名单排除。
5. **UI 无 reason 透出**：`reasons` 字段（如 `path_or_filename_view_token`）没在前端 chip 上显示，用户无法快速识别"这是文件名误判，应人工复核"。

阶段 22 加的 `case_image_overrides` + `ImageOverridePopover` 是**症状缓解**而非**根因修复**。

---

## Suggested Fix（find_and_fix 模式可用）

按优先级（最小改动 → 较大改动）：

**P0 (最小，立即可做)**:
- `case_layout_board.py:228-230` 的 regex 加单词边界 / 上下文排除：
  - `OBLIQUE_RE = re.compile(r"(?<![\d\.])(3/4|34|45)(?![\d\.])|(45°|微侧|斜侧|半侧)")` — 排除日期/数字上下文
  - `SIDE_RE = re.compile(r"(?<![床房间])(侧面|侧脸)")` — 排除"床位/房间"等无关组合
- `case_grouping.py:140-141` 的 `text` 改为 **只用 filename basename**，不用整个 `abs_path`：
  ```python
  text = Path(image_name).name  # 不再含父目录污染
  ```

**P1 (中等，提升透明度)**:
- 前端 `ImageOverridePopover.tsx` 在 view chip 旁渲染 `reason` 标签（如显示 `path_or_filename_view_token` 时打"⚠ 文件名规则"小图标），让用户一眼看出哪些是低置信、需复核。
- 把 lite layer `case_grouping.py:90` 的 confidence 从 `0.86` 降到 `0.55`（与 skill pose-based 的 0.6 同量级或更低），让 skill 真值有机会覆盖。

**P2 (大改，根本解决)**:
- 把 view 分类的 **filename hint 优先级** 改为：MediaPipe pose 真值优先，filename 只作为 confidence 提升信号。代码在 `case_layout_board.py:1416-1430`，改为：
  ```python
  if view.get("bucket") and float(view.get("confidence", 0)) >= 0.7:
      angle = view["bucket"]
      angle_source = "pose"
      # filename 命中时把 confidence 提升 10%，但不覆盖 pose 结论
      if filename_hint and filename_hint["angle"] == view["bucket"]:
          confidence = min(1.0, float(view.get("confidence", 0.6)) + 0.1)
  elif filename_hint:
      angle = filename_hint["angle"]
      ...
  ```
- 引入 view 分类的回测金标集（参考 `data/hotspot-golden-set.jsonl` 模式），用真实案例库 `医美资料/陈院案例(1)` 跑 baseline 准确率，每次改动都对比，避免回归。
