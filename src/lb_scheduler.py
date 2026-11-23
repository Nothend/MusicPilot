"""推荐联动的定时器：每 20 秒检查一次，到点就通过 LbService 启动对应任务。

与现有「自动下载」定时器同一风格（容器 TZ 本地时间、错过的时间点不补跑），区别只在触发规则：
（以下同步任务只在对应的推荐来源被选中时才触发，见设置里的「推荐来源」）
- 每日同步  daily_time（每天）       → 来源=ListenBrainz：只处理每日熟悉（Daily Jams）
- 每周同步  weekly_weekday + weekly_time → 来源=ListenBrainz：处理 Weekly Exploration / Weekly Jams
- 每日推荐  netease_daily_time（每天）→ 来源=音乐平台：获取并下载今日每日推荐（该列表「启用」时）
- 转正检查  每 promote_interval_min 分钟（需开启「自动转正」）
- 红心同步  每 love_interval_min 分钟（需开启红心同步）
- 过期清理  cleanup_time（每天，需开启「自动清理」）
没有总开关：选了推荐来源就会按各列表的「启用」和定时自动运行（缺配置时被拒绝并记日志）。触发时若已有任务在运行（全局只跑一个任务）：不丢弃，每个检查周期重试，
时间点任务最多等 BUSY_WAIT_MIN 分钟，间隔任务一直等到能跑为止且不重置计时。
定时同步因网络类错误失败：RETRY_DELAY_MIN 分钟后自动重试，最多 MAX_RETRIES 次。
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
BUSY_WAIT_MIN = 10          # 时间点任务撞上别的任务时，最多等多久
RETRY_DELAY_MIN = 10        # 定时同步失败后多久重试
MAX_RETRIES = 3
STATE_KEY = "lb.schedule_state"       # {任务名: 上次触发的时间戳键}，防止重启后在同一分钟重复触发


class LbScheduler:
    def __init__(self, service: LbService, kv: SettingsStore, now: Callable[[], datetime] = datetime.now):
        self.svc, self.kv, self.now = service, kv, now
        self._stop = threading.Event()
        started = now()
        self._interval_last: Dict[str, datetime] = {"promote": started, "love": started}   # 启动后先等满一个间隔，不立刻跑
        # 待重试的任务：{name: {task, due, deadline, attempt}}；撞上忙碌 / 网络失败后放这里
        self._pending: Dict[str, Dict[str, Any]] = {}
        self.svc.on_failed = self._on_failed

    # ------------------------------------------------------------ 规则
    def due(self, s: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
        """此刻到期的任务（纯判断，不触发）：[{name, kind, types, key}]。"""
        sc, hhmm = s["schedule"], now.strftime("%H:%M")
        day = now.strftime("%Y-%m-%d ") + hhmm
        out: List[Dict[str, Any]] = []
        lb_src = s["source"] == "listenbrainz"
        if not lb_src and hhmm == sc["netease_daily_time"] and s["strategy"]["netease_daily"]["enabled"]:
            out.append({"name": "netease_daily_sync", "kind": "sync", "types": ["netease_daily"], "key": day})
        if lb_src and hhmm == sc["daily_time"] and s["strategy"]["daily"]["enabled"]:
            out.append({"name": "daily_sync", "kind": "sync", "types": ["daily"], "key": day})
        if lb_src and now.weekday() == sc["weekly_weekday"] and hhmm == sc["weekly_time"]:
            types = [t for t in ("weekly_exploration", "weekly_jams") if s["strategy"][t]["enabled"]]
            if types:
                out.append({"name": "weekly_sync", "kind": "sync", "types": types, "key": day})
        nd_mode = s["mode"] == "navidrome"            # 仅下载模式没有清理 / 转正 / 红心同步
        if nd_mode and hhmm == sc["cleanup_time"] and s["discovery"]["auto_cleanup"]:
            out.append({"name": "cleanup", "kind": "cleanup", "types": None, "key": day})
        if nd_mode and s["discovery"]["auto_promote"] and self._interval_due("promote", sc["promote_interval_min"], now):
            out.append({"name": "promote", "kind": "promote", "types": None, "key": "interval"})
        if nd_mode and s["love"]["enabled"] and self._interval_due("love", sc["love_interval_min"], now):
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
        todo: List[Dict[str, Any]] = []
        due_now = self.due(s, now)
        for t in due_now:
            if t["key"] != "interval":
                if fired_state.get(t["name"]) == t["key"]:
                    continue                                  # 这一分钟已经触发过了（20 秒一次的检查会命中同一分钟多次）
                fired_state[t["name"]] = t["key"]
                self.kv.set(STATE_KEY, fired_state)
            elif t["name"] in self._pending:
                continue
            todo.append({"task": t, "attempt": 1, "deadline": None})
        due_names = {t["name"] for t in due_now}
        for name, p in list(self._pending.items()):           # 待重试的：到时间了就再试一次
            if p["task"]["key"] == "interval" and name not in due_names:
                del self._pending[name]                       # 期间被关掉了开关：不再重试
                continue
            if p["due"] <= now and not any(x["task"]["name"] == name for x in todo):
                todo.append(p)
        # 红心同步排在转正前面：转正几乎瞬间完成，但它先拿到全局锁时会把同刻到期的红心同步挡掉
        todo.sort(key=lambda x: 0 if x["task"]["name"] == "love" else 1)
        for p in todo:
            t, attempt = p["task"], p["attempt"]
            name = t["name"]
            try:
                self.svc.start(t["kind"], trigger="schedule", types=t["types"], attempt=attempt)
            except TaskBusy as e:
                self._on_busy(t, p, now, e)
                continue
            except NotReady as e:
                logger.warning(f"定时触发 {name} 被拒绝：{e}")
            except Exception as e:
                logger.error(f"定时触发 {name} 失败: {e}", exc_info=True)
            else:
                started.append(name)
                logger.info(f"定时触发：{LABELS[t['kind']]}（{name}，{now:%Y-%m-%d %H:%M}"
                            f"{'，第 %d 次重试' % (attempt - 1) if attempt > 1 else ''}）")
            self._pending.pop(name, None)
            if t["key"] == "interval":
                self._interval_last[name] = now               # 间隔任务：真正尝试过（成功/被拒/出错）才重新计时
        return started

    def _on_busy(self, t: Dict[str, Any], p: Dict[str, Any], now: datetime, e: Exception) -> None:
        name = t["name"]
        if t["key"] == "interval":
            if name not in self._pending:
                logger.info(f"定时触发 {name} 时已有任务在运行，稍后再试：{e}")
            self._pending[name] = {"task": t, "attempt": p["attempt"], "due": now, "deadline": None}
            return
        deadline = p.get("deadline") or now + timedelta(minutes=BUSY_WAIT_MIN)
        if now >= deadline:
            logger.warning(f"定时触发 {name} 等了 {BUSY_WAIT_MIN} 分钟仍有任务在运行，放弃本次：{e}")
            self._pending.pop(name, None)
            return
        if name not in self._pending:
            logger.info(f"定时触发 {name} 时已有任务在运行，稍后再试：{e}")
        self._pending[name] = {"task": t, "attempt": p["attempt"], "due": now + timedelta(seconds=TICK), "deadline": deadline}

    def _on_failed(self, kind: str, types: Optional[List[str]], attempt: int) -> bool:
        """定时任务因网络类错误失败（由 LbService 回调）：安排稍后重试；已达上限返回 False（由调用方发通知）。"""
        if attempt > MAX_RETRIES:
            return False
        name = "retry_" + kind + "_" + "_".join(types or [])
        now = self.now()
        self._pending[name] = {"task": {"name": name, "kind": kind, "types": types, "key": name},
                               "attempt": attempt + 1, "due": now + timedelta(minutes=RETRY_DELAY_MIN),
                               "deadline": now + timedelta(minutes=RETRY_DELAY_MIN + BUSY_WAIT_MIN)}
        return True

    # ------------------------------------------------------------ 展示用：下一次什么时候跑
    def upcoming(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or self.now()
        s = self.svc.settings()
        sc = s["schedule"]
        out: List[Dict[str, Any]] = []

        def next_daily(hhmm: str) -> datetime:
            h, m = map(int, hhmm.split(":"))
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return t if t > now else t + timedelta(days=1)

        lb_src = s["source"] == "listenbrainz"
        if not lb_src and s["strategy"]["netease_daily"]["enabled"]:
            out.append({"task": "每日推荐同步", "next": next_daily(sc["netease_daily_time"])})
        if lb_src and s["strategy"]["daily"]["enabled"]:
            out.append({"task": "每日同步（每日熟悉）", "next": next_daily(sc["daily_time"])})
        if lb_src and any(s["strategy"][t]["enabled"] for t in ("weekly_exploration", "weekly_jams")):
            t = next_daily(sc["weekly_time"])
            while t.weekday() != sc["weekly_weekday"]:
                t += timedelta(days=1)
            out.append({"task": "每周同步（Weekly）", "next": t})
        nd_mode = s["mode"] == "navidrome"
        if nd_mode and s["discovery"]["auto_cleanup"]:
            out.append({"task": "过期清理", "next": next_daily(sc["cleanup_time"])})
        if nd_mode and s["discovery"]["auto_promote"]:
            out.append({"task": "转正检查", "next": max(now, self._interval_last["promote"] + timedelta(minutes=sc["promote_interval_min"]))})
        if nd_mode and s["love"]["enabled"]:
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
