import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.credentials import hash_password, is_hashed, verify_password


class ClientUserDB:
    def __init__(self, db_name=None):
        self.db_name = db_name or str(Path(__file__).with_name("client_users.db"))
        self.create_table()
        self.migrate_plaintext_passwords()
        self.add_default_user()

    def _get_conn(self):
        return sqlite3.connect(self.db_name)

    def create_table(self):
        with self._get_conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS client_users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                vip_level INTEGER NOT NULL DEFAULT 0
            )""")

    def add_default_user(self):
        with self._get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO client_users "
                "(username, password, vip_level) VALUES (?, ?, ?)",
                ("client001", hash_password("123456"), 1),
            )

    def migrate_plaintext_passwords(self):
        """把历史明文口令升级为 PBKDF2 散列。"""
        with self._get_conn() as conn:
            rows = conn.execute("SELECT id, password FROM client_users").fetchall()
            pending = [(hash_password(stored), user_id) for user_id, stored in rows if not is_hashed(stored)]
            if pending:
                conn.executemany("UPDATE client_users SET password=? WHERE id=?", pending)
        return len(pending)

    def authenticate(self, username, password):
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT username, password, vip_level FROM client_users WHERE username=?",
                (username,),
            ).fetchone()
        if row is None or not verify_password(password, row[1]):
            return None
        return {"username": row[0], "vip_level": row[2]}

    def list_users(self):
        """对外只返回可展示字段，口令散列不离开数据层。"""
        with self._get_conn() as conn:
            return conn.execute(
                "SELECT id, username, vip_level FROM client_users ORDER BY id"
            ).fetchall()

    def update_user(self, user_id, username, password, vip_level):
        try:
            with self._get_conn() as conn:
                if password:
                    conn.execute(
                        "UPDATE client_users SET username=?, password=?, vip_level=? "
                        "WHERE id=?",
                        (username, hash_password(password), vip_level, user_id),
                    )
                else:
                    # 留空表示不修改口令，避免把空串写进库里。
                    conn.execute(
                        "UPDATE client_users SET username=?, vip_level=? WHERE id=?",
                        (username, vip_level, user_id),
                    )
            return True
        except sqlite3.IntegrityError:
            return False

    def create_user(self, username, password, vip_level):
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO client_users(username, password, vip_level) "
                    "VALUES (?, ?, ?)",
                    (username, hash_password(password), vip_level),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def delete_user(self, user_id):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM client_users WHERE id=?", (user_id,))