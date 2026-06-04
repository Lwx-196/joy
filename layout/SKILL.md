---
name: case-layout-board
description: >
  医美案例目录自动识别、预检和三角度术前术后拼图 skill。用于对真实本地案例图按固定模板排版，
  自动识别术前/术后与正面、45°侧、侧面；支持批量分类、散图整理和正式排版三条链路。
---

# case-layout-board

> 面向真实案例目录的固定模板编排 skill。先 inspect，再 render；不负责补角度、不负责全流程 AI 生图，但会在三角度不齐时自动降级模板。

## Use When

- 用户要“按模板把案例图排成术前术后对比”
- 用户要“批量扫描文件夹，把案例按能不能出图和图片角度分类”
- 用户先要“把散图/帧图整理成候选清单，再决定能不能排版”
- 用户明确提到：
  - 对齐人物大小
  - 裁切面部区域
  - 加术前/术后标注
  - 加品牌信息
  - 自动拼图 / 自动排版
- 或者明确说：
  - 先整理案例图
  - 先看哪些能配成术前术后
  - 这组图能不能进案例排版
- 输入是本地真实案例目录，不是纯文生图需求
- 面部、颈纹、肩颈/身体案例现在都走同一飞书入口

## Current Truth

- 当前执行器：
  - [`scripts/case_layout_classify.py`](scripts/case_layout_classify.py)
  - [`scripts/case_layout_pick.py`](scripts/case_layout_pick.py)
  - [`scripts/case_layout_organize.py`](scripts/case_layout_organize.py)
  - [`scripts/case_layout_board.py`](scripts/case_layout_board.py)
  - [`scripts/render_brand_clean.py`](scripts/render_brand_clean.py)
  - [`scripts/batch_render_brand_clean.py`](scripts/batch_render_brand_clean.py)
- 首版主模板：`tri-compare`
- 当前支持自动降级：
  - `tri-compare`
  - `bi-compare`
  - `single-compare`
- 身体/颈纹案例会自动切到 `body-dual-compare`
- 身体/颈纹图片文件名若含 `正面` / `背面` / `45侧` / `侧面`，会直接作为 section hint；命名清晰时可关闭语义判官走确定性链路
- 身体/颈纹原始目录若只有 `术前1/术后1`、`术前2/术后2` 这种编号命名，会按身体模板优先级做 section 兜底：
  - 直角肩/肩颈/身体：`1 -> 正面`、`2 -> 背面`
  - 颈纹/颈部：`1 -> 正面`、`2 -> 45侧`
  - 目录内历史成品 `术前术后对比图` 会被忽略，不参与原图挑选
- inspect/classify 会跳过历史 `.case-layout-*` 派生目录，避免分类/挑图输出被下一次扫描递归吃回源目录
- 可渲染的身体/颈纹双视图分类为 `ready_body_dual_compare`，可直接继续 `案例挑图` 并导出标准命名 picked 目录
- `brand` 在飞书入口可省略；未传时默认 `fumei`
- 飞书 bridge 入口如果用户没有显式写 `--out` / `--输出`，会默认把 `案例分类` / `案例挑图` / `案例整理` / `案例预检` / `案例出图` 输出写入 bridge runtime 的 `case-layout-output`，避免污染真实案例源目录；本地 CLI 直接执行仍保留脚本原默认输出
- 工作流当前固定为：
  - `classify`（批量分类；输出案例级 bucket + 图片级角度分图）
  - `pick`（从 `ready_*` 分类结果里挑推荐图，并导出标准命名目录）
  - `repick`（对 `pick-manifest` 的单案例结果按槽位改选候选图）
  - `organize`（可选，仅用于无规范命名的散图/帧图目录）
  - `inspect`
  - `render`
- 默认启用 `GPT-5.4` 视觉判官，并作为几何主判定的语义优先层：
  - 单图语义补判
  - 所有已选槽位 pair 复核
  - render 后最终质检
- 语义判官的 Node 子进程带超时保护：
  - 默认 `60s`
  - 可用 `CASE_LAYOUT_SEMANTIC_TIMEOUT_SEC` 统一覆盖
  - 可用 `CASE_LAYOUT_SCREEN_TIMEOUT_SEC` / `CASE_LAYOUT_PAIR_REVIEW_TIMEOUT_SEC` / `CASE_LAYOUT_FINAL_QA_TIMEOUT_SEC` 分阶段覆盖
  - 超时会记录到 `semantic_errors` 并回到几何挑图 / 已有降级链路，不会无限拖住 inspect/render 或测试
