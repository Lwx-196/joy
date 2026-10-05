# 支撑"灵策智算 LynxceAI"四项机制的开源项目与组件（记忆 / Skills / 定时触发 / 多模型与审批审计）

> 时间基准：2026-10-05。Star 数、许可证（SPDX）、最近推送时间均来自 GitHub API 当日查询（通过 GitHub MCP `search_repositories`，下文标注"GitHub API 2026-10-05"），来源链接给出仓库地址；许可证细节另以 raw.githubusercontent.com 的 LICENSE 文件核对。多数官方文档站（docs.mem0.ai、docs.letta.com、help.getzep.com、docs.openclaw.ai、agentskills.io、clawhub.ai、docs.litellm.ai、langfuse.com、docs.langchain.com、arxiv.org、deepwiki.com）被网络策略拦截，相关内容改用仓库内 docs 源文件（raw.githubusercontent.com）或搜索摘要，并在"Gaps"中注明未能一手核实的点。

---

## 关键问题 1：机制(1) Agent 长时记忆与"记忆自进化"——有哪些成熟开源件，各自是否支持跨会话、分层、冲突消解、主动调用

### Takeaway
跨会话长时记忆已是开源界的"标配"：Mem0、Graphiti/Zep、MemOS、Letta、Cognee、LangMem 都是生产可用的独立记忆层，且都可本地部署；"冲突消解"方面 Mem0（ADD/UPDATE/DELETE/NOOP）、Graphiti（时间有效窗、旧事实失效而非删除）、MemOS（去重 + 分层演化）有明确机制；"三层记忆 + 后台自动整理（自进化）"在 OpenClaw（daily log → MEMORY.md，dreaming 三阶段整合）、MemOS（L1 traces / L2 policies / L3 world models + 结晶化 Skills）、Letta（memory blocks + sleep-time/dreaming）中已内置；"主动调用而非被动存储"对应 Hermes/Letta/LangMem 的"agent 自己决定何时读写记忆（memory tool）"模式。

### Cited Findings

**独立记忆层（可嵌入任意 Agent）**

