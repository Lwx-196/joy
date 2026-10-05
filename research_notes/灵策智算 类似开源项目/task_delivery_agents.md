# 与"灵策智算 LynxceAI 直接交付结果"能力对应的开源项目（通用任务执行 Agent + 垂直产出型 Agent，截至 2026-10）

> 数据口径说明：下文所有 star 数、许可证字段（SPDX）、最近推送日期（pushed_at）、是否归档（archived）均于 **2026-10-05** 通过 GitHub Search API（经授权的 GitHub MCP 工具）一次性读取；来源统一标为该仓库的 GitHub 主页 URL。许可证一栏中 "API: NOASSERTION" 表示 GitHub 无法自动识别许可证，此时我另行抓取 raw LICENSE 文件核实并注明。功能性描述主要来自各项目 README（raw.githubusercontent.com）与官方博客。
> 关于"灵策智算 LynxceAI"本身：WebSearch 未检索到任何官方网站、产品页或第三方报道（仅见一个 CSDN 账号 "lynxce2026"），本笔记中的产品画像完全来自任务说明，未经独立核实。— [WebSearch 结果](https://blog.csdn.net/lynxce2026/article/details/162623315)

---

## 关键问题 1：各项目基本档案（名称、地址、许可证、star、活跃度、维护方/国别、运行形态、可否本地/私有化部署）

### Takeaway
A 组（通用"给目标就跑完全程"Agent）在 2026 年已明显分化：一类是"个人助理/Harness"型超大项目（OpenClaw 39 万 star、Hermes Agent 25 万、DeerFlow 8.3 万、nanobot 4.9 万），一类是 2025 年 Manus 复刻潮的幸存者（OpenManus、OWL/Eigent、Suna→Kortix、ai-manus、agenticSeek、II-Agent），还有一批已归档或停更（AgentGPT、gpt-engineer、Open Deep Research、Vanna、PandasAI 近一年无提交、JoyAgent 2026-02 后无提交）。B 组（垂直产出型）在 2026 年出现了一批"让 Agent 直接产出 Office 文件"的新项目（OfficeCLI、ppt-master、genoffice、HermesOffice、OpenWorkBuddy），许可证总体以 MIT/Apache-2.0 为主，但多个项目是"开源核心 + 受限目录"的双许可证（AutoGPT、Dyad、PandasAI、WrenAI、Open WebUI、SuperSonic、Chat2DB、Suna）。

### Cited Findings

#### A 组：通用型任务执行 Agent

| 项目 | GitHub | 许可证 | Star（2026-10-05） | 最近推送 / 状态 | 维护方·国别 | 运行形态 | 本地/私有化 |
|---|---|---|---|---|---|---|---|
| OpenManus | https://github.com/FoundationAgents/OpenManus | MIT | 58,466 | 2026-09-30 | FoundationAgents（MetaGPT 核心成员 Xinbing Liang、Jinyu Xiang 等，中国 DeepWisdom 社区）| CLI（`python main.py`）、MCP 模式（`run_mcp.py`）、多 Agent flow（`run_flow.py`，README 注明不稳定）；无内置 Web UI | 可，本地 Python + 自配 LLM API（config.toml）|
| OWL | https://github.com/camel-ai/owl | README 称 Apache-2.0；GitHub API 未返回许可证字段，raw `LICENSE` 404（未核实许可证文件）| 20,151 | 2026-09-30 | CAMEL-AI（社区组织，核心团队与 Eigent 公司相关）| Python 脚本 + Gradio Web UI（英/中/日三版）| 可 |
| Eigent | https://github.com/eigent-ai/eigent | Apache-2.0 | 15,456 | 2026-10-02 | Eigent AI（基于 CAMEL-AI/OWL）| Electron 桌面客户端 | 可，"Local Deployment (Recommended)"，支持 vLLM/Ollama/LM Studio |
| Suna → Kortix | https://github.com/kortix-ai/suna | **main 分支 LICENSE 文件为 Elastic License 2.0（ELv2，source-available，非 OSI）**；API: NOASSERTION；第三方文章称其早期为 Apache-2.0（可能发生过许可证变更，变更时间未核实）| 20,247 | 2026-10-05；仓库 issues 已关闭 | Kortix（美/欧创业公司）| Web + API + CLI + Docker 沙箱 + Electron 桌面/移动端 | 可 Docker Compose 自托管，但完整功能依赖托管 git、GitHub、Pipedream 连接器 |
| Magentic-UI → MagenticLite | https://github.com/microsoft/magentic-ui | MIT | 10,088 | 2026-09-23 | Microsoft Research AI Frontiers（美）| `uv pip install magentic_ui>=0.2.0`，本地 `magentic-ui --port 8081` Web UI | 可，浏览器会话运行于 Quicksand（QEMU 轻量 VM 沙箱）|
| AutoGPT | https://github.com/Significant-Gravitas/AutoGPT | 双许可证：`autogpt_platform/` 为 **Polyform Shield 1.0.0**（非 OSI，禁止作为竞争性托管服务）；其余（classic/）为 MIT | 187,656 | 2026-10-05 | Significant Gravitas（英/美）| 平台：Web 画布式 Agent 构建器 + 服务端（Docker 自托管）；Classic：CLI | 可 Docker 自托管，需自备模型 Key |
| AgentGPT | https://github.com/reworkd/AgentGPT | GPL-3.0 | 36,296 | **已归档**，最后推送 2025-04-29 | Reworkd（美）| 浏览器 Web 应用 | 可自托管（已停更）|
| DeerFlow 2.0 | https://github.com/bytedance/deer-flow | MIT | 83,396 | 2026-10-05 | 字节跳动（中国）| Web UI（localhost:2026）、TUI 终端工作台、Python 嵌入式客户端、Docker | 可：沙箱支持 Local / Docker / Kubernetes，另可接 E2B、Tenki、BoxLite 云沙箱 |
| JoyAgent-JDGenie | https://github.com/jd-opensource/joyagent-jdgenie | Apache-2.0 | 11,921 | 2026-02-12（默认分支 data_agent；此后无推送）| 京东 CHO 企业信息化团队（中国）| Java 后端 + Python 工具服务 + React 前端，Docker 一键部署 | 可 |
| Cooragent | https://github.com/LeapLabTHU/cooragent | Apache-2.0 | 1,672 | 2026-04-29 | 清华大学 LeapLab（中国）| Python 本地部署（conda/venv，Python 3.12+）| 可 |
| MetaGPT（含 Data Interpreter）| https://github.com/FoundationAgents/MetaGPT | MIT | 70,746 | 2026-01-21（此后无推送）| DeepWisdom 深度赋智（中国）；公司重心已转向商业产品 Atoms（atoms.dev）| Python 库/CLI | 可 |
| CrewAI | https://github.com/crewAIInc/crewAI | MIT | 59,354 | 2026-10-03 | CrewAI Inc（美）| Python 框架（Crews + Flows），商业版 AMP | 可（框架）|
| Open Deep Research | https://github.com/langchain-ai/open_deep_research | MIT | 12,686 | **已归档**（API archived=true；最后推送 2026-08-10）| LangChain（美）| LangGraph 应用 | 可（已停更）|
| GPT Researcher | https://github.com/assafelovic/gpt-researcher | Apache-2.0 | 29,919 | 2026-10-01 | Assaf Elovic / Tavily（以色列）| pip 包、Docker（后端 8000 + React 前端 3000）、MCP server | 可 |
| smolagents | https://github.com/huggingface/smolagents | Apache-2.0 | 29,680 | 2026-09-30 | Hugging Face（美/法）| Python 库（CodeAgent）| 可（框架）|
| Hermes Agent | https://github.com/NousResearch/hermes-agent | MIT | 251,286（API 同时返回 48,014 个 open issues，数值异常，原样记录）| 2026-10-05 | Nous Research（美）| CLI/TUI + 消息网关（Telegram、Discord、Slack、WhatsApp、Signal）| 可，7 种终端后端：local、Docker、SSH、Singularity、Modal、Daytona、Vercel Sandbox |
| Agent Zero | https://github.com/agent0ai/agent-zero | MIT（raw LICENSE 核实；API: NOASSERTION）| 19,374 | 2026-10-02 | agent0ai（Jan Tomášek，捷克）| Docker 化 Linux 桌面 + Web UI（"Canvas"）| 可，"runs wherever Docker runs, from a $6 VPS or Raspberry Pi" |
| II-Agent | https://github.com/Intelligent-Internet/ii-agent | Apache-2.0 | 3,390 | 2026-08-16 | Intelligent Internet（新加坡/美）| Docker Compose（PostgreSQL + Redis + MinIO），后端 8000/前端 1420 | 可 |
| ai-manus | https://github.com/Simpleyyt/ai-manus | MIT | 1,634 | 2026-10-04 | 个人开发者 Simpleyyt（中国）| Web + Docker 沙箱（每任务独立容器，VNC 看浏览器）| 可，"Minimal deployment requires only an LLM service" |
| agenticSeek | https://github.com/Fosowl/agenticSeek | GPL-3.0 | 27,427 | 2026-10-02 | 个人开发者 Fosowl（法）| 本地 Web/CLI | 可，"Fully Local Manus AI. No APIs" |
| OpenClaw（原 Clawdbot/Moltbot）| https://github.com/openclaw/openclaw | MIT | 391,407 | 2026-10-05（仓库创建于 2025-11-24）| OpenClaw Foundation（独立 501(c)(3)），发起人 Peter Steinberger（奥地利）| 本地 Gateway（macOS/iOS/Android/Windows/Linux）+ 20 余个消息渠道 | 可，"State, memory, and credentials live on your hardware" |
| nanobot | https://github.com/HKUDS/nanobot | MIT | 48,790 | 2026-10-05（创建于 2026-02-01）| 香港大学 HKUDS（Xubin Ren、Yongru Chen 等，中国香港）| Python，自带 WebUI、终端客户端、OpenAI 兼容 API、SDK | 可，支持 Ollama/vLLM |
| Agent TARS / UI-TARS-desktop | https://github.com/bytedance/UI-TARS-desktop | Apache-2.0 | 39,204 | 2026-09-24 | 字节跳动（中国）| 桌面 GUI Agent + CLI/Web | 可 |
| Agent S | https://github.com/simular-ai/Agent-S | Apache-2.0 | 12,539 | 2026-09-05 | Simular（美）| 计算机操作 Agent 框架 | 可 |
| browser-use | https://github.com/browser-use/browser-use | MIT | 117,161 | 2026-10-03 | Browser Use（德/美）| Python 库 | 可 |
| OpenHands | https://github.com/OpenHands/OpenHands | MIT | 90,014 | 2026-10-05（组织已由 All-Hands-AI 迁至 OpenHands）| OpenHands/All Hands AI（美）| 编码 Agent，CLI/Web | 可 |
| goose | https://github.com/aaif-goose/goose | Apache-2.0（据 Wikipedia/第三方；API 最小输出未含许可证字段）| 54,948 | 活跃；2026-04 由 Block 移交 Linux 基金会 AAIF，仓库迁至 aaif-goose/goose | AAIF / Block（美）| Rust，CLI + 桌面应用 | 可 |
| Qwen-Agent | https://github.com/QwenLM/Qwen-Agent | Apache-2.0 | 17,132 | 2026-03-04 | 阿里通义（中国）| Python 框架 | 可 |
| AgentScope | https://github.com/agentscope-ai/agentscope | Apache-2.0 | 32,762 | 2026-09-30 | 阿里（中国）| Python 框架 | 可 |
| Youtu-Agent | https://github.com/TencentCloudADP/youtu-agent | MIT（raw LICENSE 核实；API: NOASSERTION）| 4,621 | 2026-03-21 | 腾讯云（中国）| Python 框架 | 可 |
| MiroFlow | https://github.com/MiroMindAI/MiroFlow | Apache-2.0 | 3,118 | 2026-07-06 | MiroMind（新加坡/中国）| Python + Web UI | 可 |
| Eko | https://github.com/FellouAI/eko | MIT | 4,961 | 2026-03-03 | Fellou（中国/美）| JS/TS 框架 | 可 |

来源（上表 star/许可证/推送日期）：各项目 GitHub 主页经 GitHub API 读取；运行形态/部署信息来源：[OpenManus README](https://raw.githubusercontent.com/FoundationAgents/OpenManus/main/README.md)、[OWL README](https://raw.githubusercontent.com/camel-ai/owl/main/README.md)、[Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)、[Suna/Kortix README](https://raw.githubusercontent.com/kortix-ai/suna/main/README.md)、[Suna LICENSE](https://raw.githubusercontent.com/kortix-ai/suna/main/LICENSE)、[Magentic-UI README](https://raw.githubusercontent.com/microsoft/magentic-ui/main/README.md)、[AutoGPT README](https://raw.githubusercontent.com/Significant-Gravitas/AutoGPT/master/README.md)、[AutoGPT LICENSE](https://raw.githubusercontent.com/Significant-Gravitas/AutoGPT/master/LICENSE)、[DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)、[JoyAgent README](https://raw.githubusercontent.com/jd-opensource/joyagent-jdgenie/data_agent/README.md)、[Cooragent README](https://raw.githubusercontent.com/LeapLabTHU/cooragent/main/README.md)、[CrewAI README](https://raw.githubusercontent.com/crewAIInc/crewAI/main/README.md)、[GPT Researcher README](https://raw.githubusercontent.com/assafelovic/gpt-researcher/master/README.md)、[Hermes Agent README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)、[Agent Zero README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)、[Agent Zero LICENSE](https://raw.githubusercontent.com/agent0ai/agent-zero/main/LICENSE)、[II-Agent README](https://raw.githubusercontent.com/Intelligent-Internet/ii-agent/main/README.md)、[ai-manus README](https://raw.githubusercontent.com/Simpleyyt/ai-manus/main/README.md)、[OpenClaw README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)、[nanobot README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)、[Youtu-Agent LICENSE](https://raw.githubusercontent.com/TencentCloudADP/youtu-agent/main/LICENSE)。

近况补充（A 组）：
- OpenClaw 在 2026-01-27 因 Anthropic 法务对 "Clawdbot" 名称提出异议改名 Moltbot，2026-01-30 再改名 OpenClaw，同期披露零点击 WebSocket 劫持漏洞 CVE-2026-25253；2026-03 成为 GitHub 历史上 star 最多的仓库（当时 34.7 万）。— [eastondev 改名史](https://eastondev.com/blog/en/posts/ai/20260204-openclaw-rename-history/)；[Wikipedia: OpenClaw](https://en.wikipedia.org/wiki/OpenClaw)；[jitendrazaa 指南](https://www.jitendrazaa.com/blog/ai/clawdbot-complete-guide-open-source-ai-assistant-2026/)
- DeerFlow 2.0 为 2026-02 发布的"从零重写、与 v1 无共享代码"的 SuperAgent Harness，发布后登 GitHub Trending 第一；MarkTechPost 2026-03-09 报道其"编排子 Agent、记忆与沙箱"。— [dev.to 评测](https://dev.to/andrew-ooo/deerflow-20-review-bytedances-open-superagent-harness-5he0)；[MarkTechPost](https://www.marktechpost.com/2026/03/09/bytedance-releases-deerflow-2-0-an-open-source-superagent-harness-that-orchestrates-sub-agents-memory-and-sandboxes-to-do-complex-tasks/)
- Magentic-UI 已演进为 MagenticLite（0.2.x）：2026-05-21 微软研究院发布 MagenticLite + MagenticBrain（小型编排模型）+ Fara1.5（浏览器操作模型），"the app, the harness, and every model in the stack are now open"，模型权重上架 Hugging Face。— [Microsoft Research 博客](https://www.microsoft.com/en-us/research/blog/magenticlite-magenticbrain-fara1-5-an-agentic-experience-optimized-for-small-models/)；[MSFTResearch on X](https://x.com/MSFTResearch/status/2079989338994069511)
- Kortix 在 2026 年将 Suna 重新定位为 "the open-source AI Operating System"，PR #9068/#9142/#9152 显示品牌改动反复；第三方文章称其"整个平台采用 Apache-2.0"，但当前 main 分支 LICENSE 文件为 Elastic License 2.0（冲突，以仓库文件为准）。— [PR #9068](https://github.com/kortix-ai/suna/pull/9068)；[chatgate.ai](https://chatgate.ai/post/kortix-suna/)；[Suna LICENSE](https://raw.githubusercontent.com/kortix-ai/suna/main/LICENSE)
- MetaGPT 母公司 DeepWisdom 于 2026-01-13 将 MGX 升级并更名为 Atoms（atoms.dev），主打"5 分钟交付可运行网站/应用"；2025 上半年融资约 2.2 亿元人民币。— [KrASIA](https://kr-asia.com/from-metagpt-to-atoms-deepwisdom-leads-chinas-push-into-vibe-coding)；[Pandaily](https://pandaily.com/ai-programming-company-deep-wisdom-raises-30-6-million-launches-product-atoms)
- Open Deep Research 已于 2026-08-21 被 LangChain 归档为只读；LangChain 另有 DeepAgents 框架（任务规划、文件系统工具、沙箱 shell、子 Agent）。— [Open Deep Research 仓库](https://github.com/langchain-ai/open_deep_research)；[LangChain 博客](https://blog.langchain.com/open-deep-research/)
- goose 于 2025-12 被 Block 捐赠为 Linux 基金会 Agentic AI Foundation（AAIF）创始项目，2026-04 仓库与治理移交 AAIF（block/goose 重定向至 aaif-goose/goose）。— [Wikipedia: goose](https://en.wikipedia.org/wiki/Goose_(AI_agent))
- gpt-engineer 已归档，仓库描述自称 "Precursor to: https://lovable.dev"。— [gpt-engineer 仓库](https://github.com/AntonOsika/gpt-engineer)
- JoyAgent-JDGenie：京东在 2025-07 首发后又开源 DataAgent（数据治理协议 DGP、智能问数、诊断分析），需使用 data_agent 分支；第三方称 2026-03-09 至 04-23 有更新，但 GitHub API 显示仓库最后推送为 2026-02-12（冲突，API 数据更可靠）。— [京东云开发者博客园](https://www.cnblogs.com/Jcloud/p/19105433)；[JoyAgent 仓库](https://github.com/jd-opensource/joyagent-jdgenie)

#### B 组：垂直产出型

| 项目 | GitHub | 许可证 | Star | 最近推送 / 状态 | 维护方·国别 | 运行形态 | 本地 |
|---|---|---|---|---|---|---|---|
| **PPT 生成** | | | | | | | |
| Presenton | https://github.com/presenton/presenton | Apache-2.0 | 10,953 | 2026-10-02 | Presenton 团队（商业公司，presenton.ai）| Docker（可 GPU）、Electron 桌面（macOS/Win/Linux）、Helm、REST API、内置 MCP server | 可，支持 Ollama/LM Studio |
| PPTAgent | https://github.com/icip-cas/PPTAgent | MIT | 5,088 | 2026-09-28 | 中科院计算所 ICIP-CAS 等（中国）| Python 3.12 + uv + LibreOffice；2026-09 发布 Claude Code/Codex/OpenCode Skill | 可，2026-01 起支持 PPTX 导出与离线模式 |
| presentation-ai（ALLWEONE）| https://github.com/allweonedev/presentation-ai | MIT | 3,035 | 2026-06-05 | ALLWEONE（巴西）| Next.js Web 应用 | 可自托管 |
| ppt-master | https://github.com/hugohe3/ppt-master | MIT | 57,670 | 2026-10-05（创建于 2025-12-10）| 个人开发者 Hugo He（金融从业者）| 作为 Claude Code / Cursor / Codex / Gemini CLI 的 Skill/工作流本地运行，Python 3.10+ | 可，"Apart from AI model communication, the entire pipeline runs on your machine" |
| Marp | https://github.com/marp-team/marp | MIT | 12,591 | 2026-07-29 | Marp team（日）| Markdown→PDF/HTML/PPTX CLI | 可 |
| Slidev | https://github.com/slidevjs/slidev | MIT | 48,925 | 2026-10-02 | Anthony Fu / slidevjs（中国/法）| Markdown→Web 幻灯片 | 可 |
| SlideSpeak 开源版 | — | — | — | — | — | — | 未找到可核实的开源仓库（见 Gaps）|
| **Word/Excel 文档操作** | | | | | | | |
| anthropics/skills | https://github.com/anthropics/skills | 多数 skill Apache-2.0；**docx/pdf/pptx/xlsx 文档类 skill 为 "source-available, not open source"**（API 未返回许可证字段）| 179,701 | 2026-10-03 | Anthropic（美）| SKILL.md 格式（YAML frontmatter + 指令），可被 Claude Code / Claude.ai / API 加载 | 可（skill 为文本 + 脚本）|
| python-docx | https://github.com/python-openxml/python-docx | MIT | 5,733 | 2026-08-01 | python-openxml（美）| Python 库 | 可 |
| OfficeCLI | https://github.com/iOfficeAI/OfficeCLI | Apache-2.0 | 31,575 | 2026-10-05（创建于 2026-03-15）| iOfficeAI（中国，officecli.ai）| .NET 单二进制 CLI + MCP server（macOS/Linux/Win），无需安装 Office | 可 |
| genoffice | https://github.com/genspark-ai/genoffice | Apache-2.0（企业模块另行保留）| 8,642 | 2026-10-05（创建于 2026-07-31）| Genspark（Mainfunc, Inc.，美/新加坡）| 桌面 Office 套件（macOS/Win/Linux）+ `genoffice` CLI + Agent skill + MCP | 可，BYOK，本地 OpenAI 兼容端点 |
| HermesOffice | https://github.com/criptogus/HermesOffice | 未核实（API 最小输出未含许可证）| 600 | 活跃（创建于 2026-08-04）| 社区（基于 Hermes Agent + Electron）| 桌面 Office 套件（Docs/Sheets/Slides/PDF）| 可，"100% local" |
| OpenWorkBuddy | https://github.com/CatCatUncle/openworkbuddy | **PolyForm Noncommercial 1.0.0（非 OSI；商用需付费授权）** | 259 | 2026-10-05（创建于 2026-08-10）| 个人开发者"开发者猫叔"（中国）| Electron 桌面 + Web + Docker，Node.js 18+ | 可，默认仅监听 127.0.0.1 |
| AionUi | https://github.com/iOfficeAI/AionUi | Apache-2.0 | 33,319 | 2026-09-09 | iOfficeAI（中国）| 桌面 "Cowork" 应用，包裹 OpenClaw/Hermes/Claude Code/Codex 等 20+ CLI Agent | 可 |
| **数据清洗/分析** | | | | | | | |
| PandasAI | https://github.com/sinaptik-ai/pandas-ai | MIT（`pandasai/ee/` 目录另行许可）；API: NOASSERTION | 23,822 | **2025-10-28 后无推送**（约 11 个月停滞）| Sinaptik（德）| Python 库 | 可 |
| MetaGPT Data Interpreter | 同 MetaGPT 仓库 | MIT | 70,746 | 2026-01-21 后无推送 | DeepWisdom（中国）| Python（计划→写码→执行→动态调整）| 可 |
| DB-GPT | https://github.com/eosphoros-ai/DB-GPT | MIT | 20,075 | 2026-10-04 | eosphoros-ai（中国，docs.dbgpt.cn）| Docker / `uv pip install dbgpt-app` / 一键脚本 | 可，支持 vLLM、llama.cpp 本地 GPU |
| Chat2DB 社区版 | https://github.com/OtterMind/Chat2DB（已由 CodePhiliaX 迁至 OtterMind）| **5.3.0+ 为基于 Apache-2.0 的自定义 source-available 许可证；5.2.x 及以前 Apache-2.0** | 28,303 | 2026-10-01 | OtterMind（中国）| 桌面（Win/macOS/Linux）、Web、Docker、CLI（MCP）| 可，"local-first" |
| Wren AI | https://github.com/Canner/WrenAI | LICENSE 为多许可证：核心/SDK/skills Apache-2.0，文档 CC BY 4.0，预留 AGPL-3.0 模块；API: NOASSERTION | 17,800 | 2026-10-05 | Canner（中国台湾）| `pip install wrenai` CLI、自托管、Wren Cloud 商业版 | 可 |
| SuperSonic | https://github.com/tencentmusic/supersonic | Apache-2.0 + 附加条款（基于源码开发并分发衍生作品须取得商业授权）→ 非纯 OSI | 5,113 | 2026-09-08 | 腾讯音乐（中国）| Java 服务端 | 可 |
| Vanna | https://github.com/vanna-ai/vanna | MIT | 23,814 | **已归档（2026-03-29）**，最后推送 2026-02-02（v2.0.2）| Vanna.AI（美）| Python 库 | 可（已停更）|
| **短视频** | | | | | | | |
| MoneyPrinterTurbo | https://github.com/harry0703/MoneyPrinterTurbo | MIT | 128,531 | 2026-10-04 | 个人开发者 harry0703（中国）| Streamlit WebUI、API、CLI、"AI Agent" 四种用法；Docker / Windows 一键包 | 可，GPU 非必需 |
| NarratoAI | https://github.com/linyqh/NarratoAI | MIT | 11,286 | 2026-09-17（v0.8.6 于 2026-07-16 发布）| 个人开发者 linyqh（中国）| Streamlit WebUI（127.0.0.1:8501）、Docker、Win/macOS 整合包 | 可 |
| Open-Sora | https://github.com/hpcaitech/Open-Sora | Apache-2.0 | 29,853 | 2026-04-09 | 潞晨科技 HPC-AI Tech（中国/新加坡）| 视频生成模型与训练/推理管线（非 Agent）| 可（需 GPU）|
| **小游戏/互动页面** | | | | | | | |
| bolt.diy | https://github.com/stackblitz-labs/bolt.diy | MIT（但依赖 StackBlitz WebContainers API，商用需其授权）| 未核实（GitHub Search API 两次查询均未返回该仓库）| 第三方称 **2026-06-16 归档**（未经 API 核实）| 社区（Cole Medin 发起，oTTomator）| Node.js / Docker / Electron 桌面 | 可 |
| Dyad | https://github.com/dyad-sh/dyad | Apache-2.0；`src/pro/` 为 FSL-1.1-Apache-2.0（fair-source）；API: NOASSERTION | 21,660 | 2026-10-05 | Dyad（美）| 本地桌面应用（Mac/Windows）| 可，BYOK |
| Open WebUI（Artifacts）| https://github.com/open-webui/open-webui | BSD-3-Clause + 品牌条款（30 天内 ≥50 终端用户的部署不得移除 Open WebUI 品牌）；API: NOASSERTION | 153,976 | 2026-10-05 | Open WebUI（美）| 自托管 Web | 可 |
| gpt-engineer | https://github.com/AntonOsika/gpt-engineer | MIT | 55,055 | **已归档**，最后推送 2025-05-14 | Anton Osika（瑞典，Lovable 创始人）| CLI | 可（已停更）|

来源：[Presenton README](https://raw.githubusercontent.com/presenton/presenton/main/README.md)；[PPTAgent README](https://raw.githubusercontent.com/icip-cas/PPTAgent/main/README.md)；[ppt-master README](https://raw.githubusercontent.com/hugohe3/ppt-master/main/README.md)；[anthropics/skills README](https://raw.githubusercontent.com/anthropics/skills/main/README.md)；[OfficeCLI README](https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/README.md)；[genoffice README](https://raw.githubusercontent.com/genspark-ai/genoffice/main/README.md)；[OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)；[HermesOffice 仓库](https://github.com/criptogus/HermesOffice)；[PandasAI LICENSE](https://raw.githubusercontent.com/sinaptik-ai/pandas-ai/main/LICENSE)；[DB-GPT README](https://raw.githubusercontent.com/eosphoros-ai/DB-GPT/main/README.md)；[Chat2DB README_CN](https://raw.githubusercontent.com/OtterMind/Chat2DB/main/README_CN.md)；[WrenAI LICENSE](https://raw.githubusercontent.com/Canner/WrenAI/main/LICENSE)；[WrenAI README](https://raw.githubusercontent.com/Canner/WrenAI/main/README.md)；[SuperSonic LICENSE](https://raw.githubusercontent.com/tencentmusic/supersonic/master/LICENSE)；[Vanna 归档说明（dev.to）](https://dev.to/miya_ng/vanna-aivanna-has-been-archived-since-march-whats-actually-frozen-what-isnt-and-your-four-479g)；[MoneyPrinterTurbo README](https://raw.githubusercontent.com/harry0703/MoneyPrinterTurbo/main/README.md)；[NarratoAI README](https://raw.githubusercontent.com/linyqh/NarratoAI/main/README.md)；[bolt.diy README](https://raw.githubusercontent.com/stackblitz-labs/bolt.diy/main/README.md)；[bolt.diy 归档（selfhostedworld 等搜索摘要）](https://selfhostedworld.com/software/bolt-diy)；[Dyad README](https://raw.githubusercontent.com/dyad-sh/dyad/main/README.md)；[Dyad LICENSE](https://raw.githubusercontent.com/dyad-sh/dyad/main/LICENSE)；[Open WebUI LICENSE](https://raw.githubusercontent.com/open-webui/open-webui/main/LICENSE)。

### Inferences
- "开源"在这批项目里需逐一甄别：Suna（ELv2）、OpenWorkBuddy（PolyForm NC）、Chat2DB 5.3+、Anthropic 文档类 skills、AutoGPT 平台目录、SuperSonic 附加条款均不是 OSI 许可证，若灵策智算以"复用/二次分发"为目的，应优先考虑 DeerFlow（MIT）、Agent Zero（MIT）、Eigent/OWL（Apache-2.0）、OfficeCLI/genoffice/Presenton（Apache-2.0）、ppt-master/PPTAgent（MIT）。
- 2025 年"Manus 复刻"项目中，只有背靠公司/高校或转型为 Harness 的项目仍在高频迭代；个人维护型（ai-manus、agenticSeek）虽活跃但规模小。
- 仓库 star 数在 2026 年出现明显通胀（OpenClaw 39 万、Hermes 25 万、MoneyPrinterTurbo 12.8 万），star 已不适合单独作为成熟度指标，需结合 pushed_at 与 issue 关闭情况。

### Gaps
- OWL 的 LICENSE 文件未能直接核实（raw 路径 404，API 未返回许可证字段），仅有 README 与百科称 Apache-2.0。
- SlideSpeak "开源版"：未在 GitHub 搜索中找到可核实的开源仓库，无法收录。
- openpyxl 的仓库托管在 Heptapod（非 GitHub），本次未核实其 star/活跃度。
- bolt.diy 的 star 数与归档日期未能通过 GitHub API 核实（两次查询未返回该仓库），仅有第三方搜索摘要。
- HermesOffice 的许可证未核实。
- 灵策智算自身无任何公开资料可供交叉验证。

---

## 关键问题 2：能否从一句话自主规划多步并最终产出可下载的 PPT/Word/Excel？用什么机制？

### Takeaway
真正做到"一句话 → 自主拆解 → 产出可下载 Office 文件"的开源项目，在 2026 年主要靠三种机制：（1）Harness 内置"Skills + 沙箱代码执行"（DeerFlow 2.0、Hermes/OpenClaw 生态 + OfficeCLI/ppt-master 这类 skill）；（2）产品内置专用文件工具（JoyAgent 的报告/PPT 工具、II-Agent 的 PDF/Excel/Word/PPT 处理、Agent Zero 的 LibreOffice 集成、OpenWorkBuddy 的 pptx/docx/xlsx/html 产出）；（3）单一垂直生成器（Presenton、PPTAgent、ppt-master 产 PPTX；GPT Researcher 产 DOCX/PDF 报告；MoneyPrinterTurbo/NarratoAI 产视频）。框架类（CrewAI、smolagents、MetaGPT、OpenManus）需要开发者自行组装工具链才能交付文件。

### Cited Findings
- DeerFlow 2.0 自述为"an open-source super agent harness that orchestrates sub-agents, memory, and sandboxes to do almost anything — powered by extensible skills"；可产出 reports、slide decks、dashboards、images、videos、web pages、Excel/Word 文件与 podcasts；产物通过 Markdown 定义的 skills 与沙箱代码执行生成，文件落在 `/mnt/user-data/outputs`；支持斜杠激活 skill（如 `/data-analysis analyze file.csv`）。— [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- DeerFlow 2.0 的 Lead agent 负责规划并并行派生子 Agent（隔离上下文、工具与终止条件），"handles tasks that take minutes to hours"。— [MarkTechPost](https://www.marktechpost.com/2026/03/09/bytedance-releases-deerflow-2-0-an-open-source-superagent-harness-that-orchestrates-sub-agents-memory-and-sandboxes-to-do-complex-tasks/)
- JoyAgent-JDGenie 自称"业界首个开源的端到端产品级通用智能体"，内置报告生成、PPT 生成、数据分析、深度搜索；"Multiple file delivery styles: html, ppt, markdown"；采用 plan-execute 与 ReAct 两种子 Agent 模式，工具含代码解释器、报告/PPT 工具、搜索、文件工具；GAIA 验证集 75.15%、测试集 65.12%。— [JoyAgent README](https://raw.githubusercontent.com/jd-opensource/joyagent-jdgenie/data_agent/README.md)
- II-Agent（已 out of beta）列出"PDF extraction/creation, Excel formulas, Word editing, PowerPoint manipulation"，另有"Plan Mode"、"Live Editing for websites and slides"、代码解释器。— [II-Agent README](https://raw.githubusercontent.com/Intelligent-Internet/ii-agent/main/README.md)
- Agent Zero 提供"a Dockerized Linux desktop, a browser with DOM annotation, live document cowork"，文件生成通过 Markdown 编辑器与 LibreOffice（Writer、Calc、Impress）集成；"Every agent can create subordinate agents to break down work"。— [Agent Zero README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- Kortix（Suna）宣称 Agent 返回"finished work — reports, decks, code, replies, deployed changes"，每个会话在隔离沙箱的独立分支上运行。— [Kortix README](https://raw.githubusercontent.com/kortix-ai/suna/main/README.md)
- OpenWorkBuddy 自述"a local-first AI office agent that hands you files, not chat logs"，一句需求产出真实 PPTX/DOCX/XLSX/HTML；流程为"Plans tasks, executes steps, verifies outputs"。— [OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)
- OpenManus 仅提供 CLI；工具为 Browser Use 浏览器自动化、Python 代码执行、文件操作、网页抓取，另有需额外依赖的 DataAnalysis Agent（"data analysis and data visualization tasks"）；README 未提 Ollama 与 Web UI。— [OpenManus README](https://raw.githubusercontent.com/FoundationAgents/OpenManus/main/README.md)
- OWL 工具包包含浏览器自动化、代码执行、Word/Excel/PDF/PowerPoint 文档解析（parsing，非生成）与多引擎搜索；GAIA 平均分 69.09，论文被 NeurIPS 2025 接收。— [OWL README](https://raw.githubusercontent.com/camel-ai/owl/main/README.md)
- Eigent（基于 CAMEL/OWL 的桌面版）仅在用例中提到 "Word summary"，README 未系统列出文件产出格式；功能含 Browser & Terminal Toolkits、MCP 集成、Skill 集成、定时工作流。— [Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- MagenticLite 能力为浏览器自动化、网页研究、本地文件系统管理、表单填写；README 未提代码执行与文档/PPT 生成。— [Magentic-UI README](https://raw.githubusercontent.com/microsoft/magentic-ui/main/README.md)
- Hermes Agent README 未明确提及 PPT/Word/Excel 生成，但其 Skills 遵循 agentskills.io 开放标准；社区已有"Hermes Agent 自动化办公 Skills 合集（Google Workspace、邮件、PPT、Notion）"与"Hermes-Easy-Office-Suite（单 API 搞定 Word/Excel/PDF/PPT）"。— [Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；[博客园合集](https://www.cnblogs.com/qiniushanghai/p/19893019)；[Hermes-Easy-Office-Suite](https://github.com/shynloc/Hermes-Easy-Office-Suite)
- OfficeCLI 是"purpose-built for AI agents to read, edit, and automate Word, Excel, and PowerPoint files"的 .NET 单二进制，内置 MCP server，350+ Excel 函数写入即求值、原生透视表、`{{key}}` 模板合并、HTML/PNG 渲染预览、结构化错误码支持自纠错；可自动为 Claude Code/Cursor/VS Code 配置。— [OfficeCLI README](https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/README.md)
- genoffice（Genspark 开源）提供 Docs/Sheets/Slides/PDF 编辑器 + `genoffice` CLI + Agent skill，"so Claude Code, Codex and Cursor can create and edit real .docx/.xlsx/.pptx files locally"，并支持 MCP server、字节级保真保存、Word 修订与一键回滚。— [genoffice README](https://raw.githubusercontent.com/genspark-ai/genoffice/main/README.md)
- ppt-master 以 Skill 形式在 Claude Code/Cursor/Codex/Gemini CLI 中运行：AI 分析来源内容→生成 SVG 版式→导出原生 `.pptx`（原生形状、数据图表、母版、动画、旁白），输入可为 PDF/DOCX/网页/图片/主题。— [ppt-master README](https://raw.githubusercontent.com/hugohe3/ppt-master/main/README.md)
- Presenton："Create presentations from a prompt, an uploaded document, or your own PowerPoint design"，输出"Fully editable PPTX export"与 PDF，REST API `/api/v1/ppt/presentation/generate`，内置 MCP server（Electron 桌面版不含）。— [Presenton README](https://raw.githubusercontent.com/presenton/presenton/main/README.md)
- PPTAgent 采用"two-stage, edit-based approach"：从参考 PPT 抽取版式/schema，再迭代生成编辑动作合成新页；PPTEval 从 Content/Design/Coherence 三维评估；EMNLP 2025 接收，后续 DeepPresenter 被 ACL 2026 接收；2026-01 支持 PPTX 导出与离线模式，2026-09 发布面向 Claude Code/Codex/OpenCode 的 Skill。— [PPTAgent README](https://raw.githubusercontent.com/icip-cas/PPTAgent/main/README.md)
- GPT Researcher 可"Export reports to PDF, Word, and other formats"，支持对本地 PDF/CSV/Excel/Markdown/PPT/Word 文档做研究（`DOC_PATH`），Deep Research 约 5 分钟/次、约 $0.4/次（o3-mini）。— [GPT Researcher README](https://raw.githubusercontent.com/assafelovic/gpt-researcher/master/README.md)
- Anthropic skills 仓库明确包含 DOCX、PDF、PPTX、XLSX 文档类 skill，格式为含 `name`/`description` frontmatter 的 SKILL.md；但文档类 skill 为"source-available, not open source"。— [anthropics/skills README](https://raw.githubusercontent.com/anthropics/skills/main/README.md)
- Markdown→PPT 路线：社区已有 Claude Code skill "md-slides"（MD→Slides，含 AI 配图）、Marp Slide Creator skill（7 套主题）、Slidev skill；有评测称 Marp 因 CLI 简单、文本输入、错误信息清晰而在 LLM 集成项得分最高（24/25）；典型流程为 Markdown 写稿→Claude 结构化与讲稿→Marp CLI 渲染 PDF/HTML/PPTX。— [md-slides](https://github.com/zl190/md-slides)；[Marp Slide Creator](https://mcpmarket.com/tools/skills/marp-slide-creator-2)；[termdock 评测](https://www.termdock.com/en/blog/terminal-presentations-markdown-to-slides)
- DB-GPT 定位"open-source agentic AI data assistant"：Text-to-SQL、Generative BI（图表、仪表盘、HTML 报告）、AWEL 编排、多 Agent 分步执行、沙箱执行、Skills 框架；README 未提 Excel 导出。— [DB-GPT README](https://raw.githubusercontent.com/eosphoros-ai/DB-GPT/main/README.md)
- MetaGPT Data Interpreter 能"analyze stocks, imitate websites, and train models"，在 ML、数学推理与开放任务上取得 SOTA。— [KDnuggets](https://www.kdnuggets.com/metagpt-data-interpreter-open-source-llm-based-data-solutions)
- PandasAI 定位"Chat with your database or your datalake (SQL, CSV, parquet)"，但 v3.0.0 存在 SQL 注入漏洞 CVE-2026-30273（CVSS 7.3），且 2026-09 时主分支已约 10 个月无更新。— [PandasAI 仓库](https://github.com/sinaptik-ai/pandas-ai)；[OpenCVE](https://app.opencve.io/cve/CVE-2026-30273)；[note.com 分析](https://note.com/snake_dragon/n/n539bc31adfe6?hl=en)
- Chat2DB 社区版 AI 能力为"integrate custom AI models to generate, explain and optimize SQL"及仪表盘、数据可视化、导入导出；商业 Pro/企业版才有官方 AI 服务、账号、云同步与团队协作。— [Chat2DB README_CN](https://raw.githubusercontent.com/OtterMind/Chat2DB/main/README_CN.md)
- Wren AI 2026 年转为 open-core："Core engine, SDK, and skills under Apache-2.0. It runs without us."，面向 Claude Code/Cursor/MCP 客户端提供治理语义层（MDL）与 Agent skills，`pip install wrenai`。— [WrenAI README](https://raw.githubusercontent.com/Canner/WrenAI/main/README.md)
- MoneyPrinterTurbo："只需提供视频主题或关键词，即可自动生成视频脚本、匹配素材、生成字幕和背景音乐"，提供 AI Agent、WebUI、API、CLI 四种用法；支持 Kimi/Moonshot、通义千问、火山方舟、DeepSeek、Ollama 等。— [MoneyPrinterTurbo README](https://raw.githubusercontent.com/harry0703/MoneyPrinterTurbo/main/README.md)
- NarratoAI："一站式 AI 影视解说+自动化剪辑工具"，LLM 生成解说脚本→自动剪辑→配音→字幕；支持 Qwen2-VL、DeepSeek、SiliconFlow、火山引擎。— [NarratoAI README](https://raw.githubusercontent.com/linyqh/NarratoAI/main/README.md)
- bolt.diy 生成基于 NodeJS 的 Web 应用（含 Expo/React Native），19+ 模型供应商含 Ollama/LM Studio；Dyad 自述"like Lovable, v0, or Bolt, but running right on your machine"。— [bolt.diy README](https://raw.githubusercontent.com/stackblitz-labs/bolt.diy/main/README.md)；[Dyad README](https://raw.githubusercontent.com/dyad-sh/dyad/main/README.md)
- Open WebUI Artifacts 为类 Claude Artifacts 的交互面板，支持 HTML/CSS/JS、ThreeJS、D3.js 实时渲染；Python 代码执行可用浏览器内 Pyodide 或服务端 Jupyter，官方文档称二者"now legacy engines, kept for zero-setup, in-chat use"。— [Open WebUI Code Execution 文档](https://docs.openwebui.com/features/chat-conversations/chat-features/code-execution/)
- CrewAI 为"open-source Python framework with high-level abstractions and low-level APIs for building production-ready multi-agent workflows"（Crews + Flows），文档产出以 Markdown 文件与结构化 JSON 为主。— [CrewAI README](https://raw.githubusercontent.com/crewAIInc/crewAI/main/README.md)
- AutoGPT 平台提供 AutoPilot（"Describe the job in plain English and turn the conversation into a working agent"）、可视化 Build 画布、Marketplace、45+ 应用连接，Agent 可按需/定时/事件触发执行。— [AutoGPT README](https://raw.githubusercontent.com/Significant-Gravitas/AutoGPT/master/README.md)

### Inferences
- 对"季度销售表清洗→环比同比→周报→PPT"这类链路，DeerFlow 2.0（skills + 沙箱 + `/data-analysis`）、Agent Zero（LibreOffice）、II-Agent、JoyAgent（报告/PPT 工具 + DataAgent）是现成度最高的开源组合；若自研，"通用 Harness（OpenClaw/Hermes/nanobot）+ OfficeCLI/ppt-master/genoffice skill"是 2026 年社区的主流拼装方式。
- PPT 质量路线分化：Presenton/presentation-ai 走 HTML/Tailwind 模板渲染，ppt-master/PPTAgent/OfficeCLI 走原生 PPTX 对象模型，后者更贴近"可编辑交付物"。

### Gaps
- 未能获取 DeerFlow 内置 skills 的完整清单（README 只给类别），无法确认其 Excel/Word 产出是内置 skill 还是依赖代码执行时临时写 python-pptx/openpyxl。
- Eigent 与 Hermes Agent 的文档类产出能力只能从第三方/社区 skill 推断，官方 README 无明确列表。
- 没有找到任何针对"一句话→PPT/Word/Excel 交付"的横向基准测试（GAIA 等基准不衡量文件产出质量）。

---

## 关键问题 3：进度可视化、步骤确认/审批、历史回溯

### Takeaway
"关键动作需确认 + 进度可视 + 历史可追溯"在 DeerFlow（Plan mode 逐步审批、Task Notes、Skills 工具栏、快照）、Agent Zero（随时介入 + Time Travel 快照回退）、MagenticLite（关键动作前停下确认、可随时接管）、Kortix（变更需人工审批、合并默认拒绝）、Eigent（遇不确定自动暂停请求批准）、Hermes/OpenClaw（命令审批、配对审批）中都有对应实现；中文产品化项目 JoyAgent 强调全链路流式输出但 README 未见审批机制。

### Cited Findings
- DeerFlow：Plan mode "for explicit step approval before execution"；Session Goals、Manual Context Compaction（可审阅/编辑）、Current Task Notes 跟踪进度；"Skills used" 工具栏显示每次响应激活的 skill；skill 历史存入快照可在侧栏查看。— [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- MagenticLite："Keeps you in the loop and in control. Steer, approve, or take over at any point"；"stops and checks in before taking critical actions"。— [Magentic-UI README](https://raw.githubusercontent.com/microsoft/magentic-ui/main/README.md)
- Agent Zero："You watch every action, and you can intervene at any moment"；"Time Travel" 对工作区历史做快照，可 diff、检查与回退。— [Agent Zero README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- Kortix：Agent 提出变更需人工批准，"Merge is default-deny for agents: you review the diff"；配置/Agent/skills/记忆全部以 `kortix.yaml` 存于用户自有 git 仓库（天然可追溯）。— [Kortix README](https://raw.githubusercontent.com/kortix-ai/suna/main/README.md)
- Eigent 的 Human-in-the-Loop 系统"automatically pauses execution when agents encounter uncertainty, ethical concerns, or critical decisions, providing contextual prompts for approval and allowing real-time modification of agent plans"。— [brightcoding 评测](https://www.blog.brightcoding.dev/2026/04/07/eigent-your-local-multi-agent-automation-powerhouse)（第三方）
- Hermes Agent 安全特性含"command approval, DM pairing, container isolation"；记忆支持 FTS5 会话搜索 + LLM 摘要做跨会话回溯。— [Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- OpenClaw 需通过 `openclaw pairing approve` 批准配对请求；提供沙箱安全指南。— [OpenClaw README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- ai-manus：Plan-and-execute 流程 + 原生结构化输出工具；会话历史由 MongoDB/Redis 管理并支持后台任务；侧栏 Library 聚合所有会话的附件与产物（类型筛选、搜索、收藏、预览）。— [ai-manus README](https://raw.githubusercontent.com/Simpleyyt/ai-manus/main/README.md)
- JoyAgent："Full-chain streaming output"（全链路流式输出）展示执行过程；README 未提审批。— [JoyAgent README](https://raw.githubusercontent.com/jd-opensource/joyagent-jdgenie/data_agent/README.md)
- II-Agent 提供 "Plan Mode for visual project planning"。— [II-Agent README](https://raw.githubusercontent.com/Intelligent-Internet/ii-agent/main/README.md)
- OpenWorkBuddy 流程为规划→执行→校验，另有基于节点的 Canvas 可视化工作流编辑器。— [OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)
- AutoGPT 平台 Agents Dashboard 可监控所有 Agent 的运行、成本与"required actions"。— [AutoGPT README](https://raw.githubusercontent.com/Significant-Gravitas/AutoGPT/master/README.md)
- OpenManus 在中文社区评价中以"用户可实时查看 AI 的任务执行逻辑和进度"著称，但 README 未描述审批机制。— [aiho.net 2026 评测](https://aiho.net/agent/general/manus.html)

### Inferences
- "逐步审批"与"随时介入"是两种不同范式：DeerFlow/Kortix 偏前者（先批计划/后批合并），Agent Zero/MagenticLite 偏后者（实时观察与接管）；灵策智算描述的"关键动作需确认"更接近 MagenticLite/Hermes 的命令级审批。
- 只有 Agent Zero 明确提供"工作区级"回退（Time Travel），其余项目的"历史可追溯"多为会话/产物记录。

### Gaps
- Eigent 的 HITL 细节仅来自第三方博客，官方 README 节选未提及。
- OWL、OpenManus、Cooragent、CrewAI、smolagents 的 README 未描述审批/回溯功能（框架层需自行实现）。

---

## 关键问题 4：Skills / 插件 / 工具扩展机制与市场

### Takeaway
2026 年 Skills 机制已趋同于 "SKILL.md（Markdown + frontmatter）" 开放格式（Anthropic skills、agentskills.io 标准、DeerFlow、Hermes、nanobot、OpenWorkBuddy 均采用），并出现多个分发市场：OpenClaw 的 ClawHub、Hermes 的 agentskills.io Skills Hub、Agent Zero 的 Plugin Hub（100+ 插件）、DeerFlow 的 Community 标签页（导入 `.skill` 包）、AutoGPT Marketplace、Cooragent 社区、Open WebUI 的 Tools/Functions 社区；MCP 几乎成为所有项目的工具接入标配。

### Cited Findings
- Anthropic skills：skill 为"a folder containing a `SKILL.md` file with YAML frontmatter and instructions"，可通过 Claude Code 插件市场、Claude.ai 付费计划与 API 使用；多数 Apache-2.0。— [anthropics/skills README](https://raw.githubusercontent.com/anthropics/skills/main/README.md)
- DeerFlow skills 为"structured capability modules — a Markdown file that defines a workflow"，含内置公共 skills、用户自定义 skills、托管集成包（如 Lark/Feishu CLI）、Community 标签页导入 `.skill` 归档、渐进式/延迟加载、斜杠激活；工具含 bash、文件操作、web fetch 与 MCP 服务器。— [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- Hermes Agent 兼容 agentskills.io 开放标准，"autonomous skill creation after complex tasks"、"skills self-improve during use"，通过 Skills Hub（agentskills.io）获取；自动写入 `~/.hermes/skills/`。— [Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；[hermes-agent.ai](https://hermes-agent.ai/)；社区目录 [awesome-hermes-agent](https://github.com/0xNyk/awesome-hermes-agent)
- OpenClaw 以 ClawHub 作为插件分发平台；"Models and agent harnesses...are plugins you can swap without changing anything else"。— [OpenClaw README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- Agent Zero："100+ community plugins" via Plugin Hub，支持自定义 prompts/tools 与 MCP。— [Agent Zero README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- nanobot：Skills 系统（可复用指令）、MCP 集成、子 Agent 委派、模型路由与回退。— [nanobot README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- Kortix："3,000+ apps in a click — plus MCP, OpenAPI, GraphQL and raw HTTP"。— [Kortix README](https://raw.githubusercontent.com/kortix-ai/suna/main/README.md)
- AutoGPT 平台有 Marketplace（社区预置 Agent）与 Build 可视化积木画布，连接 45+ 应用。— [AutoGPT README](https://raw.githubusercontent.com/Significant-Gravitas/AutoGPT/master/README.md)
- Cooragent 采用 "Agent Factory"：描述需求即生成 Agent，可"publish your agents to the community"，工作流存于本地 `store/workflow`，支持 MCP。— [Cooragent README](https://raw.githubusercontent.com/LeapLabTHU/cooragent/main/README.md)
- JoyAgent："Sub-agents and tools are pluggable"，支持自定义 MCP 工具。— [JoyAgent README](https://raw.githubusercontent.com/jd-opensource/joyagent-jdgenie/data_agent/README.md)
- Eigent 列有 "MCP Integration" 与 "Skill Integration"。— [Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- OpenWorkBuddy："Add capabilities via Markdown files without code changes or restarts"，支持 MCP，并能驱动 Claude Code/Codex CLI。— [OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)；[仓库描述](https://github.com/CatCatUncle/openworkbuddy)
- II-Agent：Custom skills，集成 Gmail、Slack、GitHub、Notion、Google Calendar、Discord、Dropbox、Canva。— [II-Agent README](https://raw.githubusercontent.com/Intelligent-Internet/ii-agent/main/README.md)
- DB-GPT：Skills 框架（"Reusable domain-specific workflow packages"）；Wren AI：Agent skills（onboarding、context enrichment、GenBI app building）。— [DB-GPT README](https://raw.githubusercontent.com/eosphoros-ai/DB-GPT/main/README.md)；[WrenAI README](https://raw.githubusercontent.com/Canner/WrenAI/main/README.md)
- PPTAgent、ppt-master、OfficeCLI、genoffice 本身即以 Skill/MCP 形式供 Claude Code/Codex/OpenCode/Cursor 调用；OfficeCLI 可自动检测并配置兼容 AI 编辑器。— [PPTAgent README](https://raw.githubusercontent.com/icip-cas/PPTAgent/main/README.md)；[OfficeCLI README](https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/README.md)
- goose 以 MCP 扩展为核心（原 goose-plugins 仓库已归档，注明"replaced by the MCP protocol"）。— [goose-plugins 仓库](https://github.com/block/goose-plugins)
- OpenManus 提供 MCP 模式（`run_mcp.py`）；OWL 支持 MCP（"a universal protocol layer"）。— [OpenManus README](https://raw.githubusercontent.com/FoundationAgents/OpenManus/main/README.md)；[OWL README](https://raw.githubusercontent.com/camel-ai/owl/main/README.md)

### Inferences
- 灵策智算的"100+ 内置 Skills + Skills 市场"在开源侧最接近的是 Agent Zero Plugin Hub（100+）、DeerFlow Community skills、Hermes/agentskills.io 与 OpenClaw ClawHub；若采用 SKILL.md 开放格式，可直接复用 Anthropic（Apache-2.0 部分）与社区 skills。
- 文档类 skill 的许可证是坑：Anthropic 的 docx/pptx/xlsx skill 为 source-available，不能直接当开源组件分发；替代为 OfficeCLI/genoffice（Apache-2.0）或 ppt-master（MIT）。

### Gaps
- 各市场的 skill 数量（ClawHub、agentskills.io、DeerFlow Community）未获得官方统计数字。

---

## 关键问题 5：数据是否留在本地（纯本地 vs 依赖云沙箱）

### Takeaway
纯本地可行的代表：agenticSeek（无 API）、Eigent（本地部署 + 本地模型）、OpenClaw（状态/记忆/凭证在本机，仅每日版本检查）、nanobot、Agent Zero（Docker 本机）、ai-manus（仅需 LLM 服务）、DeerFlow（Local/Docker 沙箱）、OpenWorkBuddy（默认 127.0.0.1）、MagenticLite（本地 QEMU 沙箱）；依赖或推荐云组件的：Kortix（托管 git/Pipedream 连接器）、Hermes（本地为主但浏览器用 Browser Use 云浏览器）、II-Agent（本地 Docker Compose 但需 MinIO 等服务）。LLM 调用本身仍需外部 API，除非配 Ollama/vLLM。

### Cited Findings
- OpenClaw："State, memory, and credentials live on your hardware"；"By default OpenClaw itself phones home for nothing but a daily version check; anonymous feature statistics are opt-in"。— [OpenClaw README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- Eigent："Local Deployment (Recommended)"，支持 vLLM/Ollama/LM Studio，"Complete isolation from cloud services"，"Your files, credentials, and context stay under your control"。— [Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- agenticSeek："Fully Local Manus AI. No APIs, No $200 monthly bills."— [agenticSeek 仓库](https://github.com/Fosowl/agenticSeek)
- DeerFlow 沙箱模式：Local Execution（直接宿主机）、Docker、Kubernetes provisioner；云沙箱可选 E2B、Tenki、BoxLite。— [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- Hermes：7 种终端后端含 local/Docker/SSH；但浏览器自动化走 Tool Gateway 的"cloud browser (Browser Use)"。— [Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- MagenticLite 浏览器会话与代码执行由 Quicksand（开源 QEMU 运行时）沙箱化。— [Microsoft Research 博客](https://www.microsoft.com/en-us/research/blog/magenticlite-magenticbrain-fara1-5-an-agentic-experience-optimized-for-small-models/)
- nanobot 支持 Ollama、vLLM 与 OpenAI 兼容服务器。— [nanobot README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- ai-manus："Each task is allocated a separate sandbox that runs in a local Docker environment"；"Minimal deployment requires only an LLM service"。— [ai-manus README](https://raw.githubusercontent.com/Simpleyyt/ai-manus/main/README.md)
- Kortix 完整功能需"managed git, GitHub access, and Pipedream connectors"。— [Kortix README](https://raw.githubusercontent.com/kortix-ai/suna/main/README.md)
- OpenWorkBuddy：数据留在用户机器，默认仅监听 `127.0.0.1`；支持 Ollama。— [OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)
- genoffice："local by design"，仅 AI 调用离开本机，支持本地 OpenAI 兼容端点；HermesOffice 自述"100% local"。— [genoffice README](https://raw.githubusercontent.com/genspark-ai/genoffice/main/README.md)；[HermesOffice 仓库](https://github.com/criptogus/HermesOffice)
- Presenton 支持 Ollama/LM Studio，"No SaaS lock-in · No forced subscriptions · Full control over models and data"；ppt-master "Apart from AI model communication, the entire pipeline runs on your machine"。— [Presenton README](https://raw.githubusercontent.com/presenton/presenton/main/README.md)；[ppt-master README](https://raw.githubusercontent.com/hugohe3/ppt-master/main/README.md)
- DB-GPT 支持 vLLM、llama.cpp 本地 GPU 部署；Chat2DB 社区版"runs entirely on your machine"。— [DB-GPT README](https://raw.githubusercontent.com/eosphoros-ai/DB-GPT/main/README.md)；[Chat2DB README_CN](https://raw.githubusercontent.com/OtterMind/Chat2DB/main/README_CN.md)
- MoneyPrinterTurbo "GPU 不是必需项"，Edge TTS 免费无需 Key；支持 Ollama。— [MoneyPrinterTurbo README](https://raw.githubusercontent.com/harry0703/MoneyPrinterTurbo/main/README.md)
- Dyad："Bring your own keys — no vendor lock-in"，本地桌面运行；bolt.diy 支持 Ollama/LM Studio。— [Dyad README](https://raw.githubusercontent.com/dyad-sh/dyad/main/README.md)；[bolt.diy README](https://raw.githubusercontent.com/stackblitz-labs/bolt.diy/main/README.md)

### Inferences
- "数据不出本机"在开源侧可通过 Eigent/OpenClaw/nanobot/Agent Zero + Ollama 实现，但要兼顾 Office 产出质量仍需较强模型；本地小模型路线上只有微软 MagenticLite（MagenticBrain + Fara1.5 开放权重）给出了针对小模型优化的 Harness。

### Gaps
- OWL/OpenManus 对本地模型（Ollama）的支持在 README 中未明确，需看配置文档。

---

## 关键问题 6：中文支持与中文社区活跃度

### Takeaway
中文"原生"或"强支持"的项目集中在中国团队：DeerFlow（README_zh + 微信/企微/飞书/钉钉/QQ 渠道）、JoyAgent（中文产品）、OpenManus（中文 README，MetaGPT 社区）、OWL/Eigent（中文 README、微信群、中文 Gradio 界面）、nanobot（简繁中文 README、微信渠道）、ai-manus（中英）、MoneyPrinterTurbo/NarratoAI（中文为主）、DB-GPT（dbgpt.cn）、Chat2DB、OfficeCLI/AionUi、ppt-master、PPTAgent、genoffice（简繁中文）、OpenWorkBuddy（中文为主，飞书/微信/钉钉）；海外项目中 Hermes 有 README.zh-CN 与中文社区入口，OpenClaw README 未列微信/飞书/QQ 渠道。

### Cited Findings
- DeerFlow：README 顶部提供 [中文](./README_zh.md)；IM 渠道含 Telegram、Slack、Feishu/Lark、WeChat、WeCom、QQ、DingTalk。— [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- OpenManus："includes Chinese README"；中文社区称其为"知名度最高、star 数最高的 Manus 开源替代"，由 MetaGPT 团队成员 3 小时内开发。— [OpenManus README](https://raw.githubusercontent.com/FoundationAgents/OpenManus/main/README.md)；[aiho.net](https://aiho.net/agent/general/manus.html)
- OWL："Active on Discord and WeChat, with Chinese documentation"，Gradio 界面有中文版。— [OWL README](https://raw.githubusercontent.com/camel-ai/owl/main/README.md)
- Eigent：提供 README_CN.md（简体中文）。— [Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- Hermes Agent：README.zh-CN.md；SegmentFault 有"Hermes Agent 中文社区：开源自托管 AI Agent 的中文首选入口"。— [Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；[SegmentFault](https://segmentfault.com/a/1190000047711188)
- OpenClaw README 未列出 WeChat/Feishu/QQ 渠道（列出 Discord、iMessage、Slack、Teams、Telegram、WhatsApp、Signal、Google Chat 及 20+）。— [OpenClaw README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- nanobot：简体/繁体中文 README；渠道含 Telegram、Discord、Slack、WeChat、Email、Mattermost、Linear。— [nanobot README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- ai-manus："Supports both Chinese and English"，官方站有完整中文文档。— [ai-manus README](https://raw.githubusercontent.com/Simpleyyt/ai-manus/main/README.md)
- JoyAgent：仓库描述"开源的端到端产品级通用智能体"；中文技术社区（掘金、博客园京东云）有大量部署与解读文章。— [JoyAgent 仓库](https://github.com/jd-opensource/joyagent-jdgenie)；[掘金沸点](https://juejin.cn/pin/7551254626881912842)；[博客园](https://www.cnblogs.com/Jcloud/p/19105433)
- MoneyPrinterTurbo 中文文档为主（有英/日译本），README 列出 Kimi K3 赞助至 2026-12；NarratoAI 官方文档在飞书 wiki。— [MoneyPrinterTurbo README](https://raw.githubusercontent.com/harry0703/MoneyPrinterTurbo/main/README.md)；[NarratoAI README](https://raw.githubusercontent.com/linyqh/NarratoAI/main/README.md)
- OfficeCLI 有 README_zh.md；腾讯云开发者社区、知乎等有专题介绍。— [OfficeCLI README](https://raw.githubusercontent.com/iOfficeAI/OfficeCLI/main/README.md)；[腾讯云开发者社区](https://developer.cloud.tencent.com/article/2690042)
- genoffice 文档含简体与繁体中文；ppt-master 文档含简体中文；PPTAgent 有中文文档。— [genoffice README](https://raw.githubusercontent.com/genspark-ai/genoffice/main/README.md)；[ppt-master README](https://raw.githubusercontent.com/hugohe3/ppt-master/main/README.md)；[PPTAgent README](https://raw.githubusercontent.com/icip-cas/PPTAgent/main/README.md)
- OpenWorkBuddy "Primarily Chinese"，集成飞书、微信、钉钉、Telegram，支持通义/智谱/Kimi/DeepSeek/豆包；在阮一峰周刊 issue 自荐。— [OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)；[ruanyf/weekly issue #11715](https://github.com/ruanyf/weekly/issues/11715)
- HermesOffice 有中文化分支 HermesOffice-cn。— [HermesOffice-cn](https://github.com/yangshun2005/HermesOffice-cn)
- Cooragent 提供英文与简体中文文档。— [Cooragent README](https://raw.githubusercontent.com/LeapLabTHU/cooragent/main/README.md)
- GPT Researcher 有 README-zh_CN.md。— [GPT Researcher README](https://raw.githubusercontent.com/assafelovic/gpt-researcher/master/README.md)
- Presenton、II-Agent、Dyad、Magentic-UI README 未提及中文支持。— 见各 README（上文链接）

### Inferences
- 若灵策智算面向国内企业，DeerFlow（字节）、JoyAgent（京东）、nanobot（港大）、OpenWorkBuddy、OfficeCLI/AionUi 是中文生态最贴合的参照；海外 Harness（OpenClaw/Hermes）需自行补微信/飞书/钉钉渠道。

### Gaps
- 无法量化各项目中文社区规模（微信群人数、国内镜像下载量等均无公开数据）。

---

## 关键问题 7：与灵策智算"直接交付结果"最接近的 3 个项目及差距

### Takeaway
综合"一句话→多步→产出 Office 文件、本地执行、动作确认、进度/历史、Skills 市场、多模型、定时任务、团队共享"八项，最接近的开源（OSI 许可证）项目是 **DeerFlow 2.0（MIT）**、**Agent Zero（MIT）** 与 **Eigent（Apache-2.0）**；功能画像与灵策智算几乎一一对应的 **OpenWorkBuddy** 因采用 PolyForm Noncommercial（非 OSI）且仅 259 star，只能作为"同类产品"而非"开源替代"参照；**JoyAgent-JDGenie** 是中文交付型产品但已 8 个月无提交。

### Cited Findings
- DeerFlow 2.0 覆盖：skills（含社区导入）、Plan mode 逐步审批、Task Notes/Skills 工具栏/快照、Local/Docker/K8s 沙箱、Scheduled Tasks（cron）、长期记忆、微信/飞书/钉钉/QQ 等渠道、产出 reports/slide decks/Excel/Word、Web UI + TUI + Docker；MIT；字节跳动维护；2026-10-05 仍在推送。— [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)；[DeerFlow 仓库](https://github.com/bytedance/deer-flow)
- Agent Zero 覆盖：Docker 化 Linux 桌面、LibreOffice（Writer/Calc/Impress）文档协作、Projects（按项目隔离文件/指令/密钥/记忆/模型预设）、Plugin Hub 100+ 插件、MCP、Scheduled operations、"intervene at any moment"、Time Travel 回退；MIT；2026-10-02 推送。— [Agent Zero README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)；[Agent Zero 仓库](https://github.com/agent0ai/agent-zero)
- Eigent 覆盖：Electron 桌面客户端、本地部署 + 本地模型、多 Agent 并行、Browser/Terminal 工具、MCP + Skill 集成、定时工作流、HITL 暂停审批（第三方描述）、README_CN；Apache-2.0；2026-10-02 推送。— [Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)；[brightcoding](https://www.blog.brightcoding.dev/2026/04/07/eigent-your-local-multi-agent-automation-powerhouse)
- OpenWorkBuddy 覆盖：本地优先、产出 PPTX/DOCX/XLSX/HTML、规划→执行→校验、Markdown skills、DeepSeek/Claude/OpenAI/Ollama/通义/智谱/Kimi、飞书/微信/钉钉/Telegram、Cron 定时与通知、Canvas 工作流；但许可证为 PolyForm Noncommercial 1.0.0（商用需付费，企业 30 天试用）。— [OpenWorkBuddy README](https://raw.githubusercontent.com/CatCatUncle/openworkbuddy/main/README.md)
- JoyAgent-JDGenie 覆盖：报告/PPT/数据分析/深搜、html/ppt/markdown 交付、Docker 一键部署、DataAgent 智能问数与诊断分析；Apache-2.0；但仓库最后推送 2026-02-12。— [JoyAgent README](https://raw.githubusercontent.com/jd-opensource/joyagent-jdgenie/data_agent/README.md)；[JoyAgent 仓库](https://github.com/jd-opensource/joyagent-jdgenie)
- Hermes Agent / OpenClaw 覆盖 Skills 市场、cron、多渠道、命令审批、多模型，但 README 不以 Office 文件交付为核心；社区用 OfficeCLI、Hermes-Easy-Office-Suite、HermesOffice、AionUi 补齐。— [Hermes README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；[OpenClaw README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)；[AionUi 仓库](https://github.com/iOfficeAI/AionUi)；[HermesOffice](https://github.com/criptogus/HermesOffice)
- Kortix 覆盖 cron/webhook 触发、Slack/Teams 团队协作、变更审批、3000+ 应用，但许可证为 ELv2 且依赖托管连接器。— [Kortix README](https://raw.githubusercontent.com/kortix-ai/suna/main/README.md)；[Suna LICENSE](https://raw.githubusercontent.com/kortix-ai/suna/main/LICENSE)

### Inferences
- 差距对照（灵策智算 vs 三者）：
  - DeerFlow：差在"桌面客户端形态"（它是 Web/TUI/服务端）、"团队共享"（README 未见团队/权限模型）、"关键动作确认"是计划级而非动作级；强在 skills 生态、沙箱分级与中文 IM 渠道。
  - Agent Zero：差在中文支持未明示、需 Docker 环境、Office 产出依赖 LibreOffice 而非原生 pptx/docx 库；强在 Time Travel 回退、Plugin Hub、随时介入。
  - Eigent：差在 README 未明确 PPT/Word/Excel 产出格式、HITL 细节仅见第三方、社区体量（1.5 万 star）小于前两者；强在桌面客户端 + 本地模型 + 定时任务，形态最像"本地桌面 Agent"。
- 若灵策智算要做"开源替代对比表"，建议把 OpenWorkBuddy 标为"最相似但 source-available"，把 Suna/Kortix 标为"已转 ELv2"，把 JoyAgent 标为"中文交付型、疑似停更"，避免把它们算作 OSI 开源替代。

### Gaps
- "团队共享"（多用户、权限、共享 skills）在 DeerFlow、Agent Zero、Eigent 的 README 中均无明确描述（Presenton 有多用户工作区与 SSO，但它只做 PPT）；需查各项目文档/路线图。
- 没有任何公开横评同时覆盖这三者在"Office 文件交付质量"上的表现。
- 灵策智算自身功能（100+ Skills、定时任务、团队共享）无公开资料，对照仅基于任务说明。
