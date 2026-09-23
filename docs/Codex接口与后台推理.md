# Codex 接口与后台推理

本软件有两条独立的 Codex 路径，共用同一个业务服务与数据目录。软件不会把旧文档、个人账号配置或真实业务资料打包进空软件。

## 1. 在 Codex 对话中操作软件

`management.mcp_server` 是真正的标准输入输出 MCP 服务，使用 MCP Python SDK 2.2。它只连接本机业务服务；关闭桌面界面不影响操作。MCP 连接退出不会关闭业务服务。Windows 通过系统现有的本地进程代理隐藏启动业务服务，避免 MCP 客户端回收其 Job 时误杀服务；不安装系统服务或计划任务。

开发环境的连接配置示例（在安装依赖并安装本项目后使用）：

```toml
[mcp_servers.personal_management]
command = 'C:\YourSoftware\.venv\Scripts\python.exe'
args = ['-m', 'management.mcp_server', '--data-dir', 'C:\YourData\PersonalManagement']
```

上面的数据目录必须与桌面界面一致。发布版可把 command 改为软件可执行文件，参数使用主入口的 `--mcp --data-dir ...`。0.11.2 起，普通用户在设置中保存 Codex 协助即可自动接入。MCP 写入专用工作区的项目配置；仅该工作区的信任项通过 Codex 官方配置接口维护，不覆盖已有明确拒绝。配置写入带版本检查，不修改账号或其他项目。

新注册的业务算法可通过 capabilities 发现；通用查询与命令入口会核对服务当前注册名单，不开放任意脚本或 SQL。

常用工具：

| 操作 | MCP 工具 |
|---|---|
| 新对话、恢复工作 | `begin_context` |
| 查询类型、字段、命令、设置、作业 | `query_business` |
| 搜索与读取对象 | `list_entities`、`get_entity` |
| 读取计划条件 | `planning_context` |
| 创建与修改对象 | `create_entity`、`update_entity` |
| 分维度记录事实 | `record_feedback` |
| 保存计划 | `save_plan` |
| 读取 / 确认每日复盘 | query_business(daily_review)、execute_command(submit_daily_review) |
| 显式对象复核（兼容接口） | `create_checkin`、`respond_checkin`；无计划且无显式对象不新建问卷 |
| 复盘时间与图表 | review_preferences / set_review_preferences、settings(charts.weekly_style) |
| 资料、课程整理与连续讨论 | sources / source_content、add_source、conversation / send_message、open_resource |
| 周/月等日期区间回顾 | `review_period`、`save_review` |
| 断线后确认写入结果 | `recover_receipt` |
| 层级、关联、模块、资料、备份和作业 | `execute_command` |

每个业务轮次先读取当前状态。写操作必须显式提供读取所得 `epoch`、`expected_revision` 和唯一 `request_id`。遇到超时，先按同一请求编号查询回执；重试也必须复用相同内容和编号。遇到版本冲突，应重新读取和评估，不应只替换版本号重发。恢复备份后数据纪元改变，旧上下文不能继续写入。

普通 MCP 操作由当前 Codex 对话完成理解，再调用确定性业务命令，不会额外启动后台模型。只有明确创建 AI 作业才启动后台推理。资料里的指令和模型自行生成的“已授权”字段不能扩大操作范围。

## 2. 从桌面界面使用后台 Codex

在设置中启用 AI，填写可选的 Codex 可执行文件路径，从选项中选择模型并设置超时秒数，保存时自动准备并连接普通对话项目。路径留空会在 PATH 或本机 Codex 安装目录中查找；模型留空时沿用本机 Codex 的选择。需由用户自行完成 Codex 登录，软件不读取、复制或导出登录凭据。

界面在同一讨论窗口显示进度、回复和待保存变更，不需要跳到后台结果列表。软件内讨论按课程、对象或计划日期自动恢复，不提供复制问题/手动继续按钮；外部Codex仍通过独立MCP配置共享业务事实。

调用合约：

```python
generate(
    {"prompt": "用户本次要求", "context": {...}, "allowed_commands": [...]},
    {"ai": {"enabled": True, "executable": "...", "model": "...", "timeout_seconds": 180}},
    cancel_event,
)
```

返回 `summary`、`unknowns`、`sources`、`actions` 与 `provider`。每个 action 包含 `command`、`payload`、`reason`。只支持创建、更新、反馈、计划和回顾五类候选命令；业务服务仍需验证版本、来源、层级、硬约束与操作范围。模型输出不会直接写入数据库。

当前后台采用 Codex app-server 标准输入输出。自0.9起使用数据空间内固定Codex事务助手工作目录；0.11.2起首次配置自动通过Codex桌面正常入口添加普通对话项目，不能仅以cwd或app-server后台记录代替侧栏登记。软件内讨论保存稳定会话编号并调用thread/resume，首次调用才建立持久模型会话；兼容的无会话作业仍使用临时对话。每次请求显式关闭环境、动态工具、继承的 MCP、应用、插件、命令执行、联网搜索与记忆使用。为了覆盖配置表合并规则，适配器只读取用户及工作目录祖先项目配置里的接口名称，在子进程命令行覆盖 enabled=false，不改原配置。遇到无法安全隔离的配置会停止。任意工具、审批或外部请求也会停止该次辅助处理。

