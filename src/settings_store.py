"""通用键值设置存储：settings 表（key → JSON），与 WebStore 共用同一个数据库。

WebStore 管理 `web.*` 键；推荐功能使用 `lb.*` 键（见 lb_settings.py）。
"""

import json
from datetime import datetime
from typing import Any

from db import Database


class SettingsStore:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        with self.db.lock:
            row = self.db.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def set(self, key: str, value: Any) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True), now),
            )
