"""Web 控制台的状态持久化：账号 cookie、页面设置、定时任务、历史记录。

底层是 SQLite（<数据目录>/database.sqlite，见 db.py），对外仍保持原来的接口：
    store.get()          -> 状态的深拷贝快照
    store.update(fn)     -> 在锁内修改状态并落盘
这样 web_server / web_jobs / web_scheduler 无需改动。

存储布局：
- account / download_account / settings / schedules 各占 settings 表一行（键名 web.<名>，JSON）；
- 历史任务逐条存 job_history 表，保存时只写有变化的行（原先每次整体重写 JSON 文件）。

旧版状态文件 web_state.json 会在首次启动时一次性导入，成功后改名为 web_state.json.bak。
config.yaml 是只读挂载、面向 cron 批处理；Web 端需要可写状态，因此另用独立的数据目录。
"""

import copy
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from db import Database

logger = logging.getLogger(__name__)

DB_FILENAME = "database.sqlite"
_KEYS = ("account", "download_account", "settings", "schedules")  # 各占 settings 表一行
_PREFIX = "web."
_IMPORT_MARK = "meta.web_json_imported"


def default_data_dir() -> Path:
    """数据目录：WEB_DATA_DIR 环境变量 > Docker 下 /app/data > 项目根下 data/。"""
    env = os.environ.get("WEB_DATA_DIR")
    if env:
        return Path(env)
    if os.path.exists("/.dockerenv"):
        return Path("/app/data")
    return Path(__file__).resolve().parent.parent / "data"


def _default_state() -> Dict[str, Any]:
    return {
        "account": None,  # {cookies, uid, nickname, avatar, vip_type}
        # 借用账号：仅用于下载（取下载链接/详情/歌词）。结构同 account；必须是黑胶 VIP。权限同 account（库文件 600）
        "download_account": None,
        # 所有设置项为 None 表示"沿用 config.yaml（若存在）或内置默认值"，页面保存后即以页面为准
        "settings": {
            "bark_api": None,        # 字符串（含空串=关闭）
            "use_library": True,     # 总开关；音乐库类型选"不使用"等价于关闭
            "quality": None,         # VIP 账号的下载音质
            "request_delay": None,   # 每首歌之间的间隔秒数
            # 音乐库：None=沿用 config.yaml；否则 {"type": "none|navidrome|music-tag-web",
            #   "navidrome": {host,user,password}, "mysql": {host,port,user,password,database}}
            "library": None,
            # 下载账号来源："self"=用当前登录账号（默认）；"borrowed"=用借用账号
            "download_source": "self",
        },
        "schedules": [],
        "history": [],
    }


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """把读到的原始数据并入默认状态：未知键丢弃，settings 做键级合并（兼容后续新增的设置项）。"""
    state = _default_state()
    state.update({k: data[k] for k in state if k in data})
    merged = _default_state()["settings"]
    if isinstance(data.get("settings"), dict):
        merged.update(data["settings"])
    state["settings"] = merged
    return state


