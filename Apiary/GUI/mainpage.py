import tkinter as tk
from tkinter import ttk

from GUI.drone_page import DronePage
from GUI.request_page import RequestPage
from GUI.settings_page import SettingsPage
from GUI.user_page import UserPage
from GUI.visualization_page import VisualizationPage


class MainPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.current_page = None
        self._build_layout()
        self._build_pages()
        self._select_page("drone")

    def _build_layout(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)

        sidebar = ttk.Frame(self, width=220, padding=12)
        sidebar.grid_propagate(False)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(1, weight=1)

        ttk.Label(sidebar, text="Apiary 蜂巢", font=("SimHei", 18)).grid(
            row=0, column=0, pady=(4, 18), sticky="ew"
        )

        self.navigation = ttk.Treeview(sidebar, show="tree", selectmode="browse", height=10)
        self.navigation.grid(row=1, column=0, sticky="nsew")
        self.navigation.bind("<<TreeviewSelect>>", self._on_navigation_selected)
        self.navigation.insert("", "end", iid="operations", text="业务功能", open=True)
        self.navigation.insert("operations", "end", iid="drone", text="无人机监控")
        self.navigation.insert("operations", "end", iid="requests", text="任务请求处理")
        self.navigation.insert("operations", "end", iid="viz", text="系统架构与算法")
        self.navigation.insert("", "end", iid="administration", text="系统管理", open=True)
        self.navigation.insert("administration", "end", iid="users", text="系统用户管理")
        self.navigation.insert("administration", "end", iid="settings", text="系统设置")

        ttk.Button(sidebar, text="返回登录", command=self.go_login).grid(
            row=2, column=0, sticky="ew", pady=(18, 0)
        )

        self.content = ttk.Frame(self, padding=24)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    def _build_pages(self):
        self.pages = {
            "drone": DronePage(self.content, self.app),
            "requests": RequestPage(self.content, self.app),
            "viz": VisualizationPage(self.content, self.app),
            "users": UserPage(self.content, self.app),
            "settings": SettingsPage(self.content, self.app),
        }

    def _on_navigation_selected(self, event):
        selection = self.navigation.selection()
        if selection:
            self._select_page(selection[0])

    def _select_page(self, page_name):
        if page_name not in self.pages:
            return
        if self.current_page is not None:
            self.pages[self.current_page].grid_forget()
        self.pages[page_name].grid(row=0, column=0, sticky="nsew")
        self.current_page = page_name
        if self.navigation.selection() != (page_name,):
            self.navigation.selection_set(page_name)

    def go_login(self):
        self.app.show_page("LoginPage")