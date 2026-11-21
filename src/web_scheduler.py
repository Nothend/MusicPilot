"""Web 控制台的进程内定时器：每天固定 HH:MM（可多个）触发下载任务。

不依赖 cron / 第三方库：后台线程每 TICK 秒检查一次，同一时间点只触发一次（用 last_fire 去重）。
容器停机期间错过的时间点不补跑。时区取容器的 TZ（datetime.now 本地时间）。
"""

import logging
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List

from web_common import normalize_source, resolve_subdir, source_label
from web_jobs import JobManager
from web_store import WebStore

logger = logging.getLogger(__name__)

TICK = 20  # 秒
MAX_SCHEDULES = 50
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_times(times: Any) -> List[str]:
    """校验并规范化时间点列表（'8:05' -> '08:05'，去重排序）。"""
    if isinstance(times, str):
        times = [t for t in re.split(r"[,\s，]+", times) if t]
    if not isinstance(times, list) or not times:
        raise ValueError("请至少设置一个执行时间（HH:MM）")
    out = set()
    for t in times:
        m = _TIME_RE.match(str(t).strip())
        if not m:
            raise ValueError(f"时间格式不正确：{t}（应为 HH:MM，如 21:30）")
        out.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if len(out) > 24:
        raise ValueError("单个定时任务最多 24 个时间点")
    return sorted(out)


class Scheduler:
    def __init__(self, store: WebStore, jobs: JobManager,
                 now: Callable[[], datetime] = datetime.now):
        self.store = store
        self.jobs = jobs
        self.now = now
        self._stop = threading.Event()

    # ------------------------------------------------------------ CRUD
    def list(self) -> List[Dict[str, Any]]:
        return self.store.get()["schedules"]

    def upsert(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """新增或更新（payload 带 id 为更新）。校验失败抛 ValueError。"""
        source = normalize_source(payload.get("source") or {})
        subdir = str(payload.get("subdir") or "").strip().strip("/")
        resolve_subdir(self.jobs.base_dir, subdir)
        times = normalize_times(payload.get("times"))
        name = str(payload.get("name") or "").strip()[:40] or source_label(source)
        enabled = bool(payload.get("enabled", True))
        sid = payload.get("id")

        def apply(state):
            items = state["schedules"]
            if sid:
                for item in items:
                    if item["id"] == sid:
                        item.update(name=name, source=source, subdir=subdir, times=times, enabled=enabled)
                        return dict(item)
                raise ValueError("定时任务不存在")
            if len(items) >= MAX_SCHEDULES:
                raise ValueError(f"最多 {MAX_SCHEDULES} 个定时任务")
            item = {"id": uuid.uuid4().hex[:8], "name": name, "source": source, "subdir": subdir,
                    "times": times, "enabled": enabled, "last_fire": "",
                    "last_status": "", "last_message": "", "last_finished": "",
                    "last_job_id": ""}
            items.append(item)
            return dict(item)

        return self.store.update(apply)

    def delete(self, sid: str) -> bool:
        def apply(state):
            before = len(state["schedules"])
            state["schedules"] = [s for s in state["schedules"] if s["id"] != sid]
            return len(state["schedules"]) != before
        return self.store.update(apply)

    def run_now(self, sid: str) -> Dict[str, Any]:
        for item in self.list():
            if item["id"] == sid:
                return self.jobs.submit(item["source"], item["subdir"], trigger="manual-schedule",
                                        schedule_id=sid)
        raise ValueError("定时任务不存在")

    def record_result(self, job: Dict[str, Any]) -> None:
        """任务结束回调：把结果写回对应的定时任务，供页面展示。"""
        sid = job.get("schedule_id")
        if not sid:
            return

        def apply(state):
            for item in state["schedules"]:
                if item["id"] == sid:
                    item["last_status"] = job["status"]
                    item["last_message"] = job.get("message", "")
                    item["last_finished"] = job.get("finished", "")
                    item["last_job_id"] = job.get("id", "")  # 页面据此跳到该次运行的逐首明细
        self.store.update(apply)

    # ------------------------------------------------------------ 触发
    def tick(self) -> int:
        """检查一次并提交到期的任务，返回本次触发数量（便于测试）。"""
        now = self.now()
        hhmm = now.strftime("%H:%M")
        fire_key = now.strftime("%Y-%m-%d ") + hhmm
        due = [s for s in self.list()
               if s.get("enabled") and hhmm in s.get("times", []) and s.get("last_fire") != fire_key]
        fired = 0
        for item in due:
            def mark(state, sid=item["id"]):
                for s in state["schedules"]:
                    if s["id"] == sid:
                        s["last_fire"] = fire_key
            self.store.update(mark)
            try:
                self.jobs.submit(item["source"], item["subdir"], trigger="schedule", schedule_id=item["id"])
                fired += 1
                logger.info(f"定时任务触发：{item['name']}（{fire_key}）")
            except Exception as e:
                logger.error(f"定时任务 {item['name']} 触发失败: {e}")
        return fired

    def start(self) -> None:
        threading.Thread(target=self._loop, name="web-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception as e:
                logger.error(f"定时器异常: {e}", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
