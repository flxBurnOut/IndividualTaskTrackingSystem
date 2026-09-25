"""Today is only the selected day's plan, fixed events and attention items."""
from __future__ import annotations
from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import QDateEdit, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget, QSizePolicy
from .gui_workspace import CountProgress, clear_layout, make_button, plain_label


class TodayPage(QWidget):
    error = Signal(object)
    review_attention = Signal(bool)
    def __init__(self, bridge, parent=None, on_plan=None, on_manual=None, on_review=None, on_task=None, on_changed=None, on_timetable=None, on_habits=None):
        super().__init__(parent)
        self.dead = False
        self.destroyed.connect(lambda *_: setattr(self, "dead", True))
        self.review_saving = False
        self.past_warnings = None
        self.past_warning_offset = 0
        self.past_expanded = False
        self.bridge, self.on_plan, self.on_manual, self.on_review, self.on_task = bridge, on_plan, on_manual, on_review, on_task
        self.on_changed = on_changed
        self.dialogs = []
        self.daily_tasks_result = None
        self.tasks_offset = 0
        self.pending_targets = set()
        self.generation = 0
        self.has_plan = False
        self.review_result = None
        self.today_result = None
        self.warning_result = None
        self.warning_offset = 0
        self.warning_date = None
        self.show_all_reminders = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(20)
        self.date_heading = plain_label("", "TodayDateHeading")
        font = self.date_heading.font(); font.setPointSize(22); font.setBold(True); self.date_heading.setFont(font)
        outer.addWidget(self.date_heading)
        row = QHBoxLayout()
        previous = make_button("‹", lambda: self.date.setDate(self.date.date().addDays(-1)))
        previous.setFixedWidth(35)
        row.addWidget(previous)
        self.date = QDateEdit(QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy 年 M 月 d 日")
        self.date.dateChanged.connect(self.refresh)
        row.addWidget(self.date)
        following = make_button("›", lambda: self.date.setDate(self.date.date().addDays(1)))
        following.setFixedWidth(35)
        row.addWidget(following)
        row.addWidget(make_button("回到今天", lambda: self.date.setDate(QDate.currentDate())))
        self.timetable_button = make_button("每周课表", lambda: on_timetable(self.date_iso()) if on_timetable else None)
        row.addWidget(self.timetable_button)
        row.addStretch()
        self.manual_button = make_button("手动安排", lambda: self.on_manual(self.date_iso()) if self.on_manual else None)
        self.manual_button.setObjectName("Prominent")
        self.manual_button.setMinimumHeight(44)
        row.addWidget(self.manual_button)
        self.plan_button = make_button("生成计划", self.request_plan, True)
        row.addWidget(self.plan_button)
        outer.addLayout(row)
        self.habits_button=make_button('日常习惯 · 查看提醒、自动待办与安排偏好',lambda:on_habits() if on_habits else None)
        self.habits_button.setObjectName('HabitsSummary');self.habits_button.setMinimumHeight(44);outer.addWidget(self.habits_button)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 10, 10)
        self.body.setSpacing(22)
        self.plan_box = QFrame()
        self.plan_box.setObjectName("PlanCard")
        self.plan_layout = QVBoxLayout(self.plan_box)
        self.plan_layout.setContentsMargins(24, 23, 24, 23)
        self.body.addWidget(self.plan_box)
        self.events_box = QWidget()
        self.events_layout = QVBoxLayout(self.events_box)
        self.events_layout.setContentsMargins(0, 0, 0, 0)
        self.events_layout.setSpacing(9)
        self.body.addWidget(self.events_box)
        self.tasks_box = QWidget()
        self.tasks_layout = QVBoxLayout(self.tasks_box)
        self.tasks_layout.setContentsMargins(0, 0, 0, 0); self.tasks_layout.setSpacing(9)
        self.body.addWidget(self.tasks_box)
        self.attention_box = QWidget()
        self.attention_layout = QVBoxLayout(self.attention_box)
        self.attention_layout.setContentsMargins(0, 0, 0, 0)
        self.attention_layout.setSpacing(9)
        self.body.addWidget(self.attention_box)
        self.body.addStretch()
        self.scroll.setWidget(body)
        outer.addWidget(self.scroll, 1)
        self.update_date_heading()
        self.render_plan(None)

    def update_date_heading(self):
        day = self.date.date()
        self.date_heading.setText(day.toString("yyyy 年 M 月 d 日") + "  ·  星期" + "一二三四五六日"[day.dayOfWeek()-1])

    def date_iso(self):
        return self.date.date().toString("yyyy-MM-dd")

    def set_date(self, date):
        parsed = QDate.fromString(date, "yyyy-MM-dd") if isinstance(date, str) else date
        if parsed != self.date.date():
            self.date.setDate(parsed)
        else:
            self.refresh()

    def refresh(self, *_):
        if self.dead: return
        self.update_date_heading()
        self.generation += 1
        generation = self.generation
        day = self.date_iso()
        if self.warning_date != day:
            self.warning_date, self.warning_offset = day, 0
            self.tasks_offset = 0
            self.past_warning_offset = 0; self.past_warnings = None; self.past_expanded = False
            self.daily_tasks_result = None
            clear_layout(self.tasks_layout)
        def failed(error):
            if not self.dead and generation == self.generation:
                self.error.emit(error)
        def review_loaded(result):
            if self.dead or generation != self.generation:
                return
            self.review_result = result
            self.has_plan = bool(result.get("has_plan"))
            needs_review = not result.get("can_review", self.has_plan) or (result.get("summary") or {}).get("pending_review", (result.get("summary") or {}).get("unreported", 0)) > 0
            self.review_attention.emit(needs_review and self.date.date() <= QDate.currentDate())
            self.plan_button.setText("调整计划" if self.has_plan else "生成计划")
            self.plan_button.setVisible(self.has_plan or result.get("has_fixed_schedule",False))
            self.render_plan(result)
            if self.today_result:self.render_events(self.today_result.get("events",[]))
        def today_loaded(result):
            if self.dead or generation != self.generation:
                return
            self.today_result = result
            self.render_events(result.get("events", []))
            self.render_attention()
        def warnings_loaded(result):
            if self.dead or generation != self.generation:
                return
            if not result.get("items") and self.warning_offset and result.get("total", 0) <= self.warning_offset:
                self.warning_offset = max(0, self.warning_offset - 3); self.refresh(); return
            self.warning_result = result
            self.render_attention()
        def tasks_loaded(result):
            if self.dead or generation != self.generation: return
            self.daily_tasks_result = result
            if not result.get("items") and self.tasks_offset and result.get("total", 0) <= self.tasks_offset:
                self.tasks_offset = max(0, self.tasks_offset - 10); self.refresh(); return
            self.render_tasks(result)
        def habits_loaded(result):
            if not self.dead and generation==self.generation:
                self.habits_button.setText('日常习惯 · '+result['summary_text']+'  ›')
                self.habits_button.setToolTip(result.get('gap') or '查看实际作用、启用状态和下次触发时间。')
        self.bridge.query('habits_overview',habits_loaded,failed,compact=True)
        self.bridge.query("daily_tasks", tasks_loaded, failed, date=day, limit=10, offset=self.tasks_offset)
        self.bridge.query("daily_review", review_loaded, failed, date=day)
        self.bridge.query("today", today_loaded, failed, date=day)
        self.bridge.query("warnings", warnings_loaded, failed, date=day, limit=3, offset=self.warning_offset, group="current")
        if self.past_expanded: self.load_past_warnings()

    def request_plan(self):
        if self.on_plan:
            self.on_plan(self.date_iso(), self.has_plan)

    def render_plan(self, result):
        clear_layout(self.plan_layout)
        if not result:
            self.plan_layout.addWidget(plain_label("今天的安排", "SectionHeading"))
            self.plan_layout.addWidget(plain_label("正在读取当天计划…", "Quiet"))
            return
        if not result.get("can_review", result.get("has_plan")):
            self.plan_layout.addWidget(plain_label("还没有这一天的计划", "CardTitle"))
            self.plan_layout.addWidget(plain_label("先说明今天能投入的时间，以及最需要完成的事情。\nCodex 会结合已登记的固定安排，给出可以核对的计划。", "Body"))
            self.plan_layout.addSpacing(5)
            self.plan_layout.addWidget(make_button("让 Codex 安排", self.request_plan, True), alignment=Qt.AlignmentFlag.AlignLeft)
            return
        plan = result.get("plan") or {}
        header = QHBoxLayout()
        heading = plain_label(plan.get("title") or "当天固定安排", "CardTitle")
        header.addWidget(heading, 1)
        mode = {"standard": "常规", "low_state": "低精力", "no_precise_time": "按先后", "rest": "休息"}.get(plan.get("mode"), "")
        if mode:
            header.addWidget(plain_label(mode, "StatusPill"))
        self.plan_layout.addLayout(header)
        items = result.get("items", [])
        if not items:
            self.plan_layout.addWidget(plain_label("今天以休息为主。" if plan.get("mode") == "rest" else "这份计划没有安排任务条目。", "Body"))
        for item in items:
            self.plan_layout.addWidget(self.plan_row(item))
        summary = result.get("summary", {})
        if items:
            self.progress = CountProgress()
            self.progress.title.setText("执行反馈 · 出勤不代表学习完成")
            self.progress.set_counts(summary)
            self.plan_layout.addSpacing(7)
            self.plan_layout.addWidget(self.progress)
        footer = QHBoxLayout()
        footer.addWidget(plain_label("直接标记即保存；每日复盘会同步显示同一份记录。", "Quiet"), 1)
        footer.addWidget(make_button("前往复盘", lambda: self.on_review(self.date_iso()) if self.on_review else None))
        self.plan_layout.addLayout(footer)

    def plan_row(self, item):
        row = QFrame()
        row.setObjectName("TimelineRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 16, 0, 16)
        layout.setSpacing(18)
        start, end = item.get("start"), item.get("end")
        clock = plain_label((str(start) + ("\n" + str(end) if end else "")) if start else "按先后", "TimeLabel")
        clock.setFixedWidth(74)
        layout.addWidget(clock)
        dot = plain_label("●", "DoneDot" if item.get("result") == "done" else "WaitingDot")
        dot.setFixedWidth(18)
        layout.addWidget(dot)
        content = QVBoxLayout()
        title = make_button(item.get("display_title") or item.get("title", "计划事项"), lambda: self.on_task(item["target_id"]) if self.on_task else None)
        title.setObjectName("TextLink"); title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred); title.setToolTip(item.get("display_title") or item.get("title", ""))
        content.addWidget(title)
        details = [item["owner_label"]] if item.get("owner_label") else []
        if item.get("target_archived"):
            details.append("已删除 · 保留原计划记录")
        if item.get("fixed_schedule") and not item.get("can_review") and not item.get("target_archived"): details.append("尚未开始，到时可记录实际情况")
        if item.get("catchup_task_id"):
            details.append("补课已完成" if (item.get("catchup_progress") or {}).get("completion_confirmed") else "已关联补课事项")
        if item.get("location"): details.append(item["location"])
        if item.get("completion_gate"):
            details.append(str(item["completion_gate"]))
        if item.get("planned_minutes") is not None:
            details.append(f"预计 {item['planned_minutes']} 分钟")
        if details:
            content.addWidget(plain_label(" · ".join(details), "Quiet"))
        layout.addLayout(content, 1)
        result = item.get("result")
        choices = QHBoxLayout()
        for value, caption in item.get("choices", (("done", "已完成" if result == "done" else "完成"), ("incomplete", "未完成"))):
            choice = make_button(caption, lambda _, i=item, v=value: self.complete_plan_item(i, v))
            choice.setCheckable(True); choice.setChecked(result == value); choice.setObjectName("ReviewChoice")
            choice.setProperty("result", value)
            choice.setEnabled(not self.review_saving and not item.get("target_archived") and item.get("can_review", True) and result != value)
            choice.setAccessibleName((item.get("owner_label", "") + " " + item.get("title", "事项") + "：" + caption).strip())
            choices.addWidget(choice)
        layout.addLayout(choices)
        return row

    def complete_plan_item(self, item, result):
        if self.dead or self.review_saving or not item.get("can_review", True) or item.get("target_archived"): return
        snapshot = self.review_result or {}; plan = snapshot.get("plan") or {}
        if not snapshot.get("can_review", bool(plan)) or item.get("result") == result: return
        day = self.date_iso(); self.review_saving = True; self.render_plan(snapshot)
        payload = {"date": day, "plan_id": plan.get("id"), "plan_version": plan.get("version"), "schedule_signature": snapshot.get("schedule_signature"), "answers": [{("item_id" if item.get("item_id") else "target_id"): item.get("item_id",item["target_id"]), "result": result}]}
        options = {("expected_revision" if k == "revision" else k): snapshot[k] for k in ("epoch", "revision") if snapshot.get(k) is not None}
        def saved(receipt):
            self.review_saving = False
            if self.dead: return
            self.refresh()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            self.review_saving = False
            if self.dead: return
            self.refresh(); self.error.emit(error)
        self.bridge.command("submit_daily_review", payload, saved, failed, **options)

    def render_events(self, events):
        clear_layout(self.events_layout)
        integrated=bool(self.review_result and self.review_result.get('has_fixed_schedule'))
        self.events_box.setVisible(not integrated)
        if integrated:return
        self.events_layout.addWidget(plain_label("固定安排", "SectionHeading"))
        if not events:
            self.events_layout.addWidget(plain_label("这一天没有已登记的固定安排。", "Quiet"))
            return
        for event in events:
            from .dashboard import projected_event_times
            data = event.get("effective") or event.get("data", event)
            row = QFrame()
            row.setObjectName("EventRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(17, 13, 17, 13)
            start, end = projected_event_times(event)
            time = f"{start} – {end}" if start and end else "时间待确认"
            clock = plain_label(time, "TimeLabel")
            clock.setFixedWidth(130)
            layout.addWidget(clock)
            layout.addWidget(plain_label(event.get("title", "固定安排")), 1)
            if data.get("location"):
                layout.addWidget(plain_label(str(data["location"]), "Quiet"))
            if event.get("id"): layout.addWidget(make_button("自动生成准备待办", lambda _, item=event: self.open_recurring(item)))
            self.events_layout.addWidget(row)

    def render_tasks(self, result):
        clear_layout(self.tasks_layout)
        self.tasks_layout.addWidget(plain_label("当天候选待办", "SectionHeading"))
        self.tasks_layout.addWidget(plain_label("这里只列已安排到这天、到期或逾期的未完成任务。是否今天处理由你决定；加入日计划后，才进入当天复盘。", "Quiet"))
        if not result.get("items"):
            self.tasks_layout.addWidget(plain_label("没有尚未排入当天计划的日期相关任务。其他课程任务仍可从项目与课程查看。", "Quiet"))
        day = result.get("date") or self.date_iso()
        snapshot = result.get("plan")
        group_label = None
        for task in result.get("items", []):
            owner = task.get("owner_label") or "未归属"
            if owner != group_label:
                group_label = owner; self.tasks_layout.addWidget(plain_label(owner, "SectionHeading"))
            row = QFrame(); row.setObjectName("TaskRow"); inner = QHBoxLayout(row); inner.setContentsMargins(16, 13, 16, 13)
            words = QVBoxLayout(); title = make_button(task.get("display_title") or task["title"], lambda _, identifier=task["id"]: self.on_task(identifier) if self.on_task else None); title.setObjectName("TextLink"); title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred); title.setToolTip(task.get("display_title") or task["title"]); words.addWidget(title)
            words.addWidget(plain_label(" · ".join(x for x in (task.get("candidate_date"), task.get("reason") or "日期相关待办") if x), "Quiet"))
            if task.get("data", {}).get("completion_gate"): words.addWidget(plain_label(str(task["data"]["completion_gate"]), "Quiet"))
            inner.addLayout(words, 1)
            add = make_button("加入这天计划", lambda _, item=task, d=day, p=snapshot: self.add_task(item, d, p))
            add.setEnabled((day, task["id"]) not in self.pending_targets); inner.addWidget(add); self.tasks_layout.addWidget(row)
        if self.tasks_offset or result.get("next_offset") is not None:
            row = QHBoxLayout(); row.addWidget(plain_label(f"候选待办 {self.tasks_offset + 1}–{self.tasks_offset + len(result.get('items', []))} / {result.get('total', 0)} 项", "Quiet"), 1)
            previous = make_button("上一页待办", lambda: self.set_tasks_page(max(0, self.tasks_offset - 10))); previous.setEnabled(self.tasks_offset > 0); row.addWidget(previous)
            following = make_button("更多待办", lambda: self.set_tasks_page(result.get("next_offset"))); following.setEnabled(result.get("next_offset") is not None); row.addWidget(following); self.tasks_layout.addLayout(row)

    def set_tasks_page(self, offset):
        if offset is not None: self.tasks_offset = offset; self.refresh()

    def add_task(self, task, day, plan):
        key = (day, task["id"])
        if key in self.pending_targets: return
        self.pending_targets.add(key)
        if self.daily_tasks_result: self.render_tasks(self.daily_tasks_result)
        payload = {"date": day, "target_id": task["id"], "plan_id": plan["id"] if plan else None, "plan_version": plan["version"] if plan else None}
        def saved(receipt):
            self.pending_targets.discard(key)
            if self.dead: return
            self.refresh()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            self.pending_targets.discard(key)
            if self.dead: return
            self.refresh(); self.error.emit(error)
        self.bridge.command("add_to_plan", payload, saved, failed)

    def open_recurring(self, anchor):
        from .gui_recurring import RecurringDialog
        dialog = RecurringDialog(self.bridge, anchor, self, lambda receipt: (self.refresh(), self.on_changed(receipt) if self.on_changed else None))
        self.dialogs.append(dialog); dialog.finished.connect(lambda _: self.dialogs.remove(dialog) if dialog in self.dialogs else None); dialog.open()

    def warning_card(self, item, past=False):
        card = QFrame(); card.setObjectName("AttentionCard" if past else "CurrentWarningCard")
        layout = QVBoxLayout(card); layout.setContentsMargins(17, 13, 17, 13)
        title = item.get("title") or item.get("message") or "需要核对"
        owner = item.get("owner_label")
        layout.addWidget(plain_label((owner + " · " if owner and owner not in title else "") + str(title), "AttentionTitle" if past else "CurrentWarningTitle"))
        detail = item.get("reason") or item.get("message") or item.get("description") or ""
        if detail and detail != title: layout.addWidget(plain_label(str(detail), "AttentionText"))
        return card

    def render_attention(self):
        if self.dead: return
        clear_layout(self.attention_layout)
        result = self.warning_result or {}; notices = result.get("items", [])
        past_count = result.get("counts", {}).get("past", 0)
        reminders = (self.today_result or {}).get("notifications", [])
        unknowns = (self.today_result or {}).get("unknowns", [])
        self.attention_box.setVisible(bool(notices or reminders or unknowns or past_count))
        if self.body.indexOf(self.attention_box) != 0:
            self.body.removeWidget(self.attention_box); self.body.insertWidget(0, self.attention_box)
        if notices:
            self.attention_layout.addWidget(plain_label("● 当前警戒 · 即将到来的事项", "CurrentWarningTitle"))
            for item in notices: self.attention_layout.addWidget(self.warning_card(item))
            total = result.get("total", 0)
            if total > 3:
                row = QHBoxLayout(); row.addWidget(plain_label(f"当前警戒 {self.warning_offset + 1}–{self.warning_offset + len(notices)} / {total} 条", "Quiet"), 1)
                previous = make_button("上一页警戒", lambda: self.set_warning_page(max(0, self.warning_offset - 3))); previous.setEnabled(self.warning_offset > 0); row.addWidget(previous)
                following = make_button("更多警戒", lambda: self.set_warning_page(result.get("next_offset"))); following.setEnabled(result.get("next_offset") is not None); row.addWidget(following); self.attention_layout.addLayout(row)
        if past_count:
            toggle = make_button(f"已过节点的警戒（{past_count}） · " + ("收起" if self.past_expanded else "展开"), self.toggle_past_warnings)
            toggle.setObjectName("CompletedTasksToggle"); self.attention_layout.addWidget(toggle)
            if self.past_expanded:
                self.attention_layout.addWidget(plain_label("仅按节点时间折叠，未将这些事项判为完成。", "Quiet"))
                if self.past_warnings is None: self.attention_layout.addWidget(plain_label("正在读取历史警戒…", "Quiet"))
                else:
                    for item in self.past_warnings.get("items", []): self.attention_layout.addWidget(self.warning_card(item, True))
                    if self.past_warning_offset or self.past_warnings.get("next_offset") is not None:
                        row = QHBoxLayout(); previous = make_button("上一页历史", lambda: self.page_past_warnings(max(0, self.past_warning_offset-5))); previous.setEnabled(self.past_warning_offset>0); row.addWidget(previous)
                        following = make_button("下一页历史", lambda: self.page_past_warnings(self.past_warnings.get("next_offset"))); following.setEnabled(self.past_warnings.get("next_offset") is not None); row.addWidget(following); self.attention_layout.addLayout(row)
        for item in unknowns[:2]:
            text = item if isinstance(item, str) else item.get("message", item.get("reason", "部分时间信息尚待确认"))
            if text: self.attention_layout.addWidget(plain_label("待确认 · " + str(text), "Quiet"))
        for reminder in (reminders if self.show_all_reminders else reminders[:3]):
            data = reminder.get("data", {}); card = QFrame(); card.setObjectName("AttentionCard"); layout = QVBoxLayout(card); layout.setContentsMargins(17, 13, 17, 13)
            heading = reminder.get("title", "复盘提醒")
            if data.get("business_date") and data["business_date"] != self.date_iso(): heading += " · " + data["business_date"]
            layout.addWidget(plain_label(heading, "AttentionTitle")); layout.addWidget(plain_label(data.get("content", ""), "AttentionText")); actions = QHBoxLayout(); mode = data.get("review_mode")
            if mode in ("daily", "weekly"):
                day = data.get("business_date") or self.date_iso(); actions.addWidget(make_button("查看每周回顾" if mode == "weekly" else "前往每日复盘", lambda checked=False, d=day, m=mode: self.on_review(d, m) if self.on_review else None))
            actions.addStretch(); actions.addWidget(make_button("知道了", lambda checked=False, item=reminder: self.mark_reminder_seen(item))); layout.addLayout(actions); self.attention_layout.addWidget(card)
        if len(reminders)>3 and not self.show_all_reminders: self.attention_layout.addWidget(make_button(f"展开其余 {len(reminders)-3} 条近期提醒", self.expand_reminders))

    def toggle_past_warnings(self):
        self.past_expanded = not self.past_expanded
        self.render_attention()
        if self.past_expanded and self.past_warnings is None: self.load_past_warnings()

    def page_past_warnings(self, offset):
        if offset is None: return
        self.past_warning_offset = offset; self.past_warnings = None; self.render_attention(); self.load_past_warnings()

    def load_past_warnings(self):
        generation, offset = self.generation, self.past_warning_offset
        def loaded(result):
            if self.dead or generation != self.generation or offset != self.past_warning_offset: return
            self.past_warnings = result; self.render_attention()
        def failed(error):
            if not self.dead and generation == self.generation: self.error.emit(error)
        self.bridge.query("warnings", loaded, failed, date=self.date_iso(), group="past", limit=5, offset=offset)

    def set_warning_page(self, offset):
        if offset is not None:
            self.warning_offset = offset
            self.refresh()


    def expand_reminders(self):
        self.show_all_reminders = True
        self.render_attention()

    def mark_reminder_seen(self, reminder):
        self.bridge.command("update", {"id": reminder["id"], "version": reminder["version"], "patch": {"data": {"seen": True}}}, lambda _: self.refresh(), self.error.emit)
