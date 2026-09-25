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

后台协议过程为初始化 → 原任务绑定核对 → 受控 MCP 按需读取 → 分批检查点 → 完整候选校验。软件保存消息、来源版本、处理进度及采用状态。恢复结果不明时先核对原任务，不另建同名任务；旧聊天记忆不替代当前数据库事实。已确认任务身份的临时连接故障最多自动接续三次，始终使用原操作；取消后不会自动接续。

0.14 使用 context/1：初始索引不超过 24 KiB，读取页目标 12 KiB、上限 16 KiB。每阶段累计预算 64 KiB，预留 8 KiB，同时计算工具参数、返回内容与图像估算；另有 128 次工具调用保护。达到边界后在原任务完成上下文压缩并接续，不能手工重置配额冒充压缩。每批最多 30 个候选动作，总候选分批保存、分页核对，并在一次业务事务中全部采用；30 不再是整项任务的总动作上限。单条用户消息上限 800,000 UTF-8 字节，完整原文通过 @goal 分段读取。HTTP 消息上限 1 MiB 是传输保护，不是数据库范围上限。

## 3. 实际验证范围

2026-09-22 已用本机 Codex CLI `0.155.0-alpha.9.2` 导出协议模式并完成一次真实 app-server 调用：输入为合成空上下文，返回“合成连接测试成功”及空操作列表；继承的模型为 `gpt-6-astra`。这证明本机当前登录和协议路径可用，不代表其他机器自动具备模型权限。

适配器测试覆盖 MCP SDK 调用、结构化回执、幂等重试、旧版本冲突、未知反馈省略、候选控制字段拒绝、无配置/取消/超时与工具隔离。真实模型、界面、安装包和跨进程集成需在隔离数据空间分别验证，不能仅凭单元测试推断通过。