- **Mem0**（mem0ai/mem0）：Apache-2.0，66,584 stars，最近推送 2026-10-05，维护方 Mem0（组织账号 mem0ai）— [GitHub](https://github.com/mem0ai/mem0)（GitHub API 2026-10-05）。README 称其为"Multi-Level Memory: Seamlessly retains User, Session, and Agent state"，提供"Library / Self-Hosted Server（含 Dashboard，默认开启鉴权）/ Cloud Platform"三种形态；并明确指出基准成绩"reflect Mem0's managed platform, which includes proprietary optimizations not available in the open-source SDK" — [README](https://raw.githubusercontent.com/mem0ai/mem0/main/README.md)。
- Mem0 论文（arXiv 2504.19413）描述记忆更新阶段"使用 LLM 基于语义关系决定 ADD、UPDATE、DELETE 或 NOOP"；图变体 Mem0g"通过 LLM 冲突消解机制将过时关系标记为无效而非删除" — [EmergentMind 论文页](https://www.emergentmind.com/papers/2504.19413)；[Mem0 state-of-memory-2026 博客](https://mem0.ai/blog/state-of-ai-agent-memory-2026)。
- Mem0 仓库内文档 `update.mdx`："Mem0's update operation lets you fix or enrich an existing memory without deleting it… `immutable`: Flagged memories that must be deleted and re-added instead of updated" — [docs/core-concepts/memory-operations/update.mdx](https://raw.githubusercontent.com/mem0ai/mem0/main/docs/core-concepts/memory-operations/update.mdx)。
- Mem0 README 的 v3 基准说明里出现一种"Single-pass ADD-only extraction -- one LLM call, no UPDATE/DELETE. Memories accumulate; nothing is overwritten"模式，并附 OSS v2→v3 迁移指南 — [README](https://raw.githubusercontent.com/mem0ai/mem0/main/README.md)。
- **Graphiti**（getzep/graphiti）：Apache-2.0，31,442 stars，推送 2026-10-04，维护方 Zep（getzep）— [GitHub](https://github.com/getzep/graphiti)。README："Temporal Fact Management: Facts have validity windows. When information changes, old facts are invalidated — not deleted"；"Incremental Graph Construction: New data integrates immediately without batch recomputation"；"Hybrid Retrieval: Combines semantic embeddings, keyword (BM25), and graph traversal"；Graphiti 是开源框架，"Bring your own third-party graph database"，而托管版 Zep 使用专有 Context Graph Engine；论文 arXiv 2501.13956 — [README](https://raw.githubusercontent.com/getzep/graphiti/main/README.md)。
- **MemOS**（MemTensor/MemOS）：Apache-2.0，11,694 stars，推送 2026-09-29，维护方 记忆张量 MemTensor（上海）— [GitHub](https://github.com/MemTensor/MemOS)；由记忆张量联合上海交大、同济、浙大、中科大、人大发布，提出 MemCube 记忆单元，统一"明文记忆 / 激活记忆（KV-Cache）/ 参数记忆"三类 — [腾讯新闻 2025-07-31](https://news.qq.com/rain/a/20250731A08LV700)；[智源社区](https://hub.baai.ac.cn/view/47118)。
- MemOS README（2026 版）："One core powers self-evolving memory across L1 traces, L2 policies, L3 world models, and crystallized Skills, with local-first storage and feedback-driven retrieval"；"Asynchronous Ingestion via MemScheduler"；提供 OpenClaw 云/本地插件（本地插件：SQLite + FTS5 + 向量混合检索、smart dedup、tiered skill evolution、Memory Viewer）、Hermes Agent 插件、DeepSeek Harness 插件；自托管 `docker compose up`（Neo4j + Qdrant）；自报 LoCoMo 88.83、LongMemEval 89.20，并称接入后"OpenClaw improves average task completion from 36.63% to 50.87%"（厂商自报） — [README](https://raw.githubusercontent.com/MemTensor/MemOS/main/README.md)。
- **Letta**（letta-ai/letta，原 MemGPT）：Apache-2.0，25,025 stars，推送 2026-09-10；README 称"The current source code lives in letta-ai/letta-code" — [GitHub](https://github.com/letta-ai/letta)；[README](https://raw.githubusercontent.com/letta-ai/letta/main/README.md)。**letta-code**：Apache-2.0，3,517 stars，推送 2026-10-05 — [GitHub](https://github.com/letta-ai/letta-code)。README："Agents programmatically rewrite their context to improve and adapt over time, including system prompt learning (through memory blocks) and skill learning. Configure periodic dreaming with `/sleeptime`"；"MemFS: All context (including memory blocks) is tracked via git"；源自 MemGPT（arXiv 2310.08560）与 sleep-time compute（arXiv 2504.13171） — [letta-code README](https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md)。
- Letta 记忆结构（二手）："Core memory is composed of discrete units called memory blocks, each identified by a label… and an optional size limit"；archival memory 由 agent 通过工具主动写入 — [sureprompts 走查](https://sureprompts.com/blog/letta-memgpt-walkthrough)；[Letta 官方博客 Memory Blocks](https://www.letta.com/blog/memory-blocks/)。
- **LangMem**（langchain-ai/langmem）：MIT，1,692 stars，推送 2026-10-02，LangChain — [GitHub](https://github.com/langchain-ai/langmem)。README：提供"Memory management tools that agents can use to record and search information during active conversations 'in the hot path'"与"Background memory manager that automatically extracts, consolidates, and updates agent knowledge"，基于 LangGraph Store 持久化（生产用 AsyncPostgresStore） — [README](https://raw.githubusercontent.com/langchain-ai/langmem/main/README.md)。
- **Cognee**（topoteretes/cognee）：Apache-2.0，31,366 stars，推送 2026-10-05 — [GitHub](https://github.com/topoteretes/cognee)。README："gives AI agents persistent long-term memory across sessions. Turn documents, code, and conversations into a self-hosted knowledge graph"；"Runs locally for free — no API key required"（本地 GLiNER 抽取 + 本地 embedding）；论文 arXiv 2505.24478 — [README](https://raw.githubusercontent.com/topoteretes/cognee/main/README.md)。
- **Memobase**（memodb-io/memobase）：Apache-2.0，2,923 stars，最近推送 2026-01-11（近 9 个月无推送，活跃度低） — [GitHub](https://github.com/memodb-io/memobase)。README："user profile-based memory system… for each user, there is always a user profile and event timeline" — [README](https://raw.githubusercontent.com/memodb-io/memobase/main/readme.md)。
- **A-MEM**（agiresearch/A-mem）：MIT，1,188 stars，推送 2025-12-12，Rutgers AGI Research — [GitHub](https://github.com/agiresearch/A-mem)。论文 arXiv 2502.12110：Zettelkasten 式笔记网络，"New memories can trigger updates to the contextual representations and attributes of existing historical memories"（记忆演化） — [arXiv](https://arxiv.org/abs/2502.12110)；[HF papers](https://huggingface.co/papers/2502.12110)。
- **Honcho**（plastic-labs/honcho）：AGPL-3.0，7,466 stars，推送 2026-10-02，"Memory library for building stateful agents"（用户建模），被 Hermes Agent 用于"dialectic user modeling" — [GitHub](https://github.com/plastic-labs/honcho)；[Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)。

**完整 Agent 产品内置的记忆机制**

- **OpenClaw**：workspace 内 `MEMORY.md`（长期、精选的非画像事实与决策）、`memory/YYYY-MM-DD.md`（工作层每日笔记）、`USER.md`（用户模型层：稳定偏好/画像）三层；"Over time, useful material from daily notes is distilled into MEMORY.md… while dreaming handles background consolidation"；可从 Codex（`MEMORY.md`、`memory_summary.md`）、Claude Code（auto-memory 目录）、Hermes 导入记忆；"Memory can preserve approval context, but it does not enforce policy" — [docs/concepts/memory.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/memory.md)。
- OpenClaw **Dreaming**："background memory consolidation system in memory-core… Dreaming runs three cooperative phases per sweep, in order: light -> REM -> deep"，深睡阶段"Ranks candidates with weighted scoring and threshold gates (minScore, minRecallCount, minUniqueQueries)"，只向 `MEMORY.md` 晋升长期记忆，产出 `DREAMS.md` 可审查，默认开启；可摄入脱敏后的交互会话记录 — [docs/concepts/dreaming.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/dreaming.md)。
- OpenClaw `USER.md`："stores stable preferences, communication style, relationships, and active-project context as directives"，与 `MEMORY.md` 一起在会话启动时加载 — [docs/concepts/user-model.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/user-model.md)。
- **Hermes Agent**（Nous Research）：`MEMORY.md`（2,200 字符上限）+ `USER.md`（1,375 字符）自动写入并在会话启动载入 system prompt，加 FTS5 `session_search` 跨会话检索；"Memory writes are automatic" — [website/docs/user-guide/features/memory.md](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/memory.md)；README："Agent-curated memory with periodic nudges… FTS5 session search with LLM summarization for cross-session recall" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)。
- **Claude Code**（非开源，作为对照）：两套机制——用户写的 CLAUDE.md 与"Auto memory: notes Claude writes itself based on your corrections and preferences"；auto memory 存于 `~/.claude/projects/<project>/memory/`，`MEMORY.md` 为索引，"The first 200 lines of MEMORY.md, or the first 25KB… are loaded at the start of every conversation"；"Auto memory is machine-local"；记忆按 `type` 分四类记录 — [code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory)。

### Inferences
- 灵策智算"三层记忆体"最贴近的开源对照是 OpenClaw 的 USER.md / MEMORY.md / daily-notes + dreaming，以及 MemOS 的 L1/L2/L3 + Skills 结晶；若要"拼装"，MemOS 已提供 OpenClaw 与 Hermes 的官方插件，可直接替换这两个产品的内置记忆。
- "冲突智能消解"的工程化实现以 Mem0（LLM 决策 ADD/UPDATE/DELETE/NOOP）和 Graphiti（双时态有效期、失效不删除、可追溯到来源 episode）最成熟；后者更适合需要"操作可回溯"的企业场景。
- "主动调用而非被动存储"在开源界通常落地为 memory tool（LangMem、Letta、Hermes `memory` 工具）加 system-prompt 提示（Hermes 的"nudges"），并非特殊算法。

### Gaps
- Mem0、Letta、Zep 的官方文档站被拦截，Mem0 OSS v3 默认是否仍做 UPDATE/DELETE 冲突消解（README 提到的"ADD-only"模式适用范围）未能一手核实。
- Mem0、Cognee、Memobase 公司所在国未在本次核实（Mem0 组织页与官网均未访问）；MemOS 为上海团队有多篇中文媒体佐证。
- MemOS 自报的基准与"OpenClaw 任务完成率提升"均为厂商数据，无第三方复现来源。

---

## 关键问题 2：机制(2) Skills 规范、技能市场与"自然语言流程 → Skill / 技能自进化"

### Takeaway
SKILL.md（Agent Skills）在 2025-12 由 Anthropic 开放为跨平台规范后，一年内成为事实标准：规范仓库 Apache-2.0、26–32 个工具采用、多个万星级技能集合与市场（anthropics/skills、awesome-agent-skills、vercel-labs/skills、ClawHub）。"自然语言描述流程 → 生成可复用 Skill"已有可用的开源实现（Hermes `/learn` 与 `skill_manage`、OpenClaw Skill Workshop、Paperclip Skill Studio）；"从重复模式自动沉淀技能并全局泛化"已从论文走向产品——OpenClaw Skill Workshop 自学习（off/propose/auto）、Hermes 自主建技能、HKUDS OpenSpace（FIX/DERIVED/CAPTURED）、MemOS 技能结晶——但学术界同时指出"技能误进化"安全风险。

### Cited Findings

**规范与采用**

- **agentskills/agentskills**（规范与文档）：Apache-2.0，25,916 stars，创建 2025-12-16，推送 2026-08-09，主页 agentskills.io — [GitHub](https://github.com/agentskills/agentskills)（GitHub API 2026-10-05）。
- "The open specification was released by Anthropic on December 18, 2025"；"adopted by 26+ platforms including Claude, OpenAI Codex, Gemini CLI, GitHub Copilot, Cursor, and VS Code"；OpenCode 读取 `.agents/` 目录下的 SKILL.md — [skillsboard.sh 支持矩阵](https://www.skillsboard.sh/agent-skills-support)；[Simon Willison 2025-12-19](https://simonwillison.net/2025/Dec/19/agent-skills/)；"By March 2026, 32 tools from competing companies had adopted the standard" — [agentman 生态报告](https://agentman.ai/blog/agent-skills-ecosystem-report-2026)。
- GitHub 官方 changelog（2026-04-16）："Manage agent skills with GitHub CLI" — [github.blog](https://github.blog/changelog/2026-04-16-manage-agent-skills-with-github-cli/)。
- 采用者仓库（GitHub API 2026-10-05）：**openai/codex** Apache-2.0，127,888 stars，Rust — [GitHub](https://github.com/openai/codex)；**google-gemini/gemini-cli** Apache-2.0，107,235 stars — [GitHub](https://github.com/google-gemini/gemini-cli)；**Hermes Agent** "Compatible with the agentskills.io open standard" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；**Cognee** 仓库 topic 含 `agent-skills` — [GitHub](https://github.com/topoteretes/cognee)；**Mem0** 提供 `npx skills add https://github.com/mem0ai/mem0 --skill …` 技能目录 — [README](https://raw.githubusercontent.com/mem0ai/mem0/main/README.md)。

**技能集合与市场**

- **anthropics/skills**："Public repository for Agent Skills"，179,701 stars，推送 2026-10-03；GitHub API 未识别出统一许可证（license 字段为空，需按技能目录逐个核对） — [GitHub](https://github.com/anthropics/skills)。
- **VoltAgent/awesome-agent-skills**："A curated collection of 1000+ agent skills… compatible with Claude Code, Codex, Gemini CLI, Cursor"，35,221 stars — [GitHub](https://github.com/VoltAgent/awesome-agent-skills)。
- **vercel-labs/skills**："The open agent skills tool - npx skills"，33,145 stars，创建 2026-01-14 — [GitHub](https://github.com/vercel-labs/skills)。
- **openclaw/clawhub**："Skill + Plugin Registry for OpenClaw"，9,486 stars，创建 2026-01-03 — [GitHub](https://github.com/openclaw/clawhub)；OpenClaw 文档将 ClawHub 列为"Browse and install community skills" — [docs/tools/skills.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md)；Hermes Skills Hub 可安装 `clawhub/...` 前缀技能并经"security scan"流水线 — [Hermes skills.md](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/skills.md)。
- OpenClaw 技能加载优先级（高→低）：workspace `skills/` → `.agents/skills` → `~/.agents/skills` → managed → **Workshop skills** → bundled — [docs/tools/skills.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md)。
- **Dify**（langgenius/dify）：157,865 stars，推送 2026-10-05；许可证为"modified version of the Apache License 2.0"，附加多租户与品牌条件（非纯 OSI） — [LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)；第三方称 1.16.0（2026-07-17）推出带沙箱与"skill system"的 Dify Agent 公测，1.17.0 增加"workspace-level skill management… skills are reusable, versioned capabilities" — [dify-hosting.com](https://dify-hosting.com/en/guides/dify-updates/)；Dify Marketplace 有 Skill_Agent 插件 — [marketplace.dify.ai](https://marketplace.dify.ai/plugin/lfenghx/skill_agent)。

**"自然语言流程 → Skill"与"技能自进化"的可用实现**

- **Hermes Agent** `skill_manage` 工具："The agent can create, update, and delete its own skills via the skill_manage tool. This is the agent's procedural memory — when it figures out a non-trivial workflow, it saves the approach as a skill for future reuse"；"The system prompt asks the agent to record a non-trivial workflow with skill_manage"；`/learn` 命令可从"A local SDK or doc directory / An online doc page / **The workflow you just walked the agent through in this conversation** / Pasted notes / a described procedure"生成 SKILL.md；有 write-approval gate 与 advisory linter；技能带 `version: 1.0.0` 与扫描器记录 — [skills.md](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/skills.md)；README："Autonomous skill creation after complex tasks. Skills self-improve during use" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)。
- Hermes 自动建技能触发条件（二手）："writes a skill after completing a complex task (5+ tool calls) successfully, after recovering from an error, or after a user correction" — [ssojet 博客](https://ssojet.com/blog/hermes-agent-self-evolving-skills)。
- **OpenClaw Skill Workshop**："OpenClaw's governed path for creating and updating its own generated skills"，agent/操作员创建 proposal（含内容、目标绑定、扫描状态、哈希、回滚元数据）→ 审核 → apply/reject/quarantine；"Automatic background learning and weekly collection review" — [docs/tools/skill-workshop.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skill-workshop.md)；自学习设置："After substantial work, a detached background review can turn corrections and … into proposals"，`skills.workshop.autonomous.mode` 取值 `off / propose / auto`（auto = 直接按轮次与每周维护技能库） — [configuration.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skill-workshop/configuration.md)；apply 时"reruns the security scanner before writing"、"writes rollback metadata before touching live files"，保留历史版本比对 — [how-it-works.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skill-workshop/how-it-works.md)；相关发布说明 v2026.8.1 Skills — [docs.openclaw.ai/releases/2026.8.1/skills](https://docs.openclaw.ai/releases/2026.8.1/skills)。
- **OpenSpace**（HKUDS/OpenSpace，港大 Data Intelligence Lab）：MIT，7,740 stars，创建 2026-03-24，推送 2026-08-12 — [GitHub](https://github.com/HKUDS/OpenSpace)。README："One Skill Management Layer to Power Them All — Claude Code, Codex, OpenClaw, Hermès, nanobot"；"Evolves skills through structured FIX, DERIVED, and CAPTURED updates"（FIX 修复失效技能；DERIVED 派生专用版本；CAPTURED"Save one reusable subworkflow only when the source trace shows…"）；运行模式 `fix_only` / `autonomous`（默认，"all admission-approved and validated FIX/DERIVED/CAPTURED actions may commit"）；v2（2026-07-17）加入 task-trace 上传作为质量证据、dashboard/TUI、scheduler、triggers、sandboxing、memory；以 MCP（stdio/SSE/HTTP）接入宿主 agent — [README](https://raw.githubusercontent.com/HKUDS/OpenSpace/main/README.md)；二手摘要称其"46% reduction in token usage" — [搜索摘要来源：evoailabs](https://evoailabs.medium.com/self-evolving-agents-open-source-projects-redefining-ai-in-2026-be2c60513e97)。
- **MemOS**："crystallized Skills"、"tiered skill evolution"（Hermes/OpenClaw 本地插件） — [README](https://raw.githubusercontent.com/MemTensor/MemOS/main/README.md)。
- **Letta Code**："skill learning"、`letta skills install <skill>` 将技能装入 agent 记忆 — [README](https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md)；[Letta 博客 skill-learning](https://www.letta.com/blog/skill-learning)。
- **Paperclip**："Skill Studio & shared org-wide skills · evals & saved test runs · active learning loops… skill version history & restore" — [README](https://raw.githubusercontent.com/paperclipai/paperclip/main/README.md)。

**研究项目 / 论文（原型阶段）**

- **Voyager**（MineDojo/Voyager）：MIT，7,245 stars，最后推送 2024-04-03（已停更），Minecraft 中"skill library"式自进化 — [GitHub](https://github.com/MineDojo/Voyager)。
- **SkillWeaver**（OSU-NLP-Group/SkillWeaver）：MIT，157 stars，最后推送 2025-04-14，"web agent self-improvement through environment exploration and skill synthesis" — [GitHub](https://github.com/OSU-NLP-Group/SkillWeaver)。
- **CoEvoSkills**（Zhang-Henry/CoEvoSkills，COLM 2026）：74 stars，"Self-Evolving Agent Skills via Co-Evolutionary Verification"，演化多文件 Agent Skill 包 — [GitHub](https://github.com/Zhang-Henry/CoEvoSkills)。
- 2026 论文：MUSE-Autoskill（arXiv 2605.27366）、SkillsVote 技能生命周期治理（2605.18401）、OpenSkill 开放世界自进化（2606.06741）、Dynamic Agent Skills 综述（2607.10113）、Agent Skill Evaluation and Evolution（2606.11435） — 列表来自 [搜索结果](https://arxiv.org/pdf/2607.10113)。
- **安全警示**：《Practice Makes Unsafe: Skill Misevolution in Self-Improving LLM Agents》（arXiv 2608.12851）："an unsafe success can thereby become reusable policy… Across 25 agent–method configurations, all 21 evolved configurations author unsafe artifacts, while only fifteen lead to fresh-session harm"；提出 SafeEvolve 包装器，"reduces unsafe retrieval and fresh-session harm by 26.7 and 17.3 percentage points" — [arXiv](https://arxiv.org/abs/2608.12851)；另见《Demystifying Agent Skills: Why They Work—Until They Don't》（2608.14036） — [HF papers](https://huggingface.co/papers/2608.14036)。

### Inferences
- 灵策智算"Agent 训练场：自然语言描述流程 → 结构化 → 生成 Skill（v1.3 可迭代）"在开源界有直接对应：Hermes `/learn`（可把"刚才在对话里走过的流程"固化为 SKILL.md）+ OpenClaw Skill Workshop 的 proposal/版本/回滚 + OpenSpace 的技能质量证据与演化；"100+ 内置 Skills 与市场"可直接复用 anthropics/skills、awesome-agent-skills、ClawHub。
- "一次纠正、全局泛化"最接近的是 OpenClaw 自学习（把 corrections 转成 proposals）和 Hermes（用户纠正触发建技能）；但两者都是"按会话事后复盘"，并未声称跨所有数字员工的全局泛化——跨 agent 共享要靠 OpenSpace 的共享技能库或 Paperclip 的"org-wide skills"。
- 自进化技能在开源界已可用但处于早期（OpenClaw 2026.8 才引入治理流程、OpenSpace v2 为 2026-07），且论文证据表明无治理的自进化会固化不安全做法，故"propose + 审核"模式比"auto"更适合企业。

### Gaps
- agentskills.io 规范正文与 ClawHub 的技能数量未能直接访问（被拦截），采用者数量来自第三方汇总。
- `sst/opencode` 仓库在 GitHub API 查询中未返回，OpenCode 的仓库归属/许可证未核实。
- vercel-labs/skills、openclaw/clawhub、anthropics/skills 的许可证未核实。
- Dify 1.16/1.17 的技能功能仅有第三方来源，官方博客（dify.ai）被拦截。

---

## 关键问题 3：机制(3) 定时任务、主动感知（文件/消息/周期事件）与长期运行"数字员工"的调度

### Takeaway
"定时 + 事件触发 + 试跑 + 推送到 IM"在 OpenClaw 的 Automations 中已完整内置（五种调度、条件监视脚本、stdout 流触发、webhook/Gmail 触发、投递到频道、把重复请求提升为计划任务并以"真实投递的试跑"确认），Hermes 内置 cron 并可投递到任一平台；"数字员工长流程 + 进度看板 + 预算/审批"由 Paperclip（MIT，9.7 万星）提供控制面；通用调度与持久化执行可用 n8n（非 OSI）、Huginn、Windmill、Temporal、Airflow；文件变更触发可用 watchdog 或 OpenClaw 的 stream/on-exit 调度。

### Cited Findings

**完整 Agent 产品内置**

- **OpenClaw Automations**："OpenClaw's built-in scheduler. The scheduler persists jobs, wakes the agent at the right time, and can deliver output to a chat channel, a webhook, or nowhere"；支持"scheduled jobs, webhooks, and Gmail PubSub triggers" — [docs/automation/cron-jobs.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/automation/cron-jobs.md)。
- 调度类型："five schedule kinds"，含 `on-exit`（"Fire once when a watched command exits (event trigger…)"）、stream（"keeps an operator-authored argv command running under the Gateway and fires the job from its stdout and stderr lines… event-driven, never time-due"，可按正则匹配批次）、以及"Event triggers (condition watchers)"："adds a headless condition script to an every, cron, or stream schedule… The scheduler runs the normal payload only when the script returns fire: true"；脚本可调用 MCP 工具；支持动态节奏（pacing）与 `/loop` 聊天快捷方式 — [schedules.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/automation/cron-jobs/schedules.md)。
- 运行模型："One-shot jobs (--at) auto-delete after successful completion"；隔离运行受"scheduler's own 60-minute watchdog"约束；"Run-level agent failures count as job errors… trigger failure notifications"；**把重复请求提升为自动化**："recognizes the repeat from the conversation itself and checks automations(action: 'list') for an existing job before proposing a new one… the test run is a real run with real delivery rather than a rendered preview: what you approve is exactly what the schedule will produce"；投递默认到发起该请求的频道与线程；重复出错后自动禁用并通知 — [how-it-works.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/automation/cron-jobs/how-it-works.md)。
- **OpenClaw Heartbeat**："system-owned automation that runs periodic agent turns in the main session so the model can surface anything that needs attention"；默认 `30m`（Anthropic OAuth 时 `1h`）；可 `isolatedSession`、`lightContext`、限制活动时段；提醒默认发给操作员私信；事件驱动唤醒有 30 秒最小间隔与 60 秒内 5 次的洪泛保护 — [docs/gateway/heartbeat.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/heartbeat.md)。
- OpenClaw 中国 IM 投递：社区插件 `openclaw-china`"支持飞书，钉钉，QQ，企业微信，微信" — [younggong/openclaw-china](https://github.com/younggong/openclaw-china)；[BytePioneer-AI/openclaw-china](https://github.com/BytePioneer-AI/openclaw-china)；阿里云教程称飞书为官方插件、钉钉/企微为社区插件 — [developer.aliyun.com](https://developer.aliyun.com/article/1712479)；[apifox 部署手册](https://apifox.com/apiskills/openclaw-docker-compose-feishu-dingtalk-wecom/)。
- **Hermes Agent**："Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended"；网关覆盖 Telegram/Discord/Slack/WhatsApp/Signal/Email；七种终端后端（local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox） — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)。
- **Paperclip**（paperclipai/paperclip）：MIT，97,323 stars，推送 2026-10-05 — [GitHub](https://github.com/paperclipai/paperclip)。README："If OpenClaw is an employee, Paperclip is the company"；"Heartbeats: Agents wake for assigned work, follow-up messages, or configured schedules"；"Scheduled Routines: Run recurring work on a schedule or trigger it through an API or webhook. Each run has a task, an owner, and a history"；"Company, agent, and project budgets… pause work at configured limits"；"review and approval stages"；"Governance with rollback"；适配器含 OpenClaw、Claude Code、Codex、Cursor、Gemini CLI、Pi、Hermes、Grok Build、Kimi Code（"If it can receive a heartbeat, it's hired"）；"sessions persist across reboots" — [README](https://raw.githubusercontent.com/paperclipai/paperclip/main/README.md)；二手：2026-03-02 由匿名开发者 @dotta 发布 — [rywalker 研究](https://rywalker.com/research/paperclip)。
- **Claude Code Routines**（非开源，对照）："run on a schedule, trigger on API calls, or react to GitHub events from cloud infrastructure"；三类触发（Scheduled / API bearer-token `/fire` / GitHub PR、Release 事件 + 过滤器）；"Routines are in research preview"；Pro/Max/Team/Enterprise 可用；"The minimum interval is one hour"；CLI `/schedule`；本地另有 Desktop scheduled tasks 与会话内 `/loop` — [code.claude.com/docs/en/routines](https://code.claude.com/docs/en/routines)。
- **Letta Code**：`/sleeptime` 周期性 dreaming — [README](https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md)。
- **OpenSpace v2**：加入"scheduler, skill evidence, evolution, triggers" — [README](https://raw.githubusercontent.com/HKUDS/OpenSpace/main/README.md)。
- **MemOS**：MemScheduler 异步摄取 — [README](https://raw.githubusercontent.com/MemTensor/MemOS/main/README.md)。

**通用调度 / 自动化 / 持久执行组件**

- **n8n**（n8n-io/n8n）：206,691 stars，推送 2026-10-05；许可证为 Sustainable Use License（fair-code，GitHub 标记 NOASSERTION），`.ee.` 文件另需企业许可 — [GitHub](https://github.com/n8n-io/n8n)；[LICENSE.md](https://raw.githubusercontent.com/n8n-io/n8n/master/LICENSE.md)。Schedule Trigger 支持分钟/小时/天/周/月/cron — [n8n 触发器指南](https://ryanandmattdatascience.com/n8n-trigger-node/)；社区节点：飞书 `n8n-nodes-feishu-lite`、企微 `n8n-nodes-wecom`、钉钉 `@cryozerolabs/n8n-nodes-dingtalk` — [awesome-n8n](https://github.com/restyler/awesome-n8n)；[n8n 社区钉钉节点](https://community.n8n.io/t/community-node-dingtalk-for-n8n-ai-tables-notable/192378)。
- **Huginn**（huginn/huginn）：MIT，50,024 stars，推送 2026-10-04，"Create agents that monitor and act on your behalf" — [GitHub](https://github.com/huginn/huginn)。
- **Windmill**（windmill-labs/windmill）：18,103 stars；许可证：代码主体 AGPL-3.0，部分 Apache-2.0，企业特性专有 — [GitHub](https://github.com/windmill-labs/windmill)；[LICENSE](https://raw.githubusercontent.com/windmill-labs/windmill/main/LICENSE)。
- **Temporal**（temporalio/temporal）：MIT，23,469 stars — [GitHub](https://github.com/temporalio/temporal)；与 OpenAI Agents SDK 的正式集成（2025-07/09），Pydantic AI `pip install pydantic-ai[temporal]` — [Pydantic AI 文档](https://pydantic.dev/docs/ai/integrations/durable_execution/temporal/)；[reactify 2026 综述](https://www.reactify-solutions.com/articles/durable-ai-agents-2026)。
- **Apache Airflow**：47,054 stars — [GitHub](https://github.com/apache/airflow)。
- **watchdog**（gorakhargosh/watchdog）：7,423 stars，"Python library and shell utilities to monitor filesystem events" — [GitHub](https://github.com/gorakhargosh/watchdog)。
- **AIOS**（agiresearch/AIOS）：6,445 stars，推送 2026-07-20；"AIOS kernel… managing various resources that agents require, such as LLM, memory, storage and tool"，SDK 为 Cerebrum — [GitHub](https://github.com/agiresearch/AIOS)；[README](https://raw.githubusercontent.com/agiresearch/AIOS/main/README.md)。
- **CrewAI**（crewAIInc/crewAI）：MIT，59,354 stars，推送 2026-10-03 — [GitHub](https://github.com/crewAIInc/crewAI)。

### Inferences
- 灵策智算"设定周期与触发条件 → 启用前试跑 → 结果推送飞书/企微/钉钉"的流程，OpenClaw 已几乎逐条对应（条件监视脚本、真实投递的试跑、投递到频道 + 中国 IM 插件），是最接近的开源蓝本；Hermes 次之（有 cron 与多平台投递，但无条件脚本之类的细粒度触发证据）。
- "数字员工长流程稳定执行 + 进度看板"更适合 Paperclip 这类控制面（心跳、任务票、预算、审批、会话持久化），底层执行可挂任何 agent；若要求强一致的长流程恢复，可叠加 Temporal。
- "主动感知文件变更"没有专门的 agent 级开源件，通常是 watchdog/inotify 类库 + agent 触发入口（OpenClaw stream/on-exit、n8n 触发器）组合实现。

### Gaps
- n8n 官方许可证页（docs.n8n.io）被拦截，仅核对了仓库 LICENSE.md 头部。
- watchdog、Airflow、CrewAI 的许可证在本次 API 查询中为最小输出模式未返回（watchdog 一般为 Apache-2.0、Airflow 为 Apache-2.0，未在本会话核实）。
- Hermes cron 的试跑/条件触发细节、Paperclip 看板截图与具体审计字段未能从一手文档核实（Hermes 文档站被拦截，Paperclip 仅看 README）。
- OpenClaw 文档中"Gmail PubSub triggers"与 webhook 触发的配置细节未展开阅读。

---

## 关键问题 4：机制(4) 多模型自由切换 + 工具调用审批/目录边界 + 操作回溯/全链路审计

### Takeaway
多模型切换有成熟网关：LiteLLM（MIT，100+ 供应商）、one-api/new-api（中国团队、MIT/AGPL）、Portkey、Bifrost，本地模型用 Ollama（MIT）；OpenClaw/Hermes/Letta Code 均内置 `/model` 切换与 fallback。审批与边界：Claude Code（分层权限、allow/deny/ask 规则、additionalDirectories、auto 模式分类器、PreToolUse hook、OS 沙箱）是最完整的参考模型；开源侧 Goose 四种权限模式（但默认全自动）、OpenClaw exec approvals（策略 + 按目录绑定的 allowlist + 用户审批，沙箱默认关闭）、Hermes 命令审批与容器隔离。审计：Claude Code 导出 OTel `tool_decision`/`tool_result`/`api_request` 事件可做全链路回溯；开源可用 Langfuse（MIT 核心）、Opik（Apache-2.0）、OpenLLMetry（Apache-2.0）、AgentOps（MIT）、Phoenix（ELv2，source-available）。

### Cited Findings

**多模型网关与本地模型**

- **LiteLLM**（BerriAI/litellm）：60,141 stars，推送 2026-10-05；"Call 100+ LLM APIs in OpenAI (or native) format with cost tracking, guardrails, load balancing, and logging"，Rust core + Python SDK；许可证：`enterprise/` 目录另行许可，其余 MIT — [GitHub](https://github.com/BerriAI/litellm)；[LICENSE](https://raw.githubusercontent.com/BerriAI/litellm/main/LICENSE)。
- **one-api**（songquanpeng/one-api，中国个人开发者）：MIT，37,074 stars，最近推送 2026-01-09（近 9 个月无推送）；"LLM API 管理 & 分发系统，支持 OpenAI、Azure、Anthropic Claude、Google Gemini、DeepSeek、字节豆包、ChatGLM、文心一言、讯飞星火、通义千问…单可执行文件" — [GitHub](https://github.com/songquanpeng/one-api)。
- **new-api**（QuantumNous/new-api）：AGPL-3.0，49,270 stars，推送 2026-10-01；"supports cross-converting various LLMs into OpenAI-compatible, Claude-compatible, or Gemini-compatible formats" — [GitHub](https://github.com/QuantumNous/new-api)。
- **Portkey Gateway**（Portkey-AI/gateway）：MIT，13,125 stars，推送 2026-05-25，"Route to 1,600+ LLMs, 50+ AI Guardrails" — [GitHub](https://github.com/Portkey-AI/gateway)。
- **Bifrost**（maximhq/bifrost）：Apache-2.0，8,553 stars，推送 2026-10-05，Go，"1000+ models support" — [GitHub](https://github.com/maximhq/bifrost)。
- **Ollama**：MIT，182,220 stars，推送 2026-10-04 — [GitHub](https://github.com/ollama/ollama)。**LM Studio** 桌面应用本身非开源，仅 CLI `lms` 为 MIT（5,331 stars） — [GitHub](https://github.com/lmstudio-ai/lms)。**Open WebUI**：153,976 stars，许可证为自定义"Open WebUI License"（BSD-3 条款基础上附加第 4 条品牌条件，非标准 OSI 文本） — [LICENSE](https://raw.githubusercontent.com/open-webui/open-webui/main/LICENSE)。
- **OpenClaw 模型层**：模型引用 `provider/model`；选择顺序 primary → `agents.defaults.model.fallbacks`（按序）→ 供应商内 auth-profile 轮换；`agents.defaults.modelPolicy.allow` 可做模型白名单（支持 `provider/*`）；`/model` 聊天命令；自定义供应商/本地推理服务通过 `models.providers` 配置 — [docs/concepts/models.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/models.md)；[docs/concepts/model-providers.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/model-providers.md)；README："Models and agent harnesses (Claude, Codex, local models) are plugins you can swap" — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)。
- **Hermes**："Use any model you want — Nous Portal, OpenRouter, OpenAI, your own endpoint… Switch with `hermes model` — no code changes"，`/model [provider:model]` — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)。**Letta Code**：`/connect` 配置自有 API key，`/model` 切换 — [README](https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md)。**Paperclip**："Choose models and harnesses per agent" — [README](https://raw.githubusercontent.com/paperclipai/paperclip/main/README.md)。

**工具调用审批、权限模式与目录边界**

- **Claude Code**（对照基线，非开源）："tiered permission system"：只读工具在工作目录内免审，Bash（除内置只读命令）、文件修改、WebFetch、WebSearch 需审批；模式 `default / acceptEdits / plan / auto（"a background classifier checks that they align with your request"）/ dontAsk / bypassPermissions`；"Permission rules are enforced by Claude Code, not by the model"；allow/deny/ask 规则支持 `Bash(git clean *)` 等模式；`additionalDirectories` 与 `permissions.blockReadsOutsideWorkingDirectories` 限定目录；PreToolUse hook 可在提示前拦截，且"A blocking hook also takes precedence over allow rules"；沙箱"provides OS-level enforcement that restricts shell commands' filesystem and network access"；企业可用 managed settings 禁用 bypass/auto — [code.claude.com/docs/en/permissions](https://code.claude.com/docs/en/permissions)。
- **Goose**（现为 aaif-goose/goose）：Apache-2.0，54,948 stars，推送 2026-10-05 — [GitHub](https://github.com/aaif-goose/goose)。权限模式："Completely Autonomous（默认）/ Manual Approval（支持 granular tool permissions）/ Smart Approval（risk-based… automatically approve low-risk actions and flag others）/ Chat Only"；接入 Claude Code 等 CLI provider 时"permission requests from Claude Code are routed through goose's confirmation interface" — [goose-permissions.md](https://raw.githubusercontent.com/aaif-goose/goose/main/documentation/docs/guides/managing-tools/goose-permissions.md)。二手：SmartApprove 使用 LLM 分类器"PermissionJudge"，基于 `read_only_hint` 注解 — [instagit](https://instagit.com/block/goose/goose-tool-permissions-modes/)；默认 `GooseMode::Auto` 被社区提为安全隐患 issue #12448 — [GitHub issue](https://github.com/aaif-goose/goose/issues/12448)。
- **OpenClaw exec approvals**："Commands run only when policy + allowlist + (optional) user approval all agree. Approvals stack on top of tool policy"；模式 `deny / allowlist / ask / auto / full`；"effective policy is the stricter of tools.exec.* and approvals… approvals can only tighten"；"Approvals reduce accidental execution risk, but are not a per-user auth boundary or filesystem read-only policy" — [docs/tools/exec-approvals.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/exec-approvals.md)。二手摘要：选择"Always allow here"后"authorizes the same command only in that directory"（2026.8.1 起 allowlist 条目按目录绑定）；"The workspace is the default working directory, not a hard access boundary… Enable the sandbox and configure Docker mounts to enforce real boundaries" — [搜索摘要（docs.openclaw.ai/tools/exec-approvals 等）](https://docs.openclaw.ai/tools/exec-approvals)。
- **OpenClaw 沙箱**："Sandboxing is off by default and controlled by agents.defaults.sandbox… only tool execution moves into the sandbox"；后端 Docker / Podman / SSH / OpenShell / Crabbox；"This is not a perfect security boundary, but it materially limits filesystem and process access" — [docs/gateway/sandboxing.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/sandboxing.md)。OpenClaw 架构主张"trusted gateway, untrusted execution, deterministic policy" — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)。
- **Hermes**：安全文档覆盖"Command approval, DM pairing, container isolation"；技能写入有"write-approval gate"；从 OpenClaw 迁移时可导入"Command allowlist — approval patterns" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；[skills.md](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/skills.md)。
- **Open Interpreter**：仓库已更名为 openinterpreter/openinterpreter，Apache-2.0，68,512 stars，推送 2026-10-02，Rust 重写，"A coding agent for open models like Kimi K3 and GLM 5.3" — [GitHub](https://github.com/openinterpreter/openinterpreter)。旧版 Python 的 safe mode："three options: off, ask, auto"，可用 guarddog 扫描恶意包，"experimental and does not provide any guarantees of safety" — [docs.openinterpreter.com 设置页](https://docs.openinterpreter.com/settings/all-settings)；[SAFE_MODE.md](https://github.com/OpenInterpreter/open-interpreter/blob/main/docs/SAFE_MODE.md)；社区维护分支 endolith/open-interpreter — [GitHub](https://github.com/endolith/open-interpreter)。
- **Paperclip**："Tasks, approvals & review gates"、"Governance with rollback: Approval gates are enforced, config changes are revisioned"、"scoped secrets & company boundaries" — [README](https://raw.githubusercontent.com/paperclipai/paperclip/main/README.md)。
- **HITL 框架**：LangGraph（langchain-ai/langgraph）42,727 stars — [GitHub](https://github.com/langchain-ai/langgraph)；其 interrupts 文档页存在（本次被拦截）— [docs.langchain.com interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)；AutoGen（microsoft/autogen）61,259 stars — [GitHub](https://github.com/microsoft/autogen)；HumanLayer（humanlayer/humanlayer）11,652 stars，仓库描述已转向"get AI coding agents to solve hard problems"，许可证 GitHub 标记 NOASSERTION — [GitHub](https://github.com/humanlayer/humanlayer)。

**审计日志 / 可观测性**

- **Claude Code OpenTelemetry**：导出事件 `claude_code.user_prompt`、`claude_code.tool_decision`（"Logged when a tool permission decision is made (accept/reject)"，属性含 `decision`、`source`: config/hook/user_permanent/user_temporary/user_abort/user_reject）、`claude_code.tool_result`（success、duration_ms、decision_source）、`claude_code.api_request`（model、cost、tokens）、`permission_mode_changed`、`skill_activated`；通过 `prompt.id`、`tool_use_id`、`event.sequence` 关联；内容字段默认脱敏（`OTEL_LOG_USER_PROMPTS`、`OTEL_LOG_TOOL_DETAILS` 等开关）；managed settings 可强制 OTLP 端点 — [code.claude.com/docs/en/monitoring-usage](https://code.claude.com/docs/en/monitoring-usage)。
- **Langfuse**：35,387 stars，推送 2026-10-05；LICENSE 显示"Copyright (c) 2023-2026 ClickHouse, Inc."，`ee/` 目录另行许可，其余 MIT — [GitHub](https://github.com/langfuse/langfuse)；[LICENSE](https://raw.githubusercontent.com/langfuse/langfuse/main/LICENSE)；README：自托管支持 docker compose / VM / Kubernetes Helm / Terraform（AWS、Azure、GCP） — [README](https://raw.githubusercontent.com/langfuse/langfuse/main/README.md)。
- **Opik**（comet-ml/opik）：Apache-2.0，22,383 stars，"comprehensive tracing… agentic workflows" — [GitHub](https://github.com/comet-ml/opik)。
- **OpenLLMetry**（traceloop/openllmetry）：Apache-2.0，7,470 stars，"observability… based on OpenTelemetry" — [GitHub](https://github.com/traceloop/openllmetry)。
- **AgentOps**：MIT，5,885 stars，最近推送 2026-06-25；"Replay Analytics and Debugging: Step-by-step agent execution graphs"、"Native Integrations with CrewAI, AG2 (AutoGen), Agno, LangGraph"、可自托管 — [GitHub](https://github.com/AgentOps-AI/agentops)；[README](https://raw.githubusercontent.com/AgentOps-AI/agentops/main/README.md)。
- **Arize Phoenix**：11,709 stars；许可证 Elastic License 2.0（source-available，非 OSI，禁止作为托管服务提供） — [LICENSE](https://raw.githubusercontent.com/Arize-ai/phoenix/main/LICENSE)；"Trace your LLM application's runtime using OpenTelemetry-based instrumentation" — [README](https://raw.githubusercontent.com/Arize-ai/phoenix/main/README.md)。
- OpenClaw 对定时/心跳/事件轮次在会话记录中打标记（`[OpenClaw heartbeat poll]` 等）以区分来源 — [heartbeat.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/heartbeat.md)。

### Inferences
- 灵策智算"目录边界授权 + 工具调用审批 + 关键动作确认"在开源 agent 中没有单一产品达到 Claude Code 的完整度；最接近的组合是 OpenClaw（exec approvals + 按目录 allowlist + Docker 沙箱）或 Goose（权限模式 + 细粒度工具权限），再配合 PreToolUse 式 hook。
- "操作可回溯、全链路审计"应由独立可观测性层承担：自托管 Langfuse/Opik/OpenLLMetry 接 OTel，是开源侧的标准方案；Claude Code 的 `tool_decision` 事件模型可作为"审批决策也要进审计"的设计参照。
- 多模型切换的工程风险不在网关（LiteLLM/new-api 已成熟）而在产品层的 fallback 与白名单策略，OpenClaw 的 primary/fallbacks/modelPolicy.allow 给出了可照搬的配置模型。

### Gaps
- LiteLLM、Langfuse、LangGraph 的官方文档站被拦截：LiteLLM 审计日志/虚拟 key 的企业版边界、Langfuse 审计日志功能、LangGraph `interrupt()` 细节均未一手核实。
- Goose 的 PermissionJudge 实现仅有第三方来源。
- OpenClaw exec approvals 的"按目录绑定 allowlist（2026.8.1）"细节来自搜索摘要，未在仓库文档中逐字核对。
- HumanLayer 仓库似已转型，其原 HITL SDK 的现状与许可证未核实。

---

## 关键问题 5：哪些完整开源 Agent 产品已内置这些机制中的哪几项（对照矩阵）

### Takeaway
OpenClaw 与 Hermes Agent 是唯二把"分层记忆 + 自学习技能 + 定时/事件触发 + 多模型 + 审批/沙箱"全部内置的开源 agent 产品（均 MIT），且互相可迁移；Paperclip 补上"多数字员工的公司级调度、预算、审批、看板"；Letta Code 内置记忆/技能学习/模型切换但无调度与审批证据；Goose 强在权限模式；Open Interpreter 已重写为面向开源模型的编码 agent；Dify 以工作流触发与（公测中的）Agent 技能系统覆盖部分能力。

### Cited Findings

- **OpenClaw**（openclaw/openclaw）：MIT，391,404 stars，forks 82,270，创建 2025-11-24，推送 2026-10-05 — [GitHub](https://github.com/openclaw/openclaw)。README："stewarded by the OpenClaw Foundation, an independent 501(c)(3), and has no paid tier, hosted service, or token"；"State, memory, and credentials live on your hardware"；捐助方含 Amazon、OpenAI、Red Hat 等；作者 Peter Steinberger — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)。内置：三层记忆 + dreaming（[memory.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/memory.md)）、SKILL.md 技能 + ClawHub + Skill Workshop 自学习（[skills.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md)）、Automations/Heartbeat（[cron-jobs.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/automation/cron-jobs.md)）、多供应商 + fallbacks（[models.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/models.md)）、exec approvals + 沙箱（[exec-approvals.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/exec-approvals.md)）。Wikipedia 有独立词条 — [en.wikipedia.org/wiki/OpenClaw](https://en.wikipedia.org/wiki/OpenClaw)。
- **Hermes Agent**（NousResearch/hermes-agent）：MIT，251,286 stars，创建 2025-07-22，推送 2026-10-05，Nous Research — [GitHub](https://github.com/NousResearch/hermes-agent)。内置：记忆（MEMORY.md/USER.md/session search/Honcho）、自主建技能 + Skills Hub、cron + 多平台投递、任意模型切换、命令审批 + 容器隔离；"If you're coming from OpenClaw, Hermes can automatically import your settings, memories, skills, and API keys" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)。
- **Paperclip**：MIT，97,323 stars；控制面（org chart、heartbeats、routines、budgets、approvals、Skill Studio、audit），执行委托给 OpenClaw/Claude Code/Codex/Hermes 等 — [README](https://raw.githubusercontent.com/paperclipai/paperclip/main/README.md)。
- **Letta Code**：Apache-2.0，3,517 stars；记忆块 + dreaming + skill learning + MemFS（git 跟踪）+ `/model` — [README](https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md)。
- **Goose**：Apache-2.0，54,948 stars，组织已迁至 aaif-goose；权限四模式 — [GitHub](https://github.com/aaif-goose/goose)；[goose-permissions.md](https://raw.githubusercontent.com/aaif-goose/goose/main/documentation/docs/guides/managing-tools/goose-permissions.md)。
- **Open Interpreter**：Apache-2.0，68,512 stars，Rust 重写为开源模型编码 agent — [GitHub](https://github.com/openinterpreter/openinterpreter)。
- **Dify**：修改版 Apache-2.0，157,865 stars；Trigger 让工作流"sit in the background… automatically react to what's happening in the outside world"；Dify Agent 公测含沙箱与技能系统（第三方来源） — [LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)；[dify-hosting.com](https://dify-hosting.com/en/guides/dify-updates/)；[Dify 博客 Introducing New Agent](https://dify.ai/blog/introducing-new-dify-agent)。
- **OpenSpace**（MIT）：跨 agent 技能管理层 + v2 的 scheduler/triggers/sandbox/memory — [README](https://raw.githubusercontent.com/HKUDS/OpenSpace/main/README.md)。
- **MemOS**（Apache-2.0）：作为记忆插件嵌入 OpenClaw / Hermes / DeepSeek Harness — [README](https://raw.githubusercontent.com/MemTensor/MemOS/main/README.md)。

**对照矩阵（✓ 一手文档证实 / ○ 二手或部分 / — 未见证据）**

| 产品 | 许可证 | 分层长时记忆 + 后台整理 | SKILL.md + 市场 | 技能自进化 | 定时/事件触发 + IM 投递 | 多模型切换 | 工具审批/目录边界/沙箱 | 审计回溯 |
|---|---|---|---|---|---|---|---|---|
| OpenClaw | MIT | ✓ (3 层 + dreaming) | ✓ (ClawHub) | ✓ (Skill Workshop off/propose/auto) | ✓ (5 种调度、条件脚本、heartbeat、频道投递；中国 IM 社区插件) | ✓ (primary/fallbacks/allow) | ✓ (exec approvals；沙箱默认关) | ○ (会话标记；需外接 OTel) |
| Hermes Agent | MIT | ✓ (MEMORY/USER + FTS5) | ✓ (Skills Hub, agentskills.io) | ✓ (skill_manage, /learn) | ✓ (cron + 任意平台投递) | ✓ | ○ (命令审批、容器隔离、写入审批) | — |
| Paperclip | MIT | — (控制面) | ✓ (Skill Studio、版本/回滚) | ○ (evals/active learning) | ✓ (heartbeat、routines、API/webhook) | ✓ (按 agent 选模型) | ✓ (审批门、预算、边界) | ✓ (ticket/历史/回滚) |
| Letta Code | Apache-2.0 | ✓ (memory blocks + dreaming + MemFS) | ✓ (letta skills install) | ○ (skill learning) | ○ (/sleeptime) | ✓ | — | ○ (git 跟踪上下文) |
| Goose | Apache-2.0 | — | — | — | — | ✓ (any LLM) | ✓ (4 模式 + 工具级) | — |
| Dify | 修改版 Apache-2.0 | ○ (插件级) | ○ (1.16/1.17 第三方) | — | ✓ (Trigger) | ✓ | ○ (沙箱) | ○ (tracing) |
| Claude Code（非开源，对照） | 专有 | ✓ (CLAUDE.md + auto memory) | ✓ | — | ✓ (Routines，云端) | — (Anthropic 模型) | ✓ (最完整) | ✓ (OTel 事件) |

### Inferences
- 若以"最少拼装"为目标，OpenClaw（或 Hermes）+ MemOS 插件 + OpenSpace 技能层 + Langfuse 审计，可覆盖灵策智算四项机制的绝大部分；缺口主要在"面向业务人员的训练场 UI"、"跨数字员工的全局技能泛化"和"企业级全链路审计"。
- Paperclip 的"公司/员工"抽象与灵策智算"数字员工 + 进度看板"定位最接近，但它不做执行，只做调度与治理。

### Gaps
- Goose 的记忆扩展与调度（recipes/scheduler）未在本次核实，矩阵中标为"—"仅表示无证据。
- Dify 多项能力只有第三方来源。
- 各产品的"审计"列缺少一手的审计日志字段文档（除 Claude Code）。

---

## 关键问题 6："技能自进化"（自动把重复模式沉淀为技能并全局泛化）在开源界是否有可用实现，还是停留在论文阶段

### Takeaway
已不再只是论文：2026 年内 OpenClaw（Skill Workshop 自学习，off/propose/auto 三档 + 每周技能库复盘）、Hermes Agent（agent 自主创建/修补技能，用户纠正触发）、OpenSpace（FIX/DERIVED/CAPTURED 三类演化，按任务轨迹作质量证据）、MemOS（技能结晶）都有可运行的开源实现并被万星级产品采用；但"全局泛化"（一次纠正影响所有 agent）仅在共享技能库层（OpenSpace、Paperclip org-wide skills）有雏形，且学术研究表明无治理的自进化会固化不安全行为。

### Cited Findings
- OpenClaw："After substantial work, a detached background review can turn corrections and … into proposals"；`autonomous.mode`："off disables autonomous capture, propose creates pending proposals, and auto enables direct per-turn and weekly Workshop maintenance" — [configuration.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skill-workshop/configuration.md)；proposal 生命周期 pending → applied/rejected/quarantined，apply 前重跑安全扫描并写回滚元数据 — [how-it-works.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skill-workshop/how-it-works.md)；相关 bug 报告显示该功能 2026 年仍在打磨（Web UI 报"Applied"但未写入 SKILL.md） — [issue #131330](https://github.com/openclaw/openclaw/issues/131330)。
- Hermes："when it figures out a non-trivial workflow, it saves the approach as a skill for future reuse… Skills and memory work together in the self-improvement loop" — [skills.md](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/website/docs/user-guide/features/skills.md)。
- OpenSpace："CAPTURED — Save one reusable subworkflow only when the source trace shows…"；`autonomous` 模式下"all admission-approved and validated FIX/DERIVED/CAPTURED actions may commit"；v2 把 task traces 作为"quality evidence"上传并生成"usage-quality summaries" — [README](https://raw.githubusercontent.com/HKUDS/OpenSpace/main/README.md)。
- MemOS Hermes/OpenClaw 本地插件："task summarization & skill evolution"、"tiered skill evolution" — [README](https://raw.githubusercontent.com/MemTensor/MemOS/main/README.md)。
- Letta：agents "learn and evolve over long horizons through rewriting their own memory, skills, prompts, and even the harness itself (through mods)" — [letta-code README](https://raw.githubusercontent.com/letta-ai/letta-code/main/README.md)。
- 研究原型：Voyager（停更于 2024-04） — [GitHub](https://github.com/MineDojo/Voyager)；SkillWeaver（2025-04 后无推送） — [GitHub](https://github.com/OSU-NLP-Group/SkillWeaver)；CoEvoSkills（COLM 2026，74 stars） — [GitHub](https://github.com/Zhang-Henry/CoEvoSkills)。
- 风险证据："all 21 evolved configurations author unsafe artifacts" — [arXiv 2608.12851](https://arxiv.org/abs/2608.12851)。

### Inferences
- 灵策智算宣称的"重复模式自动识别、一次纠正全局泛化"中，前半句在开源已有三条独立实现路径（会话事后复盘、agent 自主建技能、轨迹驱动演化）；后半句要靠共享技能库 + 版本治理实现，开源界尚无"自动跨员工泛化"的成熟产品证据。
- 企业落地建议采用 OpenClaw 的 `propose` 模式思路（人审后发布、可回滚、带扫描），而非全自动。

### Gaps
- Hermes 自动建技能的精确触发条件只有第三方（ssojet）来源，官方文档站被拦截。
- OpenSpace"46% token 节省"与 MemOS 技能演化效果均无独立评测来源。
- 未找到任何开源项目对"一次纠正自动同步到所有 agent 实例"给出一手文档。