class WebStore:
    def __init__(self, path: Path):
        """path 为旧版状态文件路径（data/web_state.json）：用于定位数据目录与一次性导入。"""
        self.path = Path(path)
        self.db = Database(self.path.parent / DB_FILENAME)
        self.db.migrate()
        self._lock = self.db.lock  # 与数据库共用同一把可重入锁
        self._state = _default_state()
        self._persisted: Dict[str, str] = {}   # 已落盘的各键 JSON，用于只写有变化的键
        self._hist: Dict[str, Tuple[int, str]] = {}  # 历史任务 id -> (seq, JSON)
        self._import_legacy_json()
        self._load()

    # ------------------------------------------------------------ 读取
    def _load(self) -> None:
        raw: Dict[str, Any] = {}
        with self._lock:
            for row in self.db.conn.execute("SELECT key, value FROM settings WHERE key LIKE 'web.%'"):
                name = row["key"][len(_PREFIX):]
                if name in _KEYS:
                    try:
                        raw[name] = json.loads(row["value"])
                    except json.JSONDecodeError:
                        logger.error(f"设置项 {row['key']} 不是合法 JSON，已忽略")
            history: List[Any] = []
            self._hist = {}
            for row in self.db.conn.execute("SELECT id, seq, data FROM job_history ORDER BY seq"):
                try:
                    history.append(json.loads(row["data"]))
                except json.JSONDecodeError:
                    logger.error(f"历史任务 {row['id']} 不是合法 JSON，已忽略")
                    continue
                self._hist[row["id"]] = (row["seq"], row["data"])
            raw["history"] = history
        self._state = _normalize(raw)
        # 以实际落盘内容为基准，后续只写差异
        self._persisted = {k: _dumps(self._state[k]) for k in _KEYS if k in raw}

    # ------------------------------------------------------------ 写入
    def _save(self) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            for key in _KEYS:
                js = _dumps(self._state[key])
                if self._persisted.get(key) != js:
                    conn.execute(
                        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                        (_PREFIX + key, js, now),
                    )
            new_hist = self._save_history(conn, now)
        # 事务提交后才更新内存中的"已落盘"基准
        self._persisted = {k: _dumps(self._state[k]) for k in _KEYS}
        self._hist = new_hist

    def _save_history(self, conn, now: str) -> Dict[str, Tuple[int, str]]:
        items = [it for it in self._state.get("history") or [] if isinstance(it, dict) and it.get("id")]
        ids = [str(it["id"]) for it in items]
        # 已有行的 seq 必须随列表顺序递增；否则（或 id 重复）整体重排
        seqs = [self._hist[i][0] for i in ids if i in self._hist]
        ordered = seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and len(set(ids)) == len(ids)
        next_seq = max([s for s, _ in self._hist.values()], default=0) + 1
        new: Dict[str, Tuple[int, str]] = {}
        for pos, item in enumerate(items):
            jid = str(item["id"])
            js = _dumps(item)
            if not ordered:
                seq = pos + 1
            elif jid in self._hist:
                seq = self._hist[jid][0]
            else:
                seq, next_seq = next_seq, next_seq + 1
            new[jid] = (seq, js)
        if not ordered:
            conn.execute("DELETE FROM job_history")
            old: Dict[str, Tuple[int, str]] = {}
        else:
            old = self._hist
            for gone in set(old) - set(new):
                conn.execute("DELETE FROM job_history WHERE id=?", (gone,))
        for jid, (seq, js) in new.items():
            if old.get(jid) != (seq, js):
                conn.execute(
                    "INSERT INTO job_history(id, seq, data, updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, data=excluded.data, "
                    "updated_at=excluded.updated_at",
                    (jid, seq, js, now),
                )
        return new

    def get(self) -> Dict[str, Any]:
        """返回状态的深拷贝快照（调用方随意改动不影响存储）。"""
        with self._lock:
            return copy.deepcopy(self._state)

    def update(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        """在锁内修改状态并落盘；返回 fn 的返回值。"""
        with self._lock:
            result = fn(self._state)
            self._save()
            return result

    # ------------------------------------------------------------ 旧版 JSON 一次性导入
    def _import_legacy_json(self) -> None:
        """首次启动：把 web_state.json 导入 SQLite，成功后改名为 .bak（原文件即备份）。

        只在"库里还没有任何 web 状态且从未导入过"时执行，保证不会用旧文件覆盖新数据；
        旧文件损坏则不动它、也不标记已导入（留给用户排查）。
        """
        if not self.path.exists():
            return
        with self._lock:
            conn = self.db.conn
            if conn.execute("SELECT 1 FROM settings WHERE key=?", (_IMPORT_MARK,)).fetchone():
                return
            has_rows = conn.execute("SELECT 1 FROM settings WHERE key LIKE 'web.%'").fetchone() \
                or conn.execute("SELECT 1 FROM job_history LIMIT 1").fetchone()
            if has_rows:
                logger.warning(f"数据库中已有状态，不再导入旧状态文件 {self.path}（如需重新导入请先备份并清空数据库）")
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"读取旧状态文件失败，未导入（文件保持原样）: {e}")
                return
            if not isinstance(data, dict):
                logger.error("旧状态文件格式不是对象，未导入（文件保持原样）")
                return

            state = _normalize(data)
            now = datetime.now().isoformat(timespec="seconds")
            history = [it for it in state["history"] if isinstance(it, dict) and it.get("id")]
            with self.db.transaction() as c:
                for key in _KEYS:
                    c.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)",
                              (_PREFIX + key, _dumps(state[key]), now))
                seen = set()
                for seq, item in enumerate(history, start=1):
                    jid = str(item["id"])
                    if jid in seen:
                        continue
                    seen.add(jid)
                    c.execute("INSERT INTO job_history(id, seq, data, updated_at) VALUES(?,?,?,?)",
                              (jid, seq, _dumps(item), now))
                c.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)",
                          (_IMPORT_MARK, _dumps({"from": self.path.name, "at": now,
                                                 "history": len(seen)}), now))
            logger.info(f"已从 {self.path.name} 导入状态：{len(seen)} 条历史任务")

            bak = self._backup_name()
            try:
                os.replace(self.path, bak)
                logger.info(f"旧状态文件已改名为 {bak.name}（可作为备份保留或自行删除）")
            except OSError as e:
                logger.warning(f"旧状态文件改名失败（已导入，不会重复导入）: {e}")

    def _backup_name(self) -> Path:
        bak = self.path.with_name(self.path.name + ".bak")
        if bak.exists():
            bak = self.path.with_name(f"{self.path.name}.{datetime.now():%Y%m%d%H%M%S}.bak")
        return bak
