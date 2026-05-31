# pose-upgrade 验证 + 激活（Phase 5b：vendor onnx + dense 真实环境验证）

> 2026-05-31。续 PR #43（已 merged → main `a4c9712`，pose_backend hybrid 已在 main）。
> plan `~/.claude/plans/profile-aware-pose.md` Phase 4/5。本文 = 真实环境 dense 模型验证记录 + 激活 runbook。
> **代码默认仍 `facemesh`（`pose_backend.py:34 _DEFAULT_MODE`）；激活 = env var，不改 code。**
>
> **最终裁决（2026-05-31 第二段全库 sweep 后定，详见 §6）：现语料激活 = DEFINITIVE NO-GO。** §2–§3 的分类层验证全过（侧脸纠偏 5/5、profile 召回 5→30），但全库 63 case 区域级 diff 得 **0 个 profile 产出升级** → 不翻默认 hybrid；§4 runbook 保留作未来 profile-dependent 语料出现时的复用入口。

## 0. 钉死的生产分类环境（推翻 plan/NOW 两处记载）

`CASE_WORKBENCH_POSE_BACKEND` 控制的是 `case_material_coverage.classify_views`（yaw 点 #1）。穷举调用链：

- **唯一消费者 = 手动 CLI**：`python -m backend.scripts.coverage_sweep` / `render_triptych` / `render_treatment_panel`（经 `treatment_panel_triptych`）。
- **不在任何 route / render_queue / upgrade_queue / scanner / cron**，不落 DB，不被任何 live 产物消费。
- 客户面渲染（`render_queue → render_executor.run_render`）走 **case-layout-board skill 子进程**（`face_align_compare.py`，跨 repo）= yaw 点 #4 = **Phase 6**，pose_backend 碰不到。
- `board_annotator`（在渲染链 `render_executor.py:1771`）用 `cov.classify_angle` 纯逻辑 + 自带 FaceMesh（yaw 点 #2，Phase 2 显式排除）→ **flag 对它零作用**。

**更正 1**：NOW 写「/usr/bin/python3 无 CV」是错的。实测各解释器都已带 cv2/mediapipe，**只缺 onnxruntime**：

| 解释器 | ver | cv2 | mediapipe | onnxruntime | 角色 |
|---|---|---|---|---|---|
| 后端 server `.venv` (`case-workbench/.venv`) | 3.12.13 | 4.13 | 0.10.35 | ❌ | uvicorn `backend.main`（API + render_queue worker，含 in-process board_annotator）|
| `/usr/bin/python3` | 3.9.6 | 4.13 | 0.10.33 | ❌ | `SKILL_PYTHON` 默认（skill 子进程 = Phase 6）|
| `/opt/homebrew/bin/python3.12` | 3.12.13 | 4.13 | 0.10.31 | ❌ | 候选 CLI python |
| **专用分类 venv（本次新建）** | 3.12.13 | 4.13.0.92 | 0.10.35 | **1.26.0** | **classify_views CLI 工具的规范运行环境** |

**更正 2**：翻 flag 只改 coverage-sweep/triptych/treatment-panel 这几个 **CLI 工具**的输出；侧脸纠偏进客户渲染产物是 **Phase 6**（跨 repo，~28 文件爆炸半径，未做）。

## 1. Vendor 清单（专用分类 venv，隔离，不污染 server/系统/ComfyUI）

- venv：`~/.cache/case-workbench-pose/venv`（homebrew py3.12 base）；deps：mediapipe 0.10.35 / onnxruntime 1.26.0 / opencv-contrib-python 4.13.0.92 / numpy 2.4.6 / Pillow 12.2.0（与 smoke 验证栈逐版本一致）。
- 模型（自包含，不依赖 scratch `angle-compare/`）：
  - `~/.cache/case-workbench-pose/models/face_detection_full_range.tflite` = **dense 1083786 B**（生产用；从 `storage.googleapis.com/mediapipe-assets/` 下载）。
  - `~/.cache/case-workbench-pose/models/face_detection_full_range_sparse.tflite` = sparse 676746 B（仅 A/B 对照；smoke 历史用的就是它）。
  - `~/.cache/case-workbench-pose/models/sixdrepnet.onnx` = 157319692 B（与 torch 逐位 <0.16°，Phase 1 验证）。
  - FaceMesh：`~/.cache/feishu-claude/mediapipe/face_landmarker.task`（持久，3.7MB）。
