# Single-image fidelity gate — arm A (classical) — Phase 1 result

> 2026-05-31 · L-140 单图增强线（保真增强，非 board 产品）· 真 Vertex ADC gemini-3.5-flash 保真-strict judge · N=12
> 承接 P4 board 尺度 NO-GO（`case-workbench-focal-p4/delivery/p4-gate-result.md`）+ L-139/L-140。

## 裁决：arm A (classical **clarity**) GATE PASS — win_rate **91.67%**

| | |
|---|---|
| candidate (classical clarity) wins | **11** |
| baseline (raw after) wins | **0** |
| ties | **1**（许楚楚，下巴被围布遮挡，focal 区无真皮可增强 → 诚实平局）|
| hard_veto（磨皮/去饱和/偏色）| **0 / 12** |
| prescreen（numpy 探针）| **12 ✓ / 0 ✗** |
| win_rate | **91.67%**（threshold 60%）→ **PASS ✅** |

判官 rationale 样本（徐莹）："improves overall sharpness without introducing any smoothing, artificial
texture loss, or color distortion, maintaining high fidelity"；evidence："natural redness on the cheeks
and the small blemish on the nose bridge remain fully visible and unaltered"。

## 关键发现（方法学，比数字更重要）

1. **感知尺度决定成败（L-139 的单图回响）**：`fine` 预设（radius 2.4 纯高频锐化）探针完美（focal HF ×3.6、
   tone/colour 不变、背景 pristine）、我 900px A/B 肉眼可见，但**判官判 TIE**——"identical / no discernible
   changes"。gemini 内部把 12MP 图降采到 ~1k px，**纯高频锐化在降采后消失**，跟 board 把 focal 冲淡同构，
   只是高一层尺度。
2. **保真增强的有效杠杆 = 中频局部对比（clarity），不是高频锐化**：`clarity` 预设加一道 scale-invariant
   局部对比（radius ∝ crop 尺寸、percent 55、threshold 0）→ 降采后仍可感知 → 判官翻成 **candidate WIN
   (11/12)**，且**零磨皮/零偏色**（所有 fidelity 准则 4=4 保持）。**结论：感知性必须在判官/终端观看尺度成立，
   且只能靠中频，不能靠高频。**
3. **保真严格判官有判别力**（非橡皮图章）：徐莹/王嘉琦真清晰提升 → WIN；许楚楚 focal 区被围布遮挡无真皮
   → 诚实 TIE。0 hard_veto 说明 clarity 强度没越界成"处理感"。

## 修的真 bug

- **EXIF 朝向错配**：`classical_enhance` 输出 display 朝向，但 probe/mask 读原图未 `exif_transpose` →
  手机带旋转 EXIF 的照片 raw(stored) vs enhanced(transposed) 错位 → 假的整帧 diff（out-mask Δ 84）+ focal
  判官裁剪错位。**初版 N=12 有 5 个假 prescreen-fail 全是这个**。修法 = staging 时一次性
  `exif_transpose` 落 PNG，raw/mask/enhanced/judge 同朝向 → 修后 **12✓/0✗**。

## 复现

```bash
cd ~/Desktop/案例生成器/case-workbench-single-image-fidelity
PY=~/Desktop/案例生成器/case-workbench/.venv/bin/python   # 完整 backend venv（Levenshtein+fastapi+numpy+cv2+PIL+pytest）
T54=~/Desktop/案例生成器/case-workbench/tasks/t54_vertex_adc.local.env

# 1. 建 packet（classical clarity, focal view, N=12）
$PY -m backend.scripts.single_image_packet_builder \
    --arm classical --classical-preset clarity --n 12 --judge-view focal \
    --scratch-root /tmp/single-image-gate2 --output-packet /tmp/single-image-gate2/packet.json
# 2. 保真 judge（packet 自带 judge_profile=single_image_fidelity，runner 自动切 framing）
set -a; source "$T54"; set +a
$PY -m backend.scripts.comfyui_vlm_judge_runner \
    --packet-json /tmp/single-image-gate2/packet.json --packet-root / --env-file "$T54" --concurrency 4 \
    --results-output /tmp/single-image-gate2/results.json --report-output /tmp/single-image-gate2/report.json
```

## 交付物 / 代码

- `backend/services/classical_enhance.py` — arm A：focal UnsharpMask，预设 `fine`/`clarity`，复用
  `_focal_crop_bbox`/`_composite_focal`（与 AI 臂同管线，只换 crop op），K-1 silent-fail。
- `backend/services/fidelity_probes.py` — 3 numpy 探针（focal HF 比 / tone-colour 偏移 / 背景局部性）+
  prescreen 裁决，判官前零配额淘汰磨皮/重绘。
- `backend/scripts/single_image_packet_builder.py` — 单图 packet builder：复用 focal_p4 discovery，砍掉
  board render，`--judge-view focal|full`，`--classical-preset fine|clarity`，EXIF 归一，full_res 留作交付。
- `backend/scripts/comfyui_vlm_judge_runner.py` — 加 `judge_profile=single_image_fidelity` 保真严格 framing
  （packet 驱动，board 默认行为不变）。
- 测试：`test_fidelity_probes.py` / `test_classical_enhance.py` / `test_single_image_packet_builder.py`
  （14 新测试）。**全盘 1110 passed / 3 skipped / 0 regression，ruff clean。**

## 下一步（plan `~/.claude/plans/faithful-zoom.md`）

- **arm A 已过 gate（91.67%）→ "现状能解决就不引提案" 触发**：SDXL-light / gpt-image-2 / Flux 是否还要跑，
  取决于 owner 是否要 cross-arm 对比（看谁更强）或直接 wire arm A clarity 进单图产品（Phase 5）。
- **1 tie 教训**：focal 区被遮挡/无真皮的 case（许楚楚 下巴+围布）应在 case 选择层过滤，或选展示 focal 区的
  representative after（当前默认第一张 after，可 `--after-name` override）。
- **arm A' 频率分离**（ComfyUI-Image-Filters EnhanceDetail）= 更精细的 detail 杠杆，可作 arm A 的进阶对照。
- **clarity 强度可调**：当前 percent 55 局部对比是保守值（0 veto），可上探"更强 clarity vs 处理感"边界。
