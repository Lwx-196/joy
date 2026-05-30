# 面部部位 → MediaPipe FaceMesh 关键点知识库（固定）

> owner 2026-05-29 要求：建一套固定的面部轮廓+部位示意+解剖理论知识库，
> 让任何要标注的部位都能精确定位。代码落地：`backend/services/facial_region_atlas.py`。
> 验证：`/tmp/focal-p4-asset/atlas_validate.jpg`（王嘉琦真实照 6 部位叠点）。

## 用途

把医美注射治疗部位精确锚定到 MediaPipe FaceLandmarker(Tasks API,
refine_landmarks=True, **478 点**) 关键点 → 驱动 ①治疗区标注 ②精确 focal 蒙版。
取代旧的"整图当人脸框 + 关键词猜椭圆"（journal 第 18 段根因）。

## 模型 + 运行

- 模型：`face_landmarker.task`（float16, ~3.7MB，Google 官方）
  下载：`https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task`
- 本地已验证：MediaPipe 0.10.35 **仅 Tasks API**（无 legacy `solutions.face_mesh`），
  须用 `mediapipe.tasks.python.vision.FaceLandmarker`。
- 实测：在真实大图(4284×5712)**和 AI 线稿上都能检出 478 点**（线稿可检 = 标注与线稿几何对齐的关键）。

## 左右约定（关键坑）

`left_*` / `right_*` = **受试者解剖侧**（patient's own side）。正脸非镜像照里
受试者 right = 图像左侧。医美"左泪沟/右法令纹"按患者本人解剖侧对齐。

## 置信度分级

| 等级 | 部位 | 含义 |
|---|---|---|
| **HIGH**（官方 connections 索引，直接用）| 唇 / 下颌线 / 下巴轮廓 / 鼻尖 / 鼻基底 / 鼻翼主点 / 卧蚕锚点 / 颧骨外缘 | 出自 mediapipe `face_mesh_connections.py` |
| **inferred**（需实测校准）| 泪沟下界 / 眼袋 / 苹果肌 / 面颊 / 法令纹中段 | 颊内部/眶下区 MediaPipe 点稀疏，社区图索引需叠点核对 |

## 验证结果（王嘉琦真实正脸，2026-05-29）

- ✅ **精准**：泪沟（双眼下精确）、苹果肌（中颊 malar）、下巴（颏部）、法令纹（鼻翼→嘴角路径对）
- 🟡 **待校准**：颧骨/下颌线 polyline 点序、法令纹折线平滑（inferred 项，预期内）
- 结论：知识库地基成立，关键治疗区定位精准；校准项是 Phase 1 工程化的微调。

## 形状推导（shape 字段）

- `ellipse`：点集 bbox 内切椭圆（下巴/苹果肌/眼袋/鼻基底/鼻尖）
- `polygon`：点集凸包（面颊/鼻翼/唇）
- `polyline`：有序点折线 + buffer 带宽（法令纹/颧骨/下颌线）— 点序需保证沿解剖走向
- `ribbon`：窄弧带，下睑缘弧线向下偏移（泪沟/卧蚕）— 不可用 bbox（会吃进眼球）

## 权威来源

1. MediaPipe 官方 connections 源码（FACE_OVAL/LIPS/EYE/EYEBROW/NOSE/IRIS 权威索引）
   https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/python/solutions/face_mesh_connections.py
2. MediaPipe Face Mesh wiki（478=468+10 iris / refine_landmarks）
   https://github.com/google-ai-edge/mediapipe/wiki/MediaPipe-Face-Mesh
3. Face Landmarker 官方文档（Tasks API）
   https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker
4. Sander de Snaijer — All 478 Landmark Points（社区最全可视化 + 区域索引）
   https://www.sanderdesnaijer.com/blog/mediapipe-face-mesh-landmarks
5. Hotaru Komajou — landmark 编号 overlay（实测校准用）
   https://medium.com/@hotakoma/mediapipe-landmark-face-hand-pose-sequence-number-list-view-778364d6c414
6. MediaPipe Issue #1615 / #2892（社区索引考证，确认官方无逐点解剖标注）

完整索引表见代码 `FACIAL_REGION_ATLAS`（13 部位 + FACEMESH_ANCHORS 官方锚点组）。

## 实测校准建议（Phase 1）

inferred 区（泪沟下界/眼袋/苹果肌/面颊/法令纹中段）：用 FaceLandmarker 在 5–10 张
真实正脸照渲染带索引的 478 点 overlay，人眼核对每区点是否落在解剖位，微调 list 后固化。
polyline 区（颧骨/下颌线/法令纹）保证点序沿解剖走向（避免锯齿/交叉）。
