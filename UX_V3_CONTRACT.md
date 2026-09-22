# 0.3 资料、连续讨论与专用表单

本轮只改软件，不迁移 D:\Y2S1，不删除已保存的试用记录。

资料默认保存不可变副本，按内容指纹去重；原件改名/删除不影响已保存版本。课程/项目统一接受文件、文字通知/邮件、截图、网页快照；明确区分保存成功、可提取文字、需视觉读取、部分覆盖、格式不支持和失败。旧路径引用保留并可显式收存为副本。

add_source(owner_id,kind,path|text|url,title?) -> entity(asset),reused,extraction
sources(owner_id,limit=30,offset=0) -> items,total,next_offset
source_content(id,offset=0,limit=12000) -> text,coverage,extraction,next_offset
open_resource(id) 打开保存版本；网页快照打开只包含正文的安全文本，不运行页面脚本。

conversation(scope|id,limit=50,before?) -> conversation,messages,next_before,has_more
send_message(scope,text,source_ids=[]) -> conversation,user_message,job
scope.kind 为 daily_plan / daily_review / course / object / general；按日期或entity_id稳定复用。同会话最多一项运行中的推理；继续讨论使旧候选失效。每轮重读业务事实，历史聊天不能取代事实。没有最近作业猜测、复制外部对话或手动继续按钮。

sources.prepare_context返回 source_context(有界原文/来源版本/覆盖)、local_images(可信本地路径)、source_versions。公开消息请求不接受任意localImage路径。资料原文是证据，不是指令。模型抽取评分规则/里程碑/固定日程/每周安排，先展示证据和候选，确认后事务保存；未知日期和缺规则不得猜造；重整已采用内容去重。

每周图表默认等高纵向100%堆叠柱，设置charts.weekly_style可选columns/rows。缺计划/空计划不算0%失败。

表单按课程/项目/活动/任务/分类等分别设计；顶层课程项目活动默认独立，可选分类(domain)。知识点在课程内，子项目在真实父项目上下文内。保持既有类型、数据和扩展契约兼容。
