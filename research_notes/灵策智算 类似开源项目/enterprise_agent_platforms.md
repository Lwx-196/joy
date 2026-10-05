# 与"灵策智算 LynxceAI"企业级能力对应的开源项目（A 企业级 Agent 平台 / B IM 接入 / C 轻系统）

> 时间基准：2026-10-05。GitHub star 数、最近 push 时间、许可证 SPDX 字段均来自 GitHub 仓库 API 元数据（同日查询）；许可证附加条款来自各仓库 raw LICENSE 文件；功能描述来自各项目 README / 官方文档 / 中文社区报道。官网被网络策略拦截（feishu.cn、cloud.tencent.com、maxkb.cn、fit2cloud.com、doc.fastgpt.io/.cn、nocobase.com、docs.nocobase.com、help.teable.ai、nocodb.com、dify.ai、docs.anythingllm.com、tooljet.com、larksuite.com、dev.to、sina.com.cn）的条目，只能引用搜索摘要，已逐条标注"（未直接核实）"。

---

## 关键问题 1：(A) 企业级 Agent / LLM 应用平台——各项目基本盘（许可证、star、活跃度、维护方、部署）

### Takeaway
2026 年 10 月，真正"纯开源无附加条款"的企业级 Agent 平台是 Coze Studio（Apache-2.0，但自 2026-07-29 起已逾两月无提交）、RAGFlow（Apache-2.0）、Bisheng（Apache-2.0）、Langflow（MIT）、AnythingLLM（MIT）、MaxKB（GPL-3.0，强 copyleft）；Dify、FastGPT、LobeHub、Open WebUI、n8n、Flowise、Onyx 都带有多租户/品牌/派生分发/企业目录等附加限制。中国团队维护的有 Dify、Coze Studio、FastGPT、RAGFlow、MaxKB、Bisheng、LobeHub、DeerFlow、CowAgent。

### Cited Findings

