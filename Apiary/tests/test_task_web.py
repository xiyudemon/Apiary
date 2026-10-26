import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from GUI.clientuserdb import ClientUserDB
from GUI.task_web import TaskWebServer


class Backend:
    def __init__(self):
        self.requests = []

    def submit_user_request(self, text, **kwargs):
        self.requests.append((text, kwargs))
        if text == "拒绝":
            return {"status": "rejected", "reason": "invalid_target"}
        return {"status": "pending", "task_id": "web_test"}


@pytest.fixture
def service(tmp_path):
    users = ClientUserDB(str(tmp_path / "users.db"))
    users.create_user("web-user", "test-password", 3)
    backend = Backend()
    server = TaskWebServer(backend, users, host="127.0.0.1", port=0)
    server.start()
    server.enabled.set()
    yield server, backend, users
    server.close()


def post(server, data, content_type="application/json", origin=None):
    headers = {"Content-Type": content_type}
    if origin:
        headers["Origin"] = origin
    request = Request(
        f"http://127.0.0.1:{server.httpd.server_port}/api/tasks",
        data=json.dumps(data).encode(), headers=headers,
    )
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def payload(**changes):
    return {"username": "web-user", "password": "test-password", "task": "派两架无人机到 A 点", **changes}


def test_page_and_script_available(service):
    server, _, _ = service
    for path, text in (("/", "任务提交"), ("/submit.js", "/api/tasks")):
        with urlopen(f"http://127.0.0.1:{server.httpd.server_port}{path}", timeout=5) as response:
            assert response.status == 200
            assert text in response.read().decode()


def test_authenticated_task_uses_database_identity(service):
    server, backend, _ = service
    status, body = post(server, payload(requested_by="admin", user_level="管理员"))
    assert status == 201
    assert body["result"]["task_id"] == "web_test"
    assert backend.requests == [("派两架无人机到 A 点", {"requested_by": "web-user", "user_level": "VIP 3"})]


@pytest.mark.parametrize("changes", [{"password": "wrong"}, {"username": "missing"}])
def test_bad_credentials_do_not_submit(service, changes):
    server, backend, _ = service
    assert post(server, payload(**changes))[0] == 401
    assert backend.requests == []


def test_changed_and_deleted_accounts_take_effect(service):
    server, backend, users = service
    user_id = next(row[0] for row in users.list_users() if row[1] == "web-user")
    assert users.update_user(user_id, "web-user", "changed", 4)
    assert post(server, payload())[0] == 401
    assert post(server, payload(password="changed"))[0] == 201
    users.delete_user(user_id)
    assert post(server, payload(password="changed"))[0] == 401
    assert len(backend.requests) == 1


@pytest.mark.parametrize("data", [[], payload(task=" "), payload(task=42), payload(task="a" * 8001)])
def test_invalid_payload(service, data):
    server, backend, _ = service
    assert post(server, data)[0] == 400
    assert backend.requests == []


def test_paused_cross_origin_and_non_json(service):
    server, backend, _ = service
    assert post(server, payload(), origin="http://other.example")[0] == 403
    assert post(server, payload(), content_type="text/plain")[0] == 415
    server.enabled.clear()
    assert post(server, payload())[0] == 503
    assert backend.requests == []


def test_backend_rejection(service):
    server, _, _ = service
    assert post(server, payload(task="拒绝"))[0] == 422


def test_busy_requests_do_not_submit(service):
    server, backend, _ = service
    for _ in range(4):
        server._slots.acquire()
    try:
        assert post(server, payload())[0] == 429
    finally:
        for _ in range(4):
            server._slots.release()
    assert backend.requests == []