协议字段来自 [Codex App Server 官方文档](https://learn.chatgpt.com/docs/app-server) 与本机生成的 JSON Schema；配置隔离项参照 [Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。MCP 工具元数据与结构化返回参考 [OpenAI MCP 服务器开发文档](https://developers.openai.com/plugins/build/mcp-server)。app-server 接口仍在演进，兼容性变化集中修改 ai.py，不改变业务数据合约。
## 0.3 实际新增验证

真实隔离两轮续聊使用同一个provider thread，第二轮正确记住第一轮虚构代号；取消后没有写业务事实。另一个真实合成课程测试把PDF课件和PNG截图自动整理为6项事实，保存两个评分组件、三个日程和一个截止节点；继续同一会话修改权重时仅更新原两个组件，无重复项。报告分别见 `.build/conversation-live-v3-596c8348/report.json` 与 `.build/course-sources-live-v3-66dd6f7c/report.json`。

资料输入经有界本地读取器准备，扫描页和图片通过 MCP 原生图像块输入。Office图表及邮件附件等未读取部分明确标注；原件、副本、提取覆盖和模型推理是分开的状态。每条课程新事实保留source_text；程序阻止跨课程修改，重复同值创建复用，同名冲突要求更新。

当前接口依据[Codex App Server官方文档](https://learn.chatgpt.com/docs/app-server)及本机导出的协议模式核对；实际连通性以上述真实测试为依据。


## 0.4 新增

模型从配置的本机 app-server `model/list` 读取并按准确model标识保存；不发起推理。GUI与外部Codex均可查询 `daily_tasks`、`recurring_rules`、`preview_recurring`，执行 `add_to_plan`、`set_recurring_rule`、`materialize_recurring`。后台模型只可提出set_recurring_rule候选，由确认流程保存；同批新建节点不使用虚构ID绑定，要先采用节点再继续设置规则。固定准备规则按稳定发生编号去重，未排计划不会进入逐项复盘。

已针对本机 app-server 的实际 config/read 结果核对 memories.use_memories=false、generate_memories=false、project_doc_max_bytes=0 和工具禁用值；另有strict-config正负对照。来源报告、运行时有效值报告位于.build，未放入便携软件包。已有软件讨论仍可恢复自己的上下文；这不等于读取用户其他旧对话。资料整理的事实必须能追溯到当前提供的来源，不能以模型输出本身证明事实。


## 0.5 会话工作流

业务查询新增recovery_summary和skills；命令新增set_recovery_task与record_recovery_progress。补欠字段与用法见开发与扩展的0.5段。后台模型只能提出这些声明式候选，不能写SQL或生成任意临时脚本执行。累计数量达到总量不会自动记录完成、掌握、到课或提交。

send_message增加可选skill_id='course-notes'；软件也从用户自己的“整理笔记”等请求选择该技能。课件/截图/邮件正文里的指令不触发技能。服务加载打包的有界指令，后台仍不开放外部文件与工具。候选确认、来源范围、附件覆盖、当前状态覆盖旧聊天和跨会话回执机制保持一致。


0.11.1补充：项目级MCP使普通任务可以访问业务接口；内部后台推理会同时隔离用户配置和当前工作目录的项目配置，普通对话与后台候选路径保持各自功能边界。


0.11.2 配置事务：GUI 与 MCP 均可调用 configure_codex。准备受管理文件 → 验证项目配置可加载 → MCP handshake/tools/begin_context 验证当前数据空间 → codex://new?path=... 打开空白输入框并走桌面创建流程 → project/list 与 project/read 核验 → 保存设置与回执。此过程不发送模型消息。内部持久会话 thread/start 传 projectId，续接时修复该软件会话归属；普通对话保留 MCP 工具能力，后台候选继续隔离工具。


0.11.3：从每日计划入口替换预填文字后，按本条实际消息判断用途；明确完成反馈不继承按钮的“只生成计划”限制，反馈与调整计划仍分别处理。单次等待上限保留用户设置。超时记录包含受限阶段元数据（连接/恢复/提交、等待模型/推理/返回结果/上游重试），不保存原始stderr或凭据，0.14 起，对已确认原任务身份的临时连接故障按检查点接续；发送身份不明的请求仍禁止盲目重发。


## 0.12.0 桌面连接适配

原有模式每次在独立 stdio App Server 中创建/续接任务，创建通知只到该连接；不能据 thread/list 可查就宣称 Codex 桌面已经显示。新版保留该模式用于仅在管理软件内处理，并新增显式 `ai.execution_mode=desktop_shared`。

桌面连接模式由专门启动入口设置本次进程的 CODEX_CLI_PATH。适配器接收桌面原本的 stdio，启动同一个本机 App Server，再复用桌面连接派发管理软件请求。官方 WebSocket 协议仍属实验接口；CLI 覆盖入口也需要随安装的 Codex 版本验证。不会修改 Codex 包、内部数据库、用户的全局环境或正在运行的桌面进程；不伪造工具调用者身份或 Windows sandbox 注册标记。

本机接口限定 IPv4 loopback，使用高熵认证令牌，拒绝网页 Origin；运行信息限制访问权限，不向界面、日志或模型暴露令牌。引擎由适配器的 Windows Job Object 管理，桌面结束后回收。管理软件断开只结束自己的连接，不终止桌面引擎。未建立共享连接时明确返回错误，不自动改走独立后台。

进度由真实协议事件产生，单作业只保留一个有界快照；预览按 250 ms 合并写入，不增加业务 revision。线程编号在模型开始前保存；超时仍能保留会话关联。阶段、已接收的回复和完成回执分别显示，未完成预览不是候选操作，更不是已保存事实。候选采用仍经过当前版本及来源校验。

初次切换需要保存并退出 Codex，再从管理软件设置启动连接模式。之后管理软件发送时，如果 Codex 已关闭，会按已保存的连接方式启动；如果已打开的是普通模式，会提示重新启动，不强制终止其他任务。真实桌面渲染验收须在切换后完成，协议级测试不能替代这一项。


桌面同步模式的用户输入仅包含原文和已选图片。事实快照放入明确标记为不可信资料的内部指令；候选通过唯一受限的 `submit_management_candidate` 工具交回内存并校验，最终仍显示正常中文回答。本轮正常完成后，业务服务才把候选置为可核对状态。该工具不执行候选动作，普通业务 MCP 不开放给内部候选线程。自由讨论请在固定项目内使用普通任务；软件内部任务的修改仍回到管理软件确认。

切换内部协议时，用持久化 `provider_contract` 新建一次兼容任务，后续继续同一任务；不把旧 JSON 输出线程错误恢复为工具回传线程。启动前核对桥接进程与真实引擎的可执行路径、PID 和创建时间；旧发布包或过期运行记录不能作为新版连接就绪证明。


## 0.12.1 每日计划内容超限修复

生成每日计划时，仅将未处理收件箱作为补充信息，已处理记录继续保留在数据库。固定日程的来源数据与生效数据完全相同时只携带一份；有改期等差异时两份都保留。任务候选、正式规则和已反馈的计划内容维持完整。

若必要信息仍超过单次容量，界面会明确提示请求尚未发送，并提供分段大小用于诊断。此类内容限制应与连接中断区分，刷新 Codex 配置无法改变请求大小。


## 0.13 会话协调与按次课表

共享桌面模式使用线程专属 personal_management_discussion MCP：begin_discussion 获取本轮最新快照并预留唯一请求，query_business 读取证据，submit_candidate 持久保存候选。连接关闭后工具仍属于该 Codex 任务；桌面续聊使用同一会话，不派生后台模型。提交候选不是业务写入回执，采用候选仍执行 epoch、revision、范围与业务规则校验。

conversation_operations 保存派发阶段及候选指纹，conversation_bindings 保留已知关联历史。未知的 thread/start 结果不能自动重发；thread/resume 被拒绝不会自动创建后继任务。thread/turn 早期标识在后续请求前持久化；共享连接在未收到 turn/start 回执时禁止第二个开始请求覆盖归属。已返回候选可在最终连接中断后恢复；原生桌面轮次已结束但未回传时会解除等待并记录明确失败。

daily_review 同时投影手动计划与按原始日期标识的固定日程。submit_daily_review 中固定项使用 item_id、schedule_signature 和明确结果。attendance 与 completion 分开，missed_needs_catchup 表示用户明确报告缺课并要求补课；该事务写出勤证据并创建或复用补课项。原始事件定义与历史出勤不随补课完成改变。


## 0.14 统一上下文、资料批次与结果校验

两种执行模式与普通项目 MCP 共用 context_service，不再使用“取前若干条记录拼成大请求”的生产路径。

| 接口 | 用途 |
| --- | --- |
| prepare_context / begin_discussion | 目标、集合计数、原操作编号和读取入口 |
| query_context | 记录、任务、截止项、规则、日程、计划、历史、资料索引、分析事实、候选动作的分页读取 |
| read_context_item | 指定实体及 @goal、@plan_request、@schedule、@daily_review、@capabilities、@history、@skill；超长单项按版本和位置续读 |
| next_context_step / read_material | 自动下一批原文、真实图像或明确解析缺口；指定 chunk 可回读原文 |
| checkpoint_context | 候选事实及其出处、分批动作、工作摘要；只保存分析进度，不修改业务事实 |
| operation_status / context_coverage | 阶段、范围、读取游标、已处理批次和剩余内容 |
| refresh_context | 原操作内重新核对变更；保留未变原件的检查点，归档旧候选 |
| validate_candidate | 完整本地约束和版本校验，不采用候选 |
| resume_context_operation | 明确失败或取消后由用户接续，复用已核实的原任务 |

集合页使用稳定主键游标、范围签名和实体版本。提交计划要求完整读取 deadlines、rules、events、plans；规则正文也必须完整核对。采用前在同一事务再次检查固定时段、依赖、完成状态、截止与规则。范围内新增截止项、已读事实变化、使用时区改变都会阻止旧候选覆盖新状态；无关的其他业务修订不强迫整项工作重跑。

scope.analyze_materials=false 表示仅作为参考；普通完成反馈不要求重读整门课程。课件整理或明确选择新资料时为 true，所有材料块必须完成，多个批次还必须分页合并 facts。图片工具只返回原生内容块，避免 Codex 优先结构化文字而丢弃图片。每个检查点回传 delivery_token，绑定原件版本和运行代次；需要纠正已保存分析时，回读原文并提交 expected_fingerprint，旧候选归档后重新合并。

历史分析用 query_context(collection='facts',filters={'job_id':旧作业})；旧原文用 read_material(from_job=旧作业,source_id,chunk)。读取消耗本轮预算，旧记录只作为历史证据。message:、job:、receipt: 可通过 read_context_item 分段读取，不复制全部聊天历史。

确认上下文压缩必须收到 contextCompaction 完成项及对应 turn/completed；压缩 ACK 本身不能重置预算。自动接续保留业务操作、作业及 Codex 任务身份。软件与桌面的轮次归属仍由会话协调层串行确认。来源文字中的指令不增加业务权限。

解析器支持分批纯文本、HTML、PDF、DOCX、PPTX、XLSX、EML 和常见图像。每批最多 40 个自然单元、64,000 字符、4 张图；整份资料没有旧的 80 页/100,000 字符截断。内嵌 Office 图表保留数据并明确标记未渲染布局，未知格式、加密或无法解析内容保留原件及缺口，不声称全部读懂。单解析进程、45 秒和约 768 MiB 采样保护；超限先缩小批次，仍无法处理则保留进度暂停。

验证状态以当前发布验证报告为准。真实 CLI、隔离本地模拟模型和合成桌面连接的通过结果，不等于用户账户下的 Electron 桌面验收。