**Dify（langgenius/dify）**
- GitHub 元数据：157,865 stars、24,910 forks，最近 push 2026-10-05，TypeScript，官网 dify.ai，许可证字段 NOASSERTION（非标准许可证） — [GitHub: langgenius/dify](https://github.com/langgenius/dify)
- 许可证：修改版 Apache-2.0。附加条件：(1) 未经书面授权不得用源码运营多租户环境（"一个租户 = 一个工作空间，数据与配置相互隔离"）；(2) 使用前端（`web/` 目录或 Docker `web` 镜像）时不得移除/修改控制台与应用中的 Dify Logo 和版权信息；(3) 贡献者同意许可证可被收紧或放宽、贡献代码可被商用（含云服务）；(4) 交互设计受外观专利保护 — [raw LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)
- Dify 被中文媒体称为"来自中国的 AI 框架明星" — [53AI](https://www.53ai.com/news/OpenSourceLLM/2024112965234.html)
- 社区版 vs 企业版（阿里云 DMS 版企业版介绍）：企业版支持 SAML/OIDC/OAuth2 SSO，社区版不支持 SSO；企业版支持审计日志查询与下载（按工作空间 ID、资源类型、操作类型、日期筛选），社区版不支持审计日志；企业版支持多个相互隔离的工作空间（各自应用/数据集/成员），社区版仅单一工作空间；企业版提供工作空间成员/角色/权限的统一管理与 RBAC — [阿里云帮助中心：Dify 企业版](https://help.aliyun.com/zh/dms/introduction-to-dify-enterprise-edition)；[阿里云 AIDBS：Dify Enterprise Edition（multi-tenancy, SSO, audit logs, brand customization）](https://help.aliyun.com/en/aidbs/user-guide/dify-enterprise-edition)
- 定时任务：Dify 1.10.0（2025-11）引入 Trigger（事件驱动工作流），支持按小时/天/周/月及 Cron 表达式的定时触发；**触发器目前仅支持 Workflow 类型应用，Chatflow / Agent / BasicChat 不支持** — [Dify Docs：定时触发器](https://docs.dify.ai/zh/cloud/use-dify/nodes/trigger/schedule-trigger)；[腾讯新闻：Dify 1.10.0 发布](https://news.qq.com/rain/a/20251124A01IEH00)；[Dify 博客 Introducing Trigger（被拦截，未直接核实）](https://dify.ai/blog/introducing-trigger)
- 中文落地案例：顺丰内部 AI 智能助手实施（腾讯云社区文章） — [腾讯云开发者社区](https://cloud.tencent.cn/developer/article/2505495)

**Coze Studio（coze-dev/coze-studio，字节跳动）**
- GitHub 元数据：21,672 stars，许可证 Apache-2.0，仓库创建 2025-06-26，**最近 push 2026-07-29**（截至 2026-10-05 已超过两个月无提交） — [GitHub: coze-dev/coze-studio](https://github.com/coze-dev/coze-studio)
- 配套 Coze Loop（评测/观测）：5,758 stars，Apache-2.0，最近 push 2026-10-05 — [GitHub: coze-dev/coze-loop](https://github.com/coze-dev/coze-loop)
- 2025-07-26 开源，Apache-2.0，无附加条款；与 Dify 不同，允许多租户 SaaS/转售 — [博客园：Coze 开源了](https://www.cnblogs.com/leadingcode/p/19005817)；[知乎：Coze 开源 Apache 2.0](https://zhuanlan.zhihu.com/p/1932470522802835910)；[Medium 对比（含多租户条款对比）](https://medium.com/@cyan747/comparison-of-ai-development-tools-key-difference-facts-between-dify-and-coze-studio-open-source-3a3657b0a60c)
- README：Golang 后端 + React/TS 前端，Docker 部署，最低 2 核 4 GB；Agent / Workflow / 插件 / 知识库 / 数据库 / 记忆；API 使用 Personal Access Token；"部分功能（如音色定制）仅商业版提供" — [raw README](https://raw.githubusercontent.com/coze-dev/coze-studio/main/README.md)
- 开源版缺口：仅支持单用户 PAT 授权，不支持多用户/工作区隔离；知识库、应用、模型等资源缺乏完备权限管理，无多租户，无健全的用户与组织管理，难与企业已有组织架构/认证对接 — [掘金：Coze Studio 开源企业用户需多几分考量](https://juejin.cn/post/7534879121131126825)；[枫清科技 Fabarta 博客](https://www.fabarta.com/blog/detail/6891810114427c9561158fa3)；[GitHub Issue #212：社区自发规划 Plus 版](https://github.com/coze-dev/coze-studio/issues/212)

**FastGPT（labring/FastGPT，Sealos/环界云）**
- GitHub 元数据：29,777 stars，最近 push 2026-10-01，许可证字段 NOASSERTION — [GitHub: labring/FastGPT](https://github.com/labring/FastGPT)
- 许可证：Apache-2.0 + 附加条件：不得运营"与 FastGPT 类似的多租户 SaaS 服务"；不得移除/修改 FastGPT 控制台中的 LOGO 与版权信息；贡献者同意协议可收紧/放宽、贡献代码可被云业务商用；交互设计受外观专利保护；商业授权联系 dennis@sealos.io — [raw LICENSE](https://raw.githubusercontent.com/labring/FastGPT/main/LICENSE)
- 商业版 vs 社区版：商业版在社区版基础上增加团队空间与权限功能（社区版不支持）；权限系统融合 ABAC+RBAC，支持成员/部门/群组三种管理模式，可细粒度控制团队、应用、知识库访问；商业版还包含应用发布安全配置与内容审核；Sealos 全托管起价 10,000 元/月（3 个月起）或 120,000 元/年（8C32G）；自有服务器部署含 6 个版本免费升级、14 天内部署 — [FastGPT 商业版文档（被拦截，未直接核实）](https://doc.fastgpt.io/zh-CN/guide/version/commercial)；[团队&成员组&权限文档（被拦截，未直接核实）](https://doc.fastgpt.cn/zh-CN/guide/workspace/team/team_roles_permissions)；[FastGPT 权限系统设计与演进（被拦截）](https://fastgpt.io/zh-hant/blog/fastgpt-permission-system-design-evolution)
- 落地：2026-04 深信服 × FastGPT 联合发布 SF-FastGPT 商业版；厂商口径"商业版已服务 20,000+ 企业客户、50+ 行业"、"50 万+ 全球注册用户、500+ 合作服务企业"（厂商宣传，未独立核实） — [CSDN 资讯：深信服×FastGPT](https://www.csdn.net/article/2026-04-03/159793336)；[FastGPT 客户案例中心](https://fastgpt.cn/customers)；[深信服 SF-FastGPT](https://www.sangfor.com.cn/AIfirst/FastGPT)

**RAGFlow（infiniflow/ragflow，InfiniFlow）**
- GitHub 元数据：91,688 stars，Apache-2.0，最近 push 2026-10-04，主语言已变为 Go — [GitHub: infiniflow/ragflow](https://github.com/infiniflow/ragflow)
- README_zh：最新为 v1.0.0-rc1（Go 版本）；2026-09-10 支持 Sitemap 接入网站；2026-06-29 支持 WhatsApp、钉钉、企业微信聊天渠道；2026-05-26 新增 Browser 组件支持 Agent 自主浏览与操作网页；Agentic RAG 支持 Low/Medium/High/Ultra 四种思考模式；MCP 与 Sandbox Executor 可选启用；Docker Compose 部署；README 未提及团队权限与企业版 — [raw README_zh](https://raw.githubusercontent.com/infiniflow/ragflow/main/README_zh.md)
- 团队功能：v0.13.0 为所有用户添加团队管理；v0.18.0 推出团队协作，Agent 可与团队成员共享（第三方版本解读） — [53AI：RAGFlow v0.26.2 发布详解](https://www.53ai.com/news/RAGFlow/2026063028740.html)；[RAGFlow 中文版本发布页](https://ragflow.com.cn/docs/release_notes)

**MaxKB（1Panel-dev/MaxKB，飞致云 FIT2CLOUD）**
- GitHub 元数据：22,899 stars，GPL-3.0，最近 push 2026-10-04，官网 maxkb.cn，描述"开源企业级智能体平台" — [GitHub: 1Panel-dev/MaxKB](https://github.com/1Panel-dev/MaxKB)
- README_CN：GPLv3，版权飞致云；RAG、工作流编排、函数库、MCP 工具调用、多模型（本地/公有）；Docker、1Panel 应用商店、离线安装包；明确存在"社区版和专业版" — [raw README_CN](https://raw.githubusercontent.com/1Panel-dev/MaxKB/main/README_CN.md)
- 版本演进：v2.5.0 将"应用"升级为"智能体"，支持大模型自主执行流程（自主调用工具、MCP 和智能体），上线模板中心；v2.6.0 智能体与工具新增触发器触发能力；v2.8.0（2026-04-10）新增工作流类型工具、对话时可选模型与知识库、知识库全量导入导出；v2.9.0（2026-05-07）新增长期记忆、工作流节点禁用/启用 — [MaxKB 博客 v2.6.0（被拦截，未直接核实）](https://www.maxkb.cn/blog/maxkb-v2-6-0)；[OSCHINA：MaxKB v2.5.0](https://www.oschina.net/news/395380)；[MaxKB 博客 v2.8.0（未直接核实）](https://www.maxkb.cn/blog/maxkb-v2-8-0)
- 版本差异（第三方/厂商页面摘要，未直接核实）：专业版单租户、仅单个 admin；企业版多租户 + 完整 RBAC；专业版支持 LDAP/CAS/OIDC/OAuth2 SSO、对接企业微信/钉钉/飞书/微信公众号、操作日志审计（v1.10.3 LTS 专业版新增）；社区版有知识库/应用/用户容量限制 — [飞致云 MaxKB 专业版定价页（被拦截）](https://www.fit2cloud.com/maxkb/pricing.html)；[OSCHINA：MaxKB v1.10.3 LTS](https://www.oschina.net/news/343279/maxkb-1-10-3-lts)
- 商业落地："1000+ 付费客户"、"累计免费安装量 100 万+"、客户含深圳通、华莱士、广西质检院、东北财经大学等（厂商口径） — [53AI：这个开源知识库凭什么拿下 1000+ 企业客户](https://www.53ai.com/news/MaxKB/2026062913680.html)；[MaxKB 官网](https://maxkb.cn/)

**Bisheng 毕昇（dataelement/bisheng，数据项素 DataElem）**
- GitHub 元数据：12,022 stars，Apache-2.0，最近 push 2026-09-30 — [GitHub: dataelement/bisheng](https://github.com/dataelement/bisheng)
- README_CN：独立完备的应用编排框架（成环、并行、跑批、判断、Human in the loop）、AGL（Agent Guidance Language）；企业特性：基于角色的细颗粒度权限管理、SSO/LDAP、用户组管理、分组流量控制、安全审查、漏洞扫描修复；部署要求 CPU≥8 核、内存≥32 GB；README 未提及飞书/钉钉/企微接入，也未明确提到审计日志 — [raw README_CN](https://raw.githubusercontent.com/dataelement/bisheng/main/README_CN.md)
- 第三方称其提供 RBAC、部门用户组、SSO(含 LDAP)、审计日志/使用统计；当前版本 v2.6.0（2026-07），Apache-2.0 无附加协议 — [苏米客：毕昇 BISHENG](https://www.xmsumi.com/detail/1856)；[掘金：每日一个开源项目 毕昇](https://juejin.cn/post/7664796592324460578)

**n8n（n8n-io/n8n）**
- GitHub 元数据：206,691 stars，最近 push 2026-10-05，许可证字段 NOASSERTION，自称"Fair-code" — [GitHub: n8n-io/n8n](https://github.com/n8n-io/n8n)
- 许可证：Sustainable Use License v1.0 覆盖除 `.ee.`/`.ee` 文件与目录外的全部代码；`.ee` 文件需 n8n Enterprise License；SUL 允许内部业务使用与个人非商业用途，禁止商业分发/转售、移除许可声明、再授权 — [raw LICENSE.md](https://raw.githubusercontent.com/n8n-io/n8n/master/LICENSE.md)
- 社区版无 SSO、无 RBAC、无审计日志、无日志流（Log streaming）；企业版含 SSO（SAML/OIDC/LDAP）、细粒度角色、审计、日志流到 SIEM — [codimite：Self-Hosted n8n Community vs Enterprise](https://codimite.ai/n8n/n8n-community-vs-enterprise/)；[n8n 官方社区论坛许可讨论](https://community.n8n.io/t/n8n-licens-questions/154882)

**Langflow（langflow-ai/langflow）**
- GitHub 元数据：155,502 stars，MIT，最近 push 2026-10-05 — [GitHub: langflow-ai/langflow](https://github.com/langflow-ai/langflow)
- 开源版无原生 SSO/RBAC/审计；DataStax 托管云 2026-03-09 弃用、2026-04-09 关闭，自托管（Docker/K8s Helm）成为主要路径 — [automationatlas：Can You Self-Host Langflow (2026)](https://automationatlas.io/answers/can-you-self-host-langflow-2026/)；[Langflow Docs：API keys and authentication](https://docs.langflow.org/api-keys-and-authentication)

**Flowise（FlowiseAI/Flowise）**
- GitHub 元数据：55,485 stars，最近 push 2026-08-13，许可证字段 NOASSERTION — [GitHub: FlowiseAI/Flowise](https://github.com/FlowiseAI/Flowise)
- 许可证：主体 Apache-2.0；`/packages/server/src/enterprise` 目录及带显式版权声明的文件（如 IdentityManager.ts）属单独 Commercial License — [raw LICENSE.md](https://raw.githubusercontent.com/FlowiseAI/Flowise/main/LICENSE.md)
- Workspaces 仅 Cloud 与 Enterprise 计划提供，工作空间内用 RBAC 管理权限；SSO（OIDC：Entra ID/Google/Auth0）、审计日志属企业版 — [Flowise Docs：Workspaces](https://docs.flowiseai.com/using-flowise/workspaces)

**AnythingLLM（Mintplex-Labs/anything-llm）**
- GitHub 元数据：66,718 stars，MIT，最近 push 2026-10-04，描述"local-first agent experience" — [GitHub: Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm)
- 支持多用户模式（按用户控制访问）；Agent Skill Store 一键安装技能（Web Search、图表、代码解释器）；Scheduled Jobs 可按 cron 周期运行具备完整工具的 Agent，**但仅在单用户模式可用，多用户模式的自托管实例看不到该设置页** — [AnythingLLM Docs：Scheduled Jobs（被拦截，未直接核实）](https://docs.anythingllm.com/scheduled-jobs/overview)；[PR #6580：Per workspace agent skills](https://github.com/Mintplex-Labs/anything-llm/pull/6580)

**Open WebUI（open-webui/open-webui）**
- GitHub 元数据：153,976 stars，最近 push 2026-10-05，许可证字段 NOASSERTION — [GitHub: open-webui/open-webui](https://github.com/open-webui/open-webui)
- 许可证：修改版 BSD-3-Clause，新增品牌条款：不得更改/移除/遮盖 "Open WebUI" 品牌，例外为任意 30 天滚动窗口内 ≤50 终端用户的部署、取得书面许可或签署企业许可；违反即构成"实质性违约" — [raw LICENSE](https://raw.githubusercontent.com/open-webui/open-webui/main/LICENSE)
- 功能：Admin/User 角色、用户组权限、SSO/OIDC、LDAP、SCIM、按资源访问控制、审计日志流 — [Open WebUI Docs：Security](https://docs.openwebui.com/enterprise/security/)；厂商页称 SSO/LDAP/RBAC/SCIM 2.0/审计日志在免费社区版即可用、用户无上限 — [openwebui.com](https://openwebui.com/)；另有第三方称 LDAP/AD、SSO、SCIM、白标属企业许可 — [aipedia.wiki 评测](https://aipedia.wiki/tools/open-webui/)（两说法相互矛盾，需以官方文档为准）

**LobeHub（lobehub/lobehub，原 lobe-chat）**
- GitHub 元数据：82,991 stars，最近 push 2026-10-05，许可证字段 NOASSERTION，仓库已由 lobe-chat 更名为 lobehub，自述"Chief Agent Operator…hiring, scheduling, and reporting on your entire AI team" — [GitHub: lobehub/lobehub](https://github.com/lobehub/lobehub)
- 许可证：LobeHub Community License = Apache-2.0 + 附加条件：可直接商用，但"开发并分发派生作品须取得商业许可"；贡献者同意条款可变更、贡献可商用 — [raw LICENSE](https://raw.githubusercontent.com/lobehub/lobehub/main/LICENSE)
- README：Agent 为工作单元，Agent Groups 并行团队、Pages、项目组织、Workspace 团队协作；10,000+ 工具与 MCP 插件；结构化可编辑的 Personal Memory；Docker / Vercel / Zeabur / Sealos / 阿里云一键部署 — [raw README](https://raw.githubusercontent.com/lobehub/lobehub/main/README.md)
- 第三方：提供"调度运行让 Agent 在你离开时也能工作"、团队共享空间 — [Enterprise DNA AI Pulse](https://enterprisedna.co/resources/ai-pulse/ai-pulse-2026-07-20-chief-agent-operator-an-80k-star-open-source-project-marketi/)

**Onyx（onyx-dot-app/onyx，原 Danswer）**
- GitHub 元数据：32,324 stars，最近 push 2026-10-05，许可证字段 NOASSERTION — [GitHub: onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx)
- 许可证：MIT；`backend/ee`、`web/src/app/ee`、`web/src/ee` 目录属 Onyx Enterprise License — [raw LICENSE](https://raw.githubusercontent.com/onyx-dot-app/onyx/main/LICENSE)
- 厂商页：40+ 连接器、权限感知检索、深度研究、带 MCP 工具的自定义 Agent；企业功能含 OIDC/SAML SSO、SCIM、细粒度 RBAC、审计追踪；SOC 2 Type II — [onyx.app：Self-Hosted RAG 2026](https://onyx.app/insights/self-hosted-rag)；[onyx.app：Secure Agent Access Control](https://onyx.app/insights/secure-agent-access-control)

**Khoj（khoj-ai/khoj）**
- GitHub 元数据：37,559 stars，AGPL-3.0，最近 push 2026-08-02，描述含"Build custom agents, schedule automations, do deep research" — [GitHub: khoj-ai/khoj](https://github.com/khoj-ai/khoj)

**Letta（letta-ai/letta）**
- GitHub 元数据：25,025 stars，Apache-2.0，最近 push 2026-09-10，"Platform for stateful agents…self-improve over time" — [GitHub: letta-ai/letta](https://github.com/letta-ai/letta)
- Letta Desktop 运行 ADE，可连接自托管 Letta server；企业层级含 SAML/OIDC SSO 与 RBAC；Letta Server 0.16.7（2026-03-31）默认上下文窗口升至 128k — [Letta Releases](https://github.com/letta-ai/letta/releases)；[Letta on X：Letta Desktop](https://x.com/Letta_AI/status/1953255524843114961)；[promptquorum 评测](https://www.promptquorum.com/power-local-llm/letta-review)

**Botpress / Rasa**
- Botpress：14,938 stars，MIT，最近 push 2026-10-02；v12 自托管版已日落，不再提供下载/新部署，Botpress Cloud 是唯一受支持路径 — [GitHub: botpress/botpress](https://github.com/botpress/botpress)；[Botpress Docs：v12 and self-hosted versions](https://botpress.com/docs/studio/guides/advanced/v12/)
- Rasa：21,344 stars，Apache-2.0（rasa 开源仓），最近 push 2026-07-24；Rasa Pro 需许可证，开发者许可免费但生产受限，Growth 层约 $35,000/年 — [GitHub: RasaHQ/rasa](https://github.com/RasaHQ/rasa)；[Rasa Docs：Rasa Pro License](https://rasa.com/docs/reference/api/pro/http-api/server-information/information-about-your-rasa-pro-license)

**本地执行型 Agent（与"本地执行、数据留本地"对应）**
- OpenClaw（openclaw/openclaw）：391,404 stars、82,270 forks，MIT，仓库创建 2025-11-24，最近 push 2026-10-05，TypeScript，"The AI that really does things. Any OS. Any Platform." — [GitHub: openclaw/openclaw](https://github.com/openclaw/openclaw)
- OpenClaw 功能：内置调度器支持一次性提醒、固定周期、cron 表达式与入站 webhook 触发，输出可投递到聊天频道；ClawHub 社区技能市场；exec 工具可执行任意 shell 命令，建议开启审批（每条命令先展示、确认后执行）；cron 任务可按任务覆盖模型（`openclaw cron add --model`） — [OpenClaw Docs：Automation](https://docs.openclaw.ai/automation)；[OpenClaw Docs：Skills and automation FAQ](https://docs.openclaw.ai/help/faq/skills-and-automation)；[OpenClaw Docs：Tools](https://docs.openclaw.ai/tools)
- Hermes Agent（NousResearch/hermes-agent）：251,286 stars，MIT，最近 push 2026-10-05，"The agent that grows with you" — [GitHub: NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- DeerFlow（bytedance/deer-flow）：83,396 stars，MIT，最近 push 2026-10-05；README_zh：基于 LangGraph，任务在隔离 Docker 沙箱运行，子 Agent 动态派生，跨会话本地记忆；定时任务支持 cron / interval / once，可设目标评估并在多次失败后自动暂停；Skills 为 Markdown 能力模块；MCP 扩展工具；多实例调度 + PostgreSQL — [raw README_zh](https://raw.githubusercontent.com/bytedance/deer-flow/main/README_zh.md)
- CowAgent（zhayujie/CowAgent，原 chatgpt-on-wechat 同一仓库，topics 含 chatgpt-on-wechat）：47,225 stars、10,375 forks，MIT，最近 push 2026-10-05；README：v2.2.0（2026-09-30）"能力中心、多 Agent 协作改进、Web 控制台重建"；v2.1.9/v2.1.8 加入多 Agent 团队、定时任务、工作区；个人知识库 + 知识图谱、三层长期记忆（context→daily→core）、Skill Hub/GitHub/ClawHub 一键安装技能；一行安装、Docker Compose、macOS/Windows 桌面客户端 — [GitHub: zhayujie/CowAgent](https://github.com/zhayujie/CowAgent)；[raw README](https://raw.githubusercontent.com/zhayujie/chatgpt-on-wechat/master/README.md)

**2026 年新出现的"数字员工"类开源平台**
- UniEmployee（zj-unicom-ai/UniEmployee，浙江联通）：330 stars，MIT，仓库创建 2026-08-14，最近 push 2026-10-02；"把专业员工的工作经验、业务流程和判断标准，固化为可随时上岗、可配置、可审批、可观测的 AI 数字员工"；内置 8 个示例员工（客服、数据分析、销售、HR、BI、网络运维、市场调研、保险分析）；HITL 审批与多层安全护栏；全链路可观测（LLM 调用、工具调用、延迟、token）；企业知识本体与审计轨迹；按 (user_id, employee_id) 隔离的跨会话长期记忆；**外部 IM 通道（微信/钉钉/飞书/企微）仍为规划，当前仅平台内 Web 聊天**；Python 3.12 + Vue 3.5 + PostgreSQL — [GitHub: zj-unicom-ai/UniEmployee](https://github.com/zj-unicom-ai/UniEmployee)；[raw README](https://raw.githubusercontent.com/zj-unicom-ai/UniEmployee/main/README.md)
- openbot（Peerframe/openbot）：12 stars，MIT，创建 2026-08-31，版本 0.1.0-alpha.9；"self-hosted…control plane for persistent AI employees"，Bot 保有身份/角色/记忆，频道内多 Bot 协作委派，Worker Hosts 执行电脑任务，人工审阅/审批，桌面安装包 macOS/Windows — [GitHub: Peerframe/openbot](https://github.com/Peerframe/openbot)；[raw README](https://raw.githubusercontent.com/Peerframe/openbot/main/README.md)
- KDevSec/digital-employees："数字员工套件2.0——工作台+员工包+管控平台三层分离架构"，0 stars，无许可证文件，最近 push 2026-08-28 — [GitHub: KDevSec/digital-employees](https://github.com/KDevSec/digital-employees)
- bangwozuo/digital-employees-hub-zh："30 个数字员工 · 160 原子技能 · 140 场景工作流"资产导航库（非软件平台），0 stars，创建 2026-10-02 — [GitHub: bangwozuo/digital-employees-hub-zh](https://github.com/bangwozuo/digital-employees-hub-zh)
- Lianjifu/digital-employee-platform："企业级数字员工平台…在受控边界内完成协作、执行与审计"（搜索摘要），GitHub API 未能检索到该仓库元数据（可能已改名/私有），**未核实** — [搜索结果链接](https://github.com/Lianjifu/digital-employee-platform)

### Inferences
- 在"许可证干净 + 企业治理内建 + 中文团队"的三维上，没有任何单一项目同时满足：Coze Studio 许可证最干净但缺多用户与近期维护；Bisheng/MaxKB 企业治理最完整但 MaxKB 是 GPL-3.0 且治理功能多在专业/企业版；Dify/FastGPT 社区版把 SSO/审计/多工作空间留给付费版并用附加条款禁止多租户 SaaS。
- "Agent 能动手执行长链任务"的能力主要集中在本地执行型 Agent（OpenClaw、Hermes、DeerFlow、CowAgent）而非传统 LLMOps 平台；Dify 的 Agent 类型应用甚至还不支持定时触发器。
- 2026 年新出现的"数字员工"开源项目（UniEmployee、openbot）体量极小（≤330 stars），尚未到可替代商业产品的成熟度。

### Gaps
- 无法直接读取 Dify/FastGPT/MaxKB/Teable/NocoBase/NocoDB/AnythingLLM 官方文档站（网络策略拦截），相关版本差异表只能依赖搜索摘要与第三方转述。
- Coze Studio 停更原因未找到官方说明；是否存在非 GitHub 的更新渠道未核实。
- n8n、Langflow、Onyx 等国外项目维护方所在国本次未从一手来源核实，故未写入。

---

## 关键问题 2：(A) 企业能力逐项对照——团队权限 / 审计 / 知识库 / Agent 执行 / 定时任务 / 多模型 / 插件市场 / 飞书·企微·钉钉原生集成

### Takeaway
把灵策智算的八项企业能力当作清单逐一打勾时，开源社区版普遍只能覆盖"知识库 + 多模型 + 插件"三项；"按角色分级权限 + 审计日志 + 多工作空间"在 Dify/FastGPT/MaxKB/n8n/Flowise 均是付费版功能，只有 Bisheng、Open WebUI、Onyx（部分）在开源部分提供 RBAC；"飞书/企微/钉钉原生入口"在 LLMOps 平台中极少见（RAGFlow 2026-06 新增钉钉/企微渠道、MaxKB 专业版支持），通常要靠 B 部分的 IM 框架补齐。

### Cited Findings
- 团队/多工作空间/RBAC：Dify 社区版单工作空间、无 RBAC 精细管理，企业版多工作空间 + RBAC — [阿里云：Dify 企业版](https://help.aliyun.com/zh/dms/introduction-to-dify-enterprise-edition)；FastGPT 团队空间与权限为商业版功能 — [FastGPT 商业版文档（未直接核实）](https://doc.fastgpt.io/zh-CN/guide/version/commercial)；MaxKB 专业版单租户单 admin、企业版多租户 + RBAC — [飞致云定价页（未直接核实）](https://www.fit2cloud.com/maxkb/pricing.html)；Flowise Workspaces/RBAC 仅 Cloud/Enterprise — [Flowise Docs](https://docs.flowiseai.com/using-flowise/workspaces)；n8n 社区版无 RBAC — [codimite](https://codimite.ai/n8n/n8n-community-vs-enterprise/)；Bisheng 开源 README 列出 RBAC、用户组、SSO/LDAP — [raw README_CN](https://raw.githubusercontent.com/dataelement/bisheng/main/README_CN.md)；Open WebUI 社区版提供 RBAC 与用户组 — [Open WebUI Docs](https://docs.openwebui.com/enterprise/security/)；Coze Studio 开源版无多用户/工作区隔离 — [掘金](https://juejin.cn/post/7534879121131126825)；Langflow 无原生 RBAC — [automationatlas](https://automationatlas.io/answers/can-you-self-host-langflow-2026/)
- 审计日志：Dify 企业版有、社区版无 — [阿里云](https://help.aliyun.com/zh/dms/introduction-to-dify-enterprise-edition)；MaxKB 操作日志为专业版（v1.10.3 LTS 起） — [OSCHINA](https://www.oschina.net/news/343279/maxkb-1-10-3-lts)；n8n 审计与日志流为企业版 — [codimite](https://codimite.ai/n8n/n8n-community-vs-enterprise/)；Flowise 审计日志为企业版 — [Flowise Docs](https://docs.flowiseai.com/using-flowise/workspaces)；Onyx 审计追踪在企业功能中 — [onyx.app](https://onyx.app/insights/self-hosted-rag)；Open WebUI 提供容器原生审计日志流 — [Open WebUI Docs](https://docs.openwebui.com/enterprise/security/)；UniEmployee 开源即含全链路可观测与审计轨迹 — [raw README](https://raw.githubusercontent.com/zj-unicom-ai/UniEmployee/main/README.md)
- 知识库：Dify、FastGPT、RAGFlow、MaxKB、Bisheng、AnythingLLM、Onyx、LangBot、AstrBot、CowAgent 均内置 RAG/知识库（见各 README 引用，上文）；RAGFlow 2026-09-10 支持 Sitemap 接入网站内容 — [raw README_zh](https://raw.githubusercontent.com/infiniflow/ragflow/main/README_zh.md)
- Agent 自主执行（不只聊天/RAG）：MaxKB v2.5.0 智能体可自主调用工具/MCP/智能体 — [OSCHINA](https://www.oschina.net/news/395380)；RAGFlow Agent 有 Browser 组件自主操作网页、Sandbox Executor — [raw README_zh](https://raw.githubusercontent.com/infiniflow/ragflow/main/README_zh.md)；OpenClaw exec 可运行任意 shell 命令并支持审批 — [OpenClaw Docs：Tools](https://docs.openclaw.ai/tools)；DeerFlow 在 Docker 沙箱中执行 bash/文件操作、子 Agent 并行 — [raw README_zh](https://raw.githubusercontent.com/bytedance/deer-flow/main/README_zh.md)；AstrBot 有 Agent Sandbox 安全执行代码 — [raw README](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/README.md)；Bisheng 支持成环/并行/跑批与 Human-in-the-loop — [raw README_CN](https://raw.githubusercontent.com/dataelement/bisheng/main/README_CN.md)
- 定时任务：Dify Trigger 仅 Workflow — [Dify Docs](https://docs.dify.ai/zh/cloud/use-dify/nodes/trigger/schedule-trigger)；MaxKB v2.6.0 智能体/工具触发器 — [MaxKB 博客（未直接核实）](https://www.maxkb.cn/blog/maxkb-v2-6-0)；AnythingLLM Scheduled Jobs 仅单用户模式 — [AnythingLLM Docs（未直接核实）](https://docs.anythingllm.com/scheduled-jobs/overview)；OpenClaw cron/webhook — [OpenClaw Docs：Automation](https://docs.openclaw.ai/automation)；DeerFlow cron/interval/once 并带失败自动暂停 — [raw README_zh](https://raw.githubusercontent.com/bytedance/deer-flow/main/README_zh.md)；CowAgent v2.1.x 定时任务 — [raw README](https://raw.githubusercontent.com/zhayujie/chatgpt-on-wechat/master/README.md)；Khoj "schedule automations" — [GitHub](https://github.com/khoj-ai/khoj)
- 多模型切换：Coze Studio 管理员配置模型列表（OpenAI、火山引擎等） — [raw README](https://raw.githubusercontent.com/coze-dev/coze-studio/main/README.md)；MaxKB 对话时可选模型与知识库（v2.8.0） — [MaxKB 博客（未直接核实）](https://www.maxkb.cn/blog/maxkb-v2-8-0)；OpenClaw 按 cron 任务覆盖模型 — [OpenClaw Docs](https://docs.openclaw.ai/automation)；LangBot 支持 OpenAI/Anthropic/DeepSeek/Gemini/Moonshot/智谱/Ollama/LM Studio 及硅基流动、阿里百炼 — [raw README_CN](https://raw.githubusercontent.com/langbot-app/LangBot/master/README_CN.md)
- 插件/Skills 市场：OpenClaw ClawHub — [OpenClaw Docs FAQ](https://docs.openclaw.ai/help/faq/skills-and-automation)；AstrBot 1000+ 插件市场、Skills — [raw README](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/README.md)；LangBot "数百个插件、跨进程事件驱动架构"、MCP 适配 — [raw README_CN](https://raw.githubusercontent.com/langbot-app/LangBot/master/README_CN.md)；AnythingLLM Agent Skill Store — [nullzen 指南](https://www.nullzen.dev/blog/anythingllm-enterprise-guide/)；LobeHub 10,000+ 工具与 MCP 插件 — [raw README](https://raw.githubusercontent.com/lobehub/lobehub/main/README.md)；CowAgent 从 Skill Hub / GitHub / ClawHub 一键安装 — [raw README](https://raw.githubusercontent.com/zhayujie/chatgpt-on-wechat/master/README.md)；MaxKB 模板中心（v2.5.0） — [OSCHINA](https://www.oschina.net/news/395380)
- 飞书/企微/钉钉原生集成（平台层）：RAGFlow 2026-06-29 支持钉钉与企业微信聊天渠道 — [raw README_zh](https://raw.githubusercontent.com/infiniflow/ragflow/main/README_zh.md)；MaxKB 专业版对接企业微信/钉钉/飞书/公众号、专业版支持飞书知识库 — [OSCHINA v1.10.3](https://www.oschina.net/news/343279/maxkb-1-10-3-lts)；Bisheng/Coze Studio/Dify README 未提及 IM 原生渠道（Coze Studio 仅提供飞书交流群）

### Inferences
- "审计 + 分级权限 + 多工作空间"是开源 LLMOps 平台最一致的商业化切割线；企业若坚持纯开源，自行二开或叠加 Open WebUI/Bisheng 的权限层是常见折衷。
- 定时任务在 2025Q4–2026 成为各平台补齐的重点（Dify 1.10、MaxKB 2.6、OpenClaw/DeerFlow 原生），但"定时任务启用前试跑"这一产品细节未在任何开源项目文档中看到。

### Gaps
- 各平台"运营看板（Skill 调用次数、任务完成率、活跃成员）"类功能未在可访问的来源中找到明确描述，无法逐项打勾。
- Dify 1.14.x（2026-05）及之后的 Agent 基础设施改进细节仅见 CSDN 博客，未能核实官方 changelog。

---

## 关键问题 3：(B) IM 接入框架——"在哪里发起就在哪里收到结果"、结果回写文档/文件库、任务状态推送

### Takeaway
2026 年飞书、钉钉、企业微信三家都官方拥抱了 OpenClaw：飞书官方插件（larksuite/openclaw-lark，MIT）以用户 OAuth 身份读写消息、文档、多维表格、电子表格、日程、任务；钉钉官方插件（DingTalk-Real-AI/dingtalk-openclaw-connector，MIT）可收发群/私聊消息、创建/追加/搜索钉钉文档、日程与待办、AI Card 流式；企业微信 2026-03-08 开放"智能机器人长连接 API 模式"三步接入 OpenClaw，并开源 Node.js SDK。通用多平台框架中 LangBot（Apache-2.0）、AstrBot（AGPL-3.0）、CowAgent（MIT）、DeerFlow、Hermes 均覆盖飞书/钉钉/企微，但"把结果回写到飞书文档/企微文件库"目前只有官方 OpenClaw 插件给出了明确的文档/表格写入能力。

### Cited Findings

**通用多平台 IM 机器人框架**
- LangBot（langbot-app/LangBot，原 RockChinQ/QChatGPT）：18,010 stars，Apache-2.0，最近 push 2026-10-03，描述含"WeChat(企业微信, 企微智能机器人, 公众号) / 飞书 / 钉钉 / QQ / Matrix…Integrated with…Dify, n8n, Langflow, Coze…openclaw / hermes agent, deerflow"，官网 space.langbot.app/cloud — [GitHub: langbot-app/LangBot](https://github.com/langbot-app/LangBot)
- LangBot README_CN：平台覆盖"QQ（个人号、官方机器人）、微信、企业微信、飞书、钉钉"及 Discord/Telegram/Slack/LINE/KOOK/Matrix/Satori/Email；多轮对话、工具调用、多模态、流式；内置 RAG；深度对接 Dify/Coze/n8n/Langflow；插件生态"数百个插件，跨进程的事件驱动架构"；MCP 适配；多流水线（pipeline）；Web 管理面板；`uvx langbot`、Docker Compose、K8s；"已被多家企业采用" — [raw README_CN](https://raw.githubusercontent.com/langbot-app/LangBot/master/README_CN.md)
- AstrBot（AstrBotDevs/AstrBot，原 Soulter/AstrBot）：41,413 stars，AGPL-3.0，最近 push 2026-10-05，自述"can be your openclaw alternative" — [GitHub: AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)
- AstrBot README：平台 QQ、企业微信、飞书、钉钉、微信公众号、Telegram、Slack、Discord、LINE 等；LLM 对话、多模态、Agent、MCP、Skills、知识库、人格设定、自动上下文压缩；1000+ 插件；Agent Sandbox 安全执行代码；WebUI；支持对接 Dify、阿里百炼、Coze；Docker/桌面应用/云部署 — [raw README](https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/README.md)；[AstrBot 官方文档](https://docs.astrbot.app/what-is-astrbot.html)
- CowAgent / chatgpt-on-wechat（zhayujie）：渠道含"WeChat, Feishu/Lark, DingTalk, WeCom Bot, QQ, WeCom App, WeChat Customer Service, WeChat Official Account"及 Telegram/Slack/Discord，默认 Web Console；MIT；v2.2.0 2026-09-30 — [raw README](https://raw.githubusercontent.com/zhayujie/chatgpt-on-wechat/master/README.md)；[GitHub: zhayujie/CowAgent（47,225 stars）](https://github.com/zhayujie/CowAgent)
- dify-on-wechat（hanfangyuan4396/dify-on-wechat）：CoW 下游分支，对接 Dify 智能助手/工具/知识库/工作流；README 最近更新 2025-04-12；**"本项目依赖的 itchat 与 gewechat 项目均无法使用，已无法接入微信个人号"**；企业微信应用 ✅、企微个人号 ✅(仅 Windows)、钉钉 ⏳待测试、飞书 ⏳待测试；GitHub API 未返回该仓库元数据，star 数未核实 — [raw README](https://raw.githubusercontent.com/hanfangyuan4396/dify-on-wechat/master/README.md)
- DeerFlow：IM 渠道"Feishu / Lark, WeChat, DingTalk, Telegram, Slack, 企业微信智能机器人, QQ, Buzz"，通过 WebSocket 或长轮询（无需公网回调）；当前支持文本、图片、文件入站 — [raw README_zh](https://raw.githubusercontent.com/bytedance/deer-flow/main/README_zh.md)
- Hermes Agent：网关适配 CLI、Telegram、Discord、Slack、WhatsApp、Signal、Matrix、Mattermost、Email、SMS、DingTalk、Feishu、WeCom、BlueBubbles、Home Assistant；钉钉支持图片/文件/表情回应/流式，飞书支持语音/图片/文件/话题/流式，企微支持语音/图片/文件/流式 — [Hermes Docs：Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)；[PR #3847：add WeCom platform support](https://github.com/NousResearch/hermes-agent/pull/3847)

**OpenClaw 的飞书 / 钉钉 / 企微通道（官方与社区）**
- 飞书官方插件 larksuite/openclaw-lark："飞书官方出品的 OpenClaw 飞书/Lark Channel 插件"，2,384 stars、311 forks，MIT，创建 2026-03-09，最近 push 2026-07-22，open issues 314 — [GitHub: larksuite/openclaw-lark](https://github.com/larksuite/openclaw-lark)
- 该插件 README：读消息（群/私聊历史、话题回复）、发消息/回复；交互卡片实时状态更新与确认按钮、卡片内流式文本；文档创建/更新/读取；Base（多维表格）与数据表 CRUD；电子表格创建编辑；日程 CRUD；任务（含子任务与评论）；**OAuth 用户身份委托——"OpenClaw will act under your user identity within the authorized scope"**，并警告数据泄露/越权风险；要求 Node ≥22、OpenClaw ≥2026.2.26；npm 包 `@larksuite/openclaw-lark` — [raw README](https://raw.githubusercontent.com/larksuite/openclaw-lark/main/README.md)
- 飞书官方文章称插件可"写文档、改文档、帮你发消息、约日程、创建多维表格"，并获取飞书内消息/文档/会议纪要/多维表格/日程/任务上下文（官网被拦截，仅搜索摘要） — [飞书官网文章](https://www.feishu.cn/content/article/7613711414611463386)；[17173 转载](https://news.17173.com/content/03082026/180348104.shtml)
- 社区飞书指南仓库 AlexAnys/openclaw-feishu：690 stars，MIT，最近 push 2026-03-30 — [GitHub](https://github.com/AlexAnys/openclaw-feishu)
- 钉钉官方插件 DingTalk-Real-AI/dingtalk-openclaw-connector："Official OpenClaw DingTalk channel plugin | 钉钉官方 OpenClaw 插件"，2,131 stars、216 forks，MIT，创建 2026-01-28，最近 push 2026-09-26 — [GitHub](https://github.com/DingTalk-Real-AI/dingtalk-openclaw-connector)
- 该插件 README："接收群聊/私聊消息，自动回复，发送文本/Markdown，@成员"；钉钉文档创建、追加、搜索、枚举；日程增删改查；个人待办创建/状态/截止；"AI Card 流式响应"实时状态；私聊/群聊会话隔离；要求 OpenClaw ≥2026.8.1；安装 `npx -y @dingtalk-real-ai/dingtalk-connector install`；MIT — [raw README](https://raw.githubusercontent.com/DingTalk-Real-AI/dingtalk-openclaw-connector/main/README.md)
- 钉钉 Stream 模式：WebSocket 长连接、无需公网域名；官方 SDK open-dingtalk/dingtalk-stream-sdk-python 174 stars、MIT、最近 push 2026-07-30；-go 47 stars — [GitHub: dingtalk-stream-sdk-python](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)；[GitHub: dingtalk-stream-sdk-go](https://github.com/open-dingtalk/dingtalk-stream-sdk-go)；[clawd.org.cn 钉钉机器人文档](https://clawd.org.cn/channels/dingtalk-connector)
- 企业微信官方接入：2026-03-08 企业微信向管理员推送"只需 3 步，快速将 OpenClaw 接入智能机器人"，通过"长连接方式/API 模式"创建智能机器人，无需域名；支持向机器人发送文件由 AI 读取分析、AI 生成文件（代码/文档）直接发回用户（PDF/Word/Excel/图片）；并称已支持通过 OpenClaw 写入数据至企微智能表格（来源页被拦截，未直接核实） — [腾讯云开发者社区：企业微信官方插件支持 OpenClaw](https://cloud.tencent.com/developer/article/2637067)；[新浪财经：企业微信支持接入 OpenClaw](https://finance.sina.com.cn/roll/2026-03-09/doc-inhqknrn9880261.shtml)；[Tencent Cloud techpedia](https://www.tencentcloud.com/techpedia/142828)
- 企业微信官方 SDK WecomTeam/aibot-node-sdk（`@wecom/aibot-node-sdk`）：105 stars，创建 2026-03-07；"基于 WebSocket 长连接通道，提供消息收发、流式回复、模板卡片、事件回调、文件下载解密、媒体素材上传"；AES-256-CBC 文件解密、分片上传约 50 MB、自动重连；README 标注 MIT（GitHub 许可证字段为空） — [GitHub](https://github.com/WecomTeam/aibot-node-sdk)；[raw README](https://raw.githubusercontent.com/WecomTeam/aibot-node-sdk/main/README.md)
- 企微社区 OpenClaw 插件：sunnoy/openclaw-plugin-wecom（`@sunnoy/wecom`）705 stars，ISC，最近 push 2026-05-25，"流式输出、动态 Agent 管理、群聊集成、指令白名单" — [GitHub](https://github.com/sunnoy/openclaw-plugin-wecom)；dingxiang-me/OpenClaw-Wechat 532 stars，MIT，最近 push 2026-03-15，"长连接模式/支持群聊/白名单/文档能力" — [GitHub](https://github.com/dingxiang-me/OpenClaw-Wechat)；第三方 Python/Go/Java 企微智能机器人 SDK 见 [chengyongru/wecom_aibot_sdk](https://github.com/chengyongru/wecom_aibot_sdk)、[go-sphere/wecom-aibot-go-sdk](https://github.com/go-sphere/wecom-aibot-go-sdk)、[37176427/wecom-aibot-java-sdk](https://github.com/37176427/wecom-aibot-java-sdk)
- 一站式"中国套件"：BytePioneer-AI/openclaw-china（飞书/钉钉/QQ/企微/微信）3,964 stars、340 forks，**仓库无许可证文件**，最近 push 2026-06-12 — [GitHub](https://github.com/BytePioneer-AI/openclaw-china)；justlovemaki/openclaw-china-docker 3,722 stars，GPL-3.0，预装飞书/钉钉/QQ/企微插件的 Docker 镜像，最近 push 2026-04-14 — [GitHub](https://github.com/justlovemaki/openclaw-china-docker)；Apifox 部署手册称 `@openclaw/feishu` 官方插件预装于 Docker 镜像、钉钉为 `openclaw-channel-dingtalk` 社区插件、企微为 `@sunnoy/wecom` — [Apifox：OpenClaw 多平台部署手册](https://apifox.com/apiskills/openclaw-docker-compose-feishu-dingtalk-wecom/)
- MaxClaw 为 MiniMax 基于 OpenClaw 的云托管版（非开源），支持企微/微博/飞书/钉钉接入与 ClawHub 技能发布 — [博客园 JavaGuide：MaxClaw](https://www.cnblogs.com/javaguide/p/19707575)
- 其他轻量替代：oujingzhou/openmozi（飞书/钉钉/QQ/企微，Apache-2.0，188 stars，最近 push 2026-05-21） — [GitHub](https://github.com/oujingzhou/openmozi)；hexagon-codes/hexclaw（Go，Apache-2.0，20 stars，RAG+Skill+MCP+沙箱+飞书/钉钉） — [GitHub](https://github.com/hexagon-codes/hexclaw)

**飞书开放平台官方 SDK**
- larksuite/oapi-sdk-go 621 stars、MIT、最近 push 2026-09-10；oapi-sdk-python 559 stars、MIT、2026-08-19；node-sdk 294 stars、MIT、2026-09-14 — [oapi-sdk-go](https://github.com/larksuite/oapi-sdk-go)；[oapi-sdk-python](https://github.com/larksuite/oapi-sdk-python)；[node-sdk](https://github.com/larksuite/node-sdk)

**Xiaozhi（硬件语音端，不是 IM 框架）**
- 78/xiaozhi-esp32："An MCP-based chatbot"，30,417 stars，MIT，C++，ESP32 硬件 — [GitHub](https://github.com/78/xiaozhi-esp32)；xinnan-tech/xiaozhi-esp32-server 后端 10,731 stars，MIT，topics 含 dify — [GitHub](https://github.com/xinnan-tech/xiaozhi-esp32-server)

### Inferences
- "群聊 @ 发起任务、结果回写飞书文档"在开源侧最接近的实现 = OpenClaw + larksuite/openclaw-lark（文档/多维表格写入 + 卡片状态更新）；"钉钉任务状态推送群消息" = OpenClaw + 钉钉官方插件的 AI Card 流式与文档/待办写入；"企微文件同步到企微文件库"目前只看到"发文件给用户/写入智能表格"，未见直接写入"微盘/文件库"的开源实现。
- LangBot/AstrBot 更偏"把对话型 Agent（含 Dify/Coze）送进 IM"，强在渠道广与插件多；但它们的 README 未声明对飞书文档/多维表格的回写能力。
- 以用户 OAuth 身份行事（飞书官方插件）意味着权限边界等于个人权限，企业若要"按角色分级"仍需在平台层控制。

### Gaps
- 企业微信官方"写入智能表格"、"文件库同步"能力只能引用被拦截页面的搜索摘要，未能核实细节与限制。
- LangBot README 提到的"飞书组织架构同步、审批流、日历会议"仅见第三方文章，官方 README 未直接列出。
- 各 IM 框架是否支持"任务状态推送（进行中/完成）"的结构化消息，除 OpenClaw 官方插件卡片外无明确来源。

---

## 关键问题 4：(C) "轻系统"——自然语言生成看板/表单/台账/在线应用的开源方案

### Takeaway
"一句话生成可分享的数据管理/信息收集/看板"在开源侧最成熟的是 NocoBase（AI Employees + 2026-02 许可证转为 Apache-2.0 基底并开源原商业插件）、Teable（AI Database Agent，可生成带登录/自定义域名的应用，AGPL-3.0 核心）和 NocoDB（NocoAI，但 2026-01 起改为 Sustainable Use License，不再是 OSI 开源；自托管 AI 需 Business 计划）；ToolJet/Appsmith/Budibase 的 AI 生成多页应用能力在 2025–2026 跟进；Dyad/bolt.diy/Open Lovable/Onlook 属"AI 生成前端代码"工具，不含权限/分享/数据台账体系，且 bolt.diy 与 Open Lovable 已停止更新。

### Cited Findings

**NocoBase（nocobase/nocobase）**
- GitHub 元数据：24,451 stars，最近 push 2026-10-05，许可证字段 NOASSERTION，描述"open-source AI + no-code platform for building business systems" — [GitHub](https://github.com/nocobase/nocobase)
- LICENSE.txt：Apache-2.0 + 补充条款（冲突时补充条款优先）；社区版可商用但不得对外提供"无代码/零代码/低代码/AI 平台类 SaaS/PaaS 产品"；不得移除 NocoBase 品牌与知识产权声明；不得基于本软件开发并销售开发者工具；商业版分 Standard / Professional / Enterprise — [raw LICENSE.txt](https://raw.githubusercontent.com/nocobase/nocobase/main/LICENSE.txt)
- 2026-02-26 公告：许可证由 AGPL-3.0 更新为 Apache-2.0（基底），并开源一批原商业插件（AI LLM、Steps Form、Tree block、Comments、Custom Variables、ECharts 数据可视化、Embed NocoBase、Code field、Form Drafts 等）；其余商业插件不再单卖而打包进商业许可；商业版 Standard $800、Professional $8,000、Enterprise 议价（dev.to 镜像被拦截，依据搜索摘要） — [NocoBase 博客：Weekly Updates 2026-02-26](https://www.nocobase.com/en/blog/weekly-updates-20260226)；[NocoBase 许可与定价调整](https://www.nocobase.com/en/blog/pricing-adjustment-202602)；[NocoBase Commercial](https://www.nocobase.com/commercial.html)
- AI Employees：内置 Atlas（Team Leader/总入口）、Cole（NocoBase 助手，问答与文档检索）、Ellis（邮件专家）、Dex（数据整理）；支持多会话并行；聊天回复可展示引用的知识库文档；2.1-beta 把 CLI 集成与 AI-powered building 纳入 NocoBase Skills，支持 AI 插件开发（官网/文档被拦截，依据搜索摘要） — [NocoBase Docs：Built-in AI Employees](https://docs.nocobase.com/ai-employees/features/built-in-employee)；[NocoBase 博客：2.1-beta](https://www.nocobase.com/en/blog/2.1.0-beta)；[firecat 日报：NocoBase 推出 AI 员工](https://www.firecat-web.com/daily-news/8535)
- 新主版本 nocobase/nocobase3 仓库创建 2026-08-14（develop 分支，beta 发布 2026-09-29.1） — [GitHub: nocobase/nocobase3](https://github.com/nocobase/nocobase3)；[Release 2026-09-29.1](https://github.com/nocobase/nocobase3/releases/tag/release-beta/2026-09-29.1)

**Teable（teableio/teable）**
- GitHub 元数据：21,856 stars，最近 push 2026-10-02，许可证字段 NOASSERTION，官网 teable.ai，"AI Spreadsheet for Business" — [GitHub](https://github.com/teableio/teable)
- LICENSE：核心应用（NestJS 后端、Next.js 前端）AGPL-3.0，`packages` 目录下全部包 MIT；依据 AGPL 第 7 条附加品牌保护（不得修改/替换/移除 Teable 品牌资产） — [raw LICENSE](https://raw.githubusercontent.com/teableio/teable/develop/LICENSE)
- "全球首款 AI Database Agent"：自然语言完成对话式建库、生成应用、自动化流程、数据分析与批量内容生成；发票/合同/简历可对话式抽取为结构化数据；2025-09 完成数百万美元天使轮 — [腾讯新闻：Teable 天使轮](https://news.qq.com/rain/a/20250919A0827Y00)；[极客公园](https://www.geekpark.net/news/354293)；[少数派评测](https://sspai.com/post/106231)
- 厂商博客：Agent 可建表、处理文件、查询记录、构建工作流并生成读写同一实时数据的应用；近期版本为生成的应用加入可视化编辑、内置登录、自定义域名、环境变量、运行时日志与应用内 AI — [teable.ai 博客（厂商）](https://teable.ai/blog/best-no-code-ai-app-builders)
- 分享与权限：链接分享可设"任何人/仅空间成员/密码保护"；短链；SSO(OIDC) 在社区版免费；企业版 EE 含 AI Field、Automation、Authority Matrix；Business $20/席位/月（help 站被拦截，依据搜索摘要与社区帖） — [Teable Community：SSO in self-hosting](https://community.teable.ai/t/can-i-use-sso-authentication-with-teable-self-hosting-edition/34)；[Teable 安全文档](https://help.teable.ai/en/basic/security)；[Teable 自托管定价](https://app.teable.ai/public/pricing?host=self-hosted)

**NocoDB（nocodb/nocodb）**
- GitHub 元数据：65,187 stars，最近 push 2026-10-05，许可证字段 NOASSERTION — [GitHub](https://github.com/nocodb/nocodb)
- LICENSE.md：Sustainable Use License v1.0，生效日期 2026-01-29；仅允许内部业务用途与非商业/个人使用；分发仅限免费非商业；禁止商业分发/SaaS；master 与 develop 分支受此许可，其他分支未授权 — [raw LICENSE.md](https://raw.githubusercontent.com/nocodb/nocodb/develop/LICENSE.md)
- 官方许可页称"自 2026-01-09 起从 AGPL-3.0 转为 SUL"（与 LICENSE.md 的 2026-01-29 生效日不一致）；社区讨论"0.301.0 及之后不再开源"；2026.07.0 版本把 Calendar Sync、图片标注放在企业许可之后 — [NocoDB Docs：License（被拦截）](https://nocodb.com/docs/self-hosting/license)；[GitHub Discussion #12891](https://github.com/nocodb/nocodb/discussions/12891)；[bex.co：NocoDB's Enterprise Gate](https://bex.co/blog/2026/08/17/nocodb-enterprise-gate-open-core-trust-test)
- NocoAI：用自然语言生成完整 base（表、视图、字段、关系）、仪表盘、工作流、界面；云端 Plus 计划起按积分计费，**自托管需 Business 计划及以上并自配 AI 集成**（文档被拦截，依据搜索摘要） — [NocoDB Docs：NocoAI](https://nocodb.com/docs/product/noco-ai)；[Create Base using AI](https://nocodb.com/docs/product-docs/noco-ai/create-base)

**APITable / AITable（apitable/apitable）**
- 15,627 stars，AGPL-3.0，最近 push 2026-09-06 — [GitHub](https://github.com/apitable/apitable)
- 开源版 APITable"不含 AI 功能"，AI 能力在 AITable.ai 云/自托管商业版（自托管 $19,999/年起）；AITable MCP server（2026-09 更新）提供 Create/Update/Find Records 等 4 个工具 — [aitable.ai：Self-hosted Solution](https://aitable.ai/blog/self-hosted-solution/)；[activepieces：AITable MCP](https://www.activepieces.com/mcp/apitable)

**Appsmith / ToolJet / Budibase**
- Appsmith：41,016 stars，Apache-2.0，最近 push 2026-10-05；Appsmith Agents（2025 起，连接 OpenAI/Anthropic/Google，RAG）；2.3（2026-08-13）向社区版开放 Ask AI 并为自定义组件加入 AI copilot（第三方） — [GitHub](https://github.com/appsmithorg/appsmith)；[Appsmith Docs：Self Hosting](https://docs.appsmith.com/getting-started/setup)；[aiidelist 评测](https://aiidelist.com/ide/appsmith)
- ToolJet：41,037 stars，AGPL-3.0，最近 push 2026-10-05，描述"enterprise app generation platform for internal tools, dashboards…and AI agents"；厂商对比页称 ToolJet AI 可由自然语言生成多页应用、所有计划含 Free 可用、提供 MIT 许可的 MCP server（厂商页被拦截，依据搜索摘要） — [GitHub](https://github.com/ToolJet/ToolJet)；[ToolJet vs Appsmith（厂商）](https://tooljet.com/tooljet-vs-appsmith)；[ToolJet 博客：Appsmith vs Budibase vs ToolJet 2026](https://blog.tooljet.com/appsmith-vs-budibase-vs-tooljet/)
- Budibase：28,329 stars，最近 push 2026-10-05，许可证字段 NOASSERTION；LICENSE：整体 GPL-3.0，`/packages/pro` 为 Business Source License，内置于生成应用的组件包为 MPL-2.0（故生成的应用不受 GPL 约束） — [GitHub](https://github.com/Budibase/budibase)；[raw LICENSE](https://raw.githubusercontent.com/Budibase/budibase/master/LICENSE)；AI agents 于 2025 年底推出，模型无关、支持本地 LLM、自托管免费 — [Budibase 博客：Open-Source AI Agent Platforms](https://budibase.com/blog/ai-agents/open-source-ai-agent-platforms/)；[Budibase 定价](https://budibase.com/pricing/)
- refine（35,758 stars，MIT，React 内部工具框架，非 NL 生成）、illa-builder（12,329 stars，Apache-2.0，最近 push 2026-05-27） — [GitHub: refinedev/refine](https://github.com/refinedev/refine)；[GitHub: illacloud/illa-builder](https://github.com/illacloud/illa-builder)

**AI 生成前端应用类（Dyad / bolt.diy / Open Lovable / Onlook）**
- Dyad（dyad-sh/dyad）：21,660 stars，最近 push 2026-10-05；LICENSE：Apache-2.0，`src/pro/` 目录另有单独许可；本地桌面 AI app builder（v0/Lovable/Bolt 替代） — [GitHub](https://github.com/dyad-sh/dyad)；[raw LICENSE](https://raw.githubusercontent.com/dyad-sh/dyad/main/LICENSE)
- bolt.diy（stackblitz-labs/bolt.diy）：MIT；自 2026-02-07 起无代码合并（第三方）；GitHub API 本次未能检索到该仓库元数据，star 数与状态未核实 — [Dyad 博客：Free AI App Builders Compared](https://www.dyad.sh/blog/free-ai-app-builders-compared)；[Kunavo：Dyad vs Bolt.new vs bolt.diy](https://kunavo.com/guides/dyad-vs-bolt-new-vs-bolt-diy)
- Open Lovable（firecrawl/open-lovable，原 mendableai）：28,635 stars，MIT，**最近 push 2025-11-19**（已停更近一年），定位"Clone and recreate any website as a modern React app" — [GitHub](https://github.com/firecrawl/open-lovable)
- Onlook（onlook-dev/onlook）：26,855 stars，Apache-2.0，最近 push 2026-08-25，"AI-first design tool…visually build, style, and edit your code" — [GitHub](https://github.com/onlook-dev/onlook)

**可嵌入 Agent 工作流的证据**
- NocoBase：AI 员工内嵌于平台页面/数据块（见上）；Teable：Agent 生成的应用读写同一实时数据（厂商）；NocoDB：第三方 MCP 集成（Composio） — [Composio：NocoDB MCP](https://composio.dev/toolkits/nocodb)；AITable MCP（activepieces）；ToolJet 自称提供 MIT MCP server（厂商）

### Inferences
- 对照灵策"一句话生成 + 链接分享 + 账号权限/可见范围 + 可嵌入数字员工流程"，Teable 与 NocoBase 是最完整的开源对应；NocoDB 在许可证上已不满足"真正开源"约束（SUL 非 OSI），只能作"源代码可得、内部自用免费"收录。
- Dyad/bolt.diy/Open Lovable/Onlook 生成的是代码工程，缺少表单/台账/看板的数据层与权限层，不适合直接当"轻系统"底座。
- 若要让"轻系统可被数字员工调用"，当前可行路径是通过 MCP/API（NocoBase 插件、Teable/NocoDB/AITable 的 MCP 或 REST），而非平台原生的 Agent 执行流编排。

### Gaps
- NocoBase 2.1-beta 的"AI-powered building"是否能从一句话直接生成完整页面/表单/看板，官方文档被拦截，未核实细粒度。
- Teable 应用生成器是否在社区版可用、是否依赖云端 AI 额度，未从一手来源核实。
- ToolJet AI 生成"所有计划含 Free"仅见厂商页，未核实自托管社区版的实际可用性。

---

## 关键问题 5：哪些组合最接近灵策智算的企业级功能集？缺口在哪里？

### Takeaway
最接近的开源组合是 "LLMOps 平台（Dify 社区版 / MaxKB / Bisheng）+ 本地执行 Agent（OpenClaw 或 DeerFlow）+ 官方 IM 通道（larksuite/openclaw-lark、DingTalk-Real-AI 插件、企微长连接 SDK）+ 轻系统（NocoBase 或 Teable）"；但"岗位化数字员工 + 技能自进化 + 手机远程操控 + 定时任务试跑 + 跨工具统一审计/运营看板"仍需自研粘合层，且 Dify/FastGPT 的多租户限制使"个人版免费 + 企业私有化"商业模式不能直接搬用。

### Cited Findings
- Dify 社区版多租户受限、企业版才有 RBAC/审计/多工作空间 — [raw LICENSE](https://raw.githubusercontent.com/langgenius/dify/main/LICENSE)；[阿里云](https://help.aliyun.com/zh/dms/introduction-to-dify-enterprise-edition)
- LangBot 可把 Dify/Coze/n8n/Langflow 流水线绑定到企微/飞书/钉钉机器人，并与 OpenClaw / Hermes / DeerFlow 集成 — [GitHub 描述](https://github.com/langbot-app/LangBot)；[raw README_CN](https://raw.githubusercontent.com/langbot-app/LangBot/master/README_CN.md)
- OpenClaw 自带 cron、Skills 市场、exec 审批、多模型 — [OpenClaw Docs：Automation](https://docs.openclaw.ai/automation)；[Tools](https://docs.openclaw.ai/tools)
- 飞书/钉钉官方 OpenClaw 插件可写文档/多维表格/待办、卡片流式状态 — [openclaw-lark README](https://raw.githubusercontent.com/larksuite/openclaw-lark/main/README.md)；[dingtalk-openclaw-connector README](https://raw.githubusercontent.com/DingTalk-Real-AI/dingtalk-openclaw-connector/main/README.md)
- DeerFlow 提供沙箱、记忆、Skills、子 Agent、定时任务与多 IM 网关，PostgreSQL 多实例 — [raw README_zh](https://raw.githubusercontent.com/bytedance/deer-flow/main/README_zh.md)
- CowAgent 自述"self-evolves with memory and knowledge. Multi-agent, multi-model, multi-channel" — [GitHub: zhayujie/CowAgent](https://github.com/zhayujie/CowAgent)；Hermes Agent 被第三方称为"self-improving AI agent" — [tosea.ai](https://tosea.ai/blog/hermes-agent-self-improving-ai-guide)；Letta "learn and self-improve over time" — [GitHub](https://github.com/letta-ai/letta)
- UniEmployee 提供岗位化数字员工（客服/数据分析/销售/HR 等）+ HITL 审批 + 可观测，但无 IM 通道 — [raw README](https://raw.githubusercontent.com/zj-unicom-ai/UniEmployee/main/README.md)
- NocoBase 社区版禁止对外提供低代码/AI 平台 SaaS — [raw LICENSE.txt](https://raw.githubusercontent.com/nocobase/nocobase/main/LICENSE.txt)

### Inferences
- 组合 A（中国团队、治理较全）：MaxKB 专业版/Bisheng（知识库 + RBAC/SSO）+ OpenClaw（本地执行/定时/Skills）+ 飞书/钉钉官方插件 + NocoBase（轻系统）。缺口：三套系统各自的审计与看板无法统一；OpenClaw 以个人 OAuth 身份行事，缺企业级多租户与角色隔离；"技能自进化"只有 CowAgent/Hermes/Letta 类个人 Agent 具备，且无企业审批闭环。
- 组合 B（全 MIT/Apache，许可证最干净）：Coze Studio（Agent 编排）+ DeerFlow（沙箱执行/定时/IM 网关）+ Teable（轻系统，AGPL 核心）。缺口：Coze Studio 单用户、近期停更；Teable 核心 AGPL 对私有化二开有传染顾虑。
- 组合 C（IM 优先）：LangBot/AstrBot + Dify + NocoBase。缺口：LangBot/AstrBot 不负责文档回写与企微文件库同步；桌面本地执行与手机远程操控均无对应。
- 灵策独有、开源侧基本空白的点：手机远程操控本地桌面 Agent、定时任务启用前试跑、Skill 调用次数/任务完成率/活跃成员运营看板、上下文库（Context library）团队共享、企业微信会话侧边栏调用。

### Gaps
- 没有找到任何开源项目公开声明支持"手机远程操控本地桌面 Agent"这一形态；OpenClaw 文档是否覆盖移动端远程控制未核实。
- "企业微信会话侧边栏（JS-SDK 侧边栏应用）调用 AI"在本次检索的开源项目中无对应实现。

---

## 关键问题 6：中文社区活跃度与企业落地案例

### Takeaway
中文社区活跃度最高的是 OpenClaw 生态（飞书/钉钉/企微官方插件 + 多个 3,000+ star 的"中国套件"）、CowAgent（47k）、AstrBot（41k）、LangBot（18k）；企业落地宣传最充分的是 MaxKB（厂商称 1000+ 付费客户）、FastGPT（深信服联合商业版、厂商称 20,000+ 企业客户）和 Dify（顺丰等案例、阿里云联合企业版）。

### Cited Findings
- MaxKB："1000+ 付费客户""累计免费安装量 100 万+"，客户含深圳通、华莱士、广西质检院、东北财经大学、深圳大学附属华南医院；近期新增河北鑫达集团、广州港南沙集装箱码头、国元期货等（厂商口径） — [53AI](https://www.53ai.com/news/MaxKB/2026062913680.html)；[CSDN 飞致云月报](https://blog.csdn.net/FIT2CLOUD/article/details/149675550)
- FastGPT：深信服 × FastGPT 联合发布 SF-FastGPT（2026-04-03）；"50 万+ 全球注册用户、500+ 合作服务企业"、"商业版服务 20,000+ 企业客户"（厂商口径）；案例含朝阳永续金融终端 AI 搜索、医院导诊助手 — [CSDN 资讯](https://www.csdn.net/article/2026-04-03/159793336)；[FastGPT 客户案例中心](https://fastgpt.cn/customers)
- Dify：顺丰内部 AI 智能助手；阿里云 DMS 与 Dify 官方合作提供企业版；宁波思艾特等服务商宣称服务 500+ 企业（服务商口径） — [腾讯云社区：顺丰案例](https://cloud.tencent.cn/developer/article/2505495)；[阿里云 Dify 企业版](https://help.aliyun.com/zh/dms/introduction-to-dify-enterprise-edition)；[中华网：Dify 融完资企业应该找谁落地](https://tech.china.com/jujiao/2026/0507/1862231.html)
- Bisheng："服务大量行业头部组织及世界 500 强企业"（README 自述） — [raw README_CN](https://raw.githubusercontent.com/dataelement/bisheng/main/README_CN.md)
- LangBot："已被多家企业采用"（README 自述）；优云智算提供一键部署文档 — [raw README_CN](https://raw.githubusercontent.com/langbot-app/LangBot/master/README_CN.md)；[优云智算文档](https://www.compshare.cn/docs/operation/bestpractices/installlangbot)
- OpenClaw 中文生态：企业微信 2026-03-08 官方推送三步接入；钉钉官方插件；飞书官方插件 2026-03 上线；阿里云开发者社区多篇 2026 年钉钉对接教程；中文社区站 clawd.org.cn — [53AI：25 万 Star 的 OpenClaw 入乡随俗](https://www.53ai.com/news/Openclaw/2026030969083.html)；[阿里云开发者社区教程](https://developer.aliyun.com/article/1711502)；[clawd.org.cn](https://clawd.org.cn/)
- 华为云市场出现"OpenClaw 基础部署/企业级定制部署"服务商品（2026-03） — [华为云市场附件 PDF](https://mkp-res.hc-cdn.com/marketplace/public/appv2/attachment/977/6FB/8F2/00000000009776FB8F2.20260316093227.415aa7fae6834b74a54371163b3c0725.pdf)
- UniEmployee 由浙江联通维护并提供企业部署联系方式 — [raw README](https://raw.githubusercontent.com/zj-unicom-ai/UniEmployee/main/README.md)

### Inferences
- 客户数字（MaxKB 1000+、FastGPT 20,000+）全部为厂商/合作方口径，口径差异巨大（FastGPT 的"企业客户"可能含云端付费小团队），不宜直接横向比较。
- 2026 年中国企业 IM 三巨头对 OpenClaw 的官方背书，使"本地 Agent + IM 官方插件"成为落地门槛最低的路径，这与灵策智算"飞书/钉钉/企微统一入口 + 本地执行"的卖点正面重叠。

### Gaps
- 未找到独立第三方（非厂商、非服务商）对上述客户数的核实报道。
- RAGFlow、Bisheng、LangBot、AstrBot 的具名企业客户案例未找到公开来源。