- 身体/颈纹链路在显式 `--semantic-judge off` 时，可使用真实文件名里的 `正面` / `背面` / `45侧` / `侧面` 提示做确定性 section 识别；无 section 提示时仍需要语义判官
- 面部格子的背景策略已切到保守通用策略：
  - 正式链默认 `auto-preserve-original-tone`
  - 背景分割不够干净时保留原图色调，避免灰块、脏边和半抠图残留
  - 只补齐仿射对齐产生的外部空白，优先用原图边缘采样色
  - 默认不做人物周围白底融合；仅显式设置 `CASE_LAYOUT_BACKGROUND_MODE=auto-clean-white` 时才尝试高置信白底
  - AI 边缘补齐仅保留为实验模式，不进入正式默认产出
- 面部真实案例支持 `术前/术后`、`治疗前/治疗后`、`before/after` 等阶段子目录归并；阶段目录中的图片会作为同一父案例组处理。
- 侧面图正脸 landmark 失败时会尝试通用 profile fallback；只有方向一致、清晰度和姿态守门通过后才进入 pick/render。
- 术后增强输出会先做稳定化守门：
  - 对 0/90/180/270 旋转候选做姿态评分，必要时自动输出 `*-upright`
  - 校验左右方向、portrait/landscape、宽高比、脸部占比与 yaw/pitch/roll 差异
  - 若增强结果不可信，自动回退到已对齐术后输入图，并在 manifest 的 `stabilization` / `fallback_count` 中记录
- 标题与标签绘制已支持逐字符字体 fallback，真实项目名里的 `➕` 等特殊符号不再通过删除文字规避，而是自动切到可用符号/Emoji 字体渲染

## Commands

飞书桥接显式入口：

```text
案例分类 <根目录或客户/案例目录>
案例挑图 <案例分类结果目录、客户/案例目录或客户名>
案例改选 <picked单案例目录或pick-manifest.json>
案例整理 <客户/案例关键词或目录路径>
案例预检 <客户/案例关键词或目录路径> [部位:效果]
案例出图 <客户/案例关键词或目录路径>
```

说明：

- `brand` 当前改为可选；未传时默认走 `fumei / 芙美`
- `案例分类` 支持：
  - 根目录
  - 客户目录
  - 单案例目录
  - 客户名
  - 可选 `--brand` / `--out`
  - 飞书里未写 `--out` 时，会自动使用 bridge runtime 输出根，不会默认写入源目录 `.case-layout-classify`
- `案例分类` 会：
  - 逐图识别 `正面 / 45侧 / 侧面 / 背面 / 局部 / 其他`
  - 再汇总成案例级 `ready_tri_compare / ready_bi_compare / ready_single_compare / manual-curation`
  - 身体/颈纹双视图可出图时会进入 `ready_body_dual_compare`
  - 把原图复制到结果目录，按 `案例级 bucket + 图片角度` 分桶
  - 在 `classify-summary.json / csv / md` 和 CLI 摘要里补齐统一动作模板：
    - `workflow_state`
    - `action_title`
    - `blocking_reason`
    - `action_message`
  - 若是单案例且 `recommended_action` 明确，会在 CLI/飞书里自动续跑：
    - `pick -> 案例挑图`
    - `organize -> 案例整理`
  - 在飞书 bridge 中，单案例分类自动续跑 `案例挑图` 后，会把 picked 目录写入当前 chat 上下文，并在飞书提示“已记住当前案例”；下一句可直接 `案例改选`、`改 背面 术后 1` 或 `案例出图`
- `案例挑图` 会：
  - 支持直接输入：
    - `案例分类` 结果目录
    - 客户目录
    - 单案例目录
    - 客户名
  - 若输入不是 `案例分类` 结果目录，会自动先跑一次 `案例分类`
  - 只处理 `ready_*` 案例
  - 为每个角度挑出推荐的术前 / 术后图
  - 导出标准命名目录，如：
    - `术前-正面.jpg`
    - `术后-右45侧.jpg`
    - `术前-左侧面.jpg`
    - 身体/颈纹可包含 `术前-背面.jpg` / `术后-背面.jpg`
  - 多案例时会直接返回：
    - `案例 1: ...`
    - `案例 2: ...`
    - 可继续回复 `案例 2` 或 `选 2`
  - 单案例时会直接提示“已记住当前案例”，并给出可复制快捷回复：
    - `案例改选`
    - `改 背面 术后 1`（身体/颈纹背面案例；面部案例会按已选槽位提示相应角度）
    - `案例出图`
  - 导出的 picked 目录可直接继续跑 `案例预检`
- `案例改选` 会：
  - 读取单案例 `pick-manifest.json`
  - 无参数时列出各槽位 `术前 / 术后` 当前选中图与候选编号
  - 传 `--slot --phase --pick` 时原地改选当前 `picked` 目录
  - 身体/颈纹 picked 目录支持 `back / 背面` 槽位改选；若真实源目录只有当前候选，会明确只列出当前候选，不额外编造替代图
  - 改选时会先做一次正式预检校验；会导致 inspect 失败的候选不会被保留
  - 改选后的 picked 目录可直接继续 `案例出图`
