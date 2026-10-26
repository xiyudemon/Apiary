"""系统架构 + 调度/避障算法可视化。

页面上半部分是三层架构数据流图，下半部分是可切换的算法流程图
（调度流程 / 事件驱动触发 / 分层避障）。图形全部用 Canvas 手绘，
不引入额外图形依赖，保持与项目其余部分一致的零第三方包风格。
"""

import tkinter as tk
from tkinter import ttk

from core import task_pool, zones


ARCHITECTURE_NODES = (
    # (标题, 副标题, 颜色, 列, 行, 列跨度, 行跨度)
    ("用户 / 管理员", "自然语言请求\n任务等级：紧急/急迫/一般/常规\n可要求多架无人机协同", "#dbeafe", 0, 0, 1, 1),
    ("AI 层（Ollama）", "识别语言 + 用户等级\n产出任务：结束地点 / 等级 / 执行机数\n只做宏观策略，不做底层移动", "#ede9fe", 1, 0, 1, 2),
    ("服务端调度核心", "任务池排序 + 事件驱动重调度\n负载均衡 → 路程最短 → 限制绕路", "#dcfce7", 2, 0, 1, 1),
    ("本地热力图", "记录无人机经过路段的信号\n仅按区块轮询 3×3 观测", "#fef3c7", 3, 0, 1, 2),
    ("无人机集群", "分配结果下发\n本地避障（绿色通行 / 黄色靠绿 / 红色回退 / 危险区域退出）", "#fee2e2", 2, 1, 1, 1),
    ("信号场", "四档分区：绿 / 黄 / 红 / 紫\n危险区域完全禁入", "#e5e7eb", 0, 1, 1, 1),
)

ARCHITECTURE_FLOWS = (
    # (起点节点下标, 终点节点下标, 连线文案)
    (0, 1, "提交请求"),
    (1, 2, "下发任务"),
    (1, 3, "同步等级"),
    (3, 2, "传输本地热力图"),
    (2, 4, "分配任务"),
    (5, 4, "观测信号"),
)

SCHEDULING_STEPS = (
    ("提交请求", "用户自然语言 + 等级"),
    ("AI 解析", "to_location 必填，from_location 可空"),
    ("入池排序", "紧急→急迫→一般→常规"),
    ("触发判定", "请求更新 / 状态更新 / 待办排队"),
    ("候选筛选", "紧急可抢占，其它不打断"),
    ("目标函数", "负载均衡→路程最短→限制绕路"),
    ("下发执行", "本地避障接管"),
)

EVENT_STEPS = (
    ("任务状态变化", "完成 / 失败 / 取消"),
    ("请求更新", "新请求或等级调整"),
    ("有待办 + 有空闲机", "排队任务继续分配"),
    ("重新调度", "仅在以上条件成立时发生"),
    ("其余 tick", "保持飞行，绝不打断非紧急任务"),
)

AVOIDANCE_STEPS = (
    ("绿色 无干扰", "全速通行", "#2fb36b"),
    ("黄色 有干扰", "不影响任务时向附近绿色靠拢", "#d9c74a"),
    ("红色 难以通行", "停下 → 搜索 4×4 回退点 → 回到黄/绿", "#f28c4a"),
    ("仍无法通行", "重规划路线（限 2 次）→ 限速穿越", "#e07b39"),
    ("紫色 危险区域", "完全禁入 → 先退出再重新规划", "#8b5cf6"),
)


class VisualizationPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._after_id = None
        self._live_after_id = None
        self._build_page()
        self._render_all()
        self.bind("<Configure>", self._on_resize)
        self._schedule_live_refresh()

    # ------------------------------------------------------------------ 布局

    def _build_page(self):
        ttk.Label(self, text="系统架构与算法可视化", font=("SimHei", 22)).pack(anchor="w")
        ttk.Label(self, text="三层解耦：AI 负责语义与宏观策略，服务端负责调度，无人机负责本地避障").pack(
            anchor="w", pady=(4, 10)
        )

        body = ttk.PanedWindow(self, orient="vertical")
        body.pack(fill="both", expand=True)

        arch_frame = ttk.LabelFrame(body, text="一、系统架构与数据流")
        body.add(arch_frame, weight=3)
        self.arch_canvas = tk.Canvas(arch_frame, bg="#f8fafc", height=260, highlightthickness=0)
        self.arch_canvas.pack(fill="both", expand=True, padx=8, pady=8)

        flow_frame = ttk.LabelFrame(body, text="二、调度与避障算法流程")
        body.add(flow_frame, weight=3)

        selector = ttk.Frame(flow_frame)
        selector.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(selector, text="流程：").pack(side="left")
        self.flow_var = tk.StringVar(value="调度流程")
        self.flow_box = ttk.Combobox(
            selector,
            state="readonly",
            width=18,
            textvariable=self.flow_var,
            values=("调度流程", "任务全生命周期", "事件驱动触发", "分层避障", "信号借用规则"),
        )
        self.flow_box.pack(side="left", padx=(4, 12))
        self.flow_box.bind("<<ComboboxSelected>>", lambda _event: self._render_flow())
        ttk.Label(selector, text="紫色危险区对所有无人机（含紧急任务）完全禁入").pack(side="left")

        self.flow_canvas = tk.Canvas(flow_frame, bg="#f8fafc", height=200, highlightthickness=0)
        self.flow_canvas.pack(fill="both", expand=True, padx=8, pady=8)

        note_frame = ttk.LabelFrame(body, text="三、关键约束")
        body.add(note_frame, weight=2)
        notes = (
            "1. 任务必须有结束地点；未提供出发地点时默认使用执行无人机的当前位置。",
            "2. 只有任务状态更新、请求更新（或有待办任务 + 空闲无人机）时才重新调度；非紧急任务不打断在飞任务。",
            "3. 一条请求可要求多架无人机协同：常规/一般/急迫只派当前空闲机，未派满的由后续 tick 自动补派；只有紧急任务才能抢占并把全部无人机调过去。",
            "4. 紧急任务可借用自身 3×3 范围内任意无人机的信号，但危险区域依然完全禁入。",
            "5. 服务端拿不到原始热力图，只能看到无人机 3×3 区块的信号条件；热力图由本地累积的经过路段生成，并在每次 AI 调度时传输。",
            "6. 同等级任务按\u201c空闲且离起点近\u201d排队；避障功能下放到无人机本地执行。",
        )
        for note in notes:
            ttk.Label(note_frame, text=note, wraplength=980, justify="left", font=("SimHei", 9)).pack(
                anchor="w", padx=10, pady=2
            )

    # ------------------------------------------------------------------ 绘制

    def _on_resize(self, _event):
        # 合并连续的 Configure 事件，避免拖拽窗口时反复重绘。
        if self._after_id is not None:
            self.after_cancel(self._after_id)
        self._after_id = self.after(120, self._render_all)

    def _render_all(self):
        self._after_id = None
        self._render_architecture()
        self._render_flow()

    def _schedule_live_refresh(self, delay=1000):
        """生命周期视图展示的是实时数据，需要周期重绘（仅该模式，避免无谓开销）。"""
        try:
            self._live_after_id = self.after(delay, self._live_refresh)
        except tk.TclError:
            self._live_after_id = None

    def _live_refresh(self):
        self._live_after_id = None
        try:
            if self.flow_var.get() == "任务全生命周期" and self.winfo_ismapped():
                self._render_flow()
        except tk.TclError:
            return
        self._schedule_live_refresh()

    def _render_architecture(self):
        canvas = self.arch_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 640)
        height = max(canvas.winfo_height(), 240)
        columns = 4
        rows = 2
        margin_x = 18
        margin_y = 16
        gap_x = 46
        gap_y = 26
        cell_w = (width - margin_x * 2 - gap_x * (columns - 1)) / columns
        cell_h = (height - margin_y * 2 - gap_y * (rows - 1)) / rows

        def center(column, row):
            x = margin_x + column * (cell_w + gap_x) + cell_w / 2
            y = margin_y + row * (cell_h + gap_y) + cell_h / 2
            return x, y

        for src, dst, label in ARCHITECTURE_FLOWS:
            src_column, src_row = ARCHITECTURE_NODES[src][3], ARCHITECTURE_NODES[src][4]
            dst_column, dst_row = ARCHITECTURE_NODES[dst][3], ARCHITECTURE_NODES[dst][4]
            sx, sy = center(src_column + 0.5, src_row + 0.5)
            dx, dy = center(dst_column + 0.5, dst_row + 0.5)
            canvas.create_line(sx, sy, dx, dy, arrow="last", fill="#64748b", width=1.6, dash=(4, 3))
            canvas.create_text((sx + dx) / 2, (sy + dy) / 2 - 8, text=label, fill="#475569", font=("SimHei", 8))

        for title, subtitle, color, column, row, colspan, rowspan in ARCHITECTURE_NODES:
            x0 = margin_x + column * (cell_w + gap_x)
            y0 = margin_y + row * (cell_h + gap_y)
            x1 = x0 + cell_w * colspan + gap_x * (colspan - 1)
            y1 = y0 + cell_h * rowspan + gap_y * (rowspan - 1)
            canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="#94a3b8", width=1.4)
            canvas.create_text((x0 + x1) / 2, y0 + 20, text=title, font=("SimHei", 11, "bold"), fill="#1e293b")
            canvas.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2 + 12,
                text=subtitle,
                font=("SimHei", 8),
                fill="#475569",
                width=max(int(x1 - x0) - 16, 90),
                justify="center",
            )

    def _render_flow(self):
        canvas = self.flow_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 640)
        height = max(canvas.winfo_height(), 180)
        mode = self.flow_var.get()
        if mode == "事件驱动触发":
            self._render_vertical_flow(canvas, width, height, EVENT_STEPS)
        elif mode == "分层避障":
            self._render_avoidance(canvas, width, height)
        elif mode == "信号借用规则":
            self._render_borrow_rules(canvas, width, height)
        elif mode == "任务全生命周期":
            self._render_lifecycle(canvas, width, height)
        else:
            self._render_scheduling(canvas, width, height)

    # --------------------------------------------------- 任务全生命周期（真实数据）

    @staticmethod
    def _short(text, limit=46):
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _lifecycle_stages(self):
        """把当前运行状态投影成 8 个生命周期阶段的「状态 + 真实数据」。"""
        backend = getattr(self.app, "backend", None)
        if backend is None:
            return []
        tasks = list(backend.task_pool.get_sorted_tasks())
        drones = backend.simulation.get_drones_state()
        status_of = {}
        for task in tasks:
            status_of[task.status] = status_of.get(task.status, 0) + 1
        by_priority = {}
        for task in tasks:
            key = task_pool.normalize_priority(task.priority)
            by_priority[key] = by_priority.get(key, 0) + 1

        latest = tasks[0] if tasks else None
        user_requests = sum(1 for task in tasks if str(task.task_id).startswith("task_user_"))
        if latest is not None:
            request_text = "队列首位「{}」 by {}（{}）；自然语言请求共 {} 条".format(
                self._short(latest.raw_text, 26),
                latest.requested_by or "unknown",
                latest.user_level or "未标注",
                user_requests,
            )
        else:
            request_text = "暂无自然语言请求"

        ai_calls = getattr(backend, "ai_dispatch_count", 0)
        payload = getattr(backend, "last_heatmap_payload", {}) or {}
        if getattr(backend, "ai_enabled", False):
            ai_text = "已调用 AI {} 次；随请求传输热力图 v{}（{} 个已观测区块）".format(
                ai_calls,
                backend.simulation.heatmap_version,
                payload.get("observed_blocks", len(backend.simulation.observed_heatmap)),
            )
        else:
            ai_text = "AI 关闭，全部由本地规则解析"

        order_text = " ".join(
            "{}×{}".format(task_pool.priority_label(key), by_priority[key])
            for key in task_pool.PRIORITY_KEYS
            if by_priority.get(key)
        ) or "任务池为空"

        pool_text = "共 {} 个：待办 {} / 执行中 {} / 已完成 {} / 取消 {}".format(
            len(tasks),
            status_of.get("pending", 0),
            status_of.get("in_progress", 0),
            status_of.get("completed", 0),
            status_of.get("cancelled", 0),
        )

        carrying = [(d["id"], d["task_id"], d.get("task_priority_label")) for d in drones if d.get("task_id")]
        if carrying:
            assign_text = "；".join("{}→{}（{}）".format(*item) for item in carrying[:3])
            if len(carrying) > 3:
                assign_text += " 等 {} 项".format(len(carrying))
        else:
            assign_text = "当前无在派任务"

        routed = [
            "{} {}/{} 段".format(d["id"], d.get("route_index", 0), d.get("route_length", 0))
            for d in drones
            if d.get("route_length")
        ]
        route_text = " / ".join(routed[:4]) if routed else "无已规划路线"

        holding = [d["id"] for d in drones if d.get("status") in {"holding", "blocked", "exiting", "avoiding"}]
        borrowing = [d["id"] for d in drones if d.get("borrowed_signal")]
        avoidance_text = "本地避障/受阻: {}；借用 3×3 信号: {}".format(
            "、".join(holding) if holding else "无",
            "、".join(borrowing) if borrowing else "无",
        )

        awaiting = [d["id"] for d in drones if d.get("status") == "awaiting"]
        returning = [d["id"] for d in drones if d.get("status") == "returning"]
        done_text = "已完成 {}/{}；原地待命 {}；返航 {}".format(
            status_of.get("completed", 0),
            len(tasks),
            "、".join(awaiting) if awaiting else "无",
            "、".join(returning) if returning else "无",
        )

        all_done = bool(tasks) and status_of.get("completed", 0) == len(tasks)
        return [
            ("1. 用户请求", request_text, bool(tasks)),
            ("2. AI 解析", ai_text, ai_calls > 0 or not getattr(backend, "ai_enabled", False)),
            ("3. 等级排序", order_text, bool(tasks)),
            ("4. 任务池", pool_text, bool(tasks)),
            ("5. 分配无人机", assign_text, bool(carrying)),
            ("6. 路线规划", route_text, bool(routed)),
            ("7. 避障", avoidance_text, bool(holding or borrowing)),
            ("8. 完成", done_text, all_done),
        ]

    def _render_lifecycle(self, canvas, width, height):
        stages = self._lifecycle_stages()
        if not stages:
            canvas.create_text(
                width / 2,
                height / 2,
                text="后端未初始化，暂无生命周期数据",
                font=("SimHei", 10),
                fill="#64748b",
            )
            return

        margin = 14
        gap = 10
        columns = 4
        rows = 2
        footer_h = 20
        box_w = (width - margin * 2 - gap * (columns - 1)) / columns
        box_h = (height - margin * 2 - footer_h - gap * (rows - 1)) / rows
        for index, (title, detail, done) in enumerate(stages):
            column = index % columns
            row = index // columns
            x0 = margin + column * (box_w + gap)
            y0 = margin + row * (box_h + gap)
            x1 = x0 + box_w
            y1 = y0 + box_h
            outline = "#16a34a" if done else "#94a3b8"
            canvas.create_rectangle(x0, y0, x1, y1, fill="#ffffff", outline=outline, width=1.6)
            canvas.create_rectangle(x0, y0, x0 + 6, y1, fill=outline, outline=outline)
            canvas.create_text(
                x0 + 14,
                y0 + 13,
                text=title,
                anchor="w",
                font=("SimHei", 9, "bold"),
                fill="#0f172a",
            )
            canvas.create_text(
                x1 - 12,
                y0 + 13,
                text="已发生" if done else "待发生",
                anchor="e",
                font=("SimHei", 8),
                fill=outline,
            )
            canvas.create_text(
                x0 + 14,
                y0 + box_h / 2 + 8,
                text=detail,
                anchor="w",
                font=("SimHei", 8),
                fill="#334155",
                width=max(int(box_w) - 26, 80),
                justify="left",
            )
            if column:  # 行内箭头
                canvas.create_line(x0 - gap + 1, y0 + box_h / 2, x0 - 2, y0 + box_h / 2, arrow="last", fill="#94a3b8")
            if row and column == 0:  # 换行箭头
                canvas.create_line(
                    margin + box_w / 2,
                    y0 - gap + 1,
                    margin + box_w / 2,
                    y0 - 2,
                    arrow="last",
                    fill="#94a3b8",
                )

        backend = getattr(self.app, "backend", None)
        reason = getattr(backend, "reschedule_reason", None) or "—"
        canvas.create_text(
            margin,
            height - 8,
            text="实时视图：重调度 {} 次（最近原因 {}）｜ AI 调度 {} 次 ｜ 热力图版本 v{} ｜ 危险区域完全禁入".format(
                backend.reschedule_count,
                reason,
                backend.ai_dispatch_count,
                backend.simulation.heatmap_version,
            ),
            anchor="w",
            font=("SimHei", 8),
            fill="#475569",
        )

    def _render_scheduling(self, canvas, width, height):
        steps = SCHEDULING_STEPS
        margin = 16
        gap = 14
        box_w = (width - margin * 2 - gap * (len(steps) - 1)) / len(steps)
        box_h = min(height - margin * 2, 110)
        y0 = (height - box_h) / 2
        for index, (title, detail) in enumerate(steps):
            x0 = margin + index * (box_w + gap)
            canvas.create_rectangle(x0, y0, x0 + box_w, y0 + box_h, fill="#e0f2fe", outline="#0284c7", width=1.4)
            canvas.create_text(x0 + box_w / 2, y0 + 22, text=f"{index + 1}. {title}", font=("SimHei", 9, "bold"), fill="#0c4a6e")
            canvas.create_text(
                x0 + box_w / 2,
                y0 + box_h / 2 + 16,
                text=detail,
                font=("SimHei", 8),
                fill="#334155",
                width=max(int(box_w) - 10, 70),
                justify="center",
            )
            if index:
                canvas.create_line(x0 - gap, y0 + box_h / 2, x0 - 2, y0 + box_h / 2, arrow="last", fill="#0284c7", width=1.6)
        canvas.create_text(
            margin,
            y0 + box_h + 26,
            text="同一等级内按\u201c空闲且离起点近\u201d排序；非紧急任务绝不打断在飞任务。",
            anchor="w",
            font=("SimHei", 9),
            fill="#475569",
        )

    def _render_vertical_flow(self, canvas, width, height, steps):
        margin = 18
        gap = 12
        box_w = min((width - margin * 2 - gap * (len(steps) - 1)) / len(steps), 170)
        box_h = min(height - margin * 2 - 24, 92)
        y0 = (height - box_h) / 2 - 8
        for index, (title, detail) in enumerate(steps):
            x0 = margin + index * (box_w + gap)
            is_last = index == len(steps) - 1
            fill = "#fee2e2" if is_last else "#ede9fe"
            outline = "#dc2626" if is_last else "#7c3aed"
            canvas.create_rectangle(x0, y0, x0 + box_w, y0 + box_h, fill=fill, outline=outline, width=1.4)
            canvas.create_text(x0 + box_w / 2, y0 + 22, text=title, font=("SimHei", 9, "bold"), fill="#1e293b")
            canvas.create_text(
                x0 + box_w / 2,
                y0 + box_h / 2 + 14,
                text=detail,
                font=("SimHei", 8),
                fill="#334155",
                width=max(int(box_w) - 10, 70),
                justify="center",
            )
            if index:
                canvas.create_line(x0 - gap, y0 + box_h / 2, x0 - 2, y0 + box_h / 2, arrow="last", fill="#7c3aed", width=1.6)
        canvas.create_text(
            margin,
            y0 + box_h + 26,
            text="触发源相互独立：任一条件成立即重新分配，条件都不成立时该 tick 不做任何调度。",
            anchor="w",
            font=("SimHei", 9),
            fill="#475569",
        )

    def _render_avoidance(self, canvas, width, height):
        margin = 18
        gap = 14
        box_w = (width - margin * 2 - gap * 2) / 3
        box_h = (height - margin * 2 - gap) / 2 - 10
        for index, (title, detail, color) in enumerate(AVOIDANCE_STEPS):
            column = index % 3
            row = index // 3
            x0 = margin + column * (box_w + gap)
            y0 = margin + row * (box_h + gap)
            canvas.create_rectangle(x0, y0, x0 + box_w, y0 + box_h, fill="#ffffff", outline=color, width=2.0)
            canvas.create_rectangle(x0, y0, x0 + 8, y0 + box_h, fill=color, outline=color)
            canvas.create_text(x0 + 18, y0 + 20, text=title, anchor="w", font=("SimHei", 10, "bold"), fill="#1e293b")
            canvas.create_text(
                x0 + 18,
                y0 + box_h / 2 + 12,
                text=detail,
                anchor="w",
                font=("SimHei", 8),
                fill="#334155",
                width=max(int(box_w) - 28, 80),
                justify="left",
            )
        legend_x = margin + 2 * (box_w + gap)
        legend_y = margin + box_h + gap
        canvas.create_text(
            legend_x,
            legend_y + box_h / 2,
            text="阈值\n绿 ≥80 / 黄 ≥55\n红 ≥30 / 紫 <30",
            font=("SimHei", 8),
            fill="#475569",
            justify="left",
        )

    def _render_borrow_rules(self, canvas, width, height):
        margin = 20
        header_y = margin + 8
        canvas.create_text(margin, header_y, text="紧急任务信号借用判定", anchor="w", font=("SimHei", 11, "bold"), fill="#1e293b")

        rows = (
            ("任务等级是紧急？", "否 → 只能使用自身位置信号", zones.ZONE_COLORS[zones.YELLOW]),
            ("自身 3×3 范围内存在其它无人机？", "否 → 只能使用自身位置信号", zones.ZONE_COLORS[zones.YELLOW]),
            ("借用区块位于紫色危险区？", "是 → 拒绝借用，保持自身信号", zones.ZONE_COLORS[zones.PURPLE]),
            ("以上皆否", "借用以最强且非紫色的信号执行任务", zones.ZONE_COLORS[zones.GREEN]),
        )
        row_h = (height - header_y - margin) / len(rows) - 8
        for index, (condition, outcome, color) in enumerate(rows):
            y0 = header_y + 24 + index * (row_h + 8)
            canvas.create_rectangle(margin, y0, width / 2 - 8, y0 + row_h, fill="#f1f5f9", outline="#cbd5e1")
            canvas.create_rectangle(margin, y0, margin + 6, y0 + row_h, fill=color, outline=color)
            canvas.create_text(margin + 16, y0 + row_h / 2, text=condition, anchor="w", font=("SimHei", 9), fill="#1e293b")
            canvas.create_line(width / 2 - 8, y0 + row_h / 2, width / 2 + 6, y0 + row_h / 2, arrow="last", fill="#64748b")
            canvas.create_rectangle(width / 2 + 10, y0, width - margin, y0 + row_h, fill="#ffffff", outline=color, width=1.4)
            canvas.create_text(
                width / 2 + 22,
                y0 + row_h / 2,
                text=outcome,
                anchor="w",
                font=("SimHei", 9),
                fill="#334155",
                width=max(int(width / 2 - margin - 30), 120),
            )
        canvas.create_text(
            margin,
            height - 8,
            text=f"危险区域（{zones.ZONE_LABELS[zones.PURPLE]}）对紧急任务同样完全禁入；借用只放宽信号强度，不放宽通行权限。",
            anchor="w",
            font=("SimHei", 8),
            fill="#7c3aed",
        )


def priority_legend():
    """供其它页面复用的等级图例文案。"""
    return [
        (tier["label"], tier["description"])
        for tier in (task_pool.priority_tier(key) for key in task_pool.PRIORITY_KEYS)
    ]
