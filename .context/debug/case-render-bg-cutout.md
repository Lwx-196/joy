---
slug: case-render-bg-cutout
mode: find_root_cause_only
status: root_cause_found
next_action: 主线决定是否对 case_layout_board.py 的 apply_conservative_background_policy 链路做保守化或 short-circuit
hypotheses_total: 2
hypotheses_refuted: 1
---

# Debug Session

## Symptoms
案例工作台「正式出图」抠图脏：背景边缘脏 / 白底反向钻进皮肤 / 棋盘格残留 / halo。
阶段 13 已用 v2 short-circuit 修过 render_brand_clean.whiten_background。
现在 bug 又出现，疑似新路径或参数退化。

## Key Files
- `~/Desktop/飞书Claude/skills/case-layout-board/scripts/render_brand_clean.py` (mtime 5/6)
- `~/Desktop/飞书Claude/skills/case-layout-board/scripts/case_layout_board.py` (mtime 5/2，1MB+)
- `~/Desktop/案例生成器/case-workbench/backend/render_executor.py` (调度，e3d3497)

## Hypothesis Chain

### H1 ❌ REFUTED — 阶段 13 v2 short-circuit 被回退
**Description**: render_brand_clean.whiten_background 重新引入了 flood-fill 反向钻进皮肤的逻辑。
**Falsifiable test**: `grep "def whiten_background"` + 读取函数体。
**Evidence**:
```
/Users/a1234/Desktop/飞书Claude/skills/case-layout-board/scripts/render_brand_clean.py:1330:
def whiten_background(img: Image.Image) -> Image.Image:
    return img
```
v2 short-circuit 仍在原位，未回退。该函数为 no-op，不可能产生脏边。

### H2 ✅ CONFIRMED — case_layout_board.py 出现第二条抠图路径
**Description**: case_layout_board.py 实现了完整的二级背景清理管线（`apply_conservative_background_policy` →
`compose_face_on_white_background`），在每张人脸 cell 仿射对齐后强制做 HSV 阈值抠图 + edge floodFill +
foreground dilate + Gaussian feather alpha-blend 到 (248,248,246) 白底。这条路径在 render_brand_clean
之前执行，是当前真正的脏边来源。

**Falsifiable test**: grep `compose_face_on_white_background` / `apply_conservative_background_policy`
+ 读取调用链与默认 BACKGROUND_MODE。

**Evidence**:
```
case_layout_board.py:59:
BACKGROUND_MODE = os.environ.get("CASE_LAYOUT_BACKGROUND_MODE", "auto-preserve-original-tone")

case_layout_board.py:3001-3011:
def apply_conservative_background_policy(image, slot, valid_mask=None):
    policy = BACKGROUND_MODE
    if policy in {"white-only", "preserve-contour"}:
        filled, foreground_mask = compose_face_on_white_background(...)
        return filled, foreground_mask, {"status": "legacy_white_blend"}
    ...
    if allow_clean_white and clean:
        # 同样做 alpha=GaussianBlur(bg_mask*255, sigma=6) 的混合
        composed = rgb*(1-alpha) + (248,248,246)*alpha

case_layout_board.py:2897-2917  compose_face_on_white_background:
    # HSV: s<=58, v>=105 当作背景候选
    candidate = (hsv[:,:,1] <= 58) & (hsv[:,:,2] >= 105)
    bg_mask = edge_connected_background_mask(candidate)  # 调 cv2.floodFill 行 2834
    bg_mask &= ~dilated_foreground_mask(foreground_mask, padding_px=14)
    alpha = GaussianBlur(bg_mask*255, sigma=6) / 255   # 软羽化
    composed = rgb*(1-alpha) + (248,248,246)*alpha     # 混到近白

case_layout_board.py:3241-3262  prepare_face_cell_for_board:
    filled_arr, foreground_mask, cleanup = apply_conservative_background_policy(aligned_arr, slot, valid_mask=valid_mask)
    # 这是每个 cell 进入正式排版前的强制路径
```