- `案例出图` 现在也支持直接吃：
  - `picked` 单案例目录
  - `pick-manifest.json`
  - 位于 `.case-layout-classify/.../pick/.../picked` 下的自动挑图目录
  - 如果当前输出根目录下还没有 `inspect/manifest.json`，会先自动对 picked 目录补一次 `inspect`，再继续 `render`
- `案例整理` 不需要 `brand`，只支持可选 `--out`
- `case_dir` 支持：
  - 绝对路径
  - 相对路径
  - `客户名 + 案例关键词`
- 若目录里还没有清晰的 `术前/术后` 命名，先走：
  - `案例整理 陈莹 颈纹2025.4.29`
  - 看候选配对、report 和 preview，再决定是否继续 `案例预检`
- 在 `案例预检` 正文里直接写 `部位:效果` 即可：
  - 例：`案例预检 许晓洁 2025.7.30脸颊 面颊凹陷:饱满支撑`
  - 命中后会自动按增强预检链路执行
- 若只给客户名且命中多个案例，bridge 会返回候选目录，不会擅自猜测
- 身体/颈纹类案例已并入这条飞书入口，会自动切到对应模板

如果要做批量分类，先跑：

```bash
python3 scripts/case_layout_classify.py "<root_dir>" --brand fumei
python3 scripts/case_layout_classify.py "<root_dir>" --brand fumei --out /tmp/case-layout-classify
```

如果要从分类结果里继续挑图，跑：

```bash
python3 scripts/case_layout_pick.py "<classify_run_dir>"
python3 scripts/case_layout_pick.py "<classify_run_dir>" --out /tmp/case-layout-pick
```

如果要对已挑好的单案例继续改选，跑：

```bash
python3 scripts/case_layout_repick.py "<picked_case_dir>"
python3 scripts/case_layout_repick.py "<picked_case_dir>" --slot oblique --phase after --pick 2
```

如果当前目录还是散图/帧图，先跑整理：

```bash
python3 scripts/case_layout_organize.py "<case_dir>"
python3 scripts/case_layout_organize.py "<case_dir>" --out /tmp/case-layout-organize
```

对话式使用时，先确认本次案例的：

- 具体优化部位
- 希望强调的效果

格式建议：

```text
面颊凹陷:饱满支撑
鼻基底:过渡更平顺
```

未确认 `部位:效果` 之前，不要直接进入 `inspect` 或术后增强。

先跑预检：

```bash
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand fumei
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand fumei --semantic-judge auto
```

预检通过后再出最终图：

```bash
python3 scripts/case_layout_board.py render "<case_dir>" --brand fumei
python3 scripts/case_layout_board.py render "<case_dir>" --brand fumei --semantic-judge auto
```

如果要自定义输出根目录：

```bash
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand shimei --out /tmp/case-layout-board
python3 scripts/case_layout_board.py render "<case_dir>" --brand shimei --out /tmp/case-layout-board
```

如果要关闭视觉判官：

```bash
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand fumei --semantic-judge off
python3 scripts/case_layout_board.py render "<case_dir>" --brand fumei --semantic-judge off
python3 scripts/case_layout_audit.py "<root_dir>" --brand fumei --semantic-judge off
python3 scripts/batch_render_brand_clean.py "<audit-summary.json>" --brand fumei --semantic-judge off --out /tmp/case-layout-brand-batch
```

如果要对体检结果里的 `ready_*` 案例批量输出正式品牌版：

```bash
python3 scripts/case_layout_audit.py "<root_dir>" --brand fumei --out /tmp/case-layout-audit
python3 scripts/batch_render_brand_clean.py "/tmp/case-layout-audit/audit-summary.json" --brand fumei --out /tmp/case-layout-brand-batch
```

如果要切增强模型：

```bash
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand fumei --enhance-after --focus "鼻背:立体感更清晰" --enhance-model gemini-3-pro-image-preview-2k-vip
```

如果不显式传 `--enhance-model`：

- 默认走 `gemini-3-pro-image-preview-4k`
- 不要为了快速 smoke 临时切回 `2k`
- 如需替换模型，必须显式传 `--enhance-model`

如果要开启术后增强，必须显式传入一个或多个 `--focus`：

```bash
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand fumei --enhance-after --focus "面颊凹陷:饱满支撑"
python3 scripts/case_layout_board.py inspect "<case_dir>" --brand fumei --enhance-after --focus "鼻基底:过渡更平顺" --focus "下巴:延伸衔接"
```

## Input Contract