- **dense ≠ sparse 已实证**：sparse 676746 B（= 缓存旧文件 exact 同），dense 1083786 B（1.08MB）。Tasks `vision.FaceDetector` **接受 dense**（已 test-load）。

## 2. 验收：14 张手标真实案例 GT（dense 模型，hybrid 模式）

dedicated venv + dense + `CASE_WORKBENCH_POSE_BACKEND=hybrid`：

```
混淆矩阵 [hybrid + DENSE]  (行=GT, 列=预测桶)
GT\pred   front  oblique  profile  unknown
front       4       0        0        0
oblique     0       0        1        0     # p_yuanxia_17 真~45°边界读 49.4° → profile?(uncertain)
profile     0       0        5        0
non-face    2       0        0        2

混淆矩阵 [facemesh 基线]（对照，暴露被修的问题）
front       4       0        0        0
oblique     0       1        0        0
profile     0       4        1        0     # 5 张侧脸里 4 张被 FaceMesh 压成 oblique
non-face    2       0        0        2
```

**三条 gate（hybrid+dense）全过**：
1. 正面零回归 **PASS**（王嘉琦 4/4 front）。
2. 侧脸纠偏 **5/5** 进 profile（FaceMesh 基线仅 1/5）。
3. 非脸不出假阳 **PASS**（后脑/旋转宏观 0 confident profile/oblique）。

**sparse → dense 边界稳定（关键）**：14 张里仅 3 张变化，全是 non-face 门控分数在 0.5 附近抖动（sparse 0.32 ↔ dense 0.37），**两种都正确拒绝**；所有真脸（front/profile）分桶 sparse 与 dense **完全一致** → 换 dense 不动真脸召回/拒绝边界。

**诚实标注**：`p_yuanxia_17`（GT=oblique 真~45°边界）hybrid 读 49.4° 落 `profile?`（certain=False，route_region 走降级不硬路由），FaceMesh 基线读 39.5° confident oblique。Plan Phase 3 已记录「真45°斜读49-52落 profile-uncertain，降级可用非硬错」；6D-specific 边界精调 defer（影子大数据后做）。

## 3. 真实案例库 coverage_sweep（10 病例 ~87 张术前照，facemesh vs hybrid+dense）

| 照片角度分布 | 正面 | 45° | 侧面 | no_face |
|---|---|---|---|---|
| facemesh 基线 | 24 | 41 | **5** | **17** |
| hybrid+dense | 23 | 33 | **30** | **1** |

- **分类层大幅更准**：profile 召回 5→30，no_face 17→1（FaceMesh 在侧脸拿不到 landmark 直接 no_face；BlazeFace 门控+6D 救回并正确读 profile）。正面稳定。
- **路由层这 10 例零变化**：两边「落 profile 板 0 次 / front+45° 覆盖 100%」——本批术式不需 profile 视角，hybrid「看得见」侧脸但无区域用它。**路由收益 case-mix 依赖**（需侧面素材的部位才兑现，正是原问题「以为无侧面→降级/drop」的修复对象）。
- 注：照片级是分类分布观测，非逐张 GT 核验；但与 14-GT 验收方向一致。

## 4. 激活 runbook（env var，最小影响，一键回滚）—— **当前 HOLD，未翻 flag；现语料 NO-GO（见 §6）**

