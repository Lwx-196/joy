# 与"灵策智算 LynxceAI"最接近的开源"本地执行型 Agent / computer-use Agent"项目（截至 2026-10）

> 时间基准：2026-10-05。Star 数、许可证、最近推送时间除特别注明外均取自 GitHub API（经 GitHub MCP 工具于 2026-10-05 查询，引用链接为仓库主页）。
> 说明：在公开网络（WebSearch 中英文多次检索）**未能找到"灵策智算 / LynxceAI"的任何官网、新闻或产品页**；本笔记对照所用的产品画像完全来自任务说明，未做独立核实（见各节 Gaps）。

---

## 关键问题 1：有哪些值得关注的项目？基本信息（名称、GitHub、许可证、Star、活跃度、维护方/国家、技术栈、运行形态）

### Takeaway
2026 年这一赛道已形成三大谱系：(a) 以 **OpenClaw**（39 万 star）为中心的"守护进程 + IM 通道 + Skills"个人 Agent 谱系（含 Hermes Agent、nanobot、ZeroClaw、NanoClaw、PicoClaw、IronClaw、QwenPaw、LobsterAI、ClawX）；(b) 以 **Eigent、OpenWorker、Cherry Studio 2.0、Goose Desktop、MyAgents** 为代表的"桌面客户端 + 交付成果"谱系；(c) 以 **UFO³、Agent S3、UI-TARS Desktop、cua、Magentic-UI/MagenticLite** 为代表的 GUI/computer-use 研究型 Agent。编码 CLI（OpenCode、Codex、Gemini CLI、Qwen Code、Cline、Open Interpreter）虽然也在本机执行任务，但定位是开发者工具。

### Cited Findings

**A. OpenClaw 谱系（守护进程 + 多通道 + Skills）**

