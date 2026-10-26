import tkinter as tk
from tkinter import messagebox, ttk


class UserPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.user_rows = {}
        self._build_page()

    def _build_page(self):
        ttk.Label(self, text="系统用户管理", font=("SimHei", 24)).pack(anchor="w")
        ttk.Label(self, text="管理系统用户账号、用户等级与访问权限").pack(
            anchor="w", pady=(6, 18)
        )

        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        self.tabs = tabs
        self._build_user_list_tab(tabs)
        self._build_user_detail_tab(tabs)

    def _build_user_list_tab(self, notebook):
        page = ttk.Frame(notebook, padding=18)
        notebook.add(page, text="用户列表")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)

        button_bar = ttk.Frame(page)
        button_bar.grid(row=0, column=0, sticky="e", pady=(0, 10))
        ttk.Button(button_bar, text="新增用户", command=self.add_user).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(button_bar, text="删除用户", command=self.delete_selected_user).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(button_bar, text="刷新列表", command=self.refresh_users).pack(side="left")
        self.user_table = ttk.Treeview(
            page,
            columns=("id", "username", "vip_level"),
            show="headings",
            selectmode="browse",
        )
        self.user_table.grid(row=1, column=0, sticky="nsew")
        headings = {
            "id": "编号",
            "username": "用户名",
            "vip_level": "用户等级",
        }
        for column, title in headings.items():
            self.user_table.heading(column, text=title)
            self.user_table.column(column, width=160, anchor="center")
        self.user_table.bind("<<TreeviewSelect>>", self._show_selected_user)
        self.refresh_users()

    def _build_user_detail_tab(self, notebook):
        page = ttk.Frame(notebook, padding=18)
        notebook.add(page, text="用户详情")
        page.columnconfigure(1, weight=1)
        self.selected_user_id = None
        self.detail_entries = {}
        fields = (("username", "用户名"), ("password", "密码（留空表示不修改）"), ("vip_level", "用户等级"))
        for row, (field, label) in enumerate(fields):
            ttk.Label(page, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=8)
            entry = ttk.Entry(page, show="*" if field == "password" else "")
            entry.grid(row=row, column=1, sticky="ew", pady=8)
            self.detail_entries[field] = entry

        ttk.Button(page, text="保存", command=self.save_user).grid(
            row=len(fields), column=1, sticky="e", pady=(18, 0)
        )

    def refresh_users(self):
        for item in self.user_table.get_children():
            self.user_table.delete(item)
        self.user_rows.clear()
        for row in self.app.client_db.list_users():
            user_id, username, vip_level = row
            self.user_rows[str(user_id)] = row
            self.user_table.insert(
                "", "end", iid=str(user_id), values=(user_id, username, vip_level)
            )

    def _show_selected_user(self, event):
        selection = self.user_table.selection()
        if not selection:
            return
        user_id, username, vip_level = self.user_rows[selection[0]]
        self.selected_user_id = user_id
        for field, value in (
            ("username", username),
            ("password", ""),
            ("vip_level", vip_level),
        ):
            entry = self.detail_entries[field]
            entry.delete(0, tk.END)
            entry.insert(0, str(value))
        self.tabs.select(1)

    def add_user(self):
        self.selected_user_id = None
        for entry in self.detail_entries.values():
            entry.delete(0, tk.END)
        self.detail_entries["vip_level"].insert(0, "0")
        self.tabs.select(1)

    def delete_selected_user(self):
        selection = self.user_table.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择一个用户。")
            return
        user_id, username, _ = self.user_rows[selection[0]]
        if not messagebox.askyesno("确认删除", f"确定要删除用户“{username}”吗？"):
            return
        self.app.client_db.delete_user(user_id)
        self.selected_user_id = None
        self.refresh_users()
        for entry in self.detail_entries.values():
            entry.delete(0, tk.END)
        messagebox.showinfo("提示", "用户已删除。")

    def save_user(self):
        username = self.detail_entries["username"].get().strip()
        password = self.detail_entries["password"].get()
        vip_text = self.detail_entries["vip_level"].get().strip()
        if not username:
            messagebox.showwarning("提示", "用户名不能为空。")
            return
        if self.selected_user_id is None and not password:
            messagebox.showwarning("提示", "新增用户时必须填写密码。")
            return
        try:
            vip_level = int(vip_text)
            if vip_level < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("提示", "用户等级必须是大于等于 0 的整数。")
            return
        if self.selected_user_id is None:
            saved = self.app.client_db.create_user(username, password, vip_level)
            success_message = "用户已添加。"
        else:
            saved = self.app.client_db.update_user(
                self.selected_user_id, username, password, vip_level
            )
            success_message = "用户信息已更新。"
        if not saved:
            messagebox.showerror("提示", "该用户名已存在。")
            return
        self.refresh_users()
        messagebox.showinfo("提示", success_message)