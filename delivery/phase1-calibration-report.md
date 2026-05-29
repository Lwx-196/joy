# Phase 1 校准报告 — inferred 区在真实正脸上的叠点校准

> 2026-05-29。plan `anchored-focal-annotation.md` Phase 1 第一步。
> 工具：`/tmp/p1-calib/calibrate_inferred.py`（FaceLandmarker→frontality 排序→region overlay）
> + `explore_idx.py`（索引浏览器）+ `render_hyp.py`（假设折线对比）。
> 数据：`incoming/无创案例库/无创注射案例库/` 21 患者真实术前照，FaceLandmarker(478) 100% 检出。
> 自动按 frontality（nose-tip 对称 + transform-matrix yaw）排序取最正 8 张校准。
> 证据图：`delivery/phase1-calibration/`。

## 校准结论（按 owner 钦定的 颧骨/下颌线/法令纹）

| 区 | 旧状态 | 校准结果 | 动作 |
|---|---|---|---|
| **下颌线** | high | ✅ 在所有正脸上精准沿下颌轮廓，点序平滑无交叉 | 不改 |
| **法令纹** | inferred（**破损**）| ❌ 旧索引含上唇点(269/267/39/37)→折线在嘴角**回钩**；路径贴鼻不贴沟 | ✅ **已修** |
| **颧骨** | high | ⚠️ 实为 face_oval 侧轮廓(颧弓 arch 解读)，非正面颧骨突起 | 见下「重大发现」，暂不重做 |

### 法令纹修法（已 commit）
```
旧 left_idx  [358, 429, 279, 331, 294, 327, 291, 269, 267]  → 含上唇/口角内点，回钩
旧 right_idx [129, 209,  49, 102,  64,  98,  61,  39,  37]
新 left_idx  [358, 423, 426, 322, 410, 287]   # ala→沿沟→口角外下
新 right_idx [129, 203, 206,  92, 186,  57]
confidence: inferred → calibrated
```
深法令纹老年脸（黄阿红）+ 年轻脸（黄艺玲）双验证：新折线干净贴合可见折痕，无回钩。
证据：`法令纹下颌线-validated-{黄阿红,黄艺玲}.jpg`。

### 顺带验证的高频 inferred 区（未改，确认可用）
- **泪沟**（19 例，最高频）：✅ 叠点落在下睑缘/泪沟带，确认 atlas doc「精准」。
- **苹果肌 / 面颊**（各 4 例）：✅ 落在中颊脂肪垫，两区嵌套重叠属解剖预期。

## 🔴 重大发现 1：颧骨在真实语料 0 例

115 个真实 case 目录术式关键词频次：

```
19 泪沟   13 下巴   10 鼻    9 唇    7 颈    5 下颌线
 4 卧蚕    4 咬肌    4 面颊   4 法令纹  4 川字   4 苹果肌   4 太阳穴
 2 眉      1 印第安   1 山根   1 额头   0 颧骨 ←
```

→ **颧骨 owner 钦定校准但真实业务 0 命中**。已在 atlas 标 `uncalibrated-unused` +
注释（当前是颧弓 arch 解读；若日后出现颧骨缩小/填充 case，需区分「骨性突起椭圆」vs
「颧弓 arch 折线」再校准，且必须与已有 苹果肌/面颊 区分避免同区重叠）。**建议：暂不重做。**

## 🔴 重大发现 2：atlas 缺真实高频区

真实 case 用到但 atlas **没有**的部位：

| 部位 | 真实例数 | atlas 有? | 说明 |
|---|---|---|---|
| **咬肌** | 4 | ❌ 缺 | 下颌角咬肌肥大（瘦脸针主战场）|
| **川字纹** | 4 | ❌ 缺 | 眉间纵纹（除皱针）|
| **太阳穴** | 4 | ❌ 缺 | 颞部凹陷填充 |
| 印第安纹 | 1 | ❌ 缺 | 中颊纵沟（泪沟外延）|
| 山根 | 1 | ❌ 缺 | 鼻根（已有「鼻尖/鼻基底」但无山根）|
| 额头 | 1 | ❌ 缺 | 额部填充 |
| 颈 | 7 | — | 颈纹（面部网格外，不适用 FaceMesh）|

→ **咬肌/川字/太阳穴 各 4 例 > 颧骨(0)**，标注价值更高。这三个区的 landmark 锚点都可定
（咬肌=下颌角 face_oval 下半弧+58/172/215；川字=眉间 168/6/8/9/107/336/55/285；
太阳穴=颞部 21/54/103/eyebrow 尾外侧），但需同样的 explorer→validate 校准循环。

## 验证 oracle
- 显式叠点 overlay → 人眼可见每点落位（这次不再「不可感知」）。
- 单测 `test_facial_region_atlas.py` 5 passed（含校准 confidence 值）；ruff clean。

## owner 决策（2026-05-29）：补缺 3 区 + 重做颧骨 — ✅ 已完成

owner 钦定最全路线。本 session 用同样 explorer→hypothesis→validate 循环完成：

| 区 | shape | 真实例 | 索引（patient-right / 单区）| 验证 |
|---|---|---|---|---|
| **咬肌** | ellipse | 4 | `[58,172,136,150,169,210,214,138,135]` | ✅ 落下颌角/下颌支肌腹 |
| **川字** | ellipse | 4 | `[9,8,168,107,336,55,285,66,296]`(midline 单区) | ✅ 落眉间纵纹带 |
| **太阳穴** | ellipse | 4 | `[21,54,68,46,70,156,139,162]` | ✅ 落颞窝凹陷 |
| **颧骨**(重做) | polyline→**ellipse** | 0 | `[116,117,118,119,100,101,50,36]` | ✅ 正面骨性突起，高于/外于苹果肌 |

全部 `confidence: calibrated`，左右用标准 MediaPipe 镜像对。黄阿红(深纹老脸)+黄艺玲(年轻脸)
双验证；证据 `4新区-from-atlas-{黄阿红,黄艺玲}.jpg`（直接读 committed atlas 渲染）。
单测 `test_facial_region_atlas.py` 6 passed（+`test_phase1_new_regions_present_and_resolve`）；ruff clean。

atlas 现覆盖 **16 区**：泪沟/卧蚕/眼袋/法令纹/苹果肌/面颊/颧骨/下颌线/下巴/鼻基底/鼻翼/鼻尖/唇/咬肌/川字/太阳穴。
真实语料剩余未覆盖：印第安纹(1，泪沟外延)/山根(1)/额头(1)/颈纹(7，FaceMesh 网格外)。

## ⚠️ 给 Phase 2 panel 设计的已知约束
- `resolve_region_key()` 只返回**单个**区。真实 case 目录常是多术式（如「下颌线、颈阔肌咬肌」
  「面颊，下巴」），panel 需画**全部** focus_targets → 要新增「全匹配抽取器」（返回 list）。
  当前 substring 匹配按 dict 顺序命中第一个，多区会漏。
