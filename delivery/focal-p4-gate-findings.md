# P4 Gate Findings (running) — 2026-05-30

## F-1: candidate board 丢"正面对比"整行 (待 N=12 确认)
- case 小绿: baseline 2 行 (正面对比[术后灰底坏合成] + 45°侧对比) vs candidate **仅 1 行** (侧面对比)
- 疑因: FOCAL 增强后从 scratch 重渲 layout 时，正面那对图(baseline 本就是灰底坏合成)未过角度/质量配对 → 整行不渲
- 影响: judge 看 2 行 vs 1 行，信息量不对等，胜负判读要打折
- candidate 侧面术后更"原片感": 碎发 + 颧骨红印 + 下颌淡拼接线 (FOCAL 只对 focus 区 native 重绘，不动整体精修)
- owner 决策: (a) 先跑完 12 个再定 GO/NO-GO，F-1 随报告交

## F-3 (HEADLINE / 方法学级): P4 gate 设计被混淆 — candidate=现场重渲 vs baseline=人工策展板
根因: candidate 臂对 FOCAL 增强后的**裸图重跑完整角度分类+质量 gate**; baseline 臂复用**已发货的人工策展 board**。两者差异 ≠ FOCAL 画质，主要被"现渲 vs 人工策展"的不对称 + FOCAL 锐化触发 parity gate 主导。

两类实测失败模式:
- 袁霞 (整 case drop): FOCAL 锐化术后(55) vs 原始术前(18.91) → "正面前后清晰度差过大"gate 判废正面 → 袁霞无其它角度 → drop。但已发货 board 用同一对(术前2+术后2 原图未锐化)渲成功 slot=front。→ FOCAL 锐化**自毁**。
- 小绿 (丢正面行): 现场分类器"正面命中过多显式候选,无法唯一确定" + 45°姿态差过大(weighted=30.28)排除 → 只剩 side 一行。

有效性影响:
- 目前 ~43% drop/降级 → gate 凑不够 N≥10
- 能渲处 candidate(自动) vs baseline(人工策展) = 苹果对橘子
- (品牌/模板**已**被 packet builder 对齐: _recover_board_spec 复用 baseline 的 brand/template, 这点没问题)

修法 = candidate 应**复用 baseline board 的精确 slot 选择**(同图/同角度/同模板), 只把每个选中的术后图换成 FOCAL 增强版 → 隔离出纯 FOCAL 效应。packet builder 已 recover after_names 但之后 run_render 重新分类而非复用 slot —— 这就是要堵的缺口。

建议: 停当前无效 run; 改 packet builder 复用 baseline slot; 重跑。

## 最终统计 (run 已停, owner 决策)
- 处理 9/12, 成功出 board **4** (康巧佳/吕碧英/小绿/徐莹), drop **5** (高雅静/江李欣/稀饭/袁霞/赵建芬)
- drop 率 56%, 外推 12 个约 5-6 板 → 确认凑不够 N≥10
- run 停于 builder elapsed ~1:15, 三后台任务(builder/watchdog/comfyui)全 TaskStop
