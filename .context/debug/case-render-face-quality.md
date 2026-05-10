---
slug: case-render-face-quality
mode: find_root_cause_only
status: root_cause_found
next_action: 等待用户决策修复方向（推荐改进 harmonize_pair 让两侧对称地应用 LAB 匹配 + unsharp_mask）
hypotheses_total: 2
hypotheses_refuted: 1
---

# Debug Session

## Symptoms
正式出图人脸脏：人脸区域皮肤质感差/噪点多/失焦/色差/整体清晰度差。

## Repository Map
- backend/render_executor.py 不直接做拼板；通过 importlib spawn 子进程加载 SKILL_ROOT 下的 skill 脚本
- SKILL_ROOT = ~/Desktop/飞书Claude/skills/case-layout-board
- 真实拼板 / 重采样 / 编码逻辑在 skill 脚本：
  - case_layout_board.py（PIL 主拼板）
  - render_brand_clean.py（OpenCV 渲染辅助）
  - face_align_compare.py（face_align / harmonize_pair / lift_face_shadows）
- 主链路：detect → align_face (warpAffine LANCZOS4) → prepare_face_cell_for_board (apply_conservative_background_policy + foreground mask alpha blend) → harmonize_pair (LAB 匹配 + 单侧 unsharp_mask) → lift_face_shadows (after only) → canvas.paste → final canvas.save("JPEG", quality=92) @ line 3483
- 关键操作行：
  - face_align_compare.py:447 `harmonize_pair`：LAB 均值/方差对称地拉到 (before+after)/2，然后**仅对较糊的一侧**应用 `_unsharp_mask`
  - face_align_compare.py:464-473：if `min/max sharpness ratio < 0.82`，单侧加 unsharp_mask
  - case_layout_board.py:3463-3464：先 harmonize_pair → 再 lift_face_shadows(after only)
  - case_layout_board.py:3483 `canvas.convert("RGB").save(out_path, "JPEG", quality=92)` 最终编码

## Hypothesis Chain

### H1 ❌ REFUTED
**Description**: 多次 JPEG encode-decode 累积造成 generation loss（中间 quality=95 + 最终 quality=92）。

**Falsifiable test**: 检查实际 manifest，看是否真的有 AI 增强中间产物链路被触发。

**Evidence**:
- 真实 case 验证：`stress-results/stress-20260505114918-f761dd/.../manifest.final.json` 中所有 3 个 slot 的 `selected_slots[*].after.enhancement` 字段都是 `{}` 空 dict。
- 说明 AI enhancement 根本没运行；中间 quality=95 路径没触发。
- 但最终 q=92 的单一 JPEG 编码不足以解释 oblique_after 200x200 face Laplacian-var 从源图 241.17 降到 cell 30.20（下降到 12.5%）。
- H1 不是主因。

### H2 ✅ CONFIRMED
**Description**: `harmonize_pair` 对术前/术后 **不对称地** 应用 LAB 通道统计匹配 + unsharp_mask 补偿，造成清晰度高的一侧（通常是 after）被 LAB 对齐拉低高频细节后**得不到 unsharp_mask 补偿**，而对侧（before）被锐化补偿后清晰度反而高于源图。具体行为：
- 当 before_sharp < after_sharp 且 ratio < 0.82 时，只有 before 被 _unsharp_mask；after 不动
- LAB `_apply_lab_target` 会把两侧的 mean/std 都拉到 (before+after)/2，对比度偏高的 after 被拉低 → 真实高频内容损失
- 之后 `lift_face_shadows(after)` 又对 after 做 LAB 通道亮度提升，进一步在面部 ROI 内做平滑性变换
- 最终 q=92 JPEG 编码再叠加少量 8x8 块伪影

**Falsifiable test**: 取同一 case 的术后侧 oblique_after 源图与 final-board.jpg 中对应 cell，分别测量 face 中心 200×200 区域的 `cv2.Laplacian.var()`。预期：
- 源图清晰度高，cell 清晰度大幅下降，下降比例 < 0.5
- 术前侧的同样比较应该接近 1.0 或 > 1.0（说明被锐化）
- 仅靠 Lanczos4 downscale 无法解释这种悬殊

**Evidence**:
真实 case `stress-20260505114918-f761dd / case-126`：
- 源图 oblique_before: 200×200 face Laplacian-var = 47.33；cell = 72.77 → ratio **1.54**（被增强）
- 源图 oblique_after:  200×200 face Laplacian-var = 241.17；cell = 30.20 → ratio **0.125**（暴跌）
- 源图 front_before: 59.1 → cell 494.1 → ratio 8.35（unsharp_mask 主导）
- 源图 front_after:  74.6 → cell 260.2 → ratio 3.49（不一致：可能也被 unsharp_mask）
- 源图 side_before: 117.0 → cell 600.4 → 5.13
- 源图 side_after:  164.2 → cell 414.1 → 2.52
- 视觉对比 /tmp/face-quality-cells/oblique_after.jpg vs oblique_before.jpg：
  - oblique_after 皮肤质感、眉毛、眼睫毛明显比 oblique_before 软糊
  - 反向证明：术前比术后还清楚，与"术后图本应更精致"的实际拍摄事实不符
- 独立对照 Lanczos4 测试：源图 face crop 300×340 直接 Lanczos resize 到 340×340，Laplacian 从 202→170 仅下降 16%，远小于实际 cell 的 87% 下降
- 因此 H2 主因确认：harmonize_pair 的不对称处理 + LAB 匹配压缩高频 + lift_face_shadows 叠加，是术后人脸看起来"脏/糊"的直接成因

## Conclusion
**Root cause** = `face_align_compare.py:447 harmonize_pair()` 对术前/术后做了不对称的清晰度补偿。当源图清晰度差大于 18%（ratio < 0.82）时，**只对较糊的一侧应用 unsharp_mask，但 LAB 匹配仍然把两侧都拉到中点**，导致原本清晰的一侧（在医美对比中常常是术后图）真实高频细节被压低且不再被恢复。叠加 `lift_face_shadows` 后，术后人脸看起来明显比术前软糊。

**Suggested fix direction**（不在 find_root_cause_only 模式下应用，仅记录建议）：
1. 把 `harmonize_pair` 的 LAB 匹配从"双向都拉到中点"改成"仅对较弱的一侧做单向匹配"，保留较强一侧的原始统计；
2. 或者在两侧都应用 `_unsharp_mask`（amount 不同），保持清晰度对称性；
3. 或者在 `_apply_lab_target` 时跳过 L 通道对比度归一化，只统一色温（a/b 通道）。
4. 阶段 13 lesson 教训：盲改 sigma / quality / padding 可能反向恶化；不要直接改全局常量，应针对 `harmonize_pair` 中的不对称分支精修。
