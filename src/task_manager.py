"""任务管理：全局互斥 + 运行记录 + 日志。

所有会改库的推荐联动任务（同步、转正、清理、索引、红心同步）和现有的下载任务都应共用一把锁，
避免并发调用网易云下载器、或同步时被清理误删刚下载的歌。
- 已有任务在跑时再触发：抛 TaskBusy（页面提示「任务正在运行」），不排队、不创建第二个；
- 每次运行在 task_runs 留一行（开始/结束/状态/统计），日志同时写 task_logs 与文件 <log_dir>/lb-YYYYMMDD.log；
- 启动时把上次遗留的 running 标为 interrupted。
"""

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from db import Database

logger = logging.getLogger(__name__)


class TaskBusy(RuntimeError):
    def __init__(self, running: Dict[str, Any]):
        super().__init__(f"任务正在运行：{running.get('type', '')}（开始于 {running.get('started_at', '')}）")
        self.running = running


class RunHandle:
    """一次运行的句柄：写日志、累计统计、设置最终状态。"""

    def __init__(self, tm: "TaskManager", run_id: int, rtype: str):
        self._tm = tm
        self.id = run_id
        self.type = rtype
        self.stats: Dict[str, Any] = {}
        self.status = "done"       # done | paused | failed（异常时自动置为 failed）
        self.summary = ""

    def log(self, msg: str, level: str = "INFO") -> None:
        self._tm._write_log(self.id, level, msg)

    def add(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n


class TaskManager:
    def __init__(self, db: Database, log_dir: Optional[Path] = None):
        self.db = db
        self.log_dir = Path(log_dir) if log_dir else None
        self._lock = threading.Lock()
        self._current: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------ 状态
    def current(self) -> Optional[Dict[str, Any]]:
        return dict(self._current) if self._current else None

    def recover(self) -> int:
        """启动时调用：把上次遗留的 running 标为 interrupted。返回数量。"""
        ts = datetime.now().isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE task_runs SET status='interrupted', ended_at=?, summary='进程被中断' WHERE status='running'", (ts,))
            return cur.rowcount

    # ------------------------------------------------------------ 运行
    def begin(self, rtype: str, trigger: str = "manual") -> RunHandle:
        """非阻塞取锁并创建运行记录；已有任务在跑则抛 TaskBusy（不排队）。必须配对调用 end()。"""
        if not self._lock.acquire(blocking=False):
            raise TaskBusy(self._current or {})
        try:
            ts = datetime.now().isoformat(timespec="seconds")
            with self.db.transaction() as conn:
                run_id = conn.execute("INSERT INTO task_runs(type, trigger, started_at, status) VALUES(?,?,?, 'running')",
                                      (rtype, trigger, ts)).lastrowid
            self._current = {"id": run_id, "type": rtype, "trigger": trigger, "started_at": ts}
            handle = RunHandle(self, run_id, rtype)
            handle.log(f"任务开始：{rtype}（{'手动' if trigger == 'manual' else '定时'}触发）")
            return handle
        except BaseException:
            self._current = None
            self._lock.release()
            raise

    def end(self, handle: RunHandle, exc: Optional[BaseException] = None) -> None:
        """结束运行：记录状态与统计并释放锁。exc 不为空表示任务异常终止。"""
        try:
            if exc is not None:
                handle.status = "failed"
                handle.summary = f"{type(exc).__name__}: {exc}"
                handle.log(f"任务异常终止：{handle.summary}", "ERROR")
            handle.log(f"任务结束：{handle.status}" + (f"（{handle.summary}）" if handle.summary else ""))
            self._finish(handle)
        finally:
            self._current = None
            self._lock.release()

    @contextmanager
    def run(self, rtype: str, trigger: str = "manual") -> Iterator[RunHandle]:
        handle = self.begin(rtype, trigger)
        try:
            yield handle
        except BaseException as e:
            self.end(handle, e)
            raise
        else:
            self.end(handle)

    def run_async(self, rtype: str, trigger: str, fn: Callable[[RunHandle], None],
                  on_done: Optional[Callable[[RunHandle, Optional[BaseException]], None]] = None) -> int:
        """在后台线程里执行 fn(handle)。锁在调用线程里同步获取：已有任务在跑则立刻抛 TaskBusy（页面据此提示）。
        返回运行 id。on_done(handle, 异常或 None) 在任务结束、锁释放之后调用（用于通知等）。"""
        handle = self.begin(rtype, trigger)

        def work() -> None:
            err: Optional[BaseException] = None
            try:
                fn(handle)
            except BaseException as e:          # 任何异常都要落库并释放锁
                err = e
            self.end(handle, err)
            if on_done:
                try:
                    on_done(handle, err)
                except Exception as e:
                    logger.error(f"任务结束回调失败: {e}")

        threading.Thread(target=work, name=f"task-{rtype}-{handle.id}", daemon=True).start()
        return handle.id

    @contextmanager
    def hold(self, label: str) -> Iterator[None]:
        """阻塞等待并持有全局锁（给「现有网易云下载队列」用：它自带排队，轮到它时等推荐任务跑完再开始）。
        不产生 task_runs 记录；持有期间 current() 会显示 label，页面据此提示「有任务在运行」。"""
        self._lock.acquire()
        self._current = {"id": 0, "type": label, "trigger": "queue", "started_at": datetime.now().isoformat(timespec="seconds")}
        try:
            yield
        finally:
            self._current = None
            self._lock.release()

    def _finish(self, h: RunHandle) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE task_runs SET ended_at=?, status=?, stats_json=?, summary=? WHERE id=?",
                         (datetime.now().isoformat(timespec="seconds"), h.status,
                          json.dumps(h.stats, ensure_ascii=False, sort_keys=True), h.summary, h.id))

    # ------------------------------------------------------------ 日志
    def _write_log(self, run_id: int, level: str, msg: str) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        try:
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO task_logs(run_id, ts, level, msg) VALUES(?,?,?,?)", (run_id, ts, level, msg))
        except Exception as e:      # 日志失败不应拖垮任务
            logger.error(f"写任务日志失败: {e}")
        getattr(logger, level.lower() if level.lower() in ("debug", "info", "warning", "error") else "info")(f"[任务 {run_id}] {msg}")
        if self.log_dir:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                with open(self.log_dir / f"lb-{datetime.now():%Y%m%d}.log", "a", encoding="utf-8") as f:
                    f.write(f"{ts} [{level}] [任务 {run_id}] {msg}\n")
            except OSError:
                pass

    # ------------------------------------------------------------ 查询（供页面）
    def recent_runs(self, limit: int = 30):
        with self.db.lock:
            rows = self.db.conn.execute("SELECT * FROM task_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def logs(self, run_id: int, after_id: int = 0, limit: int = 500):
        with self.db.lock:
            rows = self.db.conn.execute("SELECT id, ts, level, msg FROM task_logs WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                                        (run_id, after_id, limit)).fetchall()
        return [dict(r) for r in rows]
