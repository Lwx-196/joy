# 真 gpt-image-2 端到端 smoke runbook（补 anchored-simulation Phase 2 Exit）

> 目的：在**生产函数** `run_after_simulation(mode=effect_projection)` 里真跑一次 gpt-image-2，
> 验证「真 AI 出图 + mask 锚定硬底线 + 身份保真」。Phase 0 demo `run_case45.py` 已用真 AI + 真判官
> 7/7 验证过同一序列；本 runbook = 在生产函数里真跑一次，闭合 Phase 2 Exit 的最后一项。
> base = `feat/ai-effect-mappings`（Phase 1+2）。owner-gated（需 PS env + AI quota）。

## 前置
- **PS env**：`飞书Claude/claude-feishu-bridge/.env`（PS 脚本 `case_layout_enhance.js` 自读，勿扫凭证）
- **AI quota**：gpt-image-2 via tuzi（单次成功 ~58s；偶发 120s 上游超时 = F4'，重试即可）
- **源照**：`飞书Claude/医美资料/陈院案例(1)/康巧佳/2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴/术前1.JPG`
- **venv**：`案例生成器/case-workbench/.venv/bin/python`（PIL 够用；effect mask 走 separate-ellipse PIL，**不需 mediapipe**）

## 步骤

### 1. 解析真实术式 → effect_pairs + do_not_touch（精准对应）
```python
from backend.services import procedure_region_mappings as prm
p = prm.parse_procedures("2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴")
assert not p["needs_human_review"]
effect_pairs = [(pr["project"], r) for pr in p["procedures"] for r in pr["regions"]]
focus_targets = p["all_regions"]                 # [川字, 额纹, 下巴, 唇]
do_not_touch  = ["苹果肌", "泪沟", "咬肌", "鼻", "眼", "面颊"]
```

### 2. 真跑生产函数（provider=ps_model_router，effect 模式 opt-in）
```python
from pathlib import Path
from backend.ai_generation_adapter import run_after_simulation, EFFECT_PROJECTION_MODE
SRC = Path("/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)/康巧佳/"
           "2025.10.29衡力20抬头、川字、海魅1.0ml注射唇、下巴/术前1.JPG")
res = run_after_simulation(
    provider="ps_model_router",
    job_id=990001,                               # 临时 job id
    after_image_path=SRC,
    before_image_path=None,
    focus_targets=focus_targets,
    brand="fumei",
    mode=EFFECT_PROJECTION_MODE,
    effect_pairs=effect_pairs,
    do_not_touch=do_not_touch,
)
```

### 3. 验收（硬指标）
```python
pol = res["audit"]["policy"]
assert pol["simulation_mode"] == "effect_projection"
assert pol["mask_anchored"] is True
refs = {r["kind"]: r["path"] for r in res["output_refs"]}
assert "effect_anchored" in refs                 # 锚定结果产出
# 硬底线：anchored 图 mask 外角落 == 原图（管线内已 outside_exact 核验 + 日志；此处可复核）
from PIL import Image
anchored = Image.open(refs["effect_anchored"]).convert("RGB")
orig = Image.open(SRC).convert("RGB").resize(anchored.size)
assert anchored.getpixel((5, 5)) == orig.getpixel((5, 5))   # 左上角在治疗区外
```

### 4. 验收（肉眼 / 可选 judge）
- **肉眼**：唇/下巴自然丰盈 + 额纹/川字变浅保表情 + 去泛红；身份保住（脸型/痘印/肤质不变）；只改 4 治疗区。
- **方向对标**：与 Phase 0 demo `runA_composite.jpg` 同方向（不要求逐像素一致 —— 不同 AI 调用 + PNG/JPEG）。
- **可选 effect judge**（effect-judge 线落地后）：
  ```
  python -m backend.scripts.comfyui_vlm_judge_runner --packet-json <effect packet> \
    --env-file case-workbench/tasks/t54_vertex_adc.local.env  # Vertex ADC, 4 criteria
  ```

## 通过判据（闭合 Phase 2 Exit）
- [ ] 真 gpt-image-2 出图成功（success=True）
- [ ] simulation_mode=effect_projection / mask_anchored=True / effect_anchored ref 存在
- [ ] mask 外字节级 == 原图（身份硬底线）
- [ ] 肉眼：效果方向对 + 身份保 + 只改治疗区

## 注意 / 回滚
- effect mode **opt-in**：不传 `mode` 则默认 fidelity，现有 after_simulation 行为逐字不变。
- K-1 失败安全：mask-anchor 任何异常 → 返回 raw AI（不阻断交付，记日志）。
- gpt-image-2 偶发 120s 超时 → 重试（生产 PS router 已有重试；手跑时重跑即可）。
- 此 runbook 是验证脚本，**不**进自动化测试（需真 AI quota）；自动化覆盖见
  `test_effect_mask_anchor_wiring.py`（fake-subprocess 集成测）。
