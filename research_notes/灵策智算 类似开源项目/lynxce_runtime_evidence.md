# 灵策智算桌面端的底层运行时（2026-10-05 核读）

## Takeaway

灵策智算自己的官方更新日志写明其桌面端运行在 OpenClaw 之上，并有第二套引擎"DeepSeek Harness"（后更名 Lynxce Harness）。它的会话层名称、插件清单和本地存储与网易有道开源的 LobsterAI（MIT）逐项吻合，因此极可能是 LobsterAI 的二次开发版。官网与更新日志均未提到 LobsterAI，安装包未拆解，所以"基于 LobsterAI"属高可信推断。

## Cited Findings

**来源一：灵策智算官方更新日志。** 官网 https://ai.lynxce.cn/changelog.html 由页面脚本调用公开接口 `https://api.lynxce.cn/api/client/changelog` 加载内容；原始响应存于 `lynxce_site/changelog.json`（82 条，桌面端最早一条为 2026-06-20 的 1.1.9 版）。

| 版本 | 日期 | 原文 |
|---|---|---|
| 1.3.3 | 2026-07-07 | 提升 OpenClaw 缓存、目录识别和工具启动稳定性 |
| 1.7.8 | 2026-08-17 | 新增deepseek harness 引擎 |
| 1.8.8 | 2026-08-18 | 新增Lynxce harness |
| 1.9.2 | 2026-08-22 | Lynxce Harness 新增插件市场，支持浏览、安装及管理第三方插件 |
| 2.0.0 | 2026-09-05 | Lynxce Harness 对齐 DeepSeek Harness 0.1.3 |
| 2.0.7 | 2026-09-16 | 升级 OpenClaw，两套 Harness 对齐官方新版本，改善启动、历史会话迁移和异常恢复 |
| 2.0.7 | 2026-09-16 | 改善飞书、云信和 Bee 插件的新版引擎兼容性 |
| 2.0.8 | 2026-09-18 | 加强 OpenClaw 稳定性：改善网关启动、残留锁恢复、飞书加载、浏览器异常和任务运行期间的配置更新 |
| 2.1.0 | 2026-09-21 | SQLite 启动失败：修复 Windows 管理员账号权限校验误判 |
| 1.3.5 等 | 2026-07 起 | 对话模式称为"cowork"（如"cowork 新增auto/max模式"） |

**来源二：灵策智算下载接口。** `https://api.lynxce.cn/api/client/downloads`（原始响应存于 `lynxce_site/downloads.json`）列出已发布安装包：Windows x64 2.1.9（2026-10-04）、macOS Apple Silicon 2.0.6（2026-09-11）、macOS Intel 1.5.7（2026-07-28）、iOS 1.1.0（App Store，2026-09-09）、Android 1.2.3（2026-10-05）。首页静态 HTML 中的"敬请期待/内测中"是脚本加载前的占位，官网文案仍称"邀请制内测"。

**来源三：灵策智算手机端更新日志（同一接口）。** 1.2.1/1.2.2（2026-10-02）："任务可以交给云端或你自己的电脑执行，执行过程实时可见，AI 有问题时直接在手机上回答""云端和电脑做好的 PPT、文档可以在手机上逐页预览和下载"。桌面 2.1.9（2026-10-04）："加强手机与电脑协作：完善手机派发电脑任务、执行进度展示、交互确认及成果文件回传""新增跨端记忆同步"。

**来源四：LobsterAI 仓库。**
- README_zh（https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/README_zh.md）："Cowork 是 LobsterAI 的产品与会话层，OpenClaw 是底层运行时和网关"；有独立一节"DeepSeek Harness Runtime"；"会话和应用数据保存在本地 SQLite"；IM 远程控制渠道为"微信、企业微信、钉钉、飞书/Lark、QQ、Telegram、Discord、网易云信 IM、网易小蜜蜂、POPO 和邮件"。
- package.json（https://raw.githubusercontent.com/netease-youdao/LobsterAI/main/package.json）：`version` 2026.9.23；`dsh` 字段锁定 `@deepseek-ai/dsh` 0.1.5-rc.3；`openclaw` 字段锁定 OpenClaw v2026.8.1，插件包括模型提供方 qwen、deepseek、moonshot、qianfan、stepfun、zai、xiaomi、volcengine，以及渠道 dingtalk-connector、openclaw-lark、qqbot、discord、wecom-openclaw-plugin、openclaw-weixin、moltbot-popo、openclaw-nim-channel、openclaw-netease-bee、clawemail-email。

## Inferences

- 灵策智算"飞书、云信和 Bee 插件"与 LobsterAI 的 openclaw-lark、openclaw-nim-channel（网易云信）、openclaw-netease-bee（网易小蜜蜂）三件插件一一对应。网易云信与网易小蜜蜂是网易自家通讯产品，出现在一家福建公司的插件列表里难以用巧合解释。
- "两套 Harness"对应 LobsterAI 的 OpenClaw 与 DeepSeek Harness（dsh）双引擎；"Lynxce Harness"是对 dsh 的更名封装，2.0.0 版写明"对齐 DeepSeek Harness 0.1.3"。
- 加上 Cowork 命名、本地 SQLite 与几乎相同的"7×24 小时全场景个人助理 Agent"定位，灵策智算桌面端极可能由 LobsterAI 二次开发。MIT 许可允许商用改名，前提是保留版权与许可声明。
- 灵策智算在此之上的增量（据其更新日志）：云端执行与跨端记忆同步、原生 iOS/Android App 派发电脑任务、企业空间（灵策文档协同编辑、多维表格、企业 CRM、企业知识库引用）、内容生产类应用（追爆短视频、GEO 增长引擎、AI 直播数字人、PPT 工作台）、38 个行业数字员工模板。

## Gaps

- 未下载并拆解安装包（环境缺少 DMG/NSIS 解包工具，下载域名响应头不可见），无法核对包内的 package.json、LICENSE 或第三方声明。
- 灵策智算是否保留了 LobsterAI/OpenClaw 的版权与许可声明，未核实。
- 灵策智算与网易有道之间是否存在合作或授权关系，未核实。
