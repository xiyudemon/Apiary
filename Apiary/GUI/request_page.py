from concurrent.futures import ThreadPoolExecutor
import webbrowser

import tkinter as tk
from tkinter import messagebox, ttk

from core import task_pool


class RequestPage(tk.Frame):
    """任务请求处理页：自然语言请求 → AI 解析 → 任务池排序 → 调度下发。"""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._submit_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apiary-submit")
        self._submit_future = None
        self._submit_generation = 0
        self._build_page()
        self.refresh_task_table()

    def _build_page(self):
        ttk.Label(self, text="任务请求处理", font=("SimHei", 24)).pack(anchor="w")
        ttk.Label(self, text="用户指令 → AI 识别语言与用户等级 → 任务池排序 → 调度下发").pack(anchor="w", pady=(6, 8))
        web_row = ttk.Frame(self)
        web_row.pack(fill="x", pady=(0, 10))
        ttk.Label(web_row, text=self.app.web_status).pack(side="left")
        ttk.Button(web_row, text="打开提交网页", command=self._open_web).pack(side="right")

        input_frame = ttk.Frame(self)
        input_frame.pack(fill="x", pady=(0, 12))
        ttk.Label(input_frame, text="自然语言任务：").grid(row=0, column=0, sticky="w")
        self.task_entry = tk.Text(input_frame, height=5, wrap="word")
        self.task_entry.grid(row=1, column=0, columnspan=4, sticky="nsew", padx=(0, 8), pady=(6, 0))
        input_frame.columnconfigure(0, weight=1)

        ttk.Label(input_frame, text="请求人：").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.requester_var = tk.StringVar(value="admin")
        ttk.Entry(input_frame, width=16, textvariable=self.requester_var).grid(
            row=2, column=1, sticky="w", padx=(4, 16), pady=(10, 0)
        )
        ttk.Label(input_frame, text="用户等级：").grid(row=2, column=2, sticky="w", pady=(10, 0))
        self.user_level_var = tk.StringVar(value="管理员")
        ttk.Combobox(
            input_frame,
            width=12,
            state="readonly",
            textvariable=self.user_level_var,
            values=("管理员", "操作员", "访客"),
        ).grid(row=2, column=3, sticky="w", padx=(4, 0), pady=(10, 0))

        priority_row = ttk.Frame(input_frame)
        priority_row.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(priority_row, text="任务等级：").pack(side="left")
        # 管理员发送时必须在此显式选择任务等级，选择结果优先于 AI 的推断。
        self.priority_var = tk.StringVar(value=task_pool.priority_label(task_pool.DEFAULT_PRIORITY))
        self.priority_box = ttk.Combobox(
            priority_row,
            width=10,
            state="readonly",
            textvariable=self.priority_var,
            values=[choice["label"] for choice in task_pool.priority_choices()],
        )
        self.priority_box.pack(side="left", padx=(4, 12))
        self.priority_hint_var = tk.StringVar()
        ttk.Label(priority_row, textvariable=self.priority_hint_var, foreground="#5b6470").pack(side="left")
        self.priority_box.bind("<<ComboboxSelected>>", lambda _event: self._update_priority_hint())
        self._update_priority_hint()

        # 一条请求可以要求多架无人机协同执行（"所有无人机前往 A 点"）。
        drones_row = ttk.Frame(input_frame)
        drones_row.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(drones_row, text="执行机数：").pack(side="left")
        self.drone_count_var = tk.IntVar(value=1)
        self.drone_count_box = ttk.Spinbox(
            drones_row,
            from_=1,
            to=task_pool.MAX_DRONES_PER_TASK,
            width=6,
            textvariable=self.drone_count_var,
            state="readonly",
        )
        self.drone_count_box.pack(side="left", padx=(4, 12))
        self.all_drones_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            drones_row,
            text="全部无人机",
            variable=self.all_drones_var,
            command=self._toggle_all_drones,
        ).pack(side="left", padx=(0, 12))
        self.drone_hint_var = tk.StringVar()
        ttk.Label(drones_row, textvariable=self.drone_hint_var, foreground="#5b6470").pack(side="left")
        self._toggle_all_drones()

        self.submit_button = ttk.Button(priority_row, text="提交任务", command=self.submit_task)
        self.submit_button.pack(side="right")

        task_frame = ttk.Frame(self)
        task_frame.pack(fill="both", expand=True)
        ttk.Label(task_frame, text="任务池（按等级排序）：", font=("SimHei", 12)).pack(anchor="w")

        self.task_table = ttk.Treeview(
            task_frame,
            columns=("task_id", "priority", "target", "from", "drones", "progress", "status", "requester"),
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.task_table.heading("task_id", text="任务ID")
        self.task_table.heading("priority", text="任务等级")
        self.task_table.heading("target", text="结束地点")
        self.task_table.heading("from", text="出发地点")
        self.task_table.heading("drones", text="执行机数")
        self.task_table.heading("progress", text="子任务进度")
        self.task_table.column("progress", width=110)
        self.task_table.heading("status", text="状态")
        self.task_table.heading("requester", text="请求人")
        self.task_table.column("priority", width=70)
        self.task_table.column("drones", width=70)
        self.task_table.pack(fill="both", expand=True)

        button_row = ttk.Frame(task_frame)
        button_row.pack(fill="x", pady=(8, 0))
        ttk.Label(button_row, text="调整为：").pack(side="left")
        self.reassign_var = tk.StringVar(value=task_pool.priority_label("urgent"))
        ttk.Combobox(
            button_row,
            width=10,
            state="readonly",
            textvariable=self.reassign_var,
            values=[choice["label"] for choice in task_pool.priority_choices()],
        ).pack(side="left", padx=(4, 8))
        ttk.Button(button_row, text="应用等级", command=self.apply_priority).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="刷新任务", command=self.refresh_task_table).pack(side="left")

        result_frame = ttk.Frame(self)
        result_frame.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(result_frame, text="任务处理结果：", font=("SimHei", 12)).pack(anchor="w")
        self.result_box = tk.Text(result_frame, height=9, wrap="word")
        self.result_box.pack(fill="both", expand=True, pady=(8, 0))

    def _open_web(self):
        if self.app.task_web is None:
            messagebox.showwarning("网页服务", self.app.web_status)
            return
        webbrowser.open("http://127.0.0.1:3000")

    def _update_priority_hint(self):
        key = task_pool.label_to_key(self.priority_var.get()) or task_pool.DEFAULT_PRIORITY
        tier = task_pool.priority_tier(key)
        hints = []
        # 等级字典里的键名是 "interrupts"（与 may_interrupt() 内部一致）。
        if tier["interrupts"]:
            hints.append("可抢占在飞任务")
        else:
            hints.append("不打断在飞任务")
        if tier["may_borrow_signal"]:
            hints.append("可借用 3×3 集群信号")
        self.priority_hint_var.set(f"（{tier['description']}；{'，'.join(hints)}）")

    def _toggle_all_drones(self):
        """勾选"全部无人机"后机数由机队规模决定，输入框随之禁用。"""
        all_drones = bool(self.all_drones_var.get())
        self.drone_count_box.configure(state="disabled" if all_drones else "readonly")
        if all_drones:
            self.drone_hint_var.set("（按当前机队总数派出）")
        else:
            self.drone_hint_var.set("（非紧急任务只用空闲机，其余排队等待）")

    def refresh_task_table(self):
        for item in self.task_table.get_children():
            self.task_table.delete(item)

        tasks = sorted(
            self.app.backend.list_tasks(),
            key=lambda task: (
                task.get("status") in {"completed", "cancelled"},
                not task_pool.may_interrupt(task.get("priority")),
                -task_pool.priority_rank(task.get("priority")),
                str(task.get("task_id") or ""),
            ),
        )

        for task in tasks:
            # list_tasks() 返回的是 Task.to_dict() 的持久化字段（to_location / from_location），
            # end_location / start_location 只是 Task 上的只读属性，不会出现在字典里。
            end_location = task.get("to_location") or task.get("end_location") or task.get("target") or "-"
            start_location = task.get("from_location") or task.get("start_location") or "无人机自身位置"
            self.task_table.insert(
                "",
                "end",
                values=(
                    task["task_id"],
                    task_pool.priority_label(task.get("priority")),
                    end_location,
                    start_location,
                    self._format_drone_quota(task),
                    f"{int(task.get('completed_children', 0))}/{int(task.get('total_children') or 1)} ({float(task.get('progress', 0)):.0%})",
                    task_pool.status_label(task.get("status")),
                    task.get("requested_by") or "-",
                ),
            )

    @staticmethod
    def _format_drone_quota(task):
        """把任务的机数要求展示为可读文本。"""
        if task.get("all_drones"):
            return "全部机队"
        return f"{max(1, int(task.get('required_drones') or 1))} 架"

    def apply_priority(self):
        selection = self.task_table.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择一个任务。")
            return
        task_id = self.task_table.item(selection[0], "values")[0]
        target_priority = task_pool.label_to_key(self.reassign_var.get())
        if target_priority is None:
            messagebox.showerror("提示", "请选择有效的任务等级。")
            return
        ok = self.app.backend.update_task_priority(task_id, target_priority)
        if ok:
            self.refresh_task_table()
            messagebox.showinfo("提示", f"任务 {task_id} 的等级已更新为「{task_pool.priority_label(target_priority)}」。")
        else:
            messagebox.showerror("提示", "更新任务等级失败。")

    def submit_task(self):
        if self._submit_future is not None:
            return
        raw_text = self.task_entry.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showwarning("提示", "请输入任务说明。")
            return

        priority = task_pool.label_to_key(self.priority_var.get()) or task_pool.DEFAULT_PRIORITY
        all_drones = bool(self.all_drones_var.get())
        try:
            required_drones = max(1, int(self.drone_count_var.get()))
        except (tk.TclError, ValueError):
            required_drones = 1
        self._submit_generation += 1
        generation = self._submit_generation
        self._submit_future = self._submit_executor.submit(
            self.app.backend.submit_user_request,
            raw_text,
            requested_by=self.requester_var.get().strip() or None,
            user_level=self.user_level_var.get().strip() or None,
            priority=priority,
            required_drones=1 if all_drones else required_drones,
            all_drones=all_drones,
        )
        self.submit_button.configure(state="disabled")
        self.result_box.delete("1.0", "end")
        self.result_box.insert("1.0", "正在解析任务，请稍候……")
        self.after(100, lambda: self._poll_submit(generation))

    def _poll_submit(self, generation):
        future = self._submit_future
        if generation != self._submit_generation or future is None:
            return
        if not future.done():
            self.after(100, lambda: self._poll_submit(generation))
            return
        self._submit_future = None
        self.submit_button.configure(state="normal")
        try:
            result = future.result()
            self.result_box.delete("1.0", "end")
            if isinstance(result, dict) and result.get("status") == "rejected":
                # AI 拒绝时必须显式告知用户，否则请求会被静默丢弃。
                reason = result.get("reason") or "未给出原因"
                self.result_box.insert("1.0", f"请求未受理（{reason}）。\n\n{result}")
                self.refresh_task_table()
                messagebox.showwarning("提示", f"AI 未受理该请求：{reason}。")
                return
            self.result_box.insert("1.0", str(result))
            self.refresh_task_table()
            messagebox.showinfo("提示", "任务已提交到任务池。")
        except Exception as exc:
            self.result_box.delete("1.0", "end")
            self.result_box.insert("1.0", f"错误：{exc}")
            messagebox.showerror("提示", f"任务提交失败：{exc}。")

    def close(self):
        self._submit_generation += 1
        if self._submit_future is not None:
            self._submit_future.cancel()
        self._submit_executor.shutdown(wait=False, cancel_futures=True)