- 若文件名缺少明确 `术前` / `术后`，先走 `organize`，不要直接假定可排版
- 先看目录层级决定 group
- 文件名至少要包含 `术前` / `术后`
- 角度优先来源：
  - 文件名里的角度词
  - 同目录历史 `术前术后对比图` 文件名里的角度提示
  - 本地面部姿态推断（yaw）
- 详细命名规范见 [references/naming-contract.md](references/naming-contract.md)

## Outputs

`organize` 默认写到：

```text
<case_dir>/.case-layout-organize/
```

产物：

- `organize-summary.json`
- `organize-report.md`
- `organize-preview.jpg`

`classify` 默认写到：

```text
<root_dir>/.case-layout-classify/<timestamp>/
```

产物：

- `classify-summary.json`
- `classify-summary.csv`
- `classify-summary.md`
- `classify-images.json`
- `classified/<bucket>/<customer>/<case>/<view>/...`

`pick` 默认写到：

```text
<classify_run_dir>/pick/<timestamp>/
```

产物：

- `pick-summary.json`
- `pick-report.md`
- `picked/<customer>/<case>/pick-manifest.json`
- `picked/<customer>/<case>/术前-正面.jpg` 等标准命名结果

`render` 额外支持：

```bash
python3 scripts/case_layout_board.py render "<picked_case_dir>" --brand fumei
node scripts/cli-case-layout-board.js "案例出图 <picked_case_dir> --brand 芙美"
node scripts/cli-case-layout-board.js "案例出图 <pick-manifest.json> --brand 芙美"
```

`inspect` 默认写到：

```text
<case_dir>/.case-layout-board/<brand>/tri-compare/inspect/
```

产物：

- `manifest.json`
- `report.md`
- `preview.jpg`

`render` 默认写到：

```text
<case_dir>/.case-layout-board/<brand>/tri-compare/render/
```

产物：

- `final-board.jpg`
- `manifest.final.json`

## Auto Downgrade

- 如果正面、45°侧、侧面三组都齐：
  - 使用 `tri-compare`
- 如果有正面，且还能再配出一个非正面角度：
  - 自动降级到 `bi-compare`
- 如果只有正面可用：
  - 自动降级到 `single-compare`
- 如果连正面都配不出来：
  - `inspect` 仍然报错，拒绝 `render`

## Quality Gate

- 不会用 AI 去“修清晰”术前证据图
- 选图阶段会记录清晰度分数并优先选择更清楚的候选
- 如果最终配出的某个角度：
  - 一侧明显过糊
  - 或术前术后清晰度差过大
  - 该角度会被废弃，再自动降级模板

## Pose-Aligned Enhancement

- 开启 `--enhance-after` 时，默认会把同槽位的术前图作为姿态/构图参考传给图生图模型
- 术后图仍是被编辑主体；术前图只用于对齐：
  - 抬头角度
  - 下巴高度
  - 鼻尖朝向
  - 颈部展开程度
  - 整体头部姿态
- 术前图不得被 AI 改写，也不得把术前状态迁移到术后图
- 当前增强还必须满足：
  - 先确认 `部位:效果`
  - 只允许增强已确认部位及其直接相邻过渡区
  - 未点名区域只允许极轻的亮度/白平衡/清晰度统一
  - 不允许顺带祛痘、祛斑、磨皮、去纹或增强非目标轮廓

## Template

- 模板说明见 [references/template-tri-compare.md](references/template-tri-compare.md)
- 固定布局：
  - 左列术前
  - 右列术后
  - 行顺序固定：正面、45°侧、侧面
  - 底部固定品牌区
- 身体/颈纹案例：
  - 自动选 1-2 个 section
  - 常见为 `正面对比 + 背面对比`
  - 颈纹可退化为 `正面对比 + 45°侧对比`
  - 标准命名图可用 `术前-正面` / `术后-正面` / `术前-背面` / `术后-背面` 这类文件名跳过语义 section 识别

## Guardrails

- 只处理真实本地图片，不使用 mock 数据
- `classify` 只复制原图，不改名、不移动、不覆盖原目录
- `organize` 只出候选清单，不自动改名、不移动原图、不直接偷跑到 `inspect/render`
- 不补不存在的角度
- 不偷偷切换到全流程 AI 生图
- `inspect` 有 blocking issues 时，禁止 `render`
- 本地 CLI 仍建议显式传 `--brand`；飞书入口未传时默认 `fumei`
- 当前角度/方向/section 选择改为 `semantic-first`；语义高置信结果优先于 pose 桶推断
- 现有 `direction mismatch / pose delta / blurry / no face` 仍保留为硬门槛，不会被视觉判官绕过
- 当前背景补齐默认保留原图色调，仅补齐仿射/裁切产生的外部空洞；白底统一只在显式 `CASE_LAYOUT_BACKGROUND_MODE=auto-clean-white` 时尝试，AI 人物补齐不是正式默认能力