> **状态（owner 决策 Option 2 + §6 全库裁决）**：vendor + 验证已完成，分类层数据全支持激活（三 gate 全过、sparse→dense 边界稳定），
> **但 §6 全库 sweep 显示翻 flag 在现语料 0 下游产出升级 → 激活 NO-GO**，env var 未设、env 文件未创建、`_DEFAULT_MODE` 仍 facemesh。下方命令仅在**未来出现 profile-dependent 语料 + owner greenlight** 时才用。

无 daemon → 「激活」= 跑 CLI 工具前 source env + 用 dedicated venv。greenlight 时创建 `~/.cache/case-workbench-pose/pose-hybrid.env`：

```bash
# === 仅在 owner greenlight 后创建并 source ===
cat > ~/.cache/case-workbench-pose/pose-hybrid.env <<'ENV'
export CASE_WORKBENCH_POSE_BACKEND=hybrid
export CASE_WORKBENCH_FACEDETECT_MODEL="$HOME/.cache/case-workbench-pose/models/face_detection_full_range.tflite"
export CASE_WORKBENCH_SIXDREP_ONNX="$HOME/.cache/case-workbench-pose/models/sixdrepnet.onnx"
ENV
source ~/.cache/case-workbench-pose/pose-hybrid.env
~/.cache/case-workbench-pose/venv/bin/python -m backend.scripts.coverage_sweep
# 或 render_triptych / render_treatment_panel（从一个含 main pose_backend 的 checkout 跑）
```

- **回滚**：不 source（或 `unset CASE_WORKBENCH_POSE_BACKEND`）→ 立即回 facemesh 现状。删 env 文件即彻底复原。
- **不改 code 默认**（`_DEFAULT_MODE=facemesh`）；不写 shell profile（避免影响所有 shell）；不动 server `.venv`、系统 python、ComfyUI venv、owner WIP。
- coverage_sweep 硬编码 `MODEL=/tmp/focal-p4-asset/face_landmarker.task`（ephemeral）；验证期重建 symlink → 持久 `.task`。生产应把该路径改 env 化（小技术债）或重建 symlink。

## 5. 范围 / 未做

- 仅激活 **CLI 分类工具**（coverage-sweep / triptych / 治疗区 panel）的侧脸分桶。
- **客户渲染产物侧脸纠偏 = Phase 6**（跨 repo `face_align_compare.py`，高风险，未做）；本次 vendor 的 6D backend 是 Phase 6 的复用地基。
- 6D-specific oblique/profile 边界精调（48→~50）defer（需生产影子大数据，8 锚点太薄）。

## 6. 全库验证收尾（2026-05-31 第二段，DEFINITIVE NO-GO）

§3 只覆盖 10 例，留了「路由收益 case-mix 依赖」的口子。随后对**全库 63 case**（陈院 39 + incoming 47 去重）做区域级 `facemesh` vs `hybrid+dense` 双后端 diff（脚本 `/tmp/pose-smoke/hunt_b_profile_cases.py`），把这扇门关上：

- **0 个 profile 产出升级**（63 case 中 24 含 require-profile 部位：鼻 / 下巴 / 下颌线 / 太阳穴）。分类层照片级 profile 5→30 全库复现，但**整库零下游业务产出变化**——没有任何一例从 degraded/missing → covered。
- 根因 = §3 已点明的三者交集（部位 require profile **且** 缺正/斜替代视角 **且** profile 照原被 FaceMesh 误分）在现语料为空：`route_region` 按 `region_views` 顺序匹配，先命中 front/oblique 就用，profile 槽吃不到。pose 修复让分类器「看得见」侧脸，但下游无区域消费它。
- **结论：现语料激活 = NO-GO**。不值得翻默认 hybrid（除非未来语料出现 profile-dependent 真案例）。§4 runbook 保留作该类语料出现时的复用入口；§1 vendor 的 6D backend 仍是 Phase 6（客户渲染侧脸纠偏，跨 repo）的复用地基。
- 教训 **L-131**：分类层指标（profile 召回 5→30）**≠** 下游业务产出；验证必须打到落板层，而非止于分类混淆矩阵。
