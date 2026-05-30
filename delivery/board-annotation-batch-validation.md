# board annotator 真实库批量验证（零成本）

> 真实库 `飞书Claude/医美资料/陈院案例(1)/` 已渲染 board，跑 `board_annotator` 统计覆盖（不渲染、不付费、不写源目录）。脚本 `/tmp/focal-p4-asset/batch_board_annotate.py`。

## 结果（2 列模板 tri/bi-compare）

| 指标 | 值 |
|---|---|
| 总 board | 82（2 列 69 / single-compare 12 不在中线分列范围） |
| **有标注覆盖** | **33/69 = 47.8%** |
| no-region | 24（34.8%） |
| no-before-face | 12（17.4%） |
| unreadable | 0 |

**no-region 拆解**：~16 个是 `job-NNN`（render job 输出目录，manifest 无术式名 → focus 取不到，**非 atlas 缺口**）。扣掉后真实可寻址 ~53，标注率实际 **~62%**。其余 no-region：直角肩/口周/童颜 = 非单一面部注射区（正确不标）。

## 部位 / 角度命中分布

- 部位：泪沟 33 · 苹果肌 13 · **鼻背 11** · 下巴 7 · 川字 7 · 太阳穴 6 · 唇 5 · 法令纹 4 · 卧蚕 2 · 鼻基底 2 · 面颊 1
- 角度：front 29 · oblique 24 · profile 3

## 关键验证

- ✅ **鼻背洞闭合在真实库确认**：5 个真实隆鼻/鼻子 case 命中鼻背（小绿隆鼻 / 蓝凤端隆鼻 / 许楚楚耳基底+鼻子 ×2 / 注射鼻子下巴）。
- ✅ 多行 board（正面+45° 两行）全行术前列都标，术后列不碰（即使术后是坏 render）。
- ✅ near-side 过滤、多区标签避让在真实 board 工作。

## 修复 / 发现

1. **已修**：`脸颊→面颊` alias 缺口（口语脸颊抽不出，批量验证抓到）→ atlas alias + 回归测试。
2. **focus 派生鲁棒性**（job-NNN）：render job 目录 manifest focus_targets 空时取不到术式名 → 后续可增强 `_focus_from_manifest` 读 manifest groups/案例上下文（低优，~16 例）。
3. no-before-face 12：单脸/右列布局变体，待按需排查。
