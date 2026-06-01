# Effect-projection calibration report (anchored-sim Phase 3.3)

- packet scope: `effect_calibration_packet_v1` (real effect projection)
- judge items: 3
- gate pass: 0/3 (0.0%)
- verdict distribution: `{'fail': 3}`
- winner distribution: `{'baseline': 1, 'tie': 2}`

## Per-case

| case | effect_pairs | verdict | winner | confidence | note |
|---|---|---|---|---|---|
| 康巧佳__2025.10.29衡力20抬头_川字_海魅1.0ml注射唇_下巴 | botulinum_toxin/川字, botulinum_toxin/额纹, HA_filler/下巴, HA_filler/唇 | fail | baseline | 0.85 | untreated_region_altered_with_artifacts |
| 蓝凤端__2025.12.26丰颜2支注射隆鼻_苹果肌_质颜1支注射鼻基底_衡力50U川字鱼尾抬头纹 | botulinum_toxin/川字, botulinum_toxin/额纹 | fail | tie | 0.80 | Since the candidate image is pixel-for-pixel identical to the baseline, no treat |
| 许楚楚_许诺___2026.3.31缇颜1支眉弓_越致1支_海魅骨性1支_注射鼻子下巴 | HA_filler/下巴, HA_filler/鼻背 | fail | tie | 0.85 | The candidate image fails to project any visible treatment effects in the specif |
