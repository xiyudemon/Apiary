import tkinter as tk
from tkinter import messagebox, ttk


class SettingsPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.debug_flags = {
            "breakpoint_signal": tk.BooleanVar(value=False),
            "breakpoint_task": tk.BooleanVar(value=False),
            "show_unknown_cells": tk.BooleanVar(value=True),
            "reset_tasks_on_startup": tk.BooleanVar(value=True),
        }
        backend = getattr(self.app, "backend", None)
        if backend is not None:
            self.debug_flags["reset_tasks_on_startup"].set(backend.reset_tasks_on_startup)
        self._build_page()

    def _build_page(self):
        ttk.Label(self, text="系统设置", font=("SimHei", 24)).pack(anchor="w")
        ttk.Label(self, text="系统调试与断点测试").pack(anchor="w", pady=(6, 18))

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)

        self._build_debug_tab(tabs)
        self._build_system_tab(tabs)

    def _build_debug_tab(self, notebook):
        page = ttk.Frame(notebook, padding=18)
        notebook.add(page, text="断点测试")

        ttk.Checkbutton(page, text="信号断点", variable=self.debug_flags["breakpoint_signal"]).pack(anchor="w", pady=4)
        ttk.Checkbutton(page, text="任务断点", variable=self.debug_flags["breakpoint_task"]).pack(anchor="w", pady=4)
        ttk.Checkbutton(page, text="显示未观测区块边线", variable=self.debug_flags["show_unknown_cells"]).pack(anchor="w", pady=4)
        ttk.Label(page, text="每次启动创建新演示并清除旧任务进度；登录后开始调度。").pack(anchor="w", pady=4)

        button_row = ttk.Frame(page)
        button_row.pack(fill="x", pady=(18, 0))
        ttk.Button(button_row, text="应用设置", command=self._apply_debug_settings).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="测试：信号断点", command=self._test_signal_breakpoint).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="测试：任务分配", command=self._test_task_assignment).pack(side="left")

    def _build_system_tab(self, notebook):
        page = ttk.Frame(notebook, padding=18)
        notebook.add(page, text="系统状态")

        info = tk.Text(page, height=14, wrap="word", font=("Microsoft YaHei", 9))
        info.pack(fill="both", expand=True)

        self.system_box = info
        self._refresh_system_status()

        ttk.Button(page, text="刷新系统状态", command=self._refresh_system_status).pack(anchor="e", pady=(10, 0))

    def _refresh_system_status(self):
        if not hasattr(self.app, "backend") or self.app.backend is None:
            self.system_box.delete("1.0", "end")
            self.system_box.insert("1.0", "后端未初始化\n")
            return

        backend = self.app.backend
        summary = backend.simulation.get_known_unknown_summary()
        text = [
            "系统状态",
            f"- 信号断点: {'开启' if self.debug_flags['breakpoint_signal'].get() else '关闭'}",
            f"- 任务断点: {'开启' if self.debug_flags['breakpoint_task'].get() else '关闭'}",
            f"- 已观测区块: {summary['observed']}",
            f"- 已确认可通行: {summary['known']}",
            f"- 未观测区块: {summary['unknown']}",
            f"- 当前速度: {backend.runtime.get('speed', 1.0)}",
            f"- 当前机队规模: {backend.runtime.get('drone_count', 0)}",
            f"- 当前任务数: {backend.runtime.get('task_count', 0)}",
            f"- 最后动作: {backend.runtime.get('last_action', 'idle')}",
            f"- 启动时重置任务: {'开启' if backend.reset_tasks_on_startup else '关闭'}",
        ]
        self.system_box.delete("1.0", "end")
        self.system_box.insert("1.0", "\n".join(text))

    def _apply_debug_settings(self):
        if not hasattr(self.app, "backend") or self.app.backend is None:
            messagebox.showwarning("提示", "后端未初始化，无法应用调试设置。")
            return

        self._refresh_system_status()
        main_page = getattr(self.app, "pages", {}).get("MainPage")
        drone_page = getattr(main_page, "pages", {}).get("drone") if main_page is not None else None
        if drone_page is not None:
            # 立刻按新的"显示未观测区块标记"开关重绘底图。
            drone_page._draw_static_background()
        messagebox.showinfo("提示", "调试设置已同步到当前运行状态。")

    def _test_signal_breakpoint(self):
        if self.debug_flags["breakpoint_signal"].get():
            messagebox.showinfo("提示", "信号断点已触发：当前已进入弱信号检测区。")
        else:
            messagebox.showinfo("提示", "信号断点未启用，当前仅为静态测试。")

    def _test_task_assignment(self):
        if self.debug_flags["breakpoint_task"].get():
            messagebox.showinfo("提示", "任务断点已触发：当前正在处理任务分配。")
        else:
            messagebox.showinfo("提示", "任务断点未启用，当前仅为分配检查。")