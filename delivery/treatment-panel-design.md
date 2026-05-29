# 治疗区标注 panel 渲染器 — 设计 + 交付（Phase 1 panel MVP）

> 2026-05-30。plan `anchored-focal-annotation.md` Phase 1 第二步「设计 panel 渲染器」。
> owner 决策 2026-05-29：**底图=cv2 边缘线稿（0 付费，不烧 img2img）**；
> **交付=独立 panel 图（不动 case-layout-board）**。

## 管线（全 0-quota，本地）

```
术前照(BGR)
  ├─▶ cv2.pencilSketch → 白底深线线稿（退回 adaptiveThreshold）
  └─▶ FaceLandmarker(478,Tasks API) → 像素坐标
            │
            ▼ extract_regions(术式文本) → 全部命中 atlas 区
            ▼ region_geometry(区,478点) → 按 atlas shape 算像素几何
                ellipse/polygon → 闭合多边形(fill)；polyline/ribbon → 有序路径(stroke)
            ▼ PIL 合成：半透明色块(alpha 0.38) + CJK 标签(白底圆角 pill + 引导色)
  ▼ 独立 panel 图(BGR)
```

## 代码

| 文件 | 职责 |
|---|---|
| `backend/services/treatment_zone_panel.py` | 几何层(纯函数,可单测) + IO 层(lineart/landmark) + PIL 合成 |
| `backend/scripts/render_treatment_panel.py` | CLI：`--image`/`--case-dir` + `--focus` + `--model` → panel |
| `backend/services/facial_region_atlas.py` | +`extract_regions()` 多区抽取（多术式目录用） |

分层：`region_geometry` / `extract_regions` 是纯函数（合成 478 点即可测，不依赖 mediapipe）；
landmark 检测 lazy import mediapipe；CJK 字体回退与 case-layout-board 同源。

## 形状映射

| atlas shape | 绘法 | 例 |
|---|---|---|
| ellipse | fitEllipse(≥5点)/bbox 内切 → 多边形 fill | 颧骨/苹果肌/咬肌/川字/太阳穴/下巴/眼袋 |
| polygon | 凸包 → fill | 面颊/鼻翼/唇 |
| polyline | 有序路径描边(宽=脸宽×0.013) | 法令纹/下颌线 |
| ribbon | 下睑缘点序向下偏移半带宽，细描边(不吃眼球) | 泪沟/卧蚕 |

## 验证（真实数据，本地实跑）

- **diverse 8 区**（黄艺玲，泪沟/法令纹/苹果肌/下颌线/下巴/咬肌/太阳穴/川字）：
  全 16 区四种 shape 都正确落位，线稿清晰，CJK 标签全渲染。
  `delivery/treatment-panel/panel-diverse-8regions-黄艺玲.jpg`
- **真实端到端**（林真呈 目录名「玻尿酸注射面颊，下巴」）：
  `extract_regions` → `['面颊','下巴']`（多区抽取生效）→ 面颊多边形 + 下巴椭圆精准。
  `delivery/treatment-panel/panel-realcase-面颊下巴-林真呈.jpg`
- 单测 `test_treatment_zone_panel.py` 8 passed（几何层 + 多区抽取）；
  `test_facial_region_atlas.py` 6 passed；focal-p4 全相关 41 passed；ruff clean。

## 美学精修（owner 选 B「先调美学再接 board」后）

1. **线稿换 D_meanshift_canny**（6 法对比择优）：pyrMeanShift 抹平肤色斑点 → Canny 抽干净轮廓
   → 白底深线。比旧 pencilSketch 干净得多（无 grain/阴影脏块）。大图先降采样到长边 1400
   跑 meanshift（2.2s vs 旧十几秒）。证据 `/tmp/p1-calib/lineart_grid.jpg`。
2. **标签去重**：对称区原出左右两个标签 → 改为每区单标签（取最高 zone 锚点，避开下半脸拥挤）。
3. **下颌线/下巴 撞色修**：两者常同 case 出现且原配色相近 → 下颌线改紫、下巴留蓝，分明。
   下颌线描边再细（脸宽×0.011）。
4. **自动裁脸 `crop_to_face=True`**（独立 panel 默认）：裁到脸 landmark bbox + 45% 留白，
   让脸填满画面（源照常框很松/带杂物，如蔡伟玲带挂钟 → 裁掉）。接 board 传 False（board 自裁）。
5. CLI `--case-dir` 大小写不敏感（真实目录混 `.JPG`/`.jpg`）。

精修后达「参考图水准」：干净线稿脸填满画面 + 分明色块 + 单 CJK 标签。证据已刷新：
`panel-diverse-16regions-黄艺玲.jpg`（16 区全压力测试）/ `panel-realcase-下颌线下巴-蔡伟玲.jpg`
（真实 2 区，自动裁脸去挂钟）/ `panel-realcase-面颊下巴-林真呈.jpg`。

## 已知改进点（非阻塞，后续）
1. **输入帧应选正面**：CLI `--case-dir` 取 `术前*` 排序第一张，可能非正面。
   board 已分类 正面/45/侧 → 接入时喂 正面 帧即可；独立用可加 frontality 选帧。
2. **纯侧脸(90°)fallback**：FaceLandmarker 侧脸常失败 → 当前返回 None 不出 panel（待 side 策略）。
3. **下巴椭圆略低**：偏正面时贴合，偏俯视时略下移（landmark 几何固有）。

## 下一步（Phase 1 第三步，task #3）
接入 case-layout-board skill（`~/Desktop/飞书Claude/skills/case-layout-board`，独立 codebase）：
把 panel 作为新元素加进 tri-compare board，正面帧驱动。**owner 已选「先出独立 panel 不动 board」
→ 本步 deferred，待 owner 确认 panel 美学达标后再接 board。**
