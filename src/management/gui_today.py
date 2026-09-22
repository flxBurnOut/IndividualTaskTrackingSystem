"""Today is only the selected day's plan, fixed events and attention items."""
from __future__ import annotations
from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import QDateEdit, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget
from .gui_workspace import CountProgress, clear_layout, make_button, plain_label


class TodayPage(QWidget):
    error = Signal(object)
    review_attention = Signal(bool)
    def __init__(self, bridge, parent=None, on_plan=None, on_manual=None, on_review=None, on_task=None, on_changed=None, on_timetable=None):
        super().__init__(parent)
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
        self.render_plan(None)

    def date_iso(self):
        return self.date.date().toString("yyyy-MM-dd")

    def set_date(self, date):
        parsed = QDate.fromString(date, "yyyy-MM-dd") if isinstance(date, str) else date
        if parsed != self.date.date():
            self.date.setDate(parsed)
        else:
            self.refresh()

    def refresh(self, *_):
        self.generation += 1
        generation = self.generation
        day = self.date_iso()
        if self.warning_date != day:
            self.warning_date, self.warning_offset = day, 0
            self.tasks_offset = 0
            self.daily_tasks_result = None
            clear_layout(self.tasks_layout)
        def failed(error):
            if generation == self.generation:
                self.error.emit(error)
        def review_loaded(result):
            if generation != self.generation:
                return
            self.review_result = result
            self.has_plan = bool(result.get("has_plan"))
            needs_review = not self.has_plan or (result.get("summary") or {}).get("pending_review", (result.get("summary") or {}).get("unreported", 0)) > 0
            self.review_attention.emit(needs_review and self.date.date() <= QDate.currentDate())
            self.plan_button.setText("调整计划" if self.has_plan else "生成计划")
            self.plan_button.setVisible(self.has_plan)
            self.render_plan(result)
        def today_loaded(result):
            if generation != self.generation:
                return
            self.today_result = result
            self.render_events(result.get("events", []))
            self.render_attention()
        def warnings_loaded(result):
            if generation != self.generation:
                return
            self.warning_result = result
            self.render_attention()
        def tasks_loaded(result):
            if generation != self.generation: return
            self.daily_tasks_result = result
            if not result.get("items") and self.tasks_offset and result.get("total", 0) <= self.tasks_offset:
                self.tasks_offset = max(0, self.tasks_offset - 10); self.refresh(); return
            self.render_tasks(result)
        self.bridge.query("daily_tasks", tasks_loaded, failed, date=day, limit=10, offset=self.tasks_offset)
        self.bridge.query("daily_review", review_loaded, failed, date=day)
        self.bridge.query("today", today_loaded, failed, date=day)
        self.bridge.query("warnings", warnings_loaded, failed, date=day, limit=3, offset=self.warning_offset)

    def request_plan(self):
        if self.on_plan:
            self.on_plan(self.date_iso(), self.has_plan)

    def render_plan(self, result):
        clear_layout(self.plan_layout)
        if not result:
            self.plan_layout.addWidget(plain_label("今天的安排", "SectionHeading"))
            self.plan_layout.addWidget(plain_label("正在读取当天计划…", "Quiet"))
            return
        if not result.get("has_plan"):
            self.plan_layout.addWidget(plain_label("还没有这一天的计划", "CardTitle"))
            self.plan_layout.addWidget(plain_label("先说明今天能投入的时间，以及最需要完成的事情。\nCodex 会结合已登记的固定安排，给出可以核对的计划。", "Body"))
            self.plan_layout.addSpacing(5)
            self.plan_layout.addWidget(make_button("让 Codex 安排", self.request_plan, True), alignment=Qt.AlignmentFlag.AlignLeft)
            return
        plan = result.get("plan") or {}
        header = QHBoxLayout()
        heading = plain_label(plan.get("title") or "这一天的计划", "CardTitle")
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
            self.progress.title.setText("完成记录")
            self.progress.set_counts(summary)
            self.plan_layout.addSpacing(7)
            self.plan_layout.addWidget(self.progress)
        footer = QHBoxLayout()
        footer.addWidget(plain_label("完成情况在复盘中统一确认。", "Quiet"), 1)
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
        title = make_button(item.get("title", "计划事项"), lambda: self.on_task(item["target_id"]) if self.on_task else None)
        title.setObjectName("TextLink")
        content.addWidget(title)
        details = []
        if item.get("target_archived"):
            details.append("已删除 · 保留原计划记录")
        if item.get("completion_gate"):
            details.append(str(item["completion_gate"]))
        if item.get("planned_minutes") is not None:
            details.append(f"预计 {item['planned_minutes']} 分钟")
        if details:
            content.addWidget(plain_label(" · ".join(details), "Quiet"))
        layout.addLayout(content, 1)
        result = item.get("result")
        layout.addWidget(plain_label({"done": "已完成", "incomplete": "未完成"}.get(result, "已删除" if item.get("target_archived") else "尚未反馈"), "StatusPill"))
        return row

    def render_events(self, events):
        clear_layout(self.events_layout)
        self.events_layout.addWidget(plain_label("固定安排", "SectionHeading"))
        if not events:
            self.events_layout.addWidget(plain_label("这一天没有已登记的固定安排。", "Quiet"))
            return
        for event in events:
            data = event.get("data", event)
            row = QFrame()
            row.setObjectName("EventRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(17, 13, 17, 13)
            start, end = data.get("start"), data.get("end")
            time = f"{start} – {end}" if start and end else "时间待确认"
            clock = plain_label(time, "TimeLabel")
            clock.setFixedWidth(130)
            layout.addWidget(clock)
            layout.addWidget(plain_label(event.get("title", "固定安排")), 1)
            if data.get("location"):
                layout.addWidget(plain_label(str(data["location"]), "Quiet"))
            if event.get("id"): layout.addWidget(make_button("固定准备事项", lambda _, item=event: self.open_recurring(item)))
            self.events_layout.addWidget(row)

    def render_tasks(self, result):
        clear_layout(self.tasks_layout)
        self.tasks_layout.addWidget(plain_label("当天候选待办", "SectionHeading"))
        self.tasks_layout.addWidget(plain_label("这里只列已安排到这天、到期或逾期的未完成任务。是否今天处理由你决定；加入日计划后，才进入当天复盘。", "Quiet"))
        if not result.get("items"):
            self.tasks_layout.addWidget(plain_label("没有尚未排入当天计划的日期相关任务。其他课程任务仍可从项目与课程查看。", "Quiet"))
        day = result.get("date") or self.date_iso()
        snapshot = result.get("plan")
        for task in result.get("items", []):
            row = QFrame(); row.setObjectName("TaskRow"); inner = QHBoxLayout(row); inner.setContentsMargins(16, 13, 16, 13)
            words = QVBoxLayout(); title = make_button(task["title"], lambda _, identifier=task["id"]: self.on_task(identifier) if self.on_task else None); title.setObjectName("TextLink"); words.addWidget(title)
            words.addWidget(plain_label(task.get("reason") or "日期相关待办", "Quiet"))
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
            self.pending_targets.discard(key); self.refresh()
            if self.on_changed: self.on_changed(receipt)
        def failed(error):
            self.pending_targets.discard(key); self.refresh(); self.error.emit(error)
        self.bridge.command("add_to_plan", payload, saved, failed)

    def open_recurring(self, anchor):
        from .gui_recurring import RecurringDialog
        dialog = RecurringDialog(self.bridge, anchor, self, lambda receipt: (self.refresh(), self.on_changed(receipt) if self.on_changed else None))
        self.dialogs.append(dialog); dialog.finished.connect(lambda _: self.dialogs.remove(dialog) if dialog in self.dialogs else None); dialog.open()

    def render_attention(self):
        clear_layout(self.attention_layout)
        notices = []
        for item in (self.warning_result or {}).get("items", []):
            title = item.get("title") or item.get("message") or "需要核对"
            detail = item.get("reason") or item.get("message") or item.get("description") or ""
            notices.append((str(title), str(detail)))
        unknowns = (self.today_result or {}).get("unknowns", [])
        for item in unknowns[:2]:
            text = item if isinstance(item, str) else item.get("message", item.get("reason", "部分时间信息尚待确认"))
            if text and not any(text in detail for _, detail in notices):
                notices.append(("安排前需要确认", str(text)))
        reminders = (self.today_result or {}).get("notifications", [])
        if not notices and not reminders:
            self.attention_box.hide()
            return
        self.attention_box.show()
        desired_index = 0 if reminders else 3
        if self.body.indexOf(self.attention_box) != desired_index:
            self.body.removeWidget(self.attention_box)
            self.body.insertWidget(desired_index, self.attention_box)
        self.attention_layout.addWidget(plain_label("需要留意", "SectionHeading"))
        visible_reminders = reminders if self.show_all_reminders else reminders[:3]
        for reminder in visible_reminders:
            data = reminder.get("data", {})
            card = QFrame()
            card.setObjectName("AttentionCard")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(17, 13, 17, 13)
            heading = reminder.get("title", "复盘提醒")
            if data.get("business_date") and data["business_date"] != self.date_iso():
                heading += " · " + data["business_date"]
            layout.addWidget(plain_label(heading, "AttentionTitle"))
            layout.addWidget(plain_label(data.get("content", ""), "AttentionText"))
            actions = QHBoxLayout()
            mode = data.get("review_mode")
            if mode in ("daily", "weekly"):
                day = data.get("business_date") or self.date_iso()
                actions.addWidget(make_button("查看每周回顾" if mode == "weekly" else "前往每日复盘", lambda checked=False, d=day, m=mode: self.on_review(d, m) if self.on_review else None))
            actions.addStretch()
            actions.addWidget(make_button("知道了", lambda checked=False, item=reminder: self.mark_reminder_seen(item)))
            layout.addLayout(actions)
            self.attention_layout.addWidget(card)
        if len(reminders) > 3 and not self.show_all_reminders:
            self.attention_layout.addWidget(make_button(f"展开其余 {len(reminders) - 3} 条近期提醒", self.expand_reminders))
        for title, detail in notices[:4]:
            card = QFrame()
            card.setObjectName("AttentionCard")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(17, 13, 17, 13)
            layout.addWidget(plain_label(title, "AttentionTitle"))
            if detail and detail != title:
                layout.addWidget(plain_label(detail, "AttentionText"))
            self.attention_layout.addWidget(card)
        total = (self.warning_result or {}).get("total", 0)
        if total > 3:
            row = QHBoxLayout()
            returned = len((self.warning_result or {}).get("items", []))
            row.addWidget(plain_label(f"警戒 {self.warning_offset + 1}–{self.warning_offset + returned} / {total} 条", "Quiet"), 1)
            previous = make_button("上一页警戒", lambda: self.set_warning_page(max(0, self.warning_offset - 3)))
            previous.setEnabled(self.warning_offset > 0)
            row.addWidget(previous)
            next_offset = (self.warning_result or {}).get("next_offset")
            following = make_button("更多警戒" if self.warning_offset == 0 else "下一页警戒", lambda: self.set_warning_page(next_offset))
            following.setEnabled(next_offset is not None)
            row.addWidget(following)
            self.attention_layout.addLayout(row)

    def set_warning_page(self, offset):
        if offset is not None:
            self.warning_offset = offset
            self.refresh()


    def expand_reminders(self):
        self.show_all_reminders = True
        self.render_attention()

    def mark_reminder_seen(self, reminder):
        self.bridge.command("update", {"id": reminder["id"], "version": reminder["version"], "patch": {"data": {"seen": True}}}, lambda _: self.refresh(), self.error.emit)
