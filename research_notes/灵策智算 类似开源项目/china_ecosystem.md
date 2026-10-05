# 中国本土视角：与"灵策智算 LynxceAI"类似的开源项目、社区榜单与"手机远程操控电脑"方案（截至 2026-10-05）

> 数据口径说明：下文所有 GitHub star 数、许可证（SPDX）、创建/最近 push 时间均为 2026-10-05 通过 GitHub 官方 API（search/repositories 接口）查询所得，来源统一标注为该仓库 URL；README/LICENSE 的内容通过 raw.githubusercontent.com 直接核读。大量中文站（zhihu、csdn、cnblogs、juejin、oschina、infoq、36kr、sohu、aliyun/tencent 开发者社区、ithome、news.cn 等）在本环境中被网络策略拦截，只能依赖搜索引擎摘要，凡属此类均标注"（搜索摘要，原文未抓取）"。

## 关键问题 1：各国产开源 Agent / "数字员工" / 本地执行项目的基本事实（地址、许可证、star、活跃度、维护方、形态、中文社区）

### Takeaway
2026 年国产开源 Agent 生态已分化为四类：(a) 大厂"通用 Agent/Harness"（DeerFlow、JoyAgent、AgentScope/QwenPaw、OpenManus、OWL/Eigent）；(b) OpenClaw 衍生/对标的"本地个人助理"（QwenPaw、LobsterAI、nanobot、AstrBot、LangBot、StaffDeck、OpenOcta）；(c) 低代码/知识库平台（Coze Studio、Dify、MaxKB、Bisheng、WeKnora）；(d) 计算机/手机操控模型（UI-TARS-desktop、Open-AutoGLM、Qwen open-computer-use）。许可证以 Apache-2.0/MIT 为主，但 AstrBot、StaffDeck、Cherry Studio 为 AGPL-3.0，MaxKB、OpenOcta 为 GPL-3.0，Dify 为"Apache-2.0 + 多租户/Logo 附加条款"，商用时需逐一核对。

### Cited Findings

**A1. 大厂通用 Agent / Harness**

