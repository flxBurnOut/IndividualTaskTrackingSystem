# 交互重构契约 · 2026-09-22

本次用户反馈优先于旧的五导航/全部类型暴露方案。保留存量数据与核心接口；不迁移或删除任何旧资料。

## 用户可见设计

- 三个主入口：今天 / 项目与课程 / 复盘。移除独立安排、资料、收藏，以及工具栏“快速记录 / 后台结果 / 通知”。
- 今天只呈现当日计划、固定事件和需要关注的提示；生成/调整计划只在这里。完成确认只在复盘；跨页入口只导航，不再重复制造记录。
- 项目与课程按主归属浏览。全局新建仅任务、项目、课程、活动、领域、目标；里程碑/阶段/知识点在相关项目/课程内创建。领域专用实验、Case、批次、运行结果等不进入普通新建。原模型与扩展接口保留。
- 资料只在所属课程/项目显示为文件；打开原文件，选择或保存本地文件即可。无独立资料门户，不把复制原件入库作为唯一方式。手工关系维护不作为常用GUI动作。
- 不再永久显示右侧通用四页签大框。项目页按内容分区，任务点击时显示简明详情。以真实完成数/明确目标量显示进度，未知不得画成0%，完成项比例不冒称工时或掌握比例。
- 没有每日计划时，复盘出现提示标记并引导用户到Codex按实际情况复盘。读取、切页和重复点击都不产生新问卷。
- 有计划时逐项显示 **完成 / 未完成** 两个明确可选结果；未选择保持未反馈。点击确认只保存明确结果。不推断出席、提交、掌握、时长。
- 周回顾为结构化汇总与必要的缺口提示，不以“写下本期回顾”的开放大文本为主流程。
- 设置直接提供每日复盘启用/时间，周回顾启用/星期/时间；均不擅自启用。已配置值可读回，保存不重复创建schedule。
- Codex推理进度、结果与采用放在发起操作的同一对话中，不要求另找后台结果。外部Codex引导明确为复制上下文/指令，不冒称已发送。

## 新业务API（root接入，review agent实现算法）

query daily_review(date ISO) -> {date,has_plan,plan:{id,version,title,mode}|null,items:[{target_id,title,completion_gate,planned_minutes,start?,end?,result:'done'|'incomplete'|null,target_version,feedback_id?}],summary:{total,done,incomplete,unreported},needs_codex,review_id?,review_version?}

query weekly_review(start ISO,end ISO) -> {start,end,days:[{date,has_plan,summary,needs_codex}],summary:{planned,done,incomplete,unreported,days_with_plan,days_without_plan},items:[...] , coverage}. 已完成/未完成/未反馈独立，缺计划不是失败。

command submit_daily_review {date,plan_id,plan_version,answers:[{target_id,result:'done'|'incomplete'}]} -> {review,feedback_ids,changed_count,summary}. 非计划对象/重复target/旧计划版本拒绝；无计划明确needs_codex且不写假复核。相同快照答案用不同request_id反复提交不生成重复反馈。一个日期/当前计划一个复盘记录，修改结果留历史，旧明确维度不覆盖。

query review_preferences -> {daily:{enabled,time},weekly:{enabled,weekday,time},timezone}；command set_review_preferences 传同结构，原子创建或更新稳定的两条schedule，同值不复制。weekday 0为周一。root实现。

query object_workspace(id) -> {entity,children,files,summary}，root实现资料就近和进度统计。
command attach_local_file {path,owner_id,title?} -> entity，记录本地文件引用，不复制/删除原件；open_resource {id} -> {path,exists}只读解析路径，GUI调用系统默认程序。

## 文件所有权

- root：core/storage/schemas/scheduler/mcp集成、提醒设置API、文件引用API、gui_workflows.py（SettingsDialog/AssistanceDialog）、gui_forms.py简化，打包和文档。
- GUI shell agent：gui.py、gui_workspace.py等（除gui_review.py）、tests/test_gui_shell_v2.py。三导航及项目/今天/上下文新建；保留run/MainWindow/configure_palette供发布验证。使用新复盘组件。
- Review business agent：reviews.py、tests/test_reviews_v2.py，函数 query_daily(core,c,p)、query_weekly(core,c,p)、submit_daily(core,c,p,rid) 及 legacy_checkin(core,c,p,rid)。legacy_checkin相同日期/计划/显式对象范围重复不复制，无计划不再从全部事项制造问卷（可返回needs_codex给root封装），旧数据保留。
- Review UI agent：gui_review.py、gui_charts.py、tests/test_review_ui_v2.py。ReviewPage(bridge,parent=None,on_changed=None,on_codex=None), .refresh(date=None), .has_pending Signal(bool), .set_date(dateISO)；每日/每周分段、按计划逐项选择、确认与反馈覆盖图。on_codex(prompt)调用外壳Codex引导。chart无需第三方图形库，用QPainter/Qt widgets。

所有GUI仍经ServiceBridge，异步不阻塞，保留未提交选择，旧版本/旧计划冲突不静默覆盖。测试使用独立合成目录，正式data可能已被用户试用，绝不能重置。
