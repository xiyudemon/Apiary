import time
import tkinter as tk
from tkinter import ttk

from core import pathplan, task_pool, zones


class DronePage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.canvas_size = 520
        self.world_radius = 190
        self.base_station = (0, 0)
        self.task_points = [
            (120, -120),
            (-150, 80),
            (170, 140),
            (-200, -160),
        ]
        self.drones = []
        self.last_source = "local"
        self.refresh_interval = 400
        # 热力图重绘的最小间隔与界面刷新周期对齐：版本号一变就能在下一帧画出来，
        # 不再被固定 1 秒的节流压到 1Hz（原本最坏会滞后 1 秒，看起来像"没有更新"）。
        self.heatmap_min_interval = max(0.1, self.refresh_interval / 1000.0 - 0.05)
        self._visual_ticks = 0
        self._visual_errors = 0
        self._heatmap_version = None
        self._last_heatmap_draw = 0.0
        self._last_summary_update = 0.0
        self.ai_status_var = tk.StringVar(value="本地调度")
        self.ai_knowledge_var = tk.StringVar(value="AI 已确认通行区: 0 | AI 未观测区: 0")
        self._build_page()
        self._draw_static_background()
        self._refresh_visual()
        self._refresh_trace()
        self.after(self.refresh_interval, self._loop_visual)

    def _build_page(self):
        ttk.Label(self, text="无人机监控", font=("SimHei", 22)).pack(anchor="w")
        ttk.Label(self, text="局部观测与集群调度（避障已下放到无人机本地执行）").pack(anchor="w", pady=(4, 10))

        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 10))

        ttk.Label(toolbar, text="无人机：").pack(side="left")
        self.drone_count_var = tk.StringVar(value="3")
        self.drone_count_entry = ttk.Entry(toolbar, width=7, textvariable=self.drone_count_var)
        self.drone_count_entry.pack(side="left", padx=(4, 12))

        ttk.Label(toolbar, text="任务：").pack(side="left")
        self.task_count_var = tk.StringVar(value="4")
        ttk.Entry(toolbar, width=7, textvariable=self.task_count_var).pack(side="left", padx=(4, 12))

        ttk.Label(toolbar, text="速度：").pack(side="left")
        self.speed_var = tk.StringVar(value="1.0")
        self.speed_scale = ttk.Scale(
            toolbar,
            from_=0.25,
            to=3.0,
            orient="horizontal",
            length=160,
            value=1.0,
            command=self._on_speed_change,
        )
        self.speed_scale.pack(side="left", padx=(4, 8))
        ttk.Label(toolbar, textvariable=self.speed_var, width=6).pack(side="left")

        ttk.Button(toolbar, text="重置模拟", command=self.reset_simulation).pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="本地调度", command=self.run_cycle).pack(side="right", padx=(0, 6))

        self.main_body = ttk.Frame(self)
        self.main_body.pack(fill="both", expand=True)
        self.main_body.columnconfigure(0, weight=3)
        self.main_body.columnconfigure(1, weight=2)

        self.map_panel = ttk.LabelFrame(self.main_body, text="地图")
        self.map_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        self.canvas = tk.Canvas(
            self.map_panel,
            width=self.canvas_size,
            height=self.canvas_size,
            bg="#edf3f7",
            highlightthickness=1,
            highlightbackground="#c8d0d8",
        )
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)

        self.side_panel = ttk.Frame(self.main_body)
        self.side_panel.grid(row=0, column=1, sticky="nsew")

        self.summary = ttk.LabelFrame(self.side_panel, text="运行状态")
        self.summary.pack(fill="x", pady=(0, 8))
        ttk.Label(self.summary, textvariable=self.ai_status_var, font=("SimHei", 11, "bold")).pack(anchor="w", padx=10, pady=(8, 4))
        ttk.Label(self.summary, textvariable=self.ai_knowledge_var, font=("SimHei", 9)).pack(anchor="w", padx=10, pady=(0, 8))

        self.legend_frame = ttk.LabelFrame(self.side_panel, text="信号四档图例")
        self.legend_frame.pack(fill="x", pady=(0, 8))
        legend_row = tk.Frame(self.legend_frame, bg="#f2f6fa")
        legend_row.pack(fill="x", padx=10, pady=8)
        for zone, color, label, detail in zones.legend():
            chip = tk.Frame(legend_row, bg="#f2f6fa")
            chip.pack(side="left", expand=True)
            tk.Label(chip, text="  ", bg=color, width=3, relief="solid", bd=1).pack()
            tk.Label(chip, text=label, bg="#f2f6fa", font=("SimHei", 9)).pack()
            tk.Label(chip, text=detail, bg="#f2f6fa", fg="#5b6470", font=("SimHei", 7)).pack()
        ttk.Label(
            self.legend_frame,
            text="紫色危险区对全部无人机（含紧急任务）完全禁入；紧急任务仅可借用 3×3 范围内其他无人机的信号。",
            wraplength=520,
            justify="left",
            font=("SimHei", 8),
        ).pack(anchor="w", padx=10, pady=(0, 8))

        self.pipeline_frame = ttk.LabelFrame(self.side_panel, text="调度流程")
        self.pipeline_frame.pack(fill="x", pady=(0, 8))
        self.pipeline_row = tk.Frame(self.pipeline_frame, bg="#f2f6fa")
        self.pipeline_row.pack(fill="x", padx=10, pady=10)
        self.pipeline_labels = []

        self.status_list_frame = ttk.LabelFrame(self.side_panel, text="无人机运行状态")
        self.status_list_frame.pack(fill="both", expand=True, pady=(0, 8))
        columns = ("position", "task", "priority", "zone", "activity")
        self.status_tree = ttk.Treeview(
            self.status_list_frame,
            columns=columns,
            show="tree headings",
            height=6,
            selectmode="none",
        )
        self.status_tree.heading("#0", text="无人机")
        self.status_tree.column("#0", width=58, anchor="w", stretch=False)
        for key, title, width in (
            ("position", "位置", 78),
            ("task", "当前任务", 96),
            ("priority", "等级", 46),
            ("zone", "信号档", 62),
            ("activity", "正在做什么", 190),
        ):
            self.status_tree.heading(key, text=title)
            self.status_tree.column(key, width=width, anchor="w", stretch=(key == "activity"))
        self.status_tree.tag_configure("idle", foreground="#5b6470")
        self.status_tree.tag_configure("awaiting", foreground="#1d6fb8")
        self.status_tree.tag_configure("returning", foreground="#8a6d1f")
        self.status_tree.tag_configure("working", foreground="#1f2d3d")
        self.status_tree.tag_configure("blocked", foreground="#b3261e")
        self.status_tree.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        self.status_scroll = ttk.Scrollbar(self.status_list_frame, orient="vertical", command=self.status_tree.yview)
        self.status_tree.configure(yscrollcommand=self.status_scroll.set)
        self.status_scroll.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))

        # pack 顺序决定竖直分配：事件日志钉在底部，状态列表吃掉多余空间，
        # 任务列表固定 7 行。三个控件都给出显式行高，否则 Text 默认请求 24 行
        # 会把侧栏顶出窗口、把下半部分整块裁掉。
        self.trace_box = tk.Text(self.side_panel, height=3, wrap="word", font=("Microsoft YaHei", 9))
        self.trace_box_scroll = ttk.Scrollbar(self.side_panel, orient="vertical", command=self.trace_box.yview)
        self.trace_box.configure(yscrollcommand=self.trace_box_scroll.set)
        self.trace_box.pack(side="bottom", fill="x", pady=(8, 0))
        self.trace_box_scroll.pack(side="bottom", fill="y")

        self.status_box = tk.Text(self.side_panel, height=6, wrap="word", font=("Microsoft YaHei", 9))
        self.status_box_scroll = ttk.Scrollbar(self.side_panel, orient="vertical", command=self.status_box.yview)
        self.status_box.configure(yscrollcommand=self.status_box_scroll.set)
        self.status_box.pack(fill="x")
        self.status_box_scroll.pack(side="right", fill="y")

    @staticmethod
    def _zh_status(value):
        mapping = {
            "pending": "待办",
            "in_progress": "执行中",
            "completed": "已完成",
            "cancelled": "已取消",
            "idle": "空闲",
            "awaiting": "待命",
            "returning": "返航中",
            "urgent": "紧急",
            "high": "急迫",
            "normal": "一般",
            "low": "常规",
            "requested": "已请求",
            "accepted": "已接受",
            "rejected": "已拒绝",
            "move": "移动",
            "assign": "分配",
            "task": "任务",
            "target": "目标",
            "priority": "等级",
            "status": "状态",
            "reason": "原因",
            "phase": "阶段",
            "drones": "无人机",
            "strategy": "策略",
        }
        if value is None:
            return "未知"
        text = str(value).strip()
        return mapping.get(text.lower(), text)

    @staticmethod
    def _json_to_chinese(value):
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                key = DronePage._zh_status(k)
                result[key] = DronePage._json_to_chinese(v)
            return result
        if isinstance(value, list):
            return [DronePage._json_to_chinese(item) for item in value]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return "空"
            # 直接翻译常见的英文状态值和字段描述，避免直接显示原始 JSON key。
            replacements = {
                "urgent": "紧急",
                "high": "急迫",
                "normal": "一般",
                "low": "常规",
                "pending": "待办",
                "in_progress": "执行中",
                "completed": "已完成",
                "cancelled": "已取消",
                "idle": "空闲",
                "awaiting": "待命",
                "returning": "返航中",
                "task_id": "任务编号",
                "to_location": "终点",
                "target": "目标",
                "priority": "等级",
                "status": "状态",
                "reason": "原因",
                "strategy": "策略",
                "move": "移动",
                "assign": "分配",
                "hold": "待机",
                "all_drones": "全部无人机",
                "required_drones": "所需机数",
                "drones": "无人机",
                "drone_id": "无人机编号",
                "detail": "详情",
                "action": "动作",
            }
            lowered = text.lower()
            for src, dst in replacements.items():
                if lowered == src:
                    return dst
            return text
        return value

    @staticmethod
    def _format_task_message(task):
        if not isinstance(task, dict):
            return "任务信息：无"
        task_id = task.get("task_id", "unknown")
        priority = DronePage._zh_status(task.get("priority"))
        status = DronePage._zh_status(task.get("status"))
        target = task.get("target") or task.get("to_location") or "未指定"
        required = task.get("required_drones")
        all_drones = task.get("all_drones")
        if all_drones:
            drones_text = "全部无人机"
        elif required is not None:
            drones_text = f"{required} 架"
        else:
            drones_text = "1 架"
        return f"任务 {task_id}：等级={priority}，状态={status}，目标={target}，分配={drones_text}。"

    @staticmethod
    def _format_event_message(item):
        if not isinstance(item, dict):
            return "事件：无更新"
        drone_id = item.get("drone_id") or item.get("drone") or "未知无人机"
        action = DronePage._zh_status(item.get("action") or "更新")
        step = DronePage._zh_status(item.get("step") or "状态")
        detail = item.get("detail") or item.get("message") or "无详细描述"
        translated = DronePage._json_to_chinese(detail)
        if isinstance(translated, dict):
            detail_text = ", ".join(f"{k}={v}" for k, v in translated.items())
        elif isinstance(translated, list):
            detail_text = ", ".join(str(v) for v in translated)
        else:
            detail_text = str(translated)
        return f"[{drone_id}] {action} - {step}：{detail_text}"

    @staticmethod
    def _render_latest_tasks(tasks):
        if not tasks:
            return "任务列表：当前无任务。"
        latest = tasks[-1]
        return DronePage._format_task_message(latest)

    @staticmethod
    def _render_latest_event(trace):
        if not trace:
            return "事件：暂无新消息。"
        return DronePage._format_event_message(trace[-1])

    def _to_canvas(self, x, y, scale=1.0):
        cx = self.canvas_size / 2
        cy = self.canvas_size / 2
        px = cx + x * scale
        py = cy - y * scale
        return px, py

    def _show_unknown_cells(self) -> bool:
        settings = getattr(self.app, "pages", {}).get("MainPage")
        page = getattr(settings, "pages", {}).get("settings") if settings is not None else None
        flags = getattr(page, "debug_flags", None)
        if not flags:
            return True
        return bool(flags["show_unknown_cells"].get())

    def _draw_static_background(self):
        """全量重绘：热力图 + 基站/任务点等静态标记。"""
        self.canvas.delete("all")
        self._draw_heatmap()
        self._draw_static_marks()
        self._heatmap_version = getattr(self.app.backend.simulation, "heatmap_version", 0) if self._backend_ready() else 0

    def _draw_heatmap(self):
        """按四档分区绘制热力图。可由版本号触发的增量重绘单独调用。

        只渲染服务端本地累积的区块观测（``simulation.observed_heatmap``）；
        未观测区块使用中性底色，不泄露原始全局热力图。
        """
        self.canvas.delete("heatmap")
        if not self._backend_ready():
            return

        simulation = self.app.backend.simulation
        observed = simulation.observed_heatmap
        show_unknown = self._show_unknown_cells()
        cx = self.canvas_size / 2
        cy = self.canvas_size / 2
        cell_size = pathplan.BLOCK_SIZE
        half = self.world_radius + 30
        # 与 core.pathplan 的区块模型严格对齐：区块以 block*BLOCK_SIZE 为中心。
        low, high = pathplan.block_of(-half, -half)[0], pathplan.block_of(half, half)[0]

        for bx in range(low, high + 1):
            for by in range(low, high + 1):
                center_x, center_y = pathplan.block_center((bx, by))
                # Y 轴向上：与 _to_canvas / _draw_static_marks 保持一致。
                px0 = cx + center_x - cell_size / 2
                py0 = cy - center_y - cell_size / 2
                px1 = px0 + cell_size
                py1 = py0 + cell_size
                entry = observed.get((bx, by))
                if entry is None:
                    # 未观测：只铺中性底色，可选勾出区块边线；不写文字，避免整片灰底被
                    # 「未知」标签糊满、盖掉无人机与航线。
                    self.canvas.create_rectangle(
                        px0,
                        py0,
                        px1,
                        py1,
                        fill=zones.color_for_zone(None),
                        outline="#96a5b5" if show_unknown else "",
                        width=1,
                        tags=("heatmap",),
                    )
                    continue
                color = zones.color_for_zone(entry.get("zone"))
                self.canvas.create_rectangle(px0, py0, px1, py1, fill=color, outline="", tags=("heatmap",))

        self.canvas.tag_lower("heatmap")

    def _draw_static_marks(self):
        """基站、探测半径与任务点；与热力图分层，避免重绘热力图时丢标记。"""
        cx = self.canvas_size / 2
        cy = self.canvas_size / 2
        self.canvas.delete("static")

        self.canvas.create_oval(cx - 120, cy - 120, cx + 120, cy + 120, outline="#8bb6d9", width=1, fill="", stipple="gray25", tags=("static",))
        self.canvas.create_oval(cx - 60, cy - 60, cx + 60, cy + 60, outline="#74a8d9", width=1, fill="", stipple="gray25", tags=("static",))
        self.canvas.create_text(cx + 14, cy - 12, text="基地", anchor="w", fill="#2d78b6", font=("SimHei", 10), tags=("static",))
        self.canvas.create_oval(cx - 6, cy - 6, cx + 6, cy + 6, fill="#2d78b6", outline="#2d78b6", tags=("static",))

        for label, (tx, ty) in zip(("A", "B", "C", "D"), self.task_points):
            px, py = self._to_canvas(tx, ty, scale=1.0)
            self.canvas.create_oval(px - 8, py - 8, px + 8, py + 8, fill="#ff7f0e", outline="#d65f00", tags=("static",))
            self.canvas.create_text(px + 12, py - 10, text=label, anchor="w", fill="#d65f00", font=("SimHei", 9), tags=("static",))

    def _sync_heatmap(self):
        """热力图版本变化时才重绘，并按界面刷新周期节流，避免每帧 625 个矩形拖慢界面。"""
        if not self._backend_ready():
            return
        version = getattr(self.app.backend.simulation, "heatmap_version", 0)
        if version == getattr(self, "_heatmap_version", None):
            return
        now = time.monotonic()
        if now - getattr(self, "_last_heatmap_draw", 0.0) < self.heatmap_min_interval:
            return
        self._last_heatmap_draw = now
        self._heatmap_version = version
        self._draw_heatmap()

    def _backend_ready(self) -> bool:
        return getattr(self.app, "backend", None) is not None

    def _on_speed_change(self, value):
        try:
            speed = float(value)
        except ValueError:
            return
        self.speed_var.set(f"{speed:.2f}")
        if hasattr(self.app, "backend") and self.app.backend is not None:
            self.app.backend.configure_runtime(speed=speed)

    def _set_ai_status(self, source: str):
        if source == "ai":
            self.ai_status_var.set("AI状态：已调用 Ollama")
        elif source == "manual":
            self.ai_status_var.set("AI状态：人工重评估")
        else:
            self.ai_status_var.set("AI状态：本地调度")
        self.last_source = source

    def _loop_visual(self):
        try:
            self._refresh_visual()
        except tk.TclError:
            # 窗口已销毁，停止刷新循环。
            return
        except (RuntimeError, AttributeError, KeyError, ValueError) as exc:
            self._visual_errors = getattr(self, "_visual_errors", 0) + 1
            if self._visual_errors <= 3:
                print(f"[drone_page] visual refresh failed: {type(exc).__name__}: {exc}")
        try:
            self.after(self.refresh_interval, self._loop_visual)
        except tk.TclError:
            return

    def _refresh_pipeline(self):
        if not hasattr(self.app, "backend") or self.app.backend is None:
            return
        pipeline = self.app.backend.get_pipeline_state()
        steps = pipeline.get("steps", [])
        signature = (pipeline.get("current_stage"), pipeline.get("message"),
                     tuple((step.get("name"), step.get("done")) for step in steps))
        if signature == getattr(self, "_pipeline_signature", None):
            return
        self._pipeline_signature = signature
        for widget in self.pipeline_labels:
            widget.destroy()
        self.pipeline_labels.clear()

        for step in steps:
            tag = tk.Label(
                self.pipeline_row,
                text=f"{step['name']}",
                width=10,
                height=2,
                bg="#b9d9f7" if step.get("done") else "#e9eef5",
                fg="#1f2d3d",
                relief="solid",
                bd=1,
                font=("SimHei", 9),
            )
            tag.pack(side="left", fill="y", expand=True, padx=(0, 6))
            if step.get("name") == pipeline.get("current_stage"):
                tag.configure(bg="#ffcc80", relief="raised", bd=2)
            self.pipeline_labels.append(tag)

        status = tk.Label(
            self.pipeline_row,
            text=pipeline.get("message", "等待调度起始"),
            anchor="w",
            justify="left",
            bg="#f2f6fa",
            fg="#2d3d4f",
            font=("SimHei", 9),
        )
        status.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.pipeline_labels.append(status)

    def _refresh_status_list(self):
        """刷新"无人机运行状态"列表：位置 / 当前任务 / 等级 / 信号档 / 正在做什么。"""
        tree = getattr(self, "status_tree", None)
        if tree is None:
            return
        # 行高跟随机队规模，最多 6 行（再多由 Treeview 自己滚动），
        # 保证侧栏总高度不会被大机队顶出窗口。
        signature = tuple(tuple(row.get(key) for key in (
            "id", "position", "task_id", "task_priority", "signal_strength",
            "observed_zone_label", "borrowed_signal", "status", "needs_replan", "busy_with",
        )) for row in self.drones)
        if signature == getattr(self, "_status_signature", None):
            return
        self._status_signature = signature
        tree.configure(height=max(2, min(len(self.drones), 6)))
        for item in tree.get_children():
            tree.delete(item)
        for drone in self.drones:
            task_id = drone.get("task_id")
            if task_id:
                task_text = task_id
                priority_text = drone.get("task_priority_label") or task_pool.priority_label(drone.get("task_priority"))
            elif drone.get("status") == "returning":
                task_text = "返航"
                priority_text = "—"
            else:
                task_text = "无"
                priority_text = "—"
            zone_text = "{} · {}".format(
                drone.get("observed_zone_label", "未知"),
                drone.get("signal_strength", 0.0),
            )
            if drone.get("borrowed_signal"):
                zone_text += " (借用)"
            status = drone.get("status")
            if drone.get("needs_replan") or status in {"holding", "blocked", "exiting"}:
                tag = "blocked"
            elif status == "returning":
                tag = "returning"
            elif status == "awaiting":
                tag = "awaiting"
            elif status == "idle":
                tag = "idle"
            else:
                tag = "working"
            tree.insert(
                "",
                "end",
                text=drone.get("id", "?"),
                values=(
                    drone.get("position", ""),
                    task_text,
                    priority_text,
                    zone_text,
                    drone.get("busy_with", "空闲"),
                ),
                tags=(tag,),
            )

    def _refresh_visual(self):
        if not hasattr(self.app, "backend") or self.app.backend is None:
            return

        self.canvas.delete("dynamic")
        self._sync_heatmap()
        self.drones = self.app.backend.simulation.get_drones_state()
        if self._visual_ticks % 3 == 0:
            ai_summary = self.app.backend.simulation.get_known_unknown_summary()
            self.ai_knowledge_var.set(
                f"AI 已确认通行区: {ai_summary['known']} | AI 未观测区: {ai_summary['unknown']} | "
                f"已观测区块: {ai_summary['observed']}"
            )

        for drone in self.drones:
            px, py = self._to_canvas(drone["x"], drone["y"], scale=1.0)
            local_zone = zones.zone_of(float(drone.get("signal_strength", 0.0)))
            fill_color = zones.color_for_zone(local_zone)
            self.canvas.create_oval(px - 8, py - 8, px + 8, py + 8, fill=fill_color, outline="#1f1f1f", tags=("dynamic",))
            task_label = drone.get("busy_with", "空闲")
            self.canvas.create_text(px + 10, py - 10, text=f"{drone['id']}:{task_label}", anchor="w", fill="#2f2f2f", font=("SimHei", 9), tags=("dynamic",))

        tasks = self.app.backend.list_tasks()
        latest_task_text = self._render_latest_tasks(tasks)
        if latest_task_text != getattr(self, "_last_task_text", None):
            self._last_task_text = latest_task_text
            self.status_box.delete("1.0", "end")
            self.status_box.insert("1.0", f"任务动态\n{latest_task_text}")

        self._visual_ticks += 1
        self._refresh_pipeline()
        if self._visual_ticks % 2 == 1:
            self._refresh_status_list()
        if self._visual_ticks % 2 == 0:
            self._refresh_trace()

    def _refresh_trace(self):
        if not hasattr(self.app, "backend") or self.app.backend is None:
            return
        trace = self.app.backend.get_event_trace()
        latest_message = self._render_latest_event(trace)
        if latest_message != getattr(self, "_last_trace_text", None):
            self._last_trace_text = latest_message
            self.trace_box.delete("1.0", "end")
            self.trace_box.insert("1.0", latest_message)

    def reset_simulation(self):
        try:
            count = int(self.drone_count_var.get())
        except ValueError:
            count = 3
        count = max(1, min(count, 12))
        self.drone_count_var.set(str(count))
        self.app.backend.configure_runtime(drone_count=count)
        self.app.backend.configure_runtime(task_count=int(self.task_count_var.get() or 4))
        try:
            speed = float(self.speed_var.get())
            self.app.backend.configure_runtime(speed=speed)
        except ValueError:
            pass
        self._draw_static_background()
        self._refresh_visual()

    def run_cycle(self):
        try:
            count = int(self.drone_count_var.get())
        except ValueError:
            count = 3
        count = max(1, min(count, 12))
        self.drone_count_var.set(str(count))
        self.app.backend.configure_runtime(drone_count=count)

        try:
            speed = float(self.speed_var.get())
            self.app.backend.configure_runtime(speed=speed)
        except ValueError:
            pass

        if self.app.backend is not None:
            result = self.app.backend.tick_simulation(force_ai=False)
            mode = "本地调度"
        else:
            result = {"source": "local", "strategy": {"reason": "后端未初始化"}}
            mode = "本地调度"

        self._set_ai_status(result.get("source", "local"))
        self._refresh_visual()
        strategy = result.get("strategy", {}) if isinstance(result.get("strategy"), dict) else {}
        strategy_name = self._zh_status(strategy.get("strategy", "maintain"))
        reason = strategy.get("reason", "local_scheduler")
        task_order = strategy.get("task_order", [])
        summary = (
            f"[{mode}] {self._zh_status(result.get('source', 'local'))}\n"
            f"策略：{strategy_name}\n"
            f"原因：{reason}\n"
            f"任务顺序：{task_order if task_order else '无'}"
        )
        self.status_box.delete("1.0", "end")
        self.status_box.insert("1.0", summary)