**链路证据**：
- render_executor.py:343 调用 `render_module.whiten_background(...)` ← 已 short-circuit 无害
- 但在到达 whiten_background 之前，`prepare_face_cell_for_board` 已经把每个 face cell 跑过
  `compose_face_on_white_background` / `apply_conservative_background_policy`
- 阶段 13 短路了 whiten_background，但 case_layout_board.py 的二级抠图链路是独立模块化的，
  不受 render_brand_clean.py 的 short-circuit 影响

**为什么会脏（机制）**：
1. HSV 阈值 `s≤58, v≥105` 在头发与皮肤过渡区、淡阴影、镜面高光都会误判成背景
2. `edge_connected_background_mask` 通过 cv2.floodFill (从 4 角种子点向内 flood) 还原"反向钻进皮肤"的同款机制
3. `dilated_foreground_mask(padding_px=14)` 试图保护人物，但 dilate 14 像素在头发边缘不够
4. 软 alpha = GaussianBlur(σ=6) 把布尔 mask 羽化后做线性混合，
   头发/皮肤边缘的 partial alpha 区域会被部分混入纯白底 (248,248,246)，
   造成"灰边/halo/棋盘格状渐变"——正是用户报告的现象
5. policy = "white-only" 或 "preserve-contour" 时直接走 legacy_white_blend，
   没有 evaluate_clean_background_candidate 的"够干净才白化"门槛
6. 即使 policy = "auto-preserve-original-tone"（默认），
   只要 edge_connected_background_mask 抓到 candidate 区域就会做混合

**与症状的对应**：
- "背景边缘脏" → GaussianBlur σ=6 + 软 alpha 混合的副产物
- "白底反向钻进皮肤" → edge_connected_background_mask 的 floodFill 在 HSV 阈值误判区扩散
- "棋盘格残留" → 软 alpha 在低纹理区抖动 + dithering
- "头发边缘 halo" → padding_px=14 dilate 不够，发丝细节被混入白底

## Root Cause
case_layout_board.py 的二级抠图管线 `apply_conservative_background_policy` →
`compose_face_on_white_background` 是当前脏边的真实来源。它独立于 render_brand_clean.whiten_background，
所以阶段 13 的 short-circuit 修不到它。该路径用 HSV 阈值 + floodFill + dilated foreground mask +
Gaussian-feathered alpha-blend，在头发/皮肤过渡区会产生用户报告的全部四类症状。

## Suggested Fix Direction
（参考阶段 13 lesson "盲改看起来更安全的常量可能反向恶化"，三种方案按风险排序）

**方案 A（最保守，对应阶段 13 v2 同款）**：
让 `apply_conservative_background_policy` 在 `BACKGROUND_MODE != "clean-white"` 显式模式时
直接 `return image, dummy_mask, {"status": "noop"}`，跳过所有 HSV+floodFill+blend 逻辑。
等价于"默认不做任何二级抠图，只在显式开关时启用"。

**方案 B（次保守）**：
保留管线，但默认 policy 改为 `"raw-passthrough"`（新增），让 `auto-preserve-original-tone`
也走 `fill_invalid_background_to_sampled_tone`（line 3034）路径——这条路径只填仿射产生的
外部空洞，不动人物原始像素。这正是 case_layout_board.py:59 注释里"保守优先"的本意。
但实际代码 line 3003 的 `if policy in {"white-only", "preserve-contour"}` 让两种 legacy 模式
都意外走回 `compose_face_on_white_background`，绕过了"保守"分支——这本身就是 bug。

**方案 C（激进）**：
重写 `compose_face_on_white_background`，放弃 HSV 阈值，改用 MediaPipe segmentation +
trimap-guided matting（如 GuidedFilter），从源头消除发丝 halo。改动面大，需要新依赖。

**强烈建议主线选 A 或 B**，并在测试集（蔡伟玲 / 陈莹颈纹 / 许楚楚等已知脏样本）上做前后对比。
不要盲调 `padding_px` / `sigma` / HSV 阈值这些常量——阶段 13 lesson 已说明这种调整经常反向恶化。
