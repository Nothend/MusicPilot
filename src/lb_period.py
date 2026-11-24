"""推荐列表的「当前周期」：每日 = 今天；每周 = 本周（周一起）；音乐平台每日推荐 = 平台刷新后的今天。

推荐页只展示当前周期内的歌单：取不到就显示空列表并说明原因，不拿前期的歌单顶替。
日期一律是 `YYYY-MM-DD` 字符串（与 `recommendation_batches.lb_date` 同格式），可直接比较大小。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from lb_settings import NETEASE_DAILY, REC_LABELS

WEEKLY = ("weekly_exploration", "weekly_jams")
NETEASE_ROLLOVER_HOURS = 6          # 平台每天约 6 点刷新，6 点前仍算前一天
FETCH_STATUS_KEY = "lb.fetch_status"


def period_start(rec_type: str, now: datetime) -> str:
    """该类型「当前周期」的起始日期。"""
    if rec_type in WEEKLY:
        return (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    if rec_type == NETEASE_DAILY:
        return (now - timedelta(hours=NETEASE_ROLLOVER_HOURS)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def is_current(rec_type: str, date: str, now: datetime) -> bool:
    return bool(date) and date[:10] >= period_start(rec_type, now)


def period_word(rec_type: str) -> str:
    return "本周" if rec_type in WEEKLY else "今天"


def record_status(kv: Any, rec_type: str, state: str, now: datetime, latest: str = "") -> None:
    """记录某个列表最近一次获取的结果（ok | stale | none | empty），供页面在列表为空时解释原因。"""
    if kv is None:
        return
    cur = kv.get(FETCH_STATUS_KEY) or {}
    cur[rec_type] = {"state": state, "at": now.isoformat(timespec="seconds"), "latest": latest}
    kv.set(FETCH_STATUS_KEY, cur)


def empty_reason(rec_type: str, status: Optional[Dict[str, Any]], source: str) -> str:
    """列表为空时给用户看的说明。"""
    label, word = REC_LABELS.get(rec_type, rec_type), period_word(rec_type)
    if not status:
        return f"还没有获取过{label}。点右上角的「获取推荐」，或等设置里的定时时间。"
    when = (status.get("at") or "").replace("T", " ")[5:16]
    if source == "netease":
        return (f"{when} 获取时音乐平台没有返回今天的每日推荐。可能是账号状态异常或平台尚未刷新（每天约 6 点），稍后再点「获取推荐」。")
    latest = status.get("latest") or ""
    if status.get("state") == "stale":
        return (f"ListenBrainz {word}还没有生成{label}（{when} 检查过，最近一份是 {latest}，已不是{word}的，所以不显示）。"
                "常见原因：LB 服务器偶尔漏生成或延迟；近期听歌记录太少，不足以生成。"
                "可到 ListenBrainz 个人主页的「为你推荐」确认；一旦生成，下次同步会自动取到。")
    if status.get("state") == "none":
        return (f"ListenBrainz 上还没有任何{label}歌单（{when} 检查过）。可能是听歌记录不足或服务端尚未为你生成。")
    if status.get("state") == "empty":
        return f"ListenBrainz 已生成{label}（{latest}），但歌单是空的（{when} 检查过），可能稍后会补全。"
    return f"{when} 检查过，{word}的{label}暂无。"
