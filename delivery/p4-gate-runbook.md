# P4 真 gate runbook — focal vs layout-render（决定 CVGA GO/NO-GO）

> 整条 wiring 已 stub 验证（2026-05-30）：discovery 22 case / 19 proven-renderable / 真 layout 渲染跑通 / packet 结构正确。**唯一门槛 = owner 起 ComfyUI :8188。**

## 关键简化

- **baseline = 已发货 final-board**（`baseline_strategy=existing-board` 默认，**0 quota**）→ 真 gate **只需 ComfyUI**，连 POLISH 付费 enhance 都不用。
- candidate = FOCAL crop+composite（PR #41）经 ComfyUI 重渲 → layout board。
- judge = Vertex ADC gemini（凭证 `tasks/t54_vertex_adc.local.env`，ADC 无密钥）。
- gate = candidate win-rate **≥ 60%** over N≥10。

## 解释器

用有 fastapi 的 venv（packet builder import ai_generation_adapter→fastapi）；FOCAL 渲染走 skill 子进程（system python with cv2/mediapipe）。candidate 真渲约 ~5min/case on MPS → N=10 约 30-50 分钟（plan C2.2）。

## 真跑序列（owner 起 ComfyUI :8188 后）

```bash
cd ~/Desktop/案例生成器/case-workbench-focal-p4
PY=<有fastapi的venv>/bin/python   # 真跑机器需 numpy/cv2/mediapipe（ComfyUI 机器自带）
# 已验证可用: /Users/a1234/Desktop/案例生成器/case-workbench-stream-b-ops/.venv/bin/python (py3.12 + fastapi)
T54=/Users/a1234/Desktop/案例生成器/case-workbench/tasks/t54_vertex_adc.local.env  # 凭证在 main checkout，非本 worktree

# 1. 建 packet（真 FOCAL，去掉 --stub-enhance），N≥10
$PY -m backend.scripts.focal_p4_packet_builder \
    --cases-root "/Users/a1234/Desktop/飞书Claude/医美资料/陈院案例(1)" \
    --n 12 --scratch-root /tmp/focal-p4-gate \
    --output-packet /tmp/focal-p4-gate/packet.json

# 2. 跑 VLM judge（Vertex ADC）
source "$T54"
$PY -m backend.scripts.comfyui_vlm_judge_runner \
    --packet-json /tmp/focal-p4-gate/packet.json --packet-root / \
    --env-file "$T54" \
    --results-output /tmp/focal-p4-gate/results.json \
    --report-output /tmp/focal-p4-gate/judge-report.json

# 3. gate report（win-rate ≥60% GO/NO-GO + board pixel-diff perceptibility）
$PY -m backend.scripts.focal_p4_gate_report \
    --packet-json /tmp/focal-p4-gate/packet.json \
    --results-json /tmp/focal-p4-gate/results.json \
    --output /tmp/focal-p4-gate/gate-report.json
```

## 判读

- **candidate win-rate ≥ 60%** → GO：focal 真比当前产品 board 好 → 走 CVGA C2-C5（promotion/灰度/SLA）。
- **< 60%（含大量平手）** → NO-GO：focal 不值 ComfyUI 的 latency/cost/OOM → 正式转"渲染质量 + 标注 QA"主线。
- gate report 附 board pixel-diff：平手 + 近零 diff = "focal 改动在 board 尺度不可见"（P4 N=1 预警）；平手 + 大 diff = "judge 看到了但不偏好"。

## 已验证事实（stub run 2026-05-30）

- cases-root 正确路径 = `飞书Claude/医美资料/陈院案例(1)`（**非** docstring 里 stale 的 incoming/无创案例库）。
- 19 proven-renderable ≥ N=10 有余量；个别 case 因角度槽不全被 drop（袁霞）属正常。
- candidate board 真渲出（296KB JPG）；packet baseline/candidate source_path 双臂正确。
