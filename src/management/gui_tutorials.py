"""Short, read-only tours anchored to the real controls of each workspace."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QHBoxLayout, QLabel, QPushButton
from shiboken6 import isValid

from .gui_onboarding_overlay import TourStep


def first_visible(*widgets):
    return next((widget for widget in widgets
                 if widget is not None and isValid(widget) and widget.isVisible()), None)


def visible_button(container, *labels):
    """Resolve rebuilt page controls at each step, without activating them."""
    buttons = container.findChildren(QPushButton)
    for text in labels:
        target = first_visible(*(button for button in buttons if button.text() == text))
        if target is not None:
            return target
    return None


def discussion_result_target(dialog):
    cards = list(dialog.cards.values())
    return first_visible(dialog.apply,
                         *(card.proposal_heading for card in reversed(cards)),
                         *(card.author for card in reversed(cards)),
                         *dialog.empty_history.findChildren(QLabel), dialog.status)


def install_main_tutorials(window):
    manager = window.onboarding
    ready = window.onboarding_ready
    page = window.dashboard_page
    manager.watch('dashboard', page, lambda: [
        TourStep(page.date, '欢迎使用个人事务管理 Beta 测试版', '总览汇总今天、本周安排和当前需要留意的事项。接下来用几步认识常用入口；也可以随时跳过。'),
        TourStep(window.nav_buttons['tasks'], '从这里切换面板', '“任务”快速记录与整理待办；“今天”按日期安排与执行；“项目与课程”整理任务和资料；“复盘”记录实际结果。'),
        TourStep(window.new_button, '先记录需要做的事', '从“＋ 新建”创建任务、项目、课程、活动、分类或目标。任务可以归属到项目或课程，方便后续安排。'),
        TourStep(window.search_input, '直接输入名称查找', '在这里输入任务、项目或课程名称并按 Enter。Ctrl+K 可快速聚焦；搜索结果支持翻页，选中后点击打开或按 Enter。'),
        TourStep(window.habits_button, '单独管理日常习惯', '在左下角直接设置日常容量、提醒、保护时段和安排偏好，不需要进入软件设置。'),
        TourStep(window.assistant_button, '需要时再连接 Codex', '随时从这里查看 Codex 协助设置和连接状态。打开设置不会启用助手，基础记录与手动安排也不需要连接。'),
        TourStep(window.settings_button, '调整显示与数据设置', '这里调整显示、课表和数据备份等软件设置。日常习惯有独立入口。'),
        TourStep(window.update_button, '关闭窗口后，后台仍可运行', '在“版本与更新”核对版本和数据位置。要完全退出，请在 Windows 通知区域右键软件图标，选择“退出软件与后台”。'),
        TourStep(window.guide_button, '需要时再来看', '“使用指南”可以重看当前面板介绍、回到入门导览，或关闭新面板的自动介绍。引导中的高亮只用于讲解，不会执行按钮操作。'),
    ], host=window, ready=ready)
    today = window.today_page
    manager.watch('today', today, lambda: [
        TourStep(today.date, '先确认要安排的日期', '切换日期后，这里会显示那一天的计划与固定安排。可以补记过去，也可以提前安排。'),
        TourStep(today.manual_button, '自己安排一天', '从任务池选择任务，调整先后顺序和时间。时间不确定时可以只排先后；完成标准沿用源任务。'),
        TourStep(lambda: first_visible(*today.plan_box.findChildren(QPushButton, 'ReviewChoice'), today.manual_button), '在当天安排中记录实际结果', '有当天安排后，点击完成或其他反馈会立即保存，并同步到复盘；请按实际情况选择。还没有计划时，可以先“安排这一天”。'),
        TourStep(today.habits_button, '固定框架与日常习惯', '“日常习惯”设置提醒、准备待办和安排偏好；“每周课表”查看固定时段。固定课表和每日计划分别维护。'),
    ], host=window, ready=ready)
    tasks = window.tasks_page
    manager.watch('tasks', tasks, lambda: [
        TourStep(tasks.quick_input, '先记下来', '只填标题就能保存，按 Enter 可连续录入。日期、归属和完成标准可以以后补充。'),
        TourStep(tasks.group_picker, '从全部任务里查找', '按待办、未填日期、到期、未来截止或归属查找，搜索覆盖所有分页。未填日期的任务也可能已加入某天计划，列表会分别显示。'),
        TourStep(tasks.open_button, '补充信息与安排', '先选中列表中的任务，再打开详情；也可使用旁边的编辑、拆分和加入今天。上方“批量录入”可粘贴多项任务，先预览再一次保存。'),
    ], host=window, ready=ready)
    workspace = window.workspace_page
    manager.watch('projects', workspace, lambda: [
        TourStep(lambda: first_visible(workspace.expand_directory_button, *workspace.directory_panel.findChildren(QLabel, 'SectionHeading')), '按归属整理事项', '在标题下方展开目录，选择项目或课程；目录收起时先点击“展开目录”。拖动事项会修改真实归属。'),
        TourStep(lambda: visible_button(workspace.content, '＋ 添加', '新建项目', '新建课程'), '任务和资料放在所属事项里', '打开项目或课程后，通过“＋ 添加”加入任务、资料、笔记或日程。还没有所属事项时，可以先新建；各列表底部支持翻页。'),
        TourStep(window.new_button, '从新建开始', '还没有项目或课程时，先在这里创建。随后进入它的详情，逐步补充任务和资料。'),
    ], host=window, ready=ready)
    review = window.review_page
    manager.watch('review.daily', review.daily_tab, lambda: [
        TourStep(review.date_editor, '按日期复盘', '选择要复盘的日期。这里使用已保存的计划、固定安排和执行反馈。'),
        TourStep(lambda: first_visible(*review.rows.findChildren(QPushButton, 'ReviewChoice'), review.feedback_button), '只填写已知的实际情况', '列表中有事项时，按实际情况选择结果；没有选择的项目仍是未反馈。没有当天计划时，可用“记录实际情况”补充已知事实。'),
        TourStep(review.feedback_button, '没有计划也能记录', '选择已有事项记录真实情况，或写下当天小结；不会自动补造计划。'),
        TourStep(lambda: first_visible(review.confirm_button, review.feedback_button), '核对后保存实际结果', '有计划事项时，本页选择需点击“确认所选结果”才会保存；没有计划时，先打开“记录实际情况”，在记录窗口核对并保存。切到“每周回顾”可查看已保存的变化。'),
    ], host=window, ready=ready)
    manager.watch('review.weekly', review.weekly_tab, lambda: [
        TourStep(review.date_editor, '查看所选日期所在的一周', '每周回顾汇总已保存的反馈，不会把未反馈自动算作失败。'),
        TourStep(lambda: first_visible(*review.week_days.findChildren(QPushButton), review.date_editor), '从每周回顾回到某一天', '图表中的日期按钮可以打开那一天的每日复盘；尚未读取图表时也可先选择日期。图表形式可通过旁边的“图表样式”调整。'),
    ], host=window, ready=ready)


def install_dialog_tutorial(dialog, kind):
    """Also works for dialogs opened by nested workspace controls."""
    ancestor = dialog.parentWidget()
    while ancestor is not None and not hasattr(ancestor, 'onboarding'):
        ancestor = ancestor.parentWidget()
    if ancestor is None:
        return
    manager = ancestor.onboarding
    if kind == 'settings':
        _settings_tutorials(dialog, manager)
    else:
        steps = _dialog_steps(dialog, kind)
        if steps is None:
            return
        manager.watch(kind, dialog, steps, host=dialog)
    _add_dialog_help(dialog, manager)


def _add_dialog_help(dialog, manager):
    guide = QPushButton('使用指南', dialog)
    guide.setObjectName('QuietButton')
    guide.setAutoDefault(False)
    guide.setToolTip('重新查看当前面板的介绍')
    guide.clicked.connect(manager.show_current)
    dialog.guide_button = guide
    layout = dialog.layout()
    first = layout.itemAt(0)
    if first and isinstance(first.layout(), QHBoxLayout):
        first.layout().addWidget(guide)
    else:
        row = QHBoxLayout()
        if first and isinstance(first.widget(), QLabel):
            heading = layout.takeAt(0).widget()
            row.addWidget(heading, 1)
        else:
            row.addStretch()
        row.addWidget(guide, alignment=Qt.AlignmentFlag.AlignRight)
        layout.insertLayout(0, row)


def _dialog_steps(dialog, kind):
    if kind == 'habits':
        return lambda: [
            TourStep(dialog.habits.reminder_toggle, '安排复盘提醒', '设置每日复盘和每周回顾提醒。保存后在后台运行时提示；电脑关闭期间不会执行。'),
            TourStep(dialog.habits.prep_toggle, '按需要设置准备工作', '课前或截止前的准备规则先预览日期再保存；不需要这些规则时可以留空。'),
            TourStep(dialog.habits.new_kind, '你想设置什么？', '选择安排习惯、每天最多安排多久、不排任务的时段或日期提醒。选中后，下方会解释用途并给出例子，再点“填写这项设置”。'),
        ]
    if kind == 'plan':
        return lambda: [
            TourStep(dialog.mode, '选择这一天的安排方式', '先核对日期，再选择常规、低精力、只排先后或休息。具体时间可以留空。'),
            TourStep(dialog.add_candidate_button, '从已有任务中挑选', '先在上方列表勾选任务，再点击“加入勾选任务”。没有任务时可先使用“新建任务”；安排已有任务不会重复新建一份。'),
            TourStep(lambda: dialog.table.horizontalHeader(), '一行是一项具体安排', '下方每一行是一项安排，可调整先后和时间；完成标准只读，需要修改时编辑源任务。草稿按日期保留，保存时核对原计划版本。'),
            TourStep(dialog.buttons.button(QDialogButtonBox.StandardButton.Save), '核对后保存当天计划', '保存会更新所选日期的安排；已有完成记录会保留。退出引导不会替你保存，也不会清空草稿。'),
        ]
    if kind == 'discussion':
        return lambda: [
            TourStep(dialog.scope_label, '每次讨论有对应的事项', '这里显示日期或事项范围。从同一入口回来，可以继续这次讨论。调整计划时请说清楚要改变的内容。'),
            TourStep(dialog.prompt, '补充要求和实际情况', '在这里输入消息。Enter 发送，Shift + Enter 换行；“＋ 附件”可选择要参考的资料。'),
            TourStep(lambda: discussion_result_target(dialog), '区分回复与已保存的结果', '对话会显示回复和候选变更；还没有消息时会显示开始讨论的提示。收到候选不代表已修改计划，请核对具体变更后再保存。'),
            TourStep(lambda: first_visible(dialog.open_codex, dialog.desktop_connection_note, dialog.status), '查看连接和同一次讨论', '连接状态会显示在讨论窗口。“在 Codex 查看”出现后，可以打开关联会话；打开会话本身不会再次发送任务。'),
        ]
    if kind == 'timetable':
        return lambda: [
            TourStep(dialog.week_date, '先选择要查看的一周', '按日期和课表筛选已经登记的固定安排。'),
            TourStep(dialog.manage_button, '导入并核对课表', '导入课表资料，核对课程时段后保存。需要时也可以手动维护课程时段。'),
            TourStep(lambda: first_visible(*dialog.grid.blocks, dialog.selector), '固定安排提供每周框架', '选择课表后查看固定时段；有课程格子时可点击修改原日程，重复安排的修改可能作用于整个系列。每日计划另行安排。'),
        ]
    if kind == 'editor':
        return lambda: [
            TourStep(dialog.title_edit, '给事项一个明确的名称', '任务最好写成可以执行和检查的具体动作。项目、课程等则用便于查找的名称。'),
            TourStep(lambda: first_visible(*(getattr(field, 'enabled', None) for field in dialog.fields.values()), *(getattr(field, 'editor', None) for field in dialog.fields.values()), dialog.more_fields), '补充当前已知的信息', '勾选字段旁的小方框后填写日期、说明或完成标准；没有勾选框的字段可直接填写。其他字段可在“更多信息”中查看，没有依据的信息可以暂时留空。'),
            TourStep(dialog.buttons.button(QDialogButtonBox.StandardButton.Save), '确认内容后保存', '点击保存才会写入记录。教程不会填写字段、提交表单或修改你的草稿。'),
        ]
    return None


def _settings_tutorials(dialog, manager):
    tours = {
        '显示': ('display', lambda: [
            TourStep(dialog.theme_picker, '选适合自己的显示方式', '明暗模式与强调色可以分别选择，下方提供外观预览；也可以调整字体和字号。'),
            TourStep(lambda: dialog.chart_pickers.get(dialog._chart_preview_key, dialog.weekly_chart_style), '每处图表单独选择', '每个下拉框对应一个位置，选择后会显示虚构预览。总览、反馈和每周回顾可分别设置；保存不会改变业务数据。'),
            TourStep(dialog.chart_save, '保存后应用显示偏好', '明暗、强调色、字号和各处图表形式在保存后应用到已经打开的窗口。'),
        ]),
        '课表': ('timetable', lambda: [
            TourStep(dialog.timetable_settings.scope, '先选择要维护的课表', '这里可选择新课表的默认设置，或已有课表。下方维护时区和教学周规则；未明确的信息先保留待核对，再根据依据补充。'),
        ]),
        'Codex 协助': ('codex', lambda: [
            TourStep(dialog.show_assistants, '按需显示助手入口', '可以单独控制各面板的助手操作入口；保存入口显示不会启用或调用 Codex。'),
            TourStep(dialog.ai_enabled, '启用本机 Codex 协助', '先在本机 Codex 完成登录，再启用协助、选择处理方式并保存设置。'),
            TourStep(dialog.ai_mode, '选择在哪里处理', '“在 Codex 桌面同步显示”关联桌面会话；“仅在管理软件中处理”使用独立处理方式。'),
            TourStep(lambda: first_visible(dialog.codex_bridge_start, dialog.ai_save), '保存设置，再核对实际连接', '先保存所选处理方式。在 Codex 桌面同步显示时，可点击“连接 Codex”并核对实时状态；仅在管理软件中处理时，从讨论窗口发送，回复不保证同步到 Codex 桌面。'),
        ]),
        '数据与高级': ('data', lambda: [
            TourStep(lambda: visible_button(dialog, '创建并核验备份'), '保留和备份自己的资料', '通过这个按钮创建并核验备份。恢复会写入新的空目录，外部文件引用仍需保留原件；本页上方可核对当前数据位置。升级软件时继续使用原数据目录。'),
        ]),
    }
    for index in range(dialog.tabs.count()):
        title = dialog.tabs.tabText(index)
        if title in tours:
            suffix, factory = tours[title]
            manager.watch('settings.' + suffix, dialog.tabs.widget(index), factory, host=dialog)
