from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_COLUMNS = (
    "task_id",
    "raw_text",
    "action",
    "priority",
    "target",
    "constraints",
    "status",
    "task_type",
    "from_location",
    "to_location",
    "return_to_base",
    "is_special",
    "requested_by",
    "user_level",
    "required_drones",
    "all_drones",
    "execution",
)

_OPTIONAL_COLUMNS = {
    "execution": "TEXT",
    "task_type": "TEXT",
    "from_location": "TEXT",
    "to_location": "TEXT",
    "return_to_base": "INTEGER NOT NULL DEFAULT 0",
    "is_special": "INTEGER NOT NULL DEFAULT 0",
    "requested_by": "TEXT",
    "user_level": "TEXT",
    "required_drones": "INTEGER NOT NULL DEFAULT 1",
    "all_drones": "INTEGER NOT NULL DEFAULT 0",
}


class TaskDB:
    """任务持久化层。

    复用同一条连接（配合 ``check_same_thread=False``），既支持 ``:memory:`` 等临时库，
    也避免每次读写都重新打开文件句柄。
    """

    def __init__(self, db_name: Optional[str] = None):
        self.db_name = db_name or str(Path(__file__).resolve().parent.parent / "task_data.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            # 后台线程逐 tick 写入，降低 fsync 强度并启用 WAL，避免每次提交都阻塞 GUI。
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    raw_text TEXT NOT NULL,
                    action TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    target TEXT,
                    constraints TEXT,
                    status TEXT NOT NULL,
                    task_type TEXT,
                    from_location TEXT,
                    to_location TEXT,
                    return_to_base INTEGER NOT NULL DEFAULT 0,
                    is_special INTEGER NOT NULL DEFAULT 0,
                    requested_by TEXT,
                    user_level TEXT,
                    required_drones INTEGER NOT NULL DEFAULT 1,
                    all_drones INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                )
                """
            )
            self._migrate_missing_columns()
            self._conn.commit()

    def _migrate_missing_columns(self):
        """为旧版本数据库补齐后加入的列，避免用户升级后丢失任务。"""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, definition in _OPTIONAL_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def reset(self):
        with self._lock:
            self._conn.execute("DELETE FROM tasks")
            self._conn.commit()

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT setting_value FROM settings WHERE setting_key=?", (key,)
            ).fetchone()
        return default if row is None else str(row["setting_value"])

    def set_setting(self, key: str, value: object) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings(setting_key, setting_value) VALUES (?, ?)",
                (key, str(value)),
            )
            self._conn.commit()

    def get_bool_setting(self, key: str, default: bool = False) -> bool:
        value = self.get_setting(key)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def delete_tasks_except(self, task_ids: set[str]) -> None:
        with self._lock:
            if task_ids:
                placeholders = ", ".join("?" for _ in task_ids)
                self._conn.execute(
                    f"DELETE FROM tasks WHERE task_id NOT IN ({placeholders})",
                    tuple(task_ids),
                )
            else:
                self._conn.execute("DELETE FROM tasks")
            self._conn.commit()

    def add_or_update_task(self, task: Dict[str, Any], *, commit: bool = True) -> None:
        """写入或更新一条任务。

        ``commit=False`` 用于逐 tick 的批量同步：调用方最后一次写入时再提交，
        把 N 次磁盘同步合并成 1 次（GUI 中原本每次写库都会阻塞主线程）。
        """
        with self._lock:
            self._conn.execute(
                f"""
                INSERT OR REPLACE INTO tasks({", ".join(_COLUMNS)})
                VALUES ({", ".join("?" for _ in _COLUMNS)})
                """,
                (
                    task["task_id"],
                    task["raw_text"],
                    task.get("action", "new_task"),
                    task.get("priority", "normal"),
                    task.get("target"),
                    json.dumps(task.get("constraints", []), ensure_ascii=False),
                    task.get("status", "pending"),
                    task.get("task_type", "recon"),
                    task.get("from_location"),
                    task.get("to_location"),
                    1 if task.get("return_to_base", False) else 0,
                    1 if task.get("is_special", False) else 0,
                    task.get("requested_by"),
                    task.get("user_level"),
                    int(task.get("required_drones") or 1),
                    1 if task.get("all_drones", False) else 0,
                    json.dumps({key: task.get(key) for key in ("frozen_drone_count", "completed_children", "child_statuses", "child_origins")}),
                ),
            )
            if commit:
                self._conn.commit()

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def flush_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """一次性写入多条任务，只提交一次事务。"""
        with self._lock:
            for task in tasks:
                self.add_or_update_task(task, commit=False)
            self._conn.commit()

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM tasks ORDER BY task_id"
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            constraints = row["constraints"]
            result.append(
                {
                    **json.loads(row["execution"] or "{}"),
                    "task_id": row["task_id"],
                    "raw_text": row["raw_text"],
                    "action": row["action"],
                    "priority": row["priority"],
                    "target": row["target"],
                    "constraints": json.loads(constraints) if constraints else [],
                    "status": row["status"],
                    "task_type": row["task_type"] or "recon",
                    "from_location": row["from_location"],
                    "to_location": row["to_location"],
                    "return_to_base": bool(row["return_to_base"]),
                    "is_special": bool(row["is_special"]),
                    "requested_by": row["requested_by"],
                    "user_level": row["user_level"],
                    "required_drones": max(1, int(row["required_drones"] or 1)),
                    "all_drones": bool(row["all_drones"]),
                }
            )
        return result

    def update_priority(self, task_id: str, priority: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET priority=? WHERE task_id=?",
                (priority, task_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def update_status(self, task_id: str, status: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET status=? WHERE task_id=?",
                (status, task_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0
