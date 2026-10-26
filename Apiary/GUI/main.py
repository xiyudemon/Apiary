import sys
from pathlib import Path

import tkinter as tk

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.backend import ApiaryBackend
from core.scheduler import AutoScheduler
from passworddb import UserDB
from clientuserdb import ClientUserDB
from GUI.loginpage import LoginPage
from GUI.mainpage import MainPage
from GUI.task_web import TaskWebServer
from tkinter import messagebox


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Apiary 蜂巢")
        self.geometry("750x520")
        self.minsize(720, 490)

        self.db = UserDB()
        self.client_db = ClientUserDB()
        self.backend = ApiaryBackend()
        self.scheduler = AutoScheduler(self.backend, interval=1.0)
        self._closing = False
        self.task_web = None
        self.web_status = "网页任务提交：http://本机局域网IP:3000（管理员登录后可提交）"
        try:
            self.task_web = TaskWebServer(self.backend, self.client_db)
            self.task_web.start()
        except OSError as exc:
            self.web_status = "网页任务提交服务启动失败（3000 端口不可用）"
            self.after(0, lambda error=str(exc): messagebox.showwarning("网页服务", f"{self.web_status}：{error}"))

        self.container = tk.Frame(self)
        self.container.pack(fill="both", expand=True)
        self.pages = {}

        self.pages["LoginPage"] = LoginPage(self.container, self)
        self.pages["MainPage"] = MainPage(self.container, self)
        self.show_page("LoginPage")

    def show_page(self, page_name):
        if page_name not in self.pages:
            print(f"页面 {page_name} 不存在。")
            return

        if page_name == "MainPage":
            self.scheduler.start()
            if self.task_web is not None:
                self.task_web.enabled.set()
            self.geometry("1180x800")
            self.minsize(1000, 720)
        else:
            if self.task_web is not None:
                self.task_web.enabled.clear()
            self.scheduler.stop()
            self.geometry("750x520")
            self.minsize(720, 490)

        for page in self.pages.values():
            page.pack_forget()
        self.pages[page_name].pack(fill="both", expand=True)

    def destroy(self):
        if self._closing:
            return
        self._closing = True
        main_page = self.pages.get("MainPage") if hasattr(self, "pages") else None
        if main_page is not None:
            request_page = getattr(main_page, "pages", {}).get("requests")
            if request_page is not None:
                request_page.close()
        if getattr(self, "task_web", None) is not None:
            self.task_web.close()
        if hasattr(self, "scheduler"):
            self.scheduler.stop()
        if hasattr(self, "backend") and hasattr(self.backend, "close"):
            self.backend.close()
        super().destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()