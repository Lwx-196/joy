# P4 Gate Result — FOCAL vs shipped board (C2 决定性 NO-GO)

> 2026-05-31 · 真 Vertex ADC gemini-3.5-flash judge + 真 ComfyUI 重渲 · N=12
> F-3 方法学修复后首次干净 gate（candidate 复用 baseline 精确 slot，唯一差异=FOCAL 像素）

## 裁决：gate_pass = FALSE（win_rate 0%）

| | |
|---|---|
| candidate (FOCAL) wins | **0** |
| baseline (shipped board) wins | **1**（徐莹）|
| ties | **11** |
| win_rate | **0.0**（threshold 0.6）|
| 方法学 | slot_reuse 12/12 · 0 drop（pre-F3-fix 56% drop）· judge 跑全 12 · 0 error |

工件：`/tmp/focal-p4-gate/{packet,results,judge-report,gate-report}.json` + `_view/`（12 张 candidate + 徐莹 baseline）。

## 判读

- **11 平 = judge 真说"两图完全相同"**（results.json `manual_review_judgments` rationale："completely identical / perfect tie"）。board_diff 显示 FOCAL 真改了像素（pct_pixels_gt5 3–11% / mean_delta 1–1.8），但 **board 产品尺度判官看不见**——focal 区是整板一小块，feather-composite 回整板后被冲淡。
- **1 输（徐莹，唯一肉眼可辨）**：judge 选 baseline——"医美文档偏好真实中性肤色，baseline 比 candidate（暖色/提亮）更好保留自然肤色"。FOCAL 可见时反而更差。
- 结论：**FOCAL/ComfyUI AI 增强在 board 产品尺度 = 不可见(11) 或有害(1)**。

## Owner 看图观察（2026-05-31，Preview/Finder）

baseline 更自然，两点：① 自然血色更健康 ② 保留痘痘/泛红/纹理。FOCAL **磨皮太明显 + 整体偏暗/灰度偏高**。

## 根因 = 模型选型 + 增强哲学错配（不是参数微调能救）

focal workflow `comfyui-workflows/portrait_focal_enhance_v1.json`：

| 项 | 值 |
|---|---|
| **模型** | **`sd-v1-5-inpainting.ckpt`** = Stable Diffusion v1.5 局部重绘（2022、512 原生、低保真）|
| 节点 | LoadImage+Mask+FeatherMask → CheckpointLoaderSimple → InpaintModelConditioning → KSampler(dpmpp_2m/karras) → VAEDecode |
| FOCAL 档 | steps=20 / cfg=4.0 / **denoise=0.40**（重绘 40% 潜变量）|
| 提示词 | 正向 "high quality skin texture..." + 泪沟/法令纹片段写 **"smoothed... brighter"**（主动磨平+提亮）；负向 "skin smoothing **outside mask**"（只管 mask 外）|

- **磨皮/丢纹理** ← SD1.5 @ denoise 0.40 重绘低保真皮肤，画不出真实毛孔/痘痘/毛细血管；负向 guard 只约束 mask 外。
- **偏暗/灰度高/丢血色** ← SD1.5 + 采样去饱和压暗 + 提示词主动 "smoothed/brighter"。
- **🔴 model/prompt 不匹配**：`focal_prompt_library.build_focal_prompts` docstring 写 "SDXL prompts"，workflow 实际加载 **SD1.5** → 配方错配。

## 转向建议

1. **主线转"渲染质量 + 标注 QA"**（runbook 钦定）：打磨现 layout-only 产品（抠图/合成/版式/标注精度）。小绿 case 正面术后是灰背景+顶部模糊的烂抠图——**真实板质量问题在 layout/合成，FOCAL 不碰**。已建 ~80% 治理/ops/promotion 基础设施对任何渲染路径复用。
2. **若重启 focal**（非当前 board 产品）：denoise 砍到 0.05–0.10 极轻 / 换保真模型（SDXL / Flux Fill）/ 不用生成模型（古典锐化，layout 已有 unsharp）。
3. board 尺度 NO-GO 结论不随上述改变——focal 单图 zoom 可能有效，但产品是 board。

## F-3 fix（本次交付的工具改进，独立于 NO-GO 仍有价值）

`backend/scripts/focal_p4_packet_builder.py`：candidate 复用 baseline manifest 精确 slot（`run_render(selection_plan=)` 现成 seam，零改 render_executor）→ 消除"现场重渲 vs 人工策展"苹果对橘子混淆。22 passed / ruff clean / stub dry-run 验证 applied_slots 与 baseline 逐一致。**未 commit**（owner 决策是否落 main）。
