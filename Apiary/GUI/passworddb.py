import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.credentials import hash_password, is_hashed, verify_password


class UserDB:
    def __init__(self, db_name=None):
        self.db_name = db_name or str(Path(__file__).with_name("password.db"))
        self.create_table()
        self.migrate_plaintext_passwords()
        if not self.has_users():
            self.add_default_user()

    #获取数据库连接，复用代码
    def _get_conn(self):
        return sqlite3.connect(self.db_name)

    def has_users(self):
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        conn.close()
        return row is not None

    def create_table(self):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS users(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        password TEXT NOT NULL)''')
        conn.commit()
        conn.close()

    def add_default_user(self):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(username,password) VALUES (?,?)",
            ("admin", hash_password("123456")),
        )
        conn.commit()
        conn.close()

    def migrate_plaintext_passwords(self):
        """把历史明文口令升级为 PBKDF2 散列，避免旧库继续泄露明文。"""
        conn = self._get_conn()
        rows = conn.execute("SELECT id, password FROM users").fetchall()
        pending = [(hash_password(stored), user_id) for user_id, stored in rows if not is_hashed(stored)]
        if pending:
            conn.executemany("UPDATE users SET password=? WHERE id=?", pending)
            conn.commit()
        conn.close()
        return len(pending)

    #登录校验
    def verify_user(self, username, password):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("SELECT password FROM users WHERE username=?", (username,))
        row = cur.fetchone()
        if row is None:
            conn.close()
            return False

        stored = row[0]
        if not verify_password(password, stored):
            conn.close()
            return False

        # 校验通过后顺手升级历史明文口令。
        if not is_hashed(stored):
            cur.execute("UPDATE users SET password=? WHERE username=?", (hash_password(password), username))
            conn.commit()
        conn.close()
        return True