- JoyAgent-JDGenie（京东）：仓库 jd-opensource/joyagent-jdgenie，Apache-2.0，Java，11,921 stars / 1,657 forks，创建 2025-07-16，最近 push 2026-02-12（截至 2026-10 已约 8 个月无提交），默认分支为 `data_agent`，描述"开源的端到端产品级通用智能体" — [GitHub API](https://github.com/jd-opensource/joyagent-jdgenie)
- 京东称 JoyAgent-JDGenie "100% 全栈开源"，开放前端、后端、框架、引擎及报告/代码/PPT/文件等子智能体，GAIA 榜准确率超 75%，经京东内部 2 万+ 智能体实践锤炼；Docker 一键启动后浏览器访问 localhost:3000（搜索摘要，原文未抓取） — [京东云开发者 / 博客园](https://www.cnblogs.com/Jcloud/p/19013061)；[53AI 部署教程](https://www.53ai.com/news/OpenSourceLLM/2025073172680)
- DeerFlow（字节跳动）：仓库 bytedance/deer-flow，MIT，Python，83,396 stars / 11,584 forks，创建 2025-05-07，最近 push 2026-10-05（高活跃），官网 deerflow.tech，自述为"open-source long-horizon SuperAgent harness"，含 sandboxes、memories、tools、skill、subagents 与 message gateway — [GitHub API](https://github.com/bytedance/deer-flow)
- DeerFlow README（直接核读）：2.0 为对原"Deep Research"框架的全量重写；沙箱支持 本地/Docker/Kubernetes 三种模式；消息网关支持 Telegram、Slack、Feishu/Lark、WeChat、WeCom、QQ、DingTalk、Buzz 共 8 个 IM，无需公网 IP；支持 cron 定时任务、MCP（含 OAuth）、线程级读写权限分离、按 skill 的 `allowed-tools` 限制；部署推荐 Docker Compose，建议 4–8 vCPU / 8–16GB 内存；后端 Python 3.12 + LangGraph/LangChain，前端 Next.js — [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- DeerFlow 2.0 于 2026-02-28 发布后登顶 GitHub Trending，MIT 协议（搜索摘要） — [量子位](https://www.qbitai.com/2026/03/391361.html)；[知乎](https://zhuanlan.zhihu.com/p/2019713726064898703)
- Coze Studio（字节/扣子）：仓库 coze-dev/coze-studio，Apache-2.0，TypeScript/Go，21,672 stars，创建 2025-06-26，最近 push 2026-07-29（约 2 个月无提交） — [GitHub API](https://github.com/coze-dev/coze-studio)
- 扣子于 2025 年 7 月 26 日开源 Coze Studio 与 Coze Loop，两天破 1 万 star，采用 Apache 2.0（搜索摘要） — [雷锋网](https://m.leiphone.com/category/industrynews/pPrh3FtbXg3VVeFS.html)；[智源社区](https://hub.baai.ac.cn/view/47688)
- 扣子 SaaS 平台 3.0 于 2026-06-01 上线，支持接入 Claude Code、Codex CLI、OpenClaw 等本地 Agent，强调"从一个人聊 AI 到 AI 团队协作"（搜索摘要；注意这是扣子 SaaS 的版本号，开源 Coze Studio 仓库是否同步 3.0 未核实） — [搜狐](https://www.sohu.com/a/1031004942_223764)
- Qwen-Agent（阿里通义）：仓库 QwenLM/Qwen-Agent，Apache-2.0，Python，17,132 stars，创建 2023-09-22，最近 push 2026-03-04（约 7 个月无提交），特性含 Function Calling、MCP、Code Interpreter、RAG、Chrome 扩展 — [GitHub API](https://github.com/QwenLM/Qwen-Agent)
- 同组织新项目 QwenLM/open-computer-use：MIT，Swift，284 stars，创建 2026-06-01，最近 push 2026-06-10；"MCP-based Computer Use service for Qwen Code and any AI agent — controls macOS, Linux, and Windows via accessibility APIs" — [GitHub API](https://github.com/QwenLM/open-computer-use)
- AgentScope（阿里）：仓库 agentscope-ai/agentscope，Apache-2.0，Python，32,762 stars，创建 2024-01-12，最近 push 2026-09-30；Java 版 agentscope-java 5,871 stars — [GitHub API](https://github.com/agentscope-ai/agentscope)；[agentscope-java](https://github.com/agentscope-ai/agentscope-java)
- QwenPaw（原 CoPaw，阿里 AgentScope 团队）：仓库 agentscope-ai/QwenPaw，Apache-2.0，TypeScript，35,439 stars / 3,153 forks / 1,032 open issues，创建 2026-02-24，最近 push 2026-09-30，官网 qwenpaw.agentscope.io — [GitHub API](https://github.com/agentscope-ai/QwenPaw)
- QwenPaw README（直接核读）：支持 DingTalk、Lark、WeChat、Discord、Telegram、iMessage、QQ 等聊天通道；安装方式含 `pip install qwenpaw`（Python 3.11+）、一键脚本（macOS/Linux/Windows）、Docker（Docker Hub / 阿里云镜像）、阿里云 ECS 一键部署、AgentScope Platform；**桌面端为基于 Tauri 的 Beta 应用（Windows 10+ / macOS 14+）**；内置 PDF/Office/浏览器/新闻/日程等 Skills 与插件市场；"自进化个人知识库"记忆（Markdown 可读可编辑）；cron 定时任务；安全特性列出 "Kernel-level Sandbox, Tool Guard, File Guard, Skill Scanner, Access Policy"，可配置审批级别；browser-use / computer-use 列为路线图进行中；README 未提及 CoPaw — [QwenPaw README](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README.md)
- 阿里云 CoPaw 于 2026 年 4 月中旬宣布更名 QwenPaw 并发布 1.1.0；原生支持钉钉、飞书、QQ、Discord、iMessage，可本地一键部署或经阿里云计算巢/魔搭创空间云端部署（搜索摘要） — [IT之家](https://www.ithome.com/0/938/334.htm)；[i黑马](http://www.iheima.com/article-396104.html)
- CoPaw 由阿里云通义实验室基于 AgentScope 于 2026 年 2 月开源，Apache 2.0；强调"本地/云端双模、国产平台原生适配、主动任务执行、零代码扩展"（搜索摘要） — [新浪财经 2026-02-28](https://finance.sina.com.cn/roll/2026-02-28/doc-inhpiwhq0063763.shtml)；[InfoQ](https://www.infoq.cn/article/Z8OmAd2YjFBlxaA0yfij)
- ModelScope-Agent（魔搭）：现仓库名 modelscope/ms-agent，Apache-2.0，Python，4,407 stars，创建 2023-08-03，最近 push 2026-09-21 — [GitHub API](https://github.com/modelscope/ms-agent)
- MS-Agent v1.6.0 于 2026-03-23 发布（上下文压缩、多模态输入）；其 Agentic Insight v2 在 DeepResearch Bench 开源方案中排名第 2（搜索摘要） — [ms-agent README_ZH](https://github.com/modelscope/ms-agent/blob/main/README_ZH.md)
- OpenManus（MetaGPT 团队 / FoundationAgents）：仓库 FoundationAgents/OpenManus，MIT，Python，58,466 stars / 10,138 forks，创建 2025-03-06，最近 push 2026-09-30，官网 openmanus.github.io — [GitHub API](https://github.com/FoundationAgents/OpenManus)
- OWL（CAMEL-AI）：仓库 camel-ai/owl，20,151 stars，创建 2025-03-03，最近 push 2026-09-30；GitHub API 未识别出 LICENSE 文件（返回 None，raw LICENSE 返回 404），但 README 明示 "The source code is licensed under Apache 2.0"；GAIA 69.09%，自称开源框架第一；提供中/英/日 Web UI（webapp_zh.py）与 README_zh.md；2025-09-22 被 NeurIPS 2025 接收 — [GitHub API](https://github.com/camel-ai/owl)；[OWL README](https://raw.githubusercontent.com/camel-ai/owl/main/README.md)
- CAMEL 主框架 camel-ai/camel：Apache-2.0，17,809 stars，最近 push 2026-09-30 — [GitHub API](https://github.com/camel-ai/camel)
- Eigent（Eigent AI，基于 CAMEL-AI）：仓库 eigent-ai/eigent，Apache-2.0，TypeScript，15,456 stars，创建 2025-07-29，最近 push 2026-10-02，自述"The Open Source Cowork Desktop - Local and Free Alternative to Claude Cowork and Codex" — [GitHub API](https://github.com/eigent-ai/eigent)
- Eigent README（直接核读）：Electron 桌面应用，macOS/Windows/Linux；"local-first execution"，支持 vLLM/Ollama/LM Studio 本地模型；MCP 与 Skill 集成；多 Agent workforce；企业版提供 SSO、访问控制；文档含简体中文；社区渠道含 WeChat 群；许可证 Apache 2.0，README 未列附加商用限制 — [Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
- UI-TARS-desktop / Agent TARS（字节）：仓库 bytedance/UI-TARS-desktop，Apache-2.0，TypeScript，39,204 stars，创建 2025-01-19，最近 push 2026-09-24，topics 含 computer-use、gui-agent、cowork、mcp — [GitHub API](https://github.com/bytedance/UI-TARS-desktop)

**A2. 腾讯系**

- Youtu-Agent（腾讯优图/腾讯云 ADP）：仓库 TencentCloudADP/youtu-agent，LICENSE 文件为标准 MIT（GitHub API 显示 NOASSERTION，raw LICENSE 核读为 MIT，开头为腾讯"pleased to support the open source community"声明），Python，4,621 stars，创建 2025-08-21，最近 push 2026-03-21（约 6 个月无提交） — [GitHub API](https://github.com/TencentCloudADP/youtu-agent)；[LICENSE](https://raw.githubusercontent.com/TencentCloudADP/youtu-agent/main/LICENSE)
- Youtu-Agent 2025-09-02 开源，2026-01-17 宣布支持 Agent Skills（搜索摘要） — [腾讯新闻](https://news.qq.com/rain/a/20250902A053CS00)；[Youtu-Agent GitHub](https://github.com/TencentCloudADP/youtu-agent)
- WeKnora（腾讯）：仓库 Tencent/WeKnora，Go，32,085 stars，创建 2025-07-22，最近 push 2026-10-01；GitHub API 标 NOASSERTION，raw LICENSE 核读为 MIT + 第三方组件声明（Apache-2.0 51 个、BSD 49 个等）；自述"turn raw documents into a queryable RAG, an autonomous reasoning agent, and a self-maintaining Wiki"，官网 weknora.weixin.qq.com — [GitHub API](https://github.com/Tencent/WeKnora)；[LICENSE](https://raw.githubusercontent.com/Tencent/WeKnora/main/LICENSE)
- Tencent/openclaw-weixin（腾讯官方微信通道插件）：MIT（raw LICENSE 核读，版权年 2026），TypeScript，912 stars，创建 2026-03-27，最近 push 2026-09-21 — [GitHub API](https://github.com/Tencent/openclaw-weixin)；[LICENSE](https://raw.githubusercontent.com/Tencent/openclaw-weixin/main/LICENSE)
- 2026 年 3 月腾讯发布官方微信插件 @tencent-weixin/openclaw-weixin，基于官方 iLink 协议，与微信 PC 端同一授权机制，未见封号案例；而第三方协议接入个人微信"封号风险极高"（搜索摘要） — [实在智能](https://www.ai-indeed.com/encyclopedia/15436.html)；[openclawgithub.cc 微信指南](https://openclawgithub.cc/guide/channels/wechat/)
- QClaw（腾讯电脑管家）：2026-03-09 邀请制内测，3-20 全量公测，"20 秒安装"，打通企业微信、QQ、飞书、钉钉远控通道，可直连微信，内置 Kimi/MiniMax/GLM/DeepSeek；为基于 OpenClaw 的本地一键启动包，macOS/Windows 双平台（搜索摘要）；**GitHub 未见 QClaw 源码仓库（in:name 搜索 0 结果），按闭源处理** — [IT之家](https://www.ithome.com/0/927/143.htm)；[36氪实测](https://www.36kr.com/p/3718430511167106)
- WorkBuddy（腾讯云）：2026-03-09 上线，基于 OpenClaw 架构的商用桌面 Agent 工作台，内置 20+ Skills 与 MCP，闭源，宣称月活 120 万+（搜索摘要） — [IT之家](https://www.ithome.com/0/927/230.htm)；[腾讯云开发者社区](https://cloud.tencent.com/developer/article/2654425)

**A3. 智谱 / MiniMax / Kimi / 百度**

- Open-AutoGLM（智谱）：仓库 zai-org/Open-AutoGLM，Apache-2.0，Python，26,344 stars / 4,047 forks，创建 2025-12-08，最近 push 2026-03-06（约 7 个月无提交），自述 "An Open Phone Agent Model & Framework"（即 Agent 操控**手机**，而非电脑）；社区衍生 Open-AutoGLM-Hybrid（846 stars，"在手机上运行 AI 自动化，无需电脑"）、Open-AutoGLM-Android（375 stars） — [GitHub API](https://github.com/zai-org/Open-AutoGLM)；[Hybrid](https://github.com/xietao778899-rgb/Open-AutoGLM-Hybrid)
- AutoClaw（智谱"澳龙"）：2026-03-10 上线，定位"国内首个真·一键安装的本地版 OpenClaw"，预置 50+ Skills，集成 Pony-Alpha-2 与 AutoGLM，应用免费、可自带任意模型 API；搜索结果未见其源码开源的明确说明（搜索摘要）；注意 GitHub 上 tsingliuwin/autoclaw（280 stars，Docker 内无头 Agent）与智谱产品无关 — [界面新闻](https://www.jiemian.com/article/14095547.html)；[OSCHINA](https://www.oschina.net/news/409053)；[tsingliuwin/autoclaw](https://github.com/tsingliuwin/autoclaw)
- 网易 163 文章把"智谱 AutoClaw"列为"国产开源 OpenClaw 平替"（搜索摘要），与上条"未见开源说明"相冲突，应以"未核实开源"处理 — [163](https://www.163.com/dy/article/KR9KGP630556L3O4.html)
- MiniMax：Mini-Agent（MiniMax-AI/Mini-Agent）MIT，Python，3,046 stars，创建 2025-10-31，最近 push 2026-02-14，自述"minimal yet professional single agent demo"；minimax-code（MiniMax-AI/minimax-code）MIT，TypeScript，1,968 stars，创建 2026-06-01，最近 push 2026-10-05，为终端 coding agent — [Mini-Agent](https://github.com/MiniMax-AI/Mini-Agent)；[minimax-code](https://github.com/MiniMax-AI/minimax-code)
- MiniMax MaxClaw 为 2026 年 3 月上线的云端一键部署产品；某腾讯云社区文章称其"开源、GitHub 8.7 万 star"（搜索摘要），但 GitHub `MaxClaw in:name` 搜索仅见第三方 Lichas/maxclaw（230 stars，Go 语言 OpenClaw 风格 Agent）等，**未找到 MiniMax 官方仓库，该"开源"说法不可信** — [腾讯云社区](https://cloud.tencent.com/developer/article/2654425)；[Lichas/maxclaw](https://github.com/Lichas/maxclaw)
- Kimi（月之暗面）：kimi-cli 已归档（"Legacy Python Kimi CLI, no longer maintained"，11,433 stars）；替代为 MoonshotAI/kimi-code，MIT，TypeScript，7,772 stars，创建 2026-05-22，最近 push 2026-10-02 — [kimi-cli](https://github.com/MoonshotAI/kimi-cli)；[kimi-code](https://github.com/MoonshotAI/kimi-code)
- KimiClaw：2026-02-18 上线，Kimi 内一键创建的云端 OpenClaw，支持 Web/iOS/Android（搜索摘要，非开源） — [百度百科 KimiClaw](https://baike.baidu.com/item/KimiClaw/67484172)；[36氪 Kimi Claw 实测](https://www.36kr.com/p/3702801991970948)
- 百度：针对"百度 开源 Agent 框架 2026"的搜索未返回任何百度开源通用 Agent/数字员工框架；百度秒哒、千帆 AgentBuilder 在盘点中均作为"零代码/云平台"闭源产品出现（搜索摘要） — [搜狐 AI 数字员工平台盘点](https://www.sohu.com/a/1046136277_122553831)

**A4. OpenClaw 及其中文衍生生态**

- OpenClaw：仓库 openclaw/openclaw，MIT，TypeScript，391,407 stars / 82,271 forks / 9,350 open issues，创建 2025-11-24，最近 push 2026-10-05；官方技能/插件注册表 openclaw/clawhub（MIT，9,486 stars）；社区列表 VoltAgent/awesome-openclaw-skills 自称收录 "5,400+ skills"（52,956 stars） — [openclaw](https://github.com/openclaw/openclaw)；[clawhub](https://github.com/openclaw/clawhub)；[awesome-openclaw-skills](https://github.com/VoltAgent/awesome-openclaw-skills)
- 命名史：Warelay（2025-11-24）→ CLAWDIS（2025-12-03）→ Clawdbot（2026-01-02）→ Moltbot（2026-01-27，因 Anthropic 律师函称与 Claude 读音相近）→ OpenClaw（2026-01-30）；作者 Peter Steinberger（奥地利，PSPDFKit 创始人），后宣布加入 OpenAI，项目转社区驱动（搜索摘要） — [WenHaoFree](https://blog.wenhaofree.com/posts/articles/2026-01-30-clawdbot-openclaw-rebrand/)；[36氪](https://eu.36kr.com/zh/p/3667047170044420)；[知乎](https://zhuanlan.zhihu.com/p/2010325307371066774)
- openclaw-china（BytePioneer-AI）：3,964 stars，TypeScript，创建 2026-01-28，最近 push 2026-06-12；README（直接核读）：MIT 徽章；支持钉钉、飞书、QQ 机器人、企业微信（智能机器人/自建应用/微信客服三种）、微信公众号；个人微信经官方插件；安装 `npx @openclaw-china/setup` 或 `openclaw plugins install @openclaw-china/channels` — [GitHub API](https://github.com/BytePioneer-AI/openclaw-china)；[README](https://raw.githubusercontent.com/BytePioneer-AI/openclaw-china/main/README.md)
- LobsterAI / 有道龙虾（网易有道）：仓库 netease-youdao/LobsterAI，MIT，TypeScript，6,083 stars / 973 forks / 515 open issues，创建 2026-02-12，最近 push 2026-10-04，官网 lobsterai.youdao.com；描述"Built on OpenClaw; runs tools on your real desktop and takes commands from your phone via WeChat, Feishu, DingTalk & Telegram" — [GitHub API](https://github.com/netease-youdao/LobsterAI)
- LobsterAI README_zh（直接核读）：分层架构，Cowork 为产品层（桌面持久化、权限、UI 状态），OpenClaw 为底层运行时与网关；Electron 43 + React 18，macOS/Windows；IM 远控支持微信、钉钉、飞书、QQ、Telegram、Discord；28 个可配置 Skills（Word/Excel/PPT/PDF/Remotion 视频/浏览器自动化/邮件/搜索）；定时任务可用自然语言或 UI 设置；记忆为本地 SQLite + MEMORY.md/USER.md/SOUL.md；渲染进程 context isolation / nodeIntegration 禁用 / sandbox 开启；MCP 支持 — [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)
- 有道龙虾 2026-02-11 推出、02-19 宣布开源，上线首月访问量 27 万+，进入"OpenClaw 生态流量榜龙虾 Agent 分类榜"前五，与腾讯 WorkBuddy、MiniMax MaxClaw 并列（搜索摘要） — [京报网](https://news.bjd.com.cn/2026/03/18/11637393.shtml)；[InfoQ](https://www.infoq.cn/article/X7syAFUA3FkTim8aI14z)
- nanobot（香港大学 HKUDS）：仓库 HKUDS/nanobot，MIT，Python，48,790 stars / 8,608 forks，创建 2026-02-01，最近 push 2026-10-05，官网 nanobot.wiki；自述含 WebUI、tools、memory、MCP、multi-agent workflows、automation、chat apps — [GitHub API](https://github.com/HKUDS/nanobot)
- nanobot 代码约 4,000 行（相较 Clawdbot 43 万行精简 99%），支持 Telegram/Discord/WhatsApp/飞书/钉钉/Slack/Email/QQ，定时任务、持久记忆、Skills（搜索摘要） — [腾讯云社区](https://cloud.tencent.com/developer/article/2637631)；[宝玉 X](https://x.com/dotey/status/2019468065310740597)
- Hermes Agent（Nous Research，美国；非国产但中文社区大量采用）：NousResearch/hermes-agent，251,286 stars / 48,014 open issues，创建 2025-07-22，最近 push 2026-10-05；**许可证未核实** — [GitHub API](https://github.com/NousResearch/hermes-agent)
- Hermes Agent 原生支持 Windows/macOS/Linux/WSL2/Android，可接入 20+ 消息平台含微信、钉钉、飞书、Telegram；中文文档同步至 v2026.8.3（搜索摘要） — [飞书官网教程](https://www.feishu.cn/content/article/7630758640865037530)；[Hermes 中文社区](https://hermesai.top/)
- 蚂蚁集团与清华大学联合开源面向 OpenClaw 的安全防御插件 ClawAegis（搜索摘要）；GitHub `ClawAegis in:name` 仅见第三方测试仓库，官方仓库位置未核实 — [OSCHINA](https://www.oschina.net/news/417115)
- 闭源国产 Claw 类产品（仅作参照）：ArkClaw（字节火山引擎，2026-03-09，纯云端 SaaS，飞书集成，Doubao-Seed-2.0/Kimi/MiniMax/GLM）— [IT之家](https://www.ithome.com/0/927/235.htm)；StepClaw/阶跃 AI 桌面伙伴（2026-03-19，Win/Mac，无需 Docker/命令行/API Key）— [163](https://www.163.com/dy/article/KR9KGP630556L3O4.html)；AstronClaw（科大讯飞，2026-03-12，云端沙箱，接企业微信/钉钉/飞书，Skills 市场 120+，首购 16.8 元/月）— [IT之家](https://www.ithome.com/0/928/475.htm)；Molili/莫哩哩（当贝，2026 年 1 月，一体化客户端，"未开源"）— [ZNDS](https://www.znds.com/tv-1267835-1-1.html)；[百度百科](https://baike.baidu.com/item/%E5%BD%93%E8%B4%9D%20Molili/67487578)
- "TopClaw" 在掘金有多篇软文称"安装量 5000 万+、安全性能排名第一"，但 GitHub 同名仓库均为 0 star，宣传数字无法核实 — [掘金](https://juejin.cn/post/7663396987305164800)；[topway-ai/topclaw](https://github.com/topway-ai/topclaw)

**A5. IM 机器人框架、硬件与企业平台**

- LangBot：仓库 langbot-app/LangBot，Apache-2.0，Python，18,010 stars，创建 2022-12-07，最近 push 2026-10-03；描述"生产级多平台智能机器人开发平台"，通道含 Discord/Slack/LINE/Telegram/WeChat(企业微信、企微智能机器人、公众号)/飞书/钉钉/QQ/Matrix，可对接 Dify、n8n、Langflow、Coze、openclaw、hermes agent、deerflow — [GitHub API](https://github.com/langbot-app/LangBot)
- AstrBot：仓库 AstrBotDevs/AstrBot，**AGPL-3.0**，Python，41,413 stars / 1,610 open issues，创建 2022-12-08，最近 push 2026-10-05，官网 astrbot.app，描述自称"can be your openclaw alternative"；配套桌面启动器 AstrBotDevs/astrbot-launcher（Rust，MIT，1,517 stars，创建 2026-02-12） — [AstrBot](https://github.com/AstrBotDevs/AstrBot)；[astrbot-launcher](https://github.com/AstrBotDevs/astrbot-launcher)
- AstrBot 作者为北邮 00 后 UP 主 Soulter，2023 年 1 月即发布首版；2026 年 3 月达 2.6 万 star、234 位贡献者、"1000+ 插件一键安装"；定位更偏 Agent 编排/IM 机器人框架而非开箱即用个人助理（搜索摘要） — [搜狐](https://www.sohu.com/a/999600125_473283)
- xiaozhi-esp32（小智）：仓库 78/xiaozhi-esp32，MIT，C++，30,417 stars / 7,128 forks，创建 2024-08-31，最近 push 2026-10-02，官网 xiaozhi.me，自述"An MCP-based chatbot"；后端 xinnan-tech/xiaozhi-esp32-server（MIT，10,731 stars，最近 push 2026-09-29） — [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)；[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)
- MaxKB（飞致云）：仓库 1Panel-dev/MaxKB，**GPL-3.0**，Python，22,899 stars，创建 2023-09-14，最近 push 2026-10-04，默认分支 v2，自述"企业级智能体平台" — [GitHub API](https://github.com/1Panel-dev/MaxKB)
- Bisheng（数据项素）：仓库 dataelement/bisheng，Apache-2.0，Python，12,022 stars，创建 2023-08-28，最近 push 2026-09-30，自述"open LLM devops platform"含 workflow/RAG/Agent/SFT/评测 — [GitHub API](https://github.com/dataelement/bisheng)
- Dify：仓库 langgenius/dify，157,865 stars，最近 push 2026-10-05；LICENSE（直接核读）为"修改版 Apache 2.0"，附加条件：未经书面授权不得用于运营多租户环境（一租户=一工作区）；不得移除或修改控制台/应用中的 LOGO 与版权信息（仅限前端 web/ 目录）；贡献者同意许可条款可调整且贡献可商用 — [GitHub API](https://github.com/langgenius/dify)；[LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)
- StaffDeck（面壁智能/OpenBMB 等）：仓库 OpenBMB/StaffDeck，**AGPL-3.0**，Python，1,966 stars / 339 forks，创建 2026-07-13，最近 push 2026-09-29，官网 staffdeck.openbmb.cn — [GitHub API](https://github.com/OpenBMB/StaffDeck)
- StaffDeck README（直接核读）：由 ModelBest（面壁）、东北大学-面壁数据智能联合实验室、THUNLP、OpenBMB、AI9Stars 联合开发，2026-07-15 开源；提供 macOS（arm64/x86_64 .dmg）、Windows x64 .exe、Linux .deb 安装包；数字员工含岗位、工号、能力边界；状态机驱动 SOP（自然语言生成、可视化编辑、版本控制）；知识库分层索引；通过 HTTP API、MCP、定时任务自主执行；完整执行 Trace 与人工接管；IM 通道为微信（iLink 协议）与企业微信（bot WebSocket）；凭证 Fernet 加密；仅需 OpenAI 兼容端点，无需本地 GPU — [StaffDeck README](https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.md)
- StaffDeck Preview 内置财务报销、法务合规、人力服务、IT 支持、行政管理 5 个现成数字员工；支持完全私有化部署，面向国企、金融等强合规机构（搜索摘要） — [量子位](https://www.qbitai.com/2026/07/453245.html)；[掘金](https://juejin.cn/post/7675623932943401023)
- OpenOcta：仓库 openocta/openocta，**GPL-3.0**，TypeScript，3,185 stars，创建 2026-02-26，最近 push 2026-09-16，自述"open-source AIOps Agent installed on Windows & macOS"（SSH/SFTP/DBA/DevOps 场景）；第三方技能库 openocta/openocta_skills（166 stars） — [GitHub API](https://github.com/openocta/openocta)；163 文章称其原生支持飞书、钉钉、企微并提供 LLM Trace 与 Token 采集（搜索摘要） — [163](https://www.163.com/dy/article/KR9KGP630556L3O4.html)
- Cherry Studio：CherryHQ/cherry-studio，**AGPL-3.0**，52,370 stars，最近 push 2026-10-05，topics 含 hermes-agent、agent-skills、claude-code — [GitHub API](https://github.com/CherryHQ/cherry-studio)
- MindX（"仿生大脑"本地 AI 助理）：托管于 GitCode（MSCSharp/mindx），搜索摘要称 MIT 并"Token 消耗降 90%、支持飞书/微信/钉钉/QQ"；GitHub `user:MSCSharp` 搜索 0 结果，star 数未核实 — [GitCode](https://gitcode.com/MSCSharp/mindx)；[博客园](https://www.cnblogs.com/Ray-liang/p/19626557)

### Inferences
- 从 star 增速看，2026 年国产开源"通用 Agent"的注意力已从 2025 年的 OpenManus/JoyAgent（后者已停更约 8 个月）转向 DeerFlow 2.0（8.3 万）、QwenPaw（3.5 万，仅 7 个月）、nanobot（4.9 万）与 AstrBot（4.1 万）。
- 大厂"Claw 类"产品分两条路线：开源（QwenPaw、LobsterAI、StaffDeck）与闭源封装 OpenClaw（QClaw、WorkBuddy、AutoClaw、ArkClaw、KimiClaw、StepClaw、Molili、AstronClaw）；后者数量更多但不满足"真正开源"的收录条件。
- 许可证上，对于希望二次封装成商业"数字员工"产品的团队，Apache-2.0/MIT 项目（QwenPaw、LobsterAI、DeerFlow、Coze Studio、Bisheng、Eigent、nanobot、LangBot）风险最低；AGPL（AstrBot、StaffDeck、Cherry Studio）与 GPL（MaxKB、OpenOcta）要求网络服务/分发时开源衍生代码；Dify 另有多租户与 Logo 条款。

### Gaps
- Hermes Agent 的许可证未核实（未抓取 LICENSE）。
- 智谱 AutoClaw、腾讯 QClaw、WorkBuddy、MiniMax MaxClaw、字节 ArkClaw、阶跃 StepClaw、当贝 Molili 均未找到官方源码仓库，按"非开源"处理，但无法排除后续开源。
- OWL 仓库 LICENSE 文件在 main 分支缺失（404），仅 README 声明 Apache 2.0；若需法律确认应向维护者核实。
- 百度在 2026 年是否有开源的通用 Agent/数字员工框架：未找到任何来源，视为"无"。
- MindX 的 star 数与活跃度无法核实（GitCode 页面无法抓取）。
- 各项目"是否有手机端 App"：仅 QwenPaw（Tauri 桌面 Beta，无手机 App）、LobsterAI（桌面 + IM）、StaffDeck（桌面安装包 + 微信/企微）明确；其余开源项目均未发现独立手机 App，手机侧一律通过 IM 实现。

## 关键问题 2：哪些国产开源项目在"本地执行 + 桌面客户端 + IM 通道 + 技能市场"上最接近灵策智算？

### Takeaway
最接近灵策智算产品画像的国产开源项目是 **LobsterAI（有道龙虾）**、**QwenPaw（原 CoPaw）** 与 **StaffDeck**：三者都提供桌面安装包、本地执行、国内 IM 远控与 Skills 体系；LobsterAI 偏个人办公交付（Word/Excel/PPT/视频），QwenPaw 偏"个人助理 + 安全护栏"，StaffDeck 则最贴近"企业数字员工封装 + SOP + 审计 + 私有化"。DeerFlow 2.0 在沙箱、权限、IM 网关上企业化程度最高但缺桌面客户端；Eigent 有桌面端与多 Agent 但缺国内 IM 通道。

### Cited Findings
- 对照维度逐项（来源见关键问题 1 各条）：
  - **桌面客户端**：LobsterAI（Electron，macOS/Windows）— [README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)；QwenPaw（Tauri Beta，Win10+/macOS14+）— [README](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README.md)；StaffDeck（.dmg/.exe/.deb）— [README](https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.md)；Eigent（Electron，三平台）— [README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)；AstrBot 仅有图形化启动器 — [astrbot-launcher](https://github.com/AstrBotDevs/astrbot-launcher)；DeerFlow、nanobot、LangBot 为 Web/Docker 形态 — [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
  - **国内 IM 通道（飞书/企微/钉钉）**：QwenPaw（DingTalk/Lark/WeChat/QQ/…）、LobsterAI（微信/钉钉/飞书/QQ/Telegram/Discord）、DeerFlow（Feishu/WeChat/WeCom/QQ/DingTalk 等 8 个）、nanobot（飞书/钉钉/QQ 等）、LangBot（企业微信/飞书/钉钉/QQ…）、StaffDeck（微信 iLink + 企业微信）；Eigent 未列 IM 通道 — 同上各 README
  - **技能市场**：QwenPaw 内置 Skills 与插件市场；LobsterAI 28 个内置 Skills；DeerFlow 的 skills 框架含渐进加载与 `allowed-tools`；AstrBot "1000+ 插件"；OpenClaw 生态 ClawHub/awesome 列表 5,400+ skills — 同上
  - **安全/审批/审计**：QwenPaw 列出 Kernel-level Sandbox、Tool Guard、File Guard、Skill Scanner、Access Policy 与可配置审批级别；DeerFlow 有线程级权限、按 skill 的 allowed-tools、Docker/K8s 文件系统隔离、LangSmith/Langfuse 追踪；StaffDeck 有完整执行 Trace 与人工接管、Fernet 加密凭证；LobsterAI 强调本地 SQLite 存储、工作目录授权与沙箱（后者来自媒体摘要） — [QwenPaw README](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README.md)；[DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)；[StaffDeck README](https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.md)；[京报网](https://news.bjd.com.cn/2026/03/18/11637393.shtml)
  - **定时任务与记忆**：QwenPaw（cron + 自进化 Markdown 知识库）、LobsterAI（自然语言定时任务 + MEMORY.md 等）、DeerFlow（cron + 长期记忆/语义召回）、nanobot（定时任务 + 持久记忆）、StaffDeck（定时任务 + 长期记忆 + 版本演进） — 同上
  - **数字员工封装 / 团队权限 / 私有化**：StaffDeck 以"岗位、工号、能力边界、SOP、知识库、Trace"建模数字员工，市场资源有创建者/管理员权限控制，支持完全私有化；DeerFlow 推荐 Docker Compose/K8s 私有部署；Eigent 企业版提供 SSO 与访问控制 — [StaffDeck README](https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.md)；[DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)；[Eigent README](https://raw.githubusercontent.com/eigent-ai/eigent/main/README.md)
  - **办公交付物（Word/PPT/Excel/视频）**：LobsterAI 明确内置 Word/Excel/PPT/PDF/Remotion 视频技能；DeerFlow 媒体报道称内置写研报/建网站/做 PPT/生成视频技能（搜索摘要）；JoyAgent 开源报告/PPT/文件子智能体 — [LobsterAI README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)；[Daipi2020/DeerFlow-2.0 转述](https://github.com/Daipi2020/DeerFlow-2.0)；[博客园 JoyAgent](https://www.cnblogs.com/Jcloud/p/19013061)
- 阿里云开发者社区文章将"无 OpenClaw 内核依赖、纯国产开源、本地部署、源码可审计"的平替列为 5 款，覆盖个人/小团队/企业三类场景（搜索摘要，列表原文未抓取） — [阿里云开发者社区](https://developer.aliyun.com/article/1716936)
- 博客园对比文"openclaw、LobsterAI、CoPaw、molili 比较"把四者并列为 2026 年主要选择（搜索摘要） — [博客园](https://www.cnblogs.com/zxh1979/p/19702893)

### Inferences
- 若以灵策智算的"桌面客户端 + 手机远控 + 技能市场 + 本地执行 + 审批审计 + 数字员工封装 + 私有化"七项打分，没有单一开源项目全部覆盖：LobsterAI 和 QwenPaw 覆盖前五项，StaffDeck 覆盖后四项（IM 仅微信/企微，缺飞书/钉钉），DeerFlow 覆盖除桌面客户端外的大部分服务端能力。
- LobsterAI 与 QwenPaw 都依赖 OpenClaw 或自研网关实现 IM 远控，意味着它们与灵策智算一样把"手机 → IM → 本地网关 → 执行 → 回传"作为核心交互路径。
- 灵策智算的"轻系统（自然语言生成看板/表单/台账）"与"团队 Skill 库共享"在上述开源项目中仅 StaffDeck 的"市场资源权限"部分接近，属于差异化点。

### Gaps
- 未能逐一核实各项目 Skills 的精确数量（QwenPaw 未给出数字；LobsterAI 为 28）。
- "数据不出本机"在开源项目中多为架构描述，未见第三方安全审计报告。

## 关键问题 3：中文社区 2026 年公认的"开源数字员工/本地 AI 助理"头部项目及其评价与争议

### Takeaway
2026 年中文社区（博客园、阿里云/腾讯云开发者社区、掘金、知乎、53AI、36氪、InfoQ、OSCHINA）的"OpenClaw 国产平替"榜单高度集中于：开源侧 **CoPaw/QwenPaw、LobsterAI、nanobot、AstrBot、DeerFlow、StaffDeck、MindX/OpenOcta**，闭源侧 **QClaw、AutoClaw、ArkClaw、KimiClaw、StepClaw、AstronClaw、WorkBuddy、MaxClaw、Molili**。争议集中于三点：(1) 2026 年 3 月国家互联网应急中心、工信部漏洞平台、新华网接连发布 OpenClaw 风险提示并出现高校"禁虾"；(2) 云端版 Claw 的数据不出境/不出本机问题；(3) Token 费用与稳定性。

### Cited Findings

**榜单与盘点（按来源）**
- 博客园《2026 OpenClaw 国产替代品排行榜：谁才是"本土龙虾"第一梯队》《2026 最新实测 9 款国产 OpenClaw（龙虾）平替榜单》《全网最全丨OpenClaw 国产平替清单 云端部署首选科大讯飞 AstronClaw》（原文被拦截，仅标题可证） — [cnblogs/tutudatu](https://www.cnblogs.com/tutudatu/p/20804688)；[cnblogs/tzmm 9 款](https://www.cnblogs.com/tzmm/p/21273553)；[cnblogs/tzmm AstronClaw](https://www.cnblogs.com/tzmm/p/21006247)
- 阿里云开发者社区《禁虾潮退场，国产开源接棒：5 款纯国产 OpenClaw 平替技术实测与选型指南》把 StepClaw、LobsterAI、MindX、AutoClaw、CoPaw 列为代表，并总结"均支持本地部署、源码公开可审计"（搜索摘要；注意 StepClaw/AutoClaw 实际未见源码，该文表述需打折） — [阿里云开发者社区](https://developer.aliyun.com/article/1716936)
- 腾讯云开发者社区《告别命令行：7 款国产 OpenClaw 平替工具横评指南》《推荐 7 个 yyds 的 OpenClaw 轻量级平替 GitHub 项目》《2026 国产 OpenClaw 生态全景指南》（原文被拦截） — [腾讯云 7 款横评](https://cloud.tencent.com/developer/article/2690607)；[腾讯云 7 个 GitHub 项目](https://cloud.tencent.com/developer/article/2639499)；[腾讯云生态全景](https://cloud.tencent.com/developer/article/2654425)
- 掘金《OpenClaw（龙虾）国产平替全盘点：8 款主流 AI 智能体横向评测》《国产 AI Agent 横评：腾讯 QClaw、阿里 CoPaw、字节 ArkClaw、智谱 AutoClaw》《2026 年国产化 AI 智能体落地指南：8 款主流 OpenClaw 产品能力评测与选型攻略》；其摘要结论：OpenClaw 综合能力（工具调用、浏览器控制）第一，ArkClaw 为字节生态最佳，QClaw 为企业场景首选（搜索摘要） — [掘金 8 款横评](https://juejin.cn/post/7649595392360333362)；[掘金 四大厂横评](https://juejin.cn/post/7653060452369580073)；[掘金 信创选型](https://juejin.cn/post/7675246437878841359)
- 36氪《OpenClaw 光速国产化，大厂出的"龙虾"到底哪个最好用？》摘要：AutoClaw 最没门槛（无命令行、无 API 配置）；QClaw 杀手锏是直连微信；ArkClaw 是深度适配飞书的云端 SaaS；MaxClaw 主打专家团队与子 Agent；并指出"安全问题没完全解决、稳定性不够、几乎每款都会遇到 bug/卡死/中断，Token 消耗是痛点"；专为龙虾设计的 PinchBench 榜中 MiniMax M2.1 成功率 93.6%、Kimi K2.5 93.4%（搜索摘要） — [36氪](https://36kr.com/p/3720642158213765)；[36氪 国产大模型霸榜](https://36kr.com/p/3718423332222342)
- 53AI《OpenClaw 平替产品全景对比：2026 年 20+ AI Agent 工具深度评测》（原文被拦截） — [53AI](https://www.53ai.com/news/Openclaw/2026030306512.html)
- 知乎《2026 年五大硬核标杆开源 AI 项目解析：OpenClaw、Hermes Agent、DeerFlow、Kronos、AutoGPT》与《2026 年类龙虾（OpenClaw）物种分析报告》（原文被拦截） — [知乎 五大](https://zhuanlan.zhihu.com/p/2030997957277958981)；[知乎 物种报告](https://zhuanlan.zhihu.com/p/2014661147878515883)
- 搜狐《2026 AI 数字员工开发平台盘点》把 Dify（插件生态 8000+）、扣子 Coze（2025-07 开源，2026-06 迭代 3.0）、DeerFlow 2.0（"数字员工平台"）、OpenClaw（Gateway 模式）、StaffDeck（私有化 + 定时任务 + MCP）列为 2026 关键开源项目；并称全球 AI 数字员工市场 2025 年 335 亿元→2026 年 469 亿元（搜索摘要，口径未核实） — [搜狐](https://www.sohu.com/a/1046136277_122553831)
- InfoQ 中文站 2026 年报道：《从狂热到工程、组织实践，OpenClaw 这阵风能刮多久？》《通义实验室推出 CoPaw》《网易有道 LobsterAI 开源》《OpenClaw 2.0 发布：简化配置，支持智能体协作》《Seal：小红书企业级 AI 个人助理的从 0 到全员覆盖》（小红书基于 OpenClaw 自建 Seal，企业级安全隔离）（搜索摘要） — [InfoQ 刮多久](https://www.infoq.cn/article/Th0um55qt9mMORoZ7MD8)；[InfoQ OpenClaw 2.0](https://www.infoq.cn/article/hOJ5r8sQvQsGvm0KNHTd)；[InfoQ Seal](https://www.infoq.cn/article/gF4722XXWC2qLhZx6Qke)
- OSCHINA 2026 年报道：《国内通用智能体（本地操作型 Agent）深度测评对比》《Hermes Agent 与 OpenClaw：开源 AI 智能体的两种设计哲学》《智谱发布 AutoClaw》《腾讯 QClaw 正式上线，可与微信互联》（搜索摘要） — [OSCHINA 测评](https://my.oschina.net/u/9753726/blog/19686397)；[OSCHINA Hermes vs OpenClaw](https://www.oschina.net/news/419024)
- Gitee GVP（最有价值开源项目）截至 2026 年 6 月累计评出 466+ 项目；AI Agent 相关入选含 Snail AI（企业级 AI Agent 平台）、Agents-Flex（Java Agent 框架，支持 RAG/MCP/Skills）、AIFlowy（Java 企业级 AI 应用平台，对标 Dify/Coze，3,433 stars）、qKnow、Fay（搜索摘要） — [Gitee GVP](https://gitee.com/gvp)；[掘金 Gitee 推荐体系](https://juejin.cn/post/7675992325911642131)
- "2025 开源中国年终评选"获奖侧重 AI 编程与低代码（文心快码、HMOS Code Workshop/CodeGenie、LazyLLM、OpenTeleDB、CodeBuddy），未见"数字员工/本地助理"专项（搜索摘要） — [OSCHINA 年终评选](https://my.oschina.net/u/4806939/blog/19133052)

**争议点 1：安全与"禁虾潮"**
- 工信部网络安全威胁和漏洞信息共享平台 2026-03-08 紧急预警；国家互联网应急中心 03-10 发布《关于 OpenClaw 安全应用的风险提示》，指出 OpenClaw（曾用名 Clawdbot、Moltbot）"国内主流云平台均提供一键部署"，风险含提示词注入（恶意网页诱导泄露系统密钥）与误操作（删除邮件/核心生产数据），建议严格管理插件来源、禁用自动更新、仅安装签名验证扩展（搜索摘要） — [腾讯新闻](https://news.qq.com/rain/a/20260310A0753G00)；[新华网 CNCERT](https://www.news.cn/tech/20260310/959f13d18edb4759ae031a5e30523d23/c.html)；[观察者网](https://www.guancha.cn/industry-science/2026_03_10_809530.shtml)
- 新华网 2026-03-12 发布《关于防范 OpenClaw（"龙虾"）开源智能体安全风险的"六要六不要"建议》；工信部专家呼吁党政机关、企事业单位和个人审慎使用（搜索摘要） — [新华网](https://www.news.cn/tech/20260312/87381bafd60b4c59b64a8a0f3bb92536/c.html)；[新浪财经](https://finance.sina.com.cn/wm/2026-03-11/doc-inhqqupt9468131.shtml)
- 国内多所高校要求防范 OpenClaw 安全风险，有学校要求"立即卸载清除"（搜索摘要） — [安全内参](https://www.secrss.com/articles/88431)；[证券时报](https://www.stcn.com/article/detail/3673454.html)
- 社区文章引用的数字（搜索摘要，原始口径无法核实）：GitHub 安全审计发现 OpenClaw 512 个漏洞、8 个严重级；ClawHub 3,016 个插件中 336 个（10.8%）含恶意代码；"数万台部署 AI 的电脑直接暴露公网" — [知乎问答](https://www.zhihu.com/question/2014051332676347147)；[53AI 安全养虾](https://www.53ai.com/news/Openclaw/2026031229764)
- 36氪《"龙虾"入笼：为何金融行业不敢"养"？》与电子工程专辑《从万元账单到安全裸奔》报道了金融业合规顾虑与用户"万元 Token 账单"（搜索摘要） — [36氪 金融](https://36kr.com/p/3736923854602496)；[电子工程专辑](https://www.eet-china.com/news/202603106247.html)
- 蓝鲸财经报道"500 元上门安装 OpenClaw"的中间人经济，阿里云、火山引擎、腾讯云、百度智能云、京东云均推出 OpenClaw 云服务器一键部署（搜索摘要） — [蓝鲸财经](https://www.lanjinger.com/d/1772766680151033575)；[CSDN 三家云对比](https://blog.csdn.net/interpromotion/article/details/157943503)

**争议点 2：私有化/数据边界**
- 163 文章把国产平替按"开源（CoPaw、LobsterAI）/ 云端 SaaS（ArkClaw、KimiClaw）/ 桌面端（StepClaw）"分类，并归纳原版 OpenClaw 五大痛点：部署门槛高、海外链路合规风险、本地化弱、Token 消耗大、记忆缺陷（搜索摘要） — [163](https://www.163.com/dy/article/KR9KGP630556L3O4.html)
- 个人微信接入：非官方协议"极易触发封号"，企业微信是"最稳定、安全、官方支持"的替代；腾讯官方 iLink 插件无封号案例（搜索摘要） — [实在智能](https://www.ai-indeed.com/encyclopedia/15436.html)

**争议点 3：商用许可**
- MaxKB 为 GPLv3：商业闭源项目不能直接集成；GPL 允许商用但分发时必须开源（搜索摘要） — [Gitee MaxKB](https://gitee.com/fit2cloud-feizhiyun/MaxKB)；[博客园 开源协议商用解读](https://www.cnblogs.com/nuccch/p/18438350)
- Dify 许可证附加"不得多租户运营、不得移除 Logo"（直接核读） — [Dify LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)
- AstrBot、StaffDeck、Cherry Studio 为 AGPL-3.0；OpenOcta 为 GPL-3.0（GitHub API） — [AstrBot](https://github.com/AstrBotDevs/AstrBot)；[StaffDeck](https://github.com/OpenBMB/StaffDeck)；[OpenOcta](https://github.com/openocta/openocta)

### Inferences
- 中文社区的"头部"判断更多由媒体曝光与大厂背书驱动，而非 star：LobsterAI 仅 6 千 star 却被反复列入前排，nanobot/AstrBot star 更高但多被归为"轻量/IM 框架"。
- 2026 年 3 月的官方风险提示直接催生了"纯国产、无 OpenClaw 内核、源码可审计"的选型话语，这与灵策智算强调的"本地执行、数据不出本机、全链路审计"叙事一致，说明该定位有明确的市场需求背景。
- 社区评测普遍缺乏可复现的基准（除 PinchBench 模型榜），多为体验式打分，引用时应标注"主观评测"。

### Gaps
- 绝大多数榜单原文无法抓取，具体名次与打分表未能核实，只能确认标题与摘要中出现的项目名单。
- "512 个漏洞 / 336 个恶意插件"等数字的原始审计报告来源未找到。
- Gitee 2025 年度报告中是否有"数字员工/Agent"专项榜：未找到。

## 关键问题 4："手机远程操控电脑执行任务"的开源实现有哪些，成熟度如何？

### Takeaway
"手机一句话 → 电脑上 Agent 执行 → 结果回传手机/IM"在 2026 年已是开源主流形态，核心模式是**本地网关 + IM 通道**（OpenClaw gateway、DeerFlow message gateway、QwenPaw/LobsterAI/nanobot/Hermes/AstrBot/LangBot 的多通道接入），均无需公网 IP；国内通道依赖飞书/钉钉/企业微信/QQ 官方机器人，个人微信仅腾讯 iLink 官方插件被认为安全。OpenClaw 另提供 iOS/Android "节点 App" 把手机摄像头/定位/通知暴露给网关。语音硬件（小智 ESP32）与 Open Interpreter 01 属早期或已停更方案；RustDesk/Tailscale 仅解决远程桌面与组网，需与 Agent 组合。

### Cited Findings
- OpenClaw 教程大量出现"用手机远程指挥电脑""手机一句话控制电脑执行任务"主题：博客园《OpenClaw 快速上手教程：用手机远程指挥电脑，打造你的 24 小时 AI 管家》、知乎《Windows 本地搭建 AI Agent：OpenClaw + 飞书，手机一句话控制电脑执行任务》、CSDN《手机远程操控电脑！OpenClaw 对接飞书机器人完整分步教程》；安装流程为 Node.js → npm 全局安装 openclaw → 在飞书开放平台创建企业应用并开启机器人能力 → 绑定（搜索摘要） — [博客园](https://www.cnblogs.com/caituotuo/p/19684112)；[知乎 飞书](https://zhuanlan.zhihu.com/p/2001306188520911806)；[CSDN 飞书](https://adg.csdn.net/6a60732e662f9a54cb932911.html)
- OpenClaw 中国通道插件 openclaw-china 覆盖钉钉、飞书、QQ、企业微信、公众号，个人微信走官方插件（README 直接核读）；腾讯官方 Tencent/openclaw-weixin 为 MIT、2026-03-27 创建 — [openclaw-china README](https://raw.githubusercontent.com/BytePioneer-AI/openclaw-china/main/README.md)；[Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)
- OpenClaw 节点（Node）系统：手机安装 OpenClaw Node App（App Store/Play Store），在 Gateway 开启 Node Connect 后扫码配对；iOS 支持前后摄像头拍照、录像、截图、定位、通知（相机需前台）；Android 支持拍照/录屏/定位/读取通知、联系人、日历并执行设备命令（多数可后台）；支持双向文件传输；官方中文文档含"摄像头捕获"页；MarkTechPost 于 2026-06-29 报道 iOS/Android 伴侣节点应用发布（搜索摘要） — [OpenClaw 中文文档 摄像头](https://docs.openclaw.ai/zh-CN/nodes/camera)；[MarkTechPost](https://www.marktechpost.com/2026/06/29/openclaw-releases-ios-and-android-companion-node-apps-that-connect-a-phone-to-a-self-hosted-ai-agent-gateway/)；[阿里云社区 Node 实战](https://developer.aliyun.com/article/1718977)
- LobsterAI：桌面端常驻，"takes commands from your phone via WeChat, Feishu, DingTalk & Telegram"，README 列出微信/钉钉/飞书/QQ/Telegram/Discord 远控（直接核读）；有道技术博客另有 NAS 服务器部署指南 — [LobsterAI README_zh](https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md)；[有道技术博客](https://techblog.youdao.com/?p=3207)
- QwenPaw：DingTalk/Lark/WeChat/Discord/Telegram/iMessage/QQ 通道 + 本地或云端部署（直接核读） — [QwenPaw README](https://raw.githubusercontent.com/agentscope-ai/QwenPaw/main/README.md)
- DeerFlow：消息网关 8 平台"无需公网 IP"，IM 命令含 /new、/models、/agent、/memory；会话跨平台持久（直接核读） — [DeerFlow README](https://raw.githubusercontent.com/bytedance/deer-flow/main/README.md)
- nanobot：Telegram/Discord/WhatsApp/飞书/钉钉/Slack/Email/QQ；Hermes Agent：20+ 平台含微信/钉钉/飞书/Telegram，飞书机器人支持私聊/群聊/定时任务通知/文件附件，Windows 可用 PowerShell 一键安装（搜索摘要） — [腾讯云 nanobot](https://cloud.tencent.com/developer/article/2637631)；[阿里云 Hermes Windows](https://developer.aliyun.com/article/1725007)；[Freedidi Hermes Telegram](https://www.freedidi.com/23709.html)
- AstrBot/LangBot 作为 IM-first 框架可把企业微信/飞书/钉钉/QQ 消息路由到 openclaw / hermes agent / deerflow 等执行端（LangBot 仓库描述） — [LangBot](https://github.com/langbot-app/LangBot)
- StaffDeck：微信（iLink）与企业微信（bot WebSocket）通道，含意图路由与身份合并（直接核读） — [StaffDeck README](https://raw.githubusercontent.com/OpenBMB/StaffDeck/main/README.md)
- 闭源对照：QClaw 打通企业微信、QQ、飞书、钉钉"远控通道"并直连微信（搜索摘要）；KimiClaw 支持 Web/iOS/Android 跨端（搜索摘要） — [IT之家 QClaw](https://www.ithome.com/0/927/143.htm)；[搜狐 ArkClaw/KimiClaw 对比](https://m.sohu.com/a/994527698_121218495)
- Open Interpreter 01：openinterpreter/01 为 AGPL-3.0、5,156 stars，**最近 push 2024-11-01（已停更近两年）**；01-app（340 stars）为 iOS/Android 语音 App，经 WebRTC 连接家中电脑控制 Mac/Windows/Linux、文件与智能家居（搜索摘要） — [openinterpreter/01](https://github.com/openinterpreter/01)；[01-app](https://github.com/openinterpreter/01-app)；[letsclouds](https://www.letsclouds.com/news/open-interpreter-app-universal-remote)
- 小智 ESP32：硬件语音终端经小智控制台透传 MCP 到 MCP 服务器，可实现智能家居控制、"PC 桌面操作"等（搜索摘要）；社区桥接项目 Joho6666/xiaozhi-windows-agent（MIT，创建 2026-08-22，0 star）宣称让小智语音控制 Windows、读写文件、截屏视觉分析并调度本地 Codex CLI/Claude Code/OpenCode — [博客园 小智架构](https://www.cnblogs.com/wangya216/p/19800192)；[xiaozhi-windows-agent](https://github.com/Joho6666/xiaozhi-windows-agent)
- RustDesk：rustdesk/rustdesk，AGPL-3.0，125,156 stars，最近 push 2026-10-05，支持 Windows/Linux/macOS/iOS/Android，自托管远程桌面 — [GitHub API](https://github.com/rustdesk/rustdesk)
- Tailscale + RustDesk 组合被 Tailscale 官方博客与 XDA 推荐为无公网端口的远程访问方案；bscott/rdc 项目把"经 Tailscale 对另一台机器截图/鼠标/键盘/窗口控制"暴露为 MCP 工具供 Claude Code 等 Agent 调用（搜索摘要） — [Tailscale 博客](https://tailscale.com/blog/tailscale-rustdesk-remote-desktop-access)；[bscott/rdc](https://github.com/bscott/rdc)
- Home Assistant 路线：OpenClaw 经 Home Assistant 的 MCP Server 集成 + 长期访问令牌（ha-mcp skill）控制设备、读传感器，并可被设为 Assist 语音管线的对话代理（ESPHome 语音设备可唤起）（搜索摘要） — [HA 社区](https://community.home-assistant.io/t/let-openclaw-control-your-home/983029)；[openclawvps 安全手册](https://openclawvps.io/blog/openclaw-home-assistant)
- 反向方向（电脑/模型操控手机）：Open-AutoGLM 为"Phone Agent"框架（Agent 操作安卓手机），与本题方向相反 — [Open-AutoGLM](https://github.com/zai-org/Open-AutoGLM)
- 安全注意：官方风险提示指出 OpenClaw 这类"依据自然语言指令直接操控计算机"的工具存在提示词注入与误删数据风险（搜索摘要） — [腾讯新闻 CNCERT](https://news.qq.com/rain/a/20260310A0753G00)

### Inferences
- 成熟度排序（基于活跃度、通道覆盖与教程密度）：OpenClaw 网关 + 飞书/钉钉/企微/官方微信插件 ≈ LobsterAI（桌面封装）> QwenPaw / DeerFlow（功能齐全但桌面端 Beta 或无）> nanobot / Hermes（轻量或海外主导）> StaffDeck（仅微信/企微）> 小智硬件桥接（0 star 个人项目）> Open Interpreter 01（已停更）。
- "结果回传手机"在上述方案中都通过 IM 消息（文本/文件）完成；真正的手机原生 App 远控在开源项目中仅 OpenClaw Node App（侧重把手机能力给网关，而非作为指令入口）和已停更的 01-app，这正是灵策智算 iPhone/Android App 的差异化空间。
- 国内个人微信通道的合规路径在 2026-03 之后收敛到腾讯官方 iLink 插件；任何商业产品若宣称"微信直连"而不走该协议，封号风险是社区公认的争议点。

### Gaps
- docs.openclaw.ai 被拦截，OpenClaw 官方对 exec approvals、DM pairing/allowlist 等审批机制的原文未能核读，仅有社区转述。
- OpenClaw Node App 是否开源（独立仓库）未核实。
- 未找到对"手机→电脑 Agent"链路的量化成熟度评测（延迟、成功率）；仅 PinchBench 评测模型而非通道。
- 小智 ESP32 官方是否内置"控制电脑"技能：仅见社区桥接，官方 README 未核读。
