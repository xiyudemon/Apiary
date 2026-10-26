from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class TaskWebServer:
    """Share the desktop backend; HTTP workers never access Tk widgets."""

    def __init__(self, backend, users, host="0.0.0.0", port=3000):
        self.backend = backend
        self.users = users
        self.enabled = threading.Event()
        self._slots = threading.BoundedSemaphore(4)
        self._thread = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                pass

            def setup(self):
                super().setup()
                self.connection.settimeout(10)

            def reply(self, status, payload, content_type="application/json; charset=utf-8"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                files = {"/": ("task_submit.html", "text/html; charset=utf-8"),
                         "/submit.js": ("task_submit.js", "text/javascript; charset=utf-8")}
                if self.path not in files:
                    self.reply(404, {"message": "页面不存在"})
                    return
                name, content_type = files[self.path]
                self.reply(200, Path(__file__).with_name(name).read_bytes(), content_type)

            def do_POST(self):
                if self.path != "/api/tasks":
                    self.reply(404, {"message": "接口不存在"})
                    return
                # JSON-only requests prevent cross-origin HTML form submissions.
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    self.reply(415, {"message": "仅接受 JSON 请求"})
                    return
                origin = self.headers.get("Origin")
                if origin and origin != "http://" + self.headers.get("Host", ""):
                    self.reply(403, {"message": "不允许跨站提交"})
                    return
                if not owner.enabled.is_set():
                    self.reply(503, {"message": "管理员尚未登录或服务已暂停"})
                    return
                if not owner._slots.acquire(blocking=False):
                    self.reply(429, {"message": "服务繁忙，请稍后重试"})
                    return
                try:
                    try:
                        size = int(self.headers.get("Content-Length", "0"))
                        if not 0 < size <= 32768:
                            raise ValueError()
                        data = json.loads(self.rfile.read(size).decode("utf-8"))
                        if not isinstance(data, dict):
                            raise ValueError()
                        username, password, task = (data.get(k) for k in ("username", "password", "task"))
                        if not all(isinstance(v, str) for v in (username, password, task)):
                            raise ValueError()
                        if not username.strip() or not password or not task.strip() or len(task) > 8000:
                            raise ValueError()
                    except (ValueError, UnicodeError):
                        self.reply(400, {"message": "请输入账号、密码和任务（任务最多 8000 字）"})
                        return
                    user = owner.users.authenticate(username.strip(), password)
                    if user is None:
                        self.reply(401, {"message": "账号或密码错误"})
                        return
                    if not owner.enabled.is_set():
                        self.reply(503, {"message": "服务已暂停"})
                        return
                    result = owner.backend.submit_user_request(
                        task.strip(), requested_by=user["username"],
                        user_level=f"VIP {user['vip_level']}",
                    )
                    rejected = result.get("status") == "rejected"
                    self.reply(422 if rejected else 201, {
                        "message": "任务未受理" if rejected else "任务已提交到任务池",
                        "result": result,
                    })
                except Exception:
                    self.reply(500, {"message": "任务提交失败，请联系管理员确认后再重试"})
                finally:
                    owner._slots.release()

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.block_on_close = False

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="apiary-web", daemon=True)
        self._thread.start()

    def close(self):
        self.enabled.clear()
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=2)
        self.httpd.server_close()
