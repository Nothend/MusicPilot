"""推荐联动的定时器：每 20 秒检查一次，到点就通过 LbService 启动对应任务。

与现有「自动下载」定时器同一风格（容器 TZ 本地时间、错过的时间点不补跑），区别只在触发规则：
- 每日同步  daily_time（每天）       → 只处理 Daily Jams
- 每周同步  weekly_weekday + weekly_time → 处理 Weekly Exploration / Weekly Jams
- 转正检查  每 promote_interval_min 分钟（需开启「自动转正」）
- 红心同步  每 love_interval_min 分钟（需开启红心同步）
- 过期清理  cleanup_time（每天，需开启「自动清理」）
总开关关闭时什么都不触发。触发时若已有任务在运行：跳过并记录，不排队、不重试。
设置每次检查时重新读取，页面上改了立刻生效。
"""

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from lb_service import LbService, NotReady, LABELS
from settings_store import SettingsStore
from task_manager import TaskBusy

logger = logging.getLogger(__name__)

TICK = 20
STATE_KEY = "lb.schedule_state"       # {任务名: 上次触发的时间戳键}，防止重启后在同一分钟重复触发


class LbScheduler:
    def __init__(self, service: LbService, kv: SettingsStore, now: Callable[[], datetime] = datetime.now):
        self.svc, self.kv, self.now = service, kv, now
        self._stop = threading.Event()
        started = now()
        self._interval_last: Dict[str, datetime] = {"promote": started, "love": started}   # 启动后先等满一个间隔，不立刻跑

    # ------------------------------------------------------------ 规则
    def due(self, s: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
        """此刻到期的任务（纯判断，不触发）：[{name, kind, types, key}]。"""
        if not s["enabled"]:
            return []
        sc, hhmm = s["schedule"], now.strftime("%H:%M")
        day = now.strftime("%Y-%m-%d ") + hhmm
        out: List[Dict[str, Any]] = []
        if hhmm == sc["daily_time"] and s["strategy"]["daily"]["enabled"]:
            out.append({"name": "daily_sync", "kind": "sync", "types": ["daily"], "key": day})
        if now.weekday() == sc["weekly_weekday"] and hhmm == sc["weekly_time"]:
            types = [t for t in ("weekly_exploration", "weekly_jams") if s["strategy"][t]["enabled"]]
            if types:
                out.append({"name": "weekly_sync", "kind": "sync", "types": types, "key": day})
        if hhmm == sc["cleanup_time"] and s["discovery"]["auto_cleanup"]:
            out.append({"name": "cleanup", "kind": "cleanup", "types": None, "key": day})
        if s["discovery"]["auto_promote"] and self._interval_due("promote", sc["promote_interval_min"], now):
            out.append({"name": "promote", "kind": "promote", "types": None, "key": "interval"})
        if s["love"]["enabled"] and self._interval_due("love", sc["love_interval_min"], now):
            out.append({"name": "love", "kind": "love", "types": None, "key": "interval"})
        return out

    def _interval_due(self, name: str, minutes: int, now: datetime) -> bool:
        return now - self._interval_last[name] >= timedelta(minutes=minutes)

    # ------------------------------------------------------------ 触发
    def tick(self) -> List[str]:
        """检查一次并触发到期的任务，返回本次真正启动的任务名（便于测试）。"""
        now = self.now()
        s = self.svc.settings()
        fired_state = self.kv.get(STATE_KEY, {}) or {}
        started: List[str] = []
        for t in self.due(s, now):
            if t["key"] != "interval":
                if fired_state.get(t["name"]) == t["key"]:
                    continue                                  # 这一分钟已经触发过了（20 秒一次的检查会命中同一分钟多次）
                fired_state[t["name"]] = t["key"]
                self.kv.set(STATE_KEY, fired_state)
            else:
                self._interval_last[t["name"]] = now          # 间隔任务：不论本次是否启动成功，都从现在重新计时
            try:
                self.svc.start(t["kind"], trigger="schedule", types=t["types"])
                started.append(t["name"])
                logger.info(f"定时触发：{LABELS[t['kind']]}（{t['name']}，{now:%Y-%m-%d %H:%M}）")
            except TaskBusy as e:
                logger.warning(f"定时触发 {t['name']} 时已有任务在运行，本次跳过：{e}")
            except NotReady as e:
                logger.warning(f"定时触发 {t['name']} 被拒绝：{e}")
            except Exception as e:
                logger.error(f"定时触发 {t['name']} 失败: {e}", exc_info=True)
        return started

    # ------------------------------------------------------------ 展示用：下一次什么时候跑
    def upcoming(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or self.now()
        s = self.svc.settings()
        if not s["enabled"]:
            return []
        sc = s["schedule"]
        out: List[Dict[str, Any]] = []

        def next_daily(hhmm: str) -> datetime:
            h, m = map(int, hhmm.split(":"))
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return t if t > now else t + timedelta(days=1)

        if s["strategy"]["daily"]["enabled"]:
            out.append({"task": "每日同步（Daily Jams）", "next": next_daily(sc["daily_time"])})
        if any(s["strategy"][t]["enabled"] for t in ("weekly_exploration", "weekly_jams")):
            t = next_daily(sc["weekly_time"])
            while t.weekday() != sc["weekly_weekday"]:
                t += timedelta(days=1)
            out.append({"task": "每周同步（Weekly）", "next": t})
        if s["discovery"]["auto_cleanup"]:
            out.append({"task": "过期清理", "next": next_daily(sc["cleanup_time"])})
        if s["discovery"]["auto_promote"]:
            out.append({"task": "转正检查", "next": max(now, self._interval_last["promote"] + timedelta(minutes=sc["promote_interval_min"]))})
        if s["love"]["enabled"]:
            out.append({"task": "红心同步", "next": max(now, self._interval_last["love"] + timedelta(minutes=sc["love_interval_min"]))})
        return [{"task": o["task"], "next": o["next"].strftime("%Y-%m-%d %H:%M")} for o in sorted(out, key=lambda x: x["next"])]

    # ------------------------------------------------------------ 线程
    def start(self) -> None:
        threading.Thread(target=self._loop, name="lb-scheduler", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception as e:
                logger.error(f"推荐联动定时器异常: {e}", exc_info=True)

    def stop(self) -> None:
        self._stop.set()