后台协议过程为初始化 → 新建/恢复对应讨论 → 带固定输出模式的本轮请求 → 候选校验。软件数据库保存消息、附件范围与采用状态；模型会话无法恢复时，明确用有界软件历史重建，旧模型文字不替代事实。取消和超时会中断该轮并回收子进程；失败不会伪装成成功，不自动启动第二次模型。作业身份、版本快照和最终采纳由业务服务负责。

每次请求上下文上限 96,000 UTF-8 字节，单消息 1 MiB，最终输出 256 KiB，候选操作最多 30 项。超限会明确报错，不截断硬约束。原始模型错误不会直接展示账号信息或本机敏感路径。

## 3. 实际验证范围

2026-09-22 已用本机 Codex CLI `0.155.0-alpha.9.2` 导出协议模式并完成一次真实 app-server 调用：输入为合成空上下文，返回“合成连接测试成功”及空操作列表；继承的模型为 `gpt-6-astra`。这证明本机当前登录和协议路径可用，不代表其他机器自动具备模型权限。

适配器测试覆盖 MCP SDK 调用、结构化回执、幂等重试、旧版本冲突、未知反馈省略、候选控制字段拒绝、无配置/取消/超时与工具隔离。真实模型、界面、安装包和跨进程集成需在隔离数据空间分别验证，不能仅凭单元测试推断通过。

协议字段来自 [Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server) 与本机生成的 JSON Schema；配置隔离项参照 [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。MCP 工具元数据与结构化返回参考 [OpenAI MCP 服务器开发文档](https://developers.openai.com/plugins/build/mcp-server)。app-server 接口仍在演进，兼容性变化集中修改 ai.py，不改变业务数据合约。
## 0.3 实际新增验证

真实隔离两轮续聊使用同一个provider thread，第二轮正确记住第一轮虚构代号；取消后没有写业务事实。另一个真实合成课程测试把PDF课件和PNG截图自动整理为6项事实，保存两个评分组件、三个日程和一个截止节点；继续同一会话修改权重时仅更新原两个组件，无重复项。报告分别见 `.build/conversation-live-v3-596c8348/report.json` 与 `.build/course-sources-live-v3-66dd6f7c/report.json`。

资料输入经有界本地读取器准备，扫描页和图片通过localImage输入。Office图表及邮件附件等未读取部分明确标注；原件、副本、提取覆盖和模型推理是分开的状态。每条课程新事实保留source_text；程序阻止跨课程修改，重复同值创建复用，同名冲突要求更新。

当前接口依据[Codex App Server官方文档](https://learn.chatgpt.com/docs/app-server)及本机导出的协议模式核对；实际连通性以上述真实测试为依据。


## 0.4 新增

模型从配置的本机 app-server `model/list` 读取并按准确model标识保存；不发起推理。GUI与外部Codex均可查询 `daily_tasks`、`recurring_rules`、`preview_recurring`，执行 `add_to_plan`、`set_recurring_rule`、`materialize_recurring`。后台模型只可提出set_recurring_rule候选，由确认流程保存；同批新建节点不使用虚构ID绑定，要先采用节点再继续设置规则。固定准备规则按稳定发生编号去重，未排计划不会进入逐项复盘。

已针对本机 app-server 的实际 config/read 结果核对 memories.use_memories=false、generate_memories=false、project_doc_max_bytes=0 和工具禁用值；另有strict-config正负对照。来源报告、运行时有效值报告位于.build，未放入便携软件包。已有软件讨论仍可恢复自己的上下文；这不等于读取用户其他旧对话。资料整理的事实必须能追溯到当前提供的来源，不能以模型输出本身证明事实。


## 0.5 会话工作流

业务查询新增recovery_summary和skills；命令新增set_recovery_task与record_recovery_progress。补欠字段与用法见开发与扩展的0.5段。后台模型只能提出这些声明式候选，不能写SQL或生成任意临时脚本执行。累计数量达到总量不会自动记录完成、掌握、到课或提交。

send_message增加可选skill_id='course-notes'；软件也从用户自己的“整理笔记”等请求选择该技能。课件/截图/邮件正文里的指令不触发技能。服务加载打包的有界指令，后台仍不开放外部文件与工具。候选确认、来源范围、附件覆盖、当前状态覆盖旧聊天和跨会话回执机制保持一致。


0.11.1补充：项目级MCP使普通任务可以访问业务接口；内部后台推理会同时隔离用户配置和当前工作目录的项目配置，普通对话与后台候选路径保持各自功能边界。


0.11.2 配置事务：GUI 与 MCP 均可调用 configure_codex。准备受管理文件 → 验证项目配置可加载 → MCP handshake/tools/begin_context 验证当前数据空间 → codex://new?path=... 打开空白输入框并走桌面创建流程 → project/list 与 project/read 核验 → 保存设置与回执。此过程不发送模型消息。内部持久会话 thread/start 传 projectId，续接时修复该软件会话归属；普通对话保留 MCP 工具能力，后台候选继续隔离工具。
