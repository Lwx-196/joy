<!--
生成上下文（2026-06-01）：anchored-sim Phase 3.3 首次真 effect-projection 校准。
- 出图：gpt-image-2-vip（tu-zi），经 `--api-direct` Python urllib 传输（node undici 被本地代理 socket-reset，Python/curl 能穿透；切 Vless/TCP 节点后 Python 稳定）。
- 锚定：生产 `_apply_effect_mask_anchor`（separate-ellipse mask 合成，identity 锁回 mask 外）。
- 判官：effect_projection profile，Vertex ADC（t54），EffectDeliveryQA gate（含 Step 1.5 no_visible_change 预检）。
- 样本：全库 22 focus-eligible 仅 3 projectable（品牌门控，详见 runbook 校准样本约束）。

Phase 3 Exit 结论：判官 4-criteria **区分力确认**——精准分辨 mask 拼接缝 artifact（康巧佳/蓝凤端 winner=baseline）与 AI 无效果（许楚楚 winner=tie），非 Step 1 的幻觉橡皮章。
真发现：当前 `_apply_effect_mask_anchor` 的 separate-ellipse 合成留下**可见圆形/边界缝** → 下一轮需羽化 mask 边缘。
-->
# Effect-projection calibration report (anchored-sim Phase 3.3)

- packet scope: `effect_calibration_packet_v1` (real effect projection)
- judge items: 3
- gate pass: 0/3 (0.0%)
- verdict distribution: `{'fail': 3}`
- winner distribution: `{'baseline': 2, 'tie': 1}`

## Per-case

| case | effect_pairs | verdict | winner | confidence | note |
|---|---|---|---|---|---|
| 康巧佳__2025.10.29衡力20抬头_川字_海魅1.0ml注射唇_下巴 | botulinum_toxin/川字, botulinum_toxin/额纹, HA_filler/下巴, HA_filler/唇 | fail | baseline | 0.85 | Image B contains highly visible circular patch overlays/artifacts on the chin area, violating the seamless edit requirement. |
| 蓝凤端__2025.12.26丰颜2支注射隆鼻_苹果肌_质颜1支注射鼻基底_衡力50U川字鱼尾抬头纹 | botulinum_toxin/川字, botulinum_toxin/额纹 | fail | baseline | 0.80 | visible_mask_boundary_on_forehead |
| 许楚楚_许诺___2026.3.31缇颜1支眉弓_越致1支_海魅骨性1支_注射鼻子下巴 | HA_filler/下巴, HA_filler/鼻背 | fail | tie | 0.85 | The candidate image shows no visible changes compared to the baseline. Since no  |