- **OpenClaw**（原 Clawdbot → Moltbot → OpenClaw）：GitHub [openclaw/openclaw](https://github.com/openclaw/openclaw)，MIT，**391,404 star / 82,270 fork**，TypeScript，仓库创建 2025-11-24，最近推送 2026-10-05，主页 openclaw.ai — [GitHub API](https://github.com/openclaw/openclaw)
  - 改名史："2026 年 1 月 27 日因商标问题从 Clawdbot 改名 Moltbot，1 月 30 日在一次严重安全漏洞后改名 OpenClaw"；Anthropic 法务认为 Clawdbot 与 Claude 过于相似 — [fast.io 迁移指南](https://fast.io/resources/moltbot-vs-openclaw-migration-guide/)；[Forbes](https://www.forbes.com/sites/ronschmelzer/2026/01/30/moltbot-molts-again-and-becomes-openclaw-pushback-and-concerns-grow/)
  - 维护方：2026-02-14 创始人 Peter Steinberger 宣布加入 OpenAI 并成立 OpenClaw Foundation；2026-07-08 宣布为美国 501(c)(3) 非营利基金会，有首批全职员工与董事会，赞助方含 OpenAI、NVIDIA、Microsoft、Red Hat 等 — [The New Stack](https://thenewstack.io/openclaw-foundation-nonprofit-status/)；[Forbes 2026-02-16](https://www.forbes.com/sites/ronschmelzer/2026/02/16/openai-hires-openclaw-creator-peter-steinberger-and-sets-up-foundation/)
  - 运行形态：README 自述 "an open-source AI assistant that runs on your own computer"；由 **Gateway 守护进程**（本地控制平面）+ CLI/TUI + Web Control UI + **原生伴侣 App（macOS、iOS、Android、Windows、Linux）** 组成；"no paid tier, hosted service, or token" — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- **ClawX**（OpenClaw 的图形桌面客户端）：[ValueCell-ai/ClawX](https://github.com/ValueCell-ai/ClawX)，MIT，7,613 star，TypeScript（Electron + React 19），创建 2026-02-05，推送 2026-09-30，中国站 clawx.com.cn；"turns CLI-based AI orchestration into a desktop experience without using the terminal"，支持 macOS 11+/Windows 10+/Linux，含 Agent 生命周期管理与多通道/多账号管理 — [GitHub API](https://github.com/ValueCell-ai/ClawX)；[搜索摘要](https://www.ahhhhfs.com/79522/)
- **LobsterAI（有道龙虾）**：[netease-youdao/LobsterAI](https://github.com/netease-youdao/LobsterAI)，MIT，6,083 star，TypeScript（Electron 43 + React 18），创建 2026-02-12，推送 2026-10-04，维护方网易有道（中国）。仓库描述："Open-source, desktop-grade AI agent that gets real work done — data analysis, slides, docs, video & web research. Built on OpenClaw; runs tools on your real desktop and takes commands from your phone via WeChat, Feishu, DingTalk & Telegram" — [GitHub API](https://github.com/netease-youdao/LobsterAI)
  - 2026-02-11 发布，定位"7×24 小时全场景个人助理 Agent"；"Cowork 是产品与会话层，OpenClaw 是底层运行时与网关" — [量子位](https://www.qbitai.com/2026/02/378453.html)；[README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)
- **QwenPaw（原 CoPaw，千问个人智能体工作台）**：[agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw)，Apache-2.0，**35,439 star**，TypeScript，创建 2026-02-24，推送 2026-09-30，维护方阿里云 AgentScope 团队（中国）；安装方式含 pip、脚本、Docker、阿里云 ECS 一键部署、AgentScope 平台（免费 24/7）、ModelScope Spaces、**Beta 桌面应用（Tauri，Windows/macOS）** — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)；[阿里云开发者社区](https://developer.aliyun.com/article/1750416)
- **Hermes Agent**：[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，MIT，**251,286 star / 53,946 fork**，Python，创建 2025-07-22，推送 2026-10-05，维护方 Nous Research（美国 AI 实验室）；"the self-improving AI agent built by Nous Research"；形态：CLI/TUI + Gateway 守护进程 + **Hermes Desktop** — [GitHub API](https://github.com/NousResearch/hermes-agent)；[README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
  - Hermes Desktop 于 2026-06-02 以 v0.15.2 公测版发布，原生 macOS 12+/Windows 10-11/Linux，Electron + React + Python 后端，"runs the same agent core as the CLI version—same memory, same skills, same configuration" — [Decrypt](https://decrypt.co/369952/hermes-ai-agent-official-app-terminal)；[aiweekly](https://aiweekly.co/alerts/nous-research-ships-hermes-desktop-across-mac-windows-linux)；[官方 Desktop 页](https://hermes-agent.nousresearch.com/desktop)
- **nanobot**：[HKUDS/nanobot](https://github.com/HKUDS/nanobot)，MIT，**48,790 star**，Python 3.11+，创建 2026-02-01，推送 2026-10-05，维护方香港大学数据科学实验室 HKUDS（中国香港）；"ultra-lightweight, self-hosted personal AI agent framework"，WebUI（127.0.0.1:8765）+ 终端 + 聊天应用三种入口 — [GitHub API](https://github.com/HKUDS/nanobot)；[README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)；"约 4,000 行代码实现 OpenClaw 90% 以上核心能力" — [x-cmd](https://www.x-cmd.com/install/nanobot/)
- **ZeroClaw**：[zeroclaw-labs/zeroclaw](https://github.com/zeroclaw-labs/zeroclaw)，Apache-2.0，32,932 star，Rust，创建 2026-02-13，推送 2026-10-04；由哈佛/MIT 学生与 Sundai.Club 社区创建（美国）；"3.4MB binary … boots in under 10 ms … <5MB RAM" — [GitHub API](https://github.com/zeroclaw-labs/zeroclaw)；[aimagicx](https://www.aimagicx.com/blog/openclaw-alternatives-comparison-2026)
- **NanoClaw**：[nanocoai/nanoclaw](https://github.com/nanocoai/nanoclaw)，MIT，30,874 star，TypeScript，创建 2026-01-31，推送 2026-10-05；仓库描述："runs in containers for security. Connects to WhatsApp, Telegram, Slack, Discord, Gmail … has memory, scheduled jobs, and runs directly on Anthropic's Agents SDK"；创建者 Gavriel Cohen（以色列特拉维夫背景） — [GitHub API](https://github.com/nanocoai/nanoclaw)；[virtuslab](https://virtuslab.com/blog/ai/nano-claw-your-personal-ai-butler)
- **PicoClaw**：[sipeed/picoclaw](https://github.com/sipeed/picoclaw)，MIT，30,015 star，Go，创建 2026-02-04，推送 2026-09-24，维护方矽速科技 Sipeed（中国深圳，嵌入式硬件公司）；"<10 MB 内存、一秒内启动，2026-02-09 一天内构建" — [GitHub API](https://github.com/sipeed/picoclaw)；[aimagicx](https://www.aimagicx.com/blog/openclaw-alternatives-comparison-2026)
- **IronClaw**：[nearai/ironclaw](https://github.com/nearai/ironclaw)，Apache-2.0，12,639 star，Rust，创建 2026-02-03，推送 2026-10-04，维护方 NEAR AI；"Agent OS focused on privacy, security and extensibility"，WASM 沙箱 — [GitHub API](https://github.com/nearai/ironclaw)；[aimagicx](https://www.aimagicx.com/blog/openclaw-alternatives-comparison-2026)

**B. 桌面客户端 + 交付成果谱系**

- **Eigent**：[eigent-ai/eigent](https://github.com/eigent-ai/eigent)，Apache-2.0，15,456 star，TypeScript（Electron，Node 18-22），创建 2025-07-29，推送 2026-10-02；仓库描述 "The Open Source Cowork Desktop - Local and Free Alternative to Claude Cowork and Codex"；维护方 Eigent AI / CAMEL-AI.org，创始人 Guohao Li，总部英国伦敦 — [GitHub API](https://github.com/eigent-ai/eigent)；[Hacker News 创始人自述](https://news.ycombinator.com/item?id=44736011)；[Tracxn](https://tracxn.com/d/companies/camelai/__rZpLBz431xO0jB0je0YHjoZlBV335YRnNMi3qLo3i00)
- **OpenWorker**：[andrewyng/openworker](https://github.com/andrewyng/openworker)，MIT，18,430 star，Python（后端）+ Tauri 2 + React 18 前端，创建 2026-07-20，推送 2026-10-05；2026-07-23 由 Andrew Ng 与 Rohit Prasad 发布开放测试，"delivers finished work instead of chat"，本地 FastAPI 服务 127.0.0.1:8765；macOS（Apple Silicon，已签名公证）、Windows 10/11（签名进行中） — [GitHub API](https://github.com/andrewyng/openworker)；[MarkTechPost](https://www.marktechpost.com/2026/07/23/andrew-ng-just-released-openworker-an-open-source-local-first-desktop-ai-coworker-that-returns-finished-deliverables-instead-of-chat/)；[README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)
- **Cherry Studio**：[CherryHQ/cherry-studio](https://github.com/CherryHQ/cherry-studio)，**AGPL-3.0**（社区版；商业授权联系 bd@cherry-ai.com），52,370 star，TypeScript（Electron），推送 2026-10-05；仓库描述 "AI productivity studio with smart chat, autonomous agents, and 300+ assistants"，Windows/Mac/Linux — [GitHub API](https://github.com/CherryHQ/cherry-studio)；[README](https://raw.githubusercontent.com/CherryHQ/cherry-studio/main/README.md)
  - 2026-08 的 2.0 重写"以 AI Agent 为主入口"；1.9.x 引入 **CherryClaw**，带定时任务与 Telegram/Discord/Slack/飞书/微信/QQ 集成 — [promptquorum](https://www.promptquorum.com/local-llms/cherry-studio-ai-desktop-client)；[computertech](https://computertech.co/cherry-studio-review/)
- **goose**：[aaif-goose/goose](https://github.com/aaif-goose/goose)，Apache-2.0，54,948 star，Rust，创建 2024-08-23，推送 2026-10-05，主页 goose-docs.ai — [GitHub API](https://github.com/aaif-goose/goose)
  - Block 于 2025-01-28 发布，2025-12 作为创始项目捐给 Linux Foundation 旗下 Agentic AI Foundation（AAIF），2026-04-07 代码与治理正式迁移，规范仓库改为 github.com/aaif-goose/goose — [goose 博客](https://goose-docs.ai/blog/2026/04/07/goose-moves-to-aaif/)；[TechCrunch](https://techcrunch.com/2025/12/09/openai-anthropic-and-block-join-new-linux-foundation-effort-to-standardize-the-ai-agent-era/)
  - 形态："Native desktop application (macOS, Linux, Windows)" + CLI + API；旧仓库 block/goose-plugins 已归档（插件被 MCP 取代） — [README](https://raw.githubusercontent.com/aaif-goose/goose/main/README.md)；[GitHub API block/goose-plugins](https://github.com/block/goose-plugins)
- **MyAgents**：[hAcKlyc/MyAgents](https://github.com/hAcKlyc/MyAgents)，AGPL-3.0-only（可申请商业授权），906 star，TypeScript，创建 2026-01-23，推送 2026-10-05；"open-source desktop workspace for personal AI Agents"，Tauri v2 + Rust + React 19，后端 Sidecar 为 Node.js 24 + **Claude Agent SDK**（实验性支持 Claude Code CLI / Codex CLI）；macOS 13+/Windows 10+ — [GitHub API](https://github.com/hAcKlyc/MyAgents)；[README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- **AiPy（爱派）**：[knownsec/aipyapp](https://github.com/knownsec/aipyapp)，4,025 star，推送 2026-02-15（近 8 个月无推送），维护方知道创宇（北京）；LICENSE 为 **GPL-3.0 外加自定义限制**："prohibited to provide this project as a SaaS service … prohibited to integrate this project or its code into commercial products"，需联系 sec@knownsec.com 获取商业许可 → 严格说不符合 OSI 开源定义（source-available） — [GitHub API](https://github.com/knownsec/aipyapp)；[LICENSE](https://raw.githubusercontent.com/knownsec/aipyapp/main/LICENSE)；"Python-Use"范式、定时任务、浏览器控制 — [掘金](https://juejin.cn/post/7677072964903010350)
- **Agent Zero**：[agent0ai/agent-zero](https://github.com/agent0ai/agent-zero)，LICENSE 文件为标准 **MIT**（版权 Agent Zero s.r.o. 2025；GitHub 自动识别为 Other），19,374 star，Python，推送 2026-10-02；"Dockerized Linux desktop, a browser with DOM annotation, live document cowork, projects, skills, plugins, and a bridge back to your host machine"，Web UI 形态 — [GitHub API](https://github.com/agent0ai/agent-zero)；[LICENSE](https://raw.githubusercontent.com/agent0ai/agent-zero/main/LICENSE)；[README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **agenticSeek**：[Fosowl/agenticSeek](https://github.com/Fosowl/agenticSeek)，**GPL-3.0**，27,427 star，Python，推送 2026-10-02；"Fully Local Manus AI. No APIs" — [GitHub API](https://github.com/Fosowl/agenticSeek)

**C. GUI / computer-use 研究型 Agent**

- **Microsoft UFO / UFO² / UFO³**：[microsoft/UFO](https://github.com/microsoft/UFO)，MIT，9,920 star，Python，推送 2026-09-29，仓库描述 "UFO³: Weaving the Digital Agent Galaxy"（微软，美国）；UFO（2024）→ UFO²（2025-04，"Desktop AgentOS"，HostAgent + AppAgents，已进入 LTS）→ UFO³ Galaxy（2025-11，ConstellationAgent + TaskOrchestrator，"Declarative Decomposition into Dynamic DAG"，支持 Windows/Linux/Android 多设备） — [GitHub API](https://github.com/microsoft/UFO)；[README](https://raw.githubusercontent.com/microsoft/UFO/main/README.md)
- **Agent S / S2 / S3**：[simular-ai/Agent-S](https://github.com/simular-ai/Agent-S)，Apache-2.0，12,539 star，Python，推送 2026-09-05，维护方 Simular（美国）；CLI `agent_s` + Python SDK `gui_agents`（AgentS3 类），macOS/Windows/Linux，"designed for single monitor screens"；OSWorld 100 步 66%，Best-of-N 后 72.6%；2026-08-28 称生产版 Sai 在 OSWorld 2.0 达 73% — [GitHub API](https://github.com/simular-ai/Agent-S)；[README](https://raw.githubusercontent.com/simular-ai/Agent-S/main/README.md)
- **UI-TARS Desktop / Agent TARS**：[bytedance/UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop)，Apache-2.0，39,204 star，TypeScript，推送 2026-09-24，字节跳动（中国）；含 Agent TARS（"general multimodal AI Agent stack"）与 UI-TARS Desktop（"native GUI agent for your local computer, driven by UI-TARS and Seed-1.5-VL/1.6"），Windows/macOS/浏览器，内核基于 MCP；Agent TARS CLI v0.3.0 于 2025-11 发布 — [GitHub API](https://github.com/bytedance/UI-TARS-desktop)；[README](https://raw.githubusercontent.com/bytedance/UI-TARS-desktop/main/README.md)
- **cua**：[trycua/cua](https://github.com/trycua/cua)，MIT，28,092 star，Rust，推送 2026-10-05；"Scale computer-use 2.0 with open-source drivers, cross-OS fleets, and benchmarks" — [GitHub API](https://github.com/trycua/cua)
- **Magentic-UI → MagenticLite**：[microsoft/magentic-ui](https://github.com/microsoft/magentic-ui)，MIT，10,088 star，Python，推送 2026-09-23；仓库描述已改为 "MagenticLite is an experimental agent that works across the browser and local file system"；由 MagenticBrain（推理/委派/终端）与 Fara1.5（9B 纯视觉浏览器模型，基于 Qwen 3.5）驱动，浏览器会话运行在轻量 VM 沙箱 "Quicksand" 中 — [GitHub API](https://github.com/microsoft/magentic-ui)；[Microsoft Research 博客](https://www.microsoft.com/en-us/research/blog/magenticlite-magenticbrain-fara1-5-an-agentic-experience-optimized-for-small-models/)
- **Self-Operating Computer**：[OthersideAI/self-operating-computer](https://github.com/OthersideAI/self-operating-computer)，MIT，10,296 star，Python，**最近推送 2025-09-19（已一年无更新）**，维护方 OthersideAI/HyperWrite（美国） — [GitHub API](https://github.com/OthersideAI/self-operating-computer)
- **Bytebot**：[bytebot-ai/bytebot](https://github.com/bytebot-ai/bytebot)，Apache-2.0，11,079 star，**已归档**，最近推送 2025-09-12 — [GitHub API](https://github.com/bytebot-ai/bytebot)
- **OpenAdapt**：[OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)，MIT，1,759 star，推送 2026-09-26；已转向"把演示的 GUI 任务编译为可验证程序"（openadapt-flow） — [GitHub API](https://github.com/OpenAdaptAI/OpenAdapt)
- **Anthropic computer-use demo**：原 anthropics/anthropic-quickstarts 仓库在 GitHub 搜索 API 中不可见；搜索结果显示现存 [anthropics/claude-quickstarts](https://github.com/anthropics/claude-quickstarts)（疑似改名，未核实）。Demo 形态为 Docker 容器（Ubuntu 22.04 + VNC），镜像 `ghcr.io/anthropics/anthropic-quickstarts:computer-use-demo-latest`；2026 年仍在更新以支持 `computer_toolset_20260801` — [cloudvyn 2026 指南](https://www.cloudvyn.com/blog/anthropic-computer-use-locally-guide-2026)
- **browser-use**：[browser-use/browser-use](https://github.com/browser-use/browser-use)，MIT，117,161 star，Python（Playwright），推送 2026-10-03（仅浏览器） — [GitHub API](https://github.com/browser-use/browser-use)
- **Nanobrowser**：[nanobrowser/nanobrowser](https://github.com/nanobrowser/nanobrowser)，Apache-2.0，13,957 star，Chrome 扩展，推送 2026-10-02（仅浏览器） — [GitHub API](https://github.com/nanobrowser/nanobrowser)

**D. 编码 CLI / Claude Code 类似物（本机执行但定位开发者）**

- **OpenCode**：[anomalyco/opencode](https://github.com/anomalyco/opencode)，MIT，**211,796 star**，TypeScript，推送 2026-10-05；"The open source coding agent"；旧 Go 版 opencode-ai/opencode 已归档（13,787 star） — [GitHub API](https://github.com/anomalyco/opencode)；被评为"de facto open-source alternative to Claude Code"，有 CLI/TUI/beta 桌面 App/Web/IDE 扩展，75+ 模型提供方 — [devtoollab](https://devtoollab.com/blog/open-source-alternatives-claude-code)
- **Codex CLI**：[openai/codex](https://github.com/openai/codex)，Apache-2.0，127,888 star，Rust，推送 2026-10-05（OpenAI，美国） — [GitHub API](https://github.com/openai/codex)
- **Gemini CLI**：[google-gemini/gemini-cli](https://github.com/google-gemini/gemini-cli)，Apache-2.0，107,235 star，TypeScript，推送 2026-10-05 — [GitHub API](https://github.com/google-gemini/gemini-cli)
- **Qwen Code**：[QwenLM/qwen-code](https://github.com/QwenLM/qwen-code)，Apache-2.0，28,314 star，TypeScript，推送 2026-10-05（阿里，中国） — [GitHub API](https://github.com/QwenLM/qwen-code)
- **Cline**：[cline/cline](https://github.com/cline/cline)，Apache-2.0，69,873 star，TypeScript，推送 2026-10-05；"Autonomous coding agent as an SDK, IDE extension, or CLI assistant" — [GitHub API](https://github.com/cline/cline)
- **Open Interpreter（新版）**：[openinterpreter/openinterpreter](https://github.com/openinterpreter/openinterpreter)，Apache-2.0，68,512 star，**Rust**，推送 2026-10-02；README 自述为 "a fork of OpenAI's Codex, with a focus on emulating the agent harness"，可用 `/harness` 切换 claude-code、kimi-cli、qwen-code、deepseek-tui 等；"runs commands inside native sandboxing on macOS, Linux, and Windows"；经典 Python 版现为社区 fork endolith/open-interpreter — [GitHub API](https://github.com/openinterpreter/openinterpreter)；[README](https://raw.githubusercontent.com/openinterpreter/openinterpreter/main/README.md)
  - **01 项目**：[openinterpreter/01](https://github.com/openinterpreter/01)，AGPL-3.0，5,156 star，**最近推送 2024-11-01（已停滞）**；01-app 最近推送 2024-09-23 — [GitHub API](https://github.com/openinterpreter/01)
- Claude Code 本身非开源；上述 OpenCode/Codex/Gemini CLI/Qwen Code/Cline/Open Interpreter 即其开源类似物 — [devtoollab](https://devtoollab.com/blog/open-source-alternatives-claude-code)

**E. 排除 / 非开源**
- sista-ai/ai-employee-download（"Sistava Desktop Controller"）：仓库无代码语言、2 star、许可证 Other，仅为下载页 → **非开源**，排除 — [GitHub API](https://github.com/sista-ai/ai-employee-download)
- Kimi Work（月之暗面）、Claude Cowork（Anthropic）、Kimi Claw、Perplexity Computer、Manus 均为闭源产品，仅作参照 — [composio](https://composio.dev/content/openclaw-alternatives)；[Kimi Work](https://www.kimi.ai/products/kimi-work)

### Inferences
- 按 star 与活跃度，2026-10 真正的"头部"是 OpenClaw（39 万）、Hermes Agent（25 万）、OpenCode（21 万）；中国方面最活跃的本地 Agent 是 QwenPaw（3.5 万）与 nanobot（4.9 万，港大）。
- 国内大厂亲自下场做"桌面级 Agent"的有网易有道（LobsterAI）、阿里（QwenPaw、Qwen Code）、字节（UI-TARS Desktop）、知道创宇（AiPy）。
- 国家归属：微软/OpenAI/Google/Block/Nous/Simular 为美国，字节/阿里/网易/Sipeed/知道创宇为中国，HKUDS 为中国香港，Eigent 为英国，NanoClaw 创始人以色列背景，Agent Zero s.r.o. 为捷克/斯洛伐克法律实体（由公司后缀推断）。

### Gaps
- "灵策智算 / LynxceAI"本身在公开网络无任何可检索到的页面（三次中英文搜索均无结果），画像无法独立核实。
- anthropics/anthropic-quickstarts 现状（是否改名为 claude-quickstarts、star 数）未能核实（GitHub 搜索 API 拒绝、github.com 被代理拦截）。
- OpenAdapt、nanobrowser、trycua 的所在国未从来源确认。
- Hermes Agent 的 "40+ 内置工具"仅来自二手文章（width.ai），README 摘要中未直接引用。

---

## 关键问题 2：是否有图形桌面客户端？是否有手机端或通过 IM（Telegram/WhatsApp/飞书/微信/钉钉）远程下指令？

### Takeaway
原生手机 App 只有 OpenClaw（iOS/Android 伴侣 App）一家；绝大多数项目用 IM 通道实现"手机远程给电脑下指令"。能直连飞书/企微/钉钉/微信的开源项目主要是中国项目（LobsterAI、QwenPaw、nanobot、Cherry Studio、MyAgents）以及通过社区插件的 OpenClaw。

### Cited Findings
- **OpenClaw**：原生伴侣 App 覆盖 macOS、iOS、Android、Windows、Linux；"meets users in the channels you already use"，20+ 消息服务含 Discord、iMessage、Slack、Teams、Telegram、WhatsApp、Google Chat、Signal — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
  - 中国 IM：官方插件 `@openclaw/feishu`（预装于 Docker 镜像，WebSocket 长连接，无需公网 IP）；社区插件 `openclaw-channel-dingtalk`（Stream 模式）、`@sunnoy/wecom`（HTTP 回调，需公网 IP/备案域名）；BytePioneer-AI/openclaw-china 提供钉钉、企业微信（自建应用/客服/公众号）、QQ、飞书、微信通道 — [apifox 指南](https://apifox.com/apiskills/openclaw-docker-compose-feishu-dingtalk-wecom/)；[openclaw-china](https://github.com/BytePioneer-AI/openclaw-china)
- **LobsterAI**：macOS + Windows（Electron）；IM 远程控制支持微信、企业微信、钉钉、飞书/Lark、QQ、Telegram、Discord、网易云信、POPO — [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)；仓库描述 "takes commands from your phone via WeChat, Feishu, DingTalk & Telegram" — [GitHub API](https://github.com/netease-youdao/LobsterAI)
- **QwenPaw**：单实例同时接入钉钉、Lark、微信、Discord、Telegram、iMessage、QQ，另有 Console、TUI、桌面端；桌面端为 Tauri 安装包（Windows/macOS，GitHub Releases 下载） — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)；[阿里云社区](https://developer.aliyun.com/article/1750416)
- **nanobot**：WebUI + 终端 + 聊天应用；通道含 Telegram、Discord、飞书、微信、Slack、Mattermost、Linear、Email — [README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- **Cherry Studio**：Windows/Mac/Linux 桌面客户端；CherryClaw 支持把 Agent 部署到飞书、微信、Telegram、Discord 等 IM 作群聊机器人；可对 Agent 说"每天早上 9 点把 5 条新闻简报发到我的飞书"自动创建定时任务 — [docs.cherry-ai.com 定时任务](https://docs.cherry-ai.com/advanced-basic/scheduled-tasks)；[promptquorum](https://www.promptquorum.com/local-llms/cherry-studio-ai-desktop-client)
- **Hermes Agent**：Telegram、Discord、Slack、WhatsApp、Signal、CLI "all from a single gateway process"，另提及 Email 与 Home Assistant；Hermes Desktop 原生 macOS/Windows/Linux — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；[Decrypt](https://decrypt.co/369952/hermes-ai-agent-official-app-terminal)；另有第三方"Hermes Agent 中文社区桌面版" — [desktop.hermesagent.org.cn](https://desktop.hermesagent.org.cn/en/)
- **MyAgents**：macOS/Windows 桌面；IM 集成 Telegram、钉钉及 OpenClaw 插件 — [README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- **OpenWorker**：macOS/Windows 桌面（Tauri）；远程方式为 Slack："mention @OpenWorker in a channel; a session opens on your desktop"；未提及手机 App — [README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)
- **Eigent**：Electron 桌面应用；README 未明确列出 OS，未提及手机/IM 远程 — [README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- **goose**：原生桌面 App（macOS/Linux/Windows）+ CLI — [README](https://raw.githubusercontent.com/aaif-goose/goose/main/README.md)
- **NanoClaw**：WhatsApp、Telegram、Slack、Discord、Gmail — [GitHub API](https://github.com/nanocoai/nanoclaw)
- **Agent Zero**：Web UI，可部署在 "$6 VPS or Raspberry Pi"，A0 CLI Connector 桥接主机 — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **UI-TARS Desktop**：Windows/macOS 原生 GUI Agent，支持本地与远程电脑/浏览器操作 — [README](https://raw.githubusercontent.com/bytedance/UI-TARS-desktop/main/README.md)
- **UFO/Agent S/cua/Magentic-UI**：CLI/库/Web UI 形态，无桌面客户端或手机端（UFO³ 可编排 Android 设备 Agent，但不是手机遥控电脑） — [UFO README](https://raw.githubusercontent.com/microsoft/UFO/main/README.md)

### Inferences
- 灵策智算"手机 App 远程给电脑下指令"这一点，开源世界只有 OpenClaw 原生做到；LobsterAI/QwenPaw/Cherry Studio 用微信/飞书/钉钉等 IM 替代，对中国企业用户反而更贴近"飞书/企微/钉钉直连"。

### Gaps
- Eigent 是否支持移动端或 IM 远程未在 README 中找到；eigent.ai 文档站被代理拦截。
- Hermes Agent 对飞书/企微/钉钉是否有官方通道未确认（README 仅列海外 IM）。

---

## 关键问题 3：Skills / 插件机制（是否采用 Anthropic Agent Skills 规范 SKILL.md、有无市场、能否自然语言创建）

### Takeaway
SKILL.md（agentskills.io 规范）已成事实标准：OpenClaw、Hermes、Eigent、Codex CLI、Gemini CLI、Qwen Code、Open Interpreter 等明确兼容；带"市场"的有 OpenClaw ClawHub、Agent Zero Plugin Hub、Eigent agent-skills、Cherry Studio 技能商店；"由 Agent 自动沉淀技能"仅 Hermes Agent 与 OpenClaw Skill Workshop 明确实现。

### Cited Findings
- **OpenClaw**："OpenClaw follows the AgentSkills spec"（agentskills.io），SKILL.md 需 `name`/`description` 前言；技能来源 7 级优先（workspace > project > personal > managed > workshop > bundled/custodian > extra dirs/plugin）；**ClawHub 为公共技能注册表**（`openclaw skills`、`clawhub` CLI 发布/同步）；**Skill Workshop**："When the agent spots reusable work, it drafts a proposal instead of writing directly to SKILL.md"；安全提示 "Treat third-party skills as untrusted code" — [docs/tools/skills.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md)
  - 技能安装命令 `npx clawhub install <skill-slug>`；"skills follow the Agent Skill convention developed by Anthropic" — [Tencent Cloud techpedia](https://www.tencentcloud.com/techpedia/141060)
  - 风险：恶意技能曾通过 ClawHub 传播恶意软件 — [TechRadar](https://www.techradar.com/pro/moltbot-is-now-openclaw-but-watch-out-malicious-skills-are-still-trying-to-trick-victims-into-spreading-malware)
- **Hermes Agent**："Compatible with the agentskills.io open standard"；"Autonomous skill creation after complex tasks"、"Skills self-improve during use"、"agent-curated with periodic nudges" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- **Eigent**：独立仓库 eigent-ai/agent-skills，"Each skill is defined in a SKILL.md file and may include helper scripts, references, and assets"，YAML 前言 name/description；内置 "200+ MCP tools"（二手来源）；预置技能含 Salesforce、Excel、PPT、Slack、Instagram — [eigent-ai/agent-skills](https://github.com/eigent-ai/agent-skills)；[vibesparking](https://www.vibesparking.com/en/blog/ai/multi-agent/2026-01-04-eigent-ai-multi-agent-workforce-platform/)
- **LobsterAI**：28 个内置技能（`SKILLs/skills.config.json`），覆盖网页搜索、文档处理、视频生成、图像生成、邮件、天气，并支持"自定义技能创建" — [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)
- **QwenPaw**：核心特性之一为"自定义技能"；内置 Skill Scanner 检测注入与凭据风险 — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)；[阿里云社区](https://developer.aliyun.com/article/1750416)
- **Agent Zero**："100+ community plugins" via Plugin Hub；"Skills can be loaded on demand by Agent Zero, or pinned from the chat input" — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **Cherry Studio**：Agent 可调用"内置工具、技能和 MCP 外部工具"；仓库 topics 含 `agent-skills`、`skills`；有"技能商店"（CSDN 教程） — [GitHub API](https://github.com/CherryHQ/cherry-studio)；[CSDN 技能商店攻略](https://blog.csdn.net/dongyujing/article/details/159682156)
- **goose**：扩展通过 MCP（"70+ extensions via the Model Context Protocol"）；Recipes 将指令、工具、目标打包为可复用/可分享单元 — [README](https://raw.githubusercontent.com/aaif-goose/goose/main/README.md)；[deepwiki Recipes & Scheduling](https://deepwiki.com/aaif-goose/goose/4-recipes-and-scheduling)
- **MyAgents**：Skills 系统 + MCP（STDIO/HTTP/SSE） — [README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- **nanobot**：MCP 集成；README 摘要未提 SKILL.md — [README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- **Open Interpreter（新）**："reuses shared AGENTS.md instructions and `.agents/skills` directories"，支持 MCP 与 hooks — [README](https://raw.githubusercontent.com/openinterpreter/openinterpreter/main/README.md)
- **编码 CLI**："Codex CLI adopted the SKILL.md spec verbatim in Dec 2025"；"Gemini CLI uses a compatibility layer"；"Qwen Code has Custom commands, Skills, and an Extensions system" — [thepromptindex](https://www.thepromptindex.com/how-to-use-ai-agent-skills-the-complete-guide.html)；[top-agent-skills](https://top-agent-skills.com/compare/codex-vs-gemini-cli)
- **UFO³**："Template-Driven MCP-Empowered Device Agents"，知识底座为 RAG（文档、演示、执行轨迹） — [README](https://raw.githubusercontent.com/microsoft/UFO/main/README.md)
- **OpenWorker**：README 未提 SKILL.md，技能以"specialist coworkers"（Security、Cloud Posture、Incident Triage）形式出现，25+ 连接器 + MCP — [README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)

### Inferences
- 灵策智算"100+ 内置 Skills + 市场"对应最接近的是 OpenClaw/ClawHub 与 Eigent agent-skills；"用自然语言把业务流程固化为 Skill / 重复模式自动沉淀为技能"对应 Hermes Agent 的自主技能创建与 OpenClaw 的 Skill Workshop，其余项目多数只是"手写 SKILL.md"。

### Gaps
- OpenClaw 内置技能数量、ClawHub 技能总数未在文档中给出。
- nanobot、QwenPaw、LobsterAI 是否采用 SKILL.md 规范未能从 README 摘要确认。
- agensi.io 的"支持 SKILL.md 的全部 Agent 列表"页面被代理拦截。

---

## 关键问题 4：权限与安全（工具调用审批、目录/沙箱边界、操作审计日志）

### Takeaway
审批与沙箱已普遍存在：OpenClaw（5 级 exec 审批、按 Agent 的 allowlist、`openclaw security audit`、standing grants 列表）、QwenPaw（5 层安全：内核沙箱/Tool Guard/File Guard/Skill Scanner/allow-deny-ask）、OpenWorker（4 层治理含审计轨迹）、goose（auto/approve/smart_approve/chat 四模式 + 沙箱）最完整；"全链路审计 / 操作可回溯"类功能以 OpenWorker 审计轨迹与 Agent Zero Time Travel 快照最接近。

### Cited Findings
- **OpenClaw**
  - 宿主命令执行 5 种策略：deny / allowlist / ask / auto / full；取 `tools.exec.*` 与审批默认值中"更严格者"；审批提示送达 macOS/iOS/Android App（显示工作目录与完整命令）、Control UI 审批卡、聊天通道（✅/♾️/❌ 反应）；无人值守时 `askFallback` 默认 deny；"Allowlists are per agent"；自动化审批形成 "standing grants bound to that exact agent, automation, job configuration, and operation"，可在 Settings → Approvals 或 `openclaw approvals grants list` 查看并逐条撤销 — [docs/tools/exec-approvals.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/exec-approvals.md)
  - DM 四种模式（pairing/allowlist/open/disabled），"most chat channels answer an unknown DM sender with a pairing code"；工具按 Agent 配置从只读到完整文件系统；`openclaw security audit` "one command tells you if you have drifted"，有带严重级别与自动修复的检查清单；多用户边界："one trusted boundary per gateway: a single operator, or a team whose members trust each other"，对抗场景需"separate gateway + credentials, ideally separate OS users or hosts"；提示注入防护 "untrusted-input wrapping" — [docs/gateway/security/index.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/security/index.md)
  - 沙箱主机默认 deny；"Tools run locally unless sandboxing is configured"；默认仅每日版本检查 — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- **QwenPaw**：五层防护——内核级沙箱（macOS Seatbelt / Linux Bubblewrap / Windows AppContainer）、Tool Guard（ShellEvasionGuardian YAML 规则）、File Guard（保护敏感目录）、Skill Scanner、Access Policy（allow/deny/ask 细粒度） — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)
- **OpenWorker**：四层治理——Hard floors（不可逆操作永远人工）、Earned autonomy ladder（一次性审批 → 常设规则 → allowlist）、Audit trail（"who did this, and why?"）、Sandboxing（NVIDIA OpenShell、macOS 原生沙箱、Windows 隐藏账户）；"Before anything consequential — sending a message, changing a calendar, running a command — it checks in and you approve or redirect" — [README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)
- **goose**：四种权限模式 Auto / Manual（approve）/ Smart（LLM 判断只读则自动放行，可用 MCP annotations）/ Chat Only；`GOOSE_MODE` 取值 auto/approve/chat/smart_approve；v1.25.0（2026-02-23）主打 "Sandboxed … More Secure"；已知问题：无终端的定时任务会在首个工具调用处因需审批而失败（issue #11164） — [goose 权限文档](https://goose-docs.ai/docs/guides/managing-tools/goose-permissions/)；[goose v1.25.0](https://goose-docs.ai/blog/2026/02/23/goose-v1-25-0/)；[issue #11164](https://github.com/aaif-goose/goose/issues/11164)
- **Hermes Agent**："Command approval, DM pairing, container isolation" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- **LobsterAI**：对文件/终端/网络等敏感操作设用户审批门；Renderer 上下文隔离、IPC 门控、"comprehensive logging" — [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)
- **Agent Zero**：主隔离策略为 Docker；项目级 secrets；"Time Travel snapshots provide workspace history with diff inspection and rollback" — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **NanoClaw**："runs in containers for security"；**IronClaw**：WASM 沙箱 + 加密验证 — [GitHub API](https://github.com/nanocoai/nanoclaw)；[aimagicx](https://www.aimagicx.com/blog/openclaw-alternatives-comparison-2026)
- **MagenticLite**：浏览器会话在轻量 VM 沙箱 Quicksand 中，"can't reach the rest of your machine without your say-so" — [Microsoft Research](https://www.microsoft.com/en-us/research/blog/magenticlite-magenticbrain-fara1-5-an-agentic-experience-optimized-for-small-models/)
- **Agent S**：警告 "The local environment executes arbitrary code with the same permissions as the user running the agent"，bash 30 秒超时 — [README](https://raw.githubusercontent.com/simular-ai/Agent-S/main/README.md)
- **UFO³**："Safe locking, and formally verified correctness"（Galaxy），AIP 协议容错重连 — [README](https://raw.githubusercontent.com/microsoft/UFO/main/README.md)
- **Open Interpreter（新）**：原生沙箱（macOS/Linux/Windows） — [README](https://raw.githubusercontent.com/openinterpreter/openinterpreter/main/README.md)
- **Cherry Studio**：Agent 在"工作目录"内读写文件 — [promptquorum](https://www.promptquorum.com/local-llms/cherry-studio-ai-desktop-client)
- **Eigent**：企业版含 "SSO, access control"；二手来源称 "auditable runs" 与 RBAC — [README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)；[eigent 博客](https://www.eigent.ai/blog/best-open-source-claude-cowork-alternatives-2026)

### Inferences
- 灵策智算的"目录边界授权"在 QwenPaw（File Guard）、OpenClaw（按 Agent 的文件系统权限档）、Cherry Studio（工作目录）中有对应物；"关键动作需确认"几乎人人有；"全链路审计"只有 OpenWorker 明确把审计轨迹列为一层，OpenClaw 的 grants 列表与 security audit 偏向配置审计而非操作日志。

### Gaps
- 多数项目的"操作审计日志"具体格式/留存未在来源中找到。
- Eigent 的审批/HITL 机制 README 未详述，文档站被拦截。

---

## 关键问题 5：记忆（是否跨会话长时记忆、如何实现）

### Takeaway
跨会话记忆已是标配；实现路线分两类：Markdown 文件型（OpenClaw、LobsterAI、QwenPaw 的 MEMORY.md/USER.md/每日笔记 + 向量/关键字混合检索）与数据库型（Hermes 的 FTS5 + LLM 摘要、Honcho 用户建模）。**QwenPaw 明确采用"三层记忆"**，与灵策智算"三层记忆体"表述最接近。

### Cited Findings
- **OpenClaw**：工作区 Markdown 记忆文件——USER.md（偏好/沟通风格/关系/项目上下文）、MEMORY.md（长期事实与决策）、memory/YYYY-MM-DD.md 每日笔记、DREAMS.md（整合摘要）；混合检索 "vector similarity … combined with keyword matching"，嵌入提供方含 OpenAI、Gemini、Voyage、Mistral、Ollama；"useful material from daily notes is distilled into MEMORY.md by the default dreaming sweep"；三种记忆引擎：Builtin（SQLite 混合检索）、Honcho（"AI-native cross-session memory with user modeling"）、LanceDB（auto-recall/auto-capture） — [docs/concepts/memory.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/memory.md)
- **Hermes Agent**："FTS5 session search with LLM summarization for cross-session recall" + "Honcho dialectic user modeling" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- **QwenPaw**：三层记忆——实时上下文、完整对话历史、由 ReMe 驱动的自进化个人知识库，"readable, editable, searchable, mutually-linked Markdown memories" — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)
- **LobsterAI**：本地 SQLite + 工作区记忆 MEMORY.md、USER.md、SOUL.md 与每日笔记 — [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)
- **nanobot**："Keep session history and long-term memory through Dream" — [README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- **Agent Zero**：Projects 隔离 "files, instructions, secrets, memories, repositories" — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **MyAgents**：长期记忆 + 本地全文检索（Tantivy + jieba） — [README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- **OpenWorker**：对话历史本地保存；README 未描述结构化长期记忆 — [README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)
- **NanoClaw**："has memory" — [GitHub API](https://github.com/nanocoai/nanoclaw)
- **Agent S**：README 未描述持久记忆，仅 `max_trajectory_length` 控制轨迹图片轮数 — [README](https://raw.githubusercontent.com/simular-ai/Agent-S/main/README.md)
- **UFO³**：Knowledge Substrate "RAG with docs, demos, execution traces" — [README](https://raw.githubusercontent.com/microsoft/UFO/main/README.md)

### Inferences
- Markdown 文件型记忆（OpenClaw 范式）已被 LobsterAI、QwenPaw 等中国项目沿用，便于人工审阅编辑；灵策智算若主打"三层记忆体"，QwenPaw 是最直接的对标。

### Gaps
- goose、Eigent、Cherry Studio 的跨会话记忆实现未在所读来源中找到（goose 文档站被拦截）。

---

## 关键问题 6：定时任务 / cron / 主动触发（文件变更、消息触发）

### Takeaway
cron 定时任务几乎人人有（OpenClaw、Hermes、nanobot、NanoClaw、LobsterAI、Cherry Studio、OpenWorker、goose、Agent Zero、MyAgents、Eigent、AiPy）。事件触发方面，OpenClaw（webhook、Gmail PubSub、条件观察器、heartbeat）与 Eigent（Scheduled / Event / App automations）最完整；**"文件变更触发"在所有来源中都未明确找到**。

### Cited Findings
- **OpenClaw**：automations 支持 at/every/cron 三类计划，"Cron rules, pacing, and condition watchers"；运行上下文 main/current/isolated/custom；输出可投递到聊天通道、webhook 或不投递；可通过 `openclaw automations` CLI 或对话式管理；外部触发含 "webhooks, and Gmail PubSub triggers"；Heartbeat 为"periodic main-session turns" — [docs/automation/cron-jobs.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/automation/cron-jobs.md)
- **Hermes Agent**："Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- **Eigent**："Scheduled automations for recurring work, Event automations for external webhooks and events, and App automations for connected application events"；README："Schedule recurring workflows" — [eigent.ai docs（搜索摘要）](https://www.eigent.ai/docs)；[README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- **LobsterAI**：用自然语言或任务 UI 创建周期任务（每日新闻、邮件摘要、网站监控、周报） — [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)
- **Cherry Studio**：定时任务由 Agent（内置 Cherry Claw）按自然语言创建并投递到飞书等；用例"每日新闻简报、每周总结" — [docs.cherry-ai.com](https://docs.cherry-ai.com/advanced-basic/scheduled-tasks)
- **OpenWorker**："Scheduled automations: morning briefs, weekly reports, standing channel watches" — [README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)
- **nanobot**："Run long-horizon goals and scheduled automations" with Cron — [README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- **goose**：`goose schedule add` 以 cron 表达式（如 "0 0 9 * * *"）调度 recipe，副本存于 ~/.local/share/goose/scheduled_recipes；无人值守运行与审批模式存在冲突（issue #11164） — [deepwiki](https://deepwiki.com/aaif-goose/goose/4-recipes-and-scheduling)；[issue #11164](https://github.com/aaif-goose/goose/issues/11164)
- **Agent Zero**："Run recurring checks and monitoring tasks with project-scoped context and credentials" — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **MyAgents**：Cron 表达式任务调度 — [README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- **NanoClaw**："scheduled jobs" — [GitHub API](https://github.com/nanocoai/nanoclaw)
- **AiPy**：支持定时任务 — [掘金](https://juejin.cn/post/7677072964903010350)

### Inferences
- 灵策智算的"主动感知：文件变更/消息触发/周期事件"中，周期事件与消息触发在 OpenClaw/Eigent 有完整对应；文件变更触发是一个差异化点（开源项目普遍缺失，或需自行通过 webhook/脚本实现）。

### Gaps
- 未找到任何项目明确支持"本地文件变更触发 Agent"。
- QwenPaw、UI-TARS、UFO 的定时任务能力未在 README 摘要中确认。

---

## 关键问题 7：多模型切换（支持哪些提供方，是否支持本地模型 Ollama 等）

### Takeaway
所有候选都支持多提供方；本地模型（Ollama/vLLM/LM Studio）支持是常态；QwenPaw 更进一步内置 QwenPaw-Flash 本地模型（2B/4B/9B）无需 API Key，MagenticLite 则专为 9B 小模型设计。

### Cited Findings
- OpenClaw："hosted and local model providers"，provider 插件化 — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)
- Hermes Agent："Nous Portal, OpenRouter, OpenAI, your own endpoint, and many others … Switch with `hermes model`" — [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)
- goose："15+ providers — Anthropic, OpenAI, Google, Ollama, OpenRouter, Azure, Bedrock"，并可经 ACP 复用已有订阅 — [README](https://raw.githubusercontent.com/aaif-goose/goose/main/README.md)
- Eigent："any model of your choice" 含 "local inference"，vLLM/Ollama/LM Studio — [README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- Cherry Studio：OpenAI/Gemini/Anthropic 云端，Claude/Perplexity/Poe 网页服务，本地 Ollama/LM Studio — [README](https://raw.githubusercontent.com/CherryHQ/cherry-studio/main/README.md)
- nanobot："OpenAI-compatible APIs, local LLMs"，Ollama、vLLM — [README](https://raw.githubusercontent.com/HKUDS/nanobot/main/README.md)
- OpenWorker：OpenAI、Anthropic、Google Gemini、BytePlus Ark、Volcengine、GLM、DeepSeek、Kimi、Qwen、MiniMax、Mistral、Grok、Together、Fireworks，本地 Ollama — [README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)
- QwenPaw：内置 QwenPaw-Flash（2B/4B/9B）本地运行时无需 API Key；14+ 云提供方含 Ollama、LM Studio、DashScope、OpenAI、Anthropic — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)
- MyAgents：Anthropic 订阅或 API、DeepSeek、Moonshot、Gemini 等；README 未提 Ollama — [README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- Agent S：OpenAI、Anthropic、Gemini、OpenRouter、vLLM；grounding 推荐 UI-TARS-1.5-7B — [README](https://raw.githubusercontent.com/simular-ai/Agent-S/main/README.md)
- UFO：OpenAI、Azure OpenAI、Qwen、Gemini、Claude — [README](https://raw.githubusercontent.com/microsoft/UFO/main/README.md)
- UI-TARS Desktop：Claude、豆包（火山引擎）、UI-TARS、Seed VL — [README](https://raw.githubusercontent.com/bytedance/UI-TARS-desktop/main/README.md)
- Open Interpreter（新）：任何 OpenAI 兼容提供方，Kimi K3、DeepSeek 等 — [README](https://raw.githubusercontent.com/openinterpreter/openinterpreter/main/README.md)
- OpenCode：75+ 模型提供方，Ollama/LM Studio — [devtoollab](https://devtoollab.com/blog/open-source-alternatives-claude-code)
- Agent Zero：OpenAI（含 Codex OAuth），本地选项文档中有但摘要未详述 — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)

### Inferences
- 灵策智算"多模型自由切换"并非差异点；差异在于国产模型（DeepSeek/Qwen/Kimi/GLM/豆包）的开箱支持，OpenWorker、QwenPaw、UI-TARS、Cherry Studio 已覆盖。

### Gaps
- LobsterAI 的模型提供方列表与本地模型支持未从 README 摘要得到确认。

---

## 关键问题 8：可否私有化/企业化部署，有无团队/多用户/权限功能

### Takeaway
真正的"企业版 + 团队/RBAC"只有 Cherry Studio Enterprise（私有化、集中模型管理、企业知识库、RBAC）与 Eigent Enterprise（SSO、访问控制）；OpenClaw 支持"共享团队部署"但明确一网关一个信任边界；Hermes 的多实例/多用户依赖第三方控制平面；QwenPaw 提供阿里云/AgentScope 平台云部署。

### Cited Findings
- **Cherry Studio Enterprise**：集中模型管理、企业知识库、基于角色的访问控制、"Fully Private Deployment"（本地或私有云）；社区版 AGPL-3.0，商业授权另议 — [README](https://raw.githubusercontent.com/CherryHQ/cherry-studio/main/README.md)
- **Eigent**：README 列企业专属 "SSO, access control, and custom enquiries"；核心主张 "Run agents on your machine with complete isolation from cloud services" — [README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)；二手来源：RBAC、auditable runs — [eigent 博客](https://www.eigent.ai/blog/best-open-source-claude-cowork-alternatives-2026)
- **OpenClaw**："personal assistant on a laptop or as a shared team deployment; configuration is the only difference"；但安全文档要求 "one trusted boundary per gateway … a team whose members trust each other"，对抗场景需分离网关/凭据/OS 用户/主机；基金会 "no paid tier, hosted service" — [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)；[security/index.md](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/gateway/security/index.md)
- **Hermes Agent**：任务按队列顺序执行，多用户需更大实例；多实例控制平面为第三方项目 Mission Control、HermesHQ（Docker，多身份/工作区，Telegram/WhatsApp 通道）、Nora（Docker/K8s Helm，AES-256-GCM 密钥） — [hermesatlas 部署选项](https://hermesatlas.com/lists/deployment-options)；[hermes-agent.ai 团队设置](https://hermes-agent.ai/blog/hermes-agent-24-7-ai-agent-setup)
- **QwenPaw**：本地与云端双模式；Docker、阿里云 ECS 一键部署、AgentScope 平台免费 24/7、ModelScope Spaces — [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)
- **goose**：可构建带预配置提供方与品牌的 "Custom distributions" — [README](https://raw.githubusercontent.com/aaif-goose/goose/main/README.md)
- **Agent Zero**：项目级 secrets、可部署于 VPS/树莓派/GPU 服务器 — [README](https://raw.githubusercontent.com/agent0ai/agent-zero/main/README.md)
- **nanobot / NanoClaw / ZeroClaw / PicoClaw**：自托管，定位个人 — [GitHub API nanobot](https://github.com/HKUDS/nanobot)；[GitHub API nanoclaw](https://github.com/nanocoai/nanoclaw)
- **LobsterAI / OpenWorker / MyAgents**：README 未见团队/多用户功能 — [LobsterAI README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)；[OpenWorker README](https://raw.githubusercontent.com/andrewyng/openworker/main/README.md)；[MyAgents README](https://raw.githubusercontent.com/hAcKlyc/MyAgents/main/README.md)
- **AiPy**：许可证禁止未授权 SaaS/商业集成 — [LICENSE](https://raw.githubusercontent.com/knownsec/aipyapp/main/LICENSE)

### Inferences
- "团队共享知识库/Skill 库 + 企业私有化 + 全链路审计"是灵策智算相对所有开源项目的最大差异；开源侧最接近的是 Cherry Studio Enterprise（但其 Agent 执行能力被评为"early-stage"）与 Eigent Enterprise。

### Gaps
- Eigent 企业版的具体 RBAC/审计细节仅有二手来源；eigent.ai 文档站被拦截。
- Cherry Studio Enterprise 是否开源（或仅社区版开源）未确认。

---

## 关键问题 9：与灵策智算最接近的 2-3 个项目及差距（逐项对照）

### Takeaway
最接近的三个：**LobsterAI**（网易有道，桌面客户端 + 微信/飞书/钉钉手机遥控 + 定时任务 + 交付 PPT/文档/视频，但无企业版）、**QwenPaw**（阿里，桌面端 + 国内 IM + 三层记忆 + 五层安全 + 多 Agent，但缺企业团队功能与定时任务证据）、**OpenClaw（+ClawX/LobsterAI 作 GUI）**（功能最全：原生 iOS/Android、ClawHub 技能市场、Skill Workshop、cron+webhook、5 级审批，但无企业多租户/RBAC/团队知识库）。另两个强参照：**Eigent**（多 Agent workforce + 企业 SSO/RBAC + 事件触发）与 **Hermes Agent**（技能自进化）。

### Cited Findings（对照矩阵，✓=来源确认 / △=部分或二手 / ✗=未见）

| 灵策智算功能点 | LobsterAI | QwenPaw | OpenClaw | Eigent | Hermes | Cherry Studio |
|---|---|---|---|---|---|---|
| macOS/Windows 桌面客户端 | ✓ Electron（macOS/Win） | ✓ Tauri Beta（Win/macOS） | ✓ 原生 App 五平台 | ✓ Electron | ✓ Hermes Desktop 公测 | ✓ 三平台 |
| 手机远程下指令 | ✓ 微信/企微/钉钉/飞书/QQ/Telegram | ✓ 钉钉/Lark/微信/QQ/Telegram/iMessage | ✓ 原生 iOS/Android + 20+ IM；飞书官方插件，钉钉/企微社区插件 | ✗ 未见 | △ Telegram/Discord/Slack/WhatsApp（无国内 IM） | ✓ 飞书/微信/QQ/Telegram/Discord |
| 100+ 内置 Skills + 市场 | △ 28 内置，可自定义 | △ 自定义技能，有 Skill Scanner | ✓ AgentSkills 规范 + ClawHub 市场 | ✓ SKILL.md 仓库，200+ MCP 工具（二手） | ✓ agentskills.io 兼容 | △ 技能商店（二手） |
| 自然语言固化流程为 Skill / 自动沉淀 | ✗ 未见自动沉淀 | ✗ 未见 | ✓ Skill Workshop 由 Agent 起草提案 | ✗ 未见 | ✓ 复杂任务后自主创建技能并自我改进 | ✗ 未见 |
| Main Agent + Topic Agent 调度 + 执行队列 | ✗ 未见 | ✓ 多 Agent、运行时子 Agent、ACP | △ 多 Agent/子 Agent 路由 | ✓ 协调者 + 并行 worker workforce | △ subagents | △ 子任务 |
| 主动感知（文件变更/消息触发/周期） | △ 定时任务 | ✗ 未确认 | ✓ cron + webhook + Gmail PubSub + 条件观察器 + heartbeat（无文件变更） | ✓ Scheduled/Event/App automations | ✓ cron | ✓ 定时任务 |
| 数据留本地、目录边界授权 | ✓ 本地 SQLite + 敏感操作审批 | ✓ 内核沙箱 + File Guard | ✓ 按 Agent 文件系统权限档 + 沙箱 | ✓ 本地隔离 | ✓ 容器隔离 | △ 工作目录 |
| 工具调用审批 / 关键动作确认 | ✓ 审批门 | ✓ allow/deny/ask | ✓ 5 级 exec 审批，送达 App/IM | △ 未详述 | ✓ 命令审批 | ✗ 未见 |
| 操作可回溯 / 全链路审计 | △ "comprehensive logging" | ✗ 未见 | △ standing grants 列表 + security audit | △ "auditable runs"（二手） | ✗ 未见 | ✗ 未见 |
| 跨会话长时记忆 / 三层记忆 | ✓ MEMORY/USER/SOUL.md | ✓ **三层记忆**（ReMe） | ✓ Markdown + 混合检索 + Dreaming | ✗ 未见 | ✓ FTS5 + Honcho | ✗ 未见 |
| 定时任务（周报/巡检/提醒） | ✓ | ✗ 未确认 | ✓ | ✓ | ✓ | ✓ |
| "数字员工"长期运行独立 Agent | △ 7×24 助理 | △ 独立记忆/技能的多 Agent | ✓ 多 Agent + 守护进程 | ✓ workforce | ✓ 守护进程 | △ CherryClaw |
| 多模型切换 + 本地模型 | △ 未确认 | ✓ 内置本地模型 + 14 云 | ✓ | ✓ Ollama/vLLM/LM Studio | ✓ | ✓ Ollama/LM Studio |
| 团队共享知识库/Skill 库 | ✗ | ✗ | ✗（一网关一信任边界） | △ 企业版 | ✗ | ✓ 企业版知识库 + RBAC |
| 企业私有化部署 | ✗ | △ 阿里云/自托管 | △ 自托管 | ✓ 企业版 SSO/访问控制 | △ 第三方控制平面 | ✓ 企业版私有化 |
| 飞书/企微/钉钉直连 | ✓ | ✓（钉钉/Lark/微信，企微未见） | △ 官方飞书 + 社区钉钉/企微 | ✗ | ✗ | △ 飞书/微信（企微/钉钉未见） |

来源：各单元格对应上文各节引用（LobsterAI [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)；QwenPaw [README_zh](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README_zh.md)；OpenClaw [README](https://raw.githubusercontent.com/openclaw/openclaw/main/README.md)、[skills](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md)、[cron](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/automation/cron-jobs.md)、[exec-approvals](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/exec-approvals.md)、[memory](https://raw.githubusercontent.com/openclaw/openclaw/main/docs/concepts/memory.md)、[openclaw-china](https://github.com/BytePioneer-AI/openclaw-china)；Eigent [README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)、[agent-skills](https://github.com/eigent-ai/agent-skills)；Hermes [README](https://raw.githubusercontent.com/NousResearch/hermes-agent/main/README.md)；Cherry Studio [README](https://raw.githubusercontent.com/CherryHQ/cherry-studio/main/README.md)、[定时任务文档](https://docs.cherry-ai.com/advanced-basic/scheduled-tasks)、[promptquorum](https://www.promptquorum.com/local-llms/cherry-studio-ai-desktop-client)）

- 交付物维度：LobsterAI 仓库描述明确 "data analysis, slides, docs, video & web research"；OpenWorker 主张 "drafted report, a Slack reply with the real numbers, an updated calendar, a triaged inbox"；Eigent 预置 Excel/PPT 技能 — [GitHub API LobsterAI](https://github.com/netease-youdao/LobsterAI)；[lumienai](https://lumienai.com/news/andrew-ng-openworker-local-ai-desktop-agent-open-source)；[vibesparking](https://www.vibesparking.com/en/blog/ai/multi-agent/2026-01-04-eigent-ai-multi-agent-workforce-platform/)
- 成熟度提示：Cherry Studio 的自主 Agent 模式被评为 "still early-stage and not as capable as dedicated agent platforms" — [promptquorum](https://www.promptquorum.com/local-llms/cherry-studio-ai-desktop-client)；Hermes Desktop 为 "public preview … rough edges should be expected" — [Decrypt](https://decrypt.co/369952/hermes-ai-agent-official-app-terminal)；OpenWorker 为 "open beta" — [MarkTechPost](https://www.marktechpost.com/2026/07/23/andrew-ng-just-released-openworker-an-open-source-local-first-desktop-ai-coworker-that-returns-finished-deliverables-instead-of-chat/)

### Inferences
- **若按"产品形态 + 中国 IM + 交付办公成果"打分，LobsterAI 最像灵策智算个人版**；它实质是"OpenClaw 运行时 + 网易有道 Cowork 壳"，差距在企业版（私有化、团队知识库/技能库、RBAC、审计）与原生手机 App。
- **若按"架构叙事"（多 Agent 调度、三层记忆、分层安全）打分，QwenPaw 最像**，且有阿里云云端部署路径；差距在定时任务/主动感知证据不足、无企业团队功能、技能市场不明。
- **OpenClaw 是功能上限参照**（原生移动端、技能市场、Agent 自起草技能、事件触发、分级审批），但定位个人/互信小团队，缺少企业多租户、RBAC、团队知识库与全链路操作审计；中文 IM 依赖插件。
- 灵策智算相对开源生态的可辩护差异点集中在：企业私有化 + 团队共享 Skill/知识库 + 全链路审计 + 文件变更触发 + 原生手机 App 远程；其"100+ Skills""三层记忆""定时任务""审批"等已被开源项目普遍覆盖。

### Gaps
- 灵策智算画像来自任务描述，未能用任何公开来源核实，对照只能是"画像 vs 来源"。
- Eigent 与 Cherry Studio 文档站被代理拦截，部分企业功能只能引用 README 与二手评测。
- QwenPaw 的定时任务、技能市场与企微支持需进一步从其文档站 qwenpaw.agentscope.io 核实（本次未抓取）。
