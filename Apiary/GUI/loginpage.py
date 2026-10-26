import tkinter as tk
from tkinter import messagebox

class LoginPage(tk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent)
        self.app = app
        self.db = app.db

        title_label = tk.Label(self, text="Apiary 蜂巢", font=("SimHei",34), fg="#0088ee")
        title_label.pack(pady=30)

        login_title = tk.Label(self, text="系统登录", font=("SimHei",32))
        login_title.pack(pady=10)

        #账户输入框
        self.entry_account = tk.Entry(self, font=("SimHei",28), justify="center")
        self.entry_account.pack(pady=15, ipadx=80, ipady=8)
        self.account_tip = "账户"
        self.entry_account.insert(0,self.account_tip)
        self.entry_account.bind("<FocusIn>",self.clear_account_tip)
        self.entry_account.bind("<FocusOut>",self.reset_account_tip)

        #密码输入框
        self.entry_password = tk.Entry(self, font=("SimHei",28), justify="center", show="*")
        self.entry_password.pack(pady=15, ipadx=80, ipady=8)
        self.pwd_tip = "密码"
        self.entry_password.insert(0,self.pwd_tip)
        self.entry_password.bind("<FocusIn>",self.clear_pwd_tip)
        self.entry_password.bind("<FocusOut>",self.reset_pwd_tip)
        self._hide_placeholder_password()

        btn_ok = tk.Button(self, text="登录", font=("SimHei",30), command=self.login)
        btn_ok.pack(pady=25)

    def clear_account_tip(self,event):
        if self.entry_account.get() == self.account_tip:
            self.entry_account.delete(0,tk.END)

    def reset_account_tip(self,event):
        if self.entry_account.get() == "":
            self.entry_account.insert(0,self.account_tip)

    def _hide_placeholder_password(self):
        """占位提示不是真实口令，显示时必须还原成明文提示文字。"""
        self.entry_password.config(show="")

    def clear_pwd_tip(self,event):
        if self.entry_password.get() == self.pwd_tip:
            self.entry_password.delete(0,tk.END)
            self.entry_password.config(show="*")

    def reset_pwd_tip(self,event):
        if self.entry_password.get() == "":
            self.entry_password.delete(0,tk.END)
            self.entry_password.insert(0,self.pwd_tip)
            self.entry_password.config(show="")

    def login(self):
        user_input = self.entry_account.get().strip()
        pwd_input = self.entry_password.get().strip()
        #过滤占位提示
        if user_input == self.account_tip:
            user_input = ""
        if pwd_input == self.pwd_tip:
            pwd_input = ""

        if not user_input or not pwd_input:
            messagebox.showwarning("提示", "账户和密码不能为空。")
            return
        if self.db.verify_user(user_input, pwd_input):
            messagebox.showinfo("提示", "登录成功。")
            self.app.show_page("MainPage")
        else:
            messagebox.showerror("提示", "账户或者密码错误。")

