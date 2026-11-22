"""红心同步：Navidrome 里标了红心的歌 → ListenBrainz 的 love（recording feedback）。

会真实写入 ListenBrainz（影响推荐），所以：
- 默认关闭（settings.love.enabled），由调用方检查；有只读的 dry_run 预览；每轮有上限；
- **只增不减**：Navidrome 取消红心不会取消 LB 上的 love；
- 已在 LB 上 love 过的、或我们提交过的，不再重复提交；
- 只提交有把握的 recording MBID，来源按可信度：
    item      推荐歌单自带的 recording MBID（歌单条目已关联到这首 Navidrome 歌）——最可靠
    navidrome Navidrome 标签里的 MBID（musicBrainzId）
    lookup    ListenBrainz 元数据查询（按歌手+歌名）；结果要再用匹配器核对歌手/歌名，对不上就丢弃，
              宁可少同步，也不把红心点到别的录音上
    manual    用户在页面上手动指定的 MBID（永久有效，优先级最高）；用户也可把某首标记为「忽略」
  解析不出 MBID 的跳过并计数（PRD：没有 MBID 就不同步），并把清单交给页面让用户人工处理；
- 单首失败只记录、下次重试；Token 失效（401/403）则整轮终止。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import lb_repo
from db import Database
from lb_client import LBError
from matcher import LocalFile, LocalIndex, Target, artist_key, match, title_key
from task_manager import RunHandle

logger = logging.getLogger(__name__)

NEGATIVE_CACHE_DAYS = 30        # 查不到 MBID 的结果缓存多久再重查（找到的永久缓存）
# ListenBrainz 的名字查询很慢（实测单次数秒、偶发超时），而同步任务持有全局锁：
# 每轮最多做这么多次「新的」查询（缓存命中不算），其余留到下一轮；结果有缓存，几轮后自然全部解析完
LOOKUPS_PER_RUN = 100
LOG_UNRESOLVED_MAX = 15         # 日志里最多列出多少首解析不出的歌


@dataclass
class LoveDeps:
    db: Database
    settings: Dict[str, Any]
    lb: Any                      # ListenBrainzClient
    nd: Any                      # NavidromeClient
    handle: RunHandle
    dry_run: bool = False
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] = lambda s: None
    plan: List[Dict[str, Any]] = field(default_factory=list)      # dry_run 时填充：将要提交的歌
    unresolved: List[Dict[str, Any]] = field(default_factory=list)  # 解析不出的歌（供页面人工处理）：{id,artist,title,reason,cand}
    lookup_budget: int = LOOKUPS_PER_RUN
    lookups_done: int = field(default=0, init=False)


class LookupBudgetExhausted(Exception):
    pass


def resolve_mbid(d: LoveDeps, song: Dict[str, Any], by_item: Dict[str, str]) -> Optional[Dict[str, str]]:
    """返回 {mbid, source}；解析不出（或不可信）返回 None。"""
    if song["id"] in by_item:
        return {"mbid": by_item[song["id"]].lower(), "source": "item"}
    if song["mbid"]:
        return {"mbid": song["mbid"].lower(), "source": "navidrome"}

    na, nt = artist_key(song["artist"]), title_key(song["title"])
    if not na or not nt:
        return None
    cached = lb_repo.cache_get_mbid(d.db, na, nt)
    if cached is not None:
        if cached["ignored"]:                                # 用户手动标记「忽略」：不提交、不再查询
            return {"mbid": "", "source": "ignored"}
        if cached["manual"] and cached["mbid"]:              # 用户手动指定：永久有效，优先于自动结果
            return {"mbid": cached["mbid"].lower(), "source": "manual"}
        if cached["found"]:
            return {"mbid": cached["mbid"], "source": "lookup"}
        looked = datetime.fromisoformat(cached["looked_up_at"])
        if d.now() - looked < timedelta(days=NEGATIVE_CACHE_DAYS):
            return None

    if d.lookups_done >= d.lookup_budget:
        raise LookupBudgetExhausted()
    d.lookups_done += 1
    r = d.lb.lookup(song["artist"], song["title"])
    ok = False
    if r and r.get("recording_mbid"):
        # 核对：LB 返回的歌手/歌名必须与这首歌吻合（含繁简、版本词、多歌手），否则不信
        probe = LocalIndex([LocalFile(nd_song_id=song["id"], library="main", artist=song["artist"], title=song["title"])])
        ok = match(probe, Target(artist=r.get("artist", ""), title=r.get("title", ""))).status == "main"
    reason = "" if ok else ("mismatch" if r else "no_result")
    cand = "" if ok or not r else f"{r.get('artist', '')} - {r.get('title', '')}"
    lb_repo.cache_put_mbid(d.db, na, nt, r["recording_mbid"].lower() if ok else "", ok, d.now(), reason=reason, cand=cand)
    return {"mbid": r["recording_mbid"].lower(), "source": "lookup"} if ok else None


def run_love_sync(d: LoveDeps) -> Dict[str, Any]:
    s, h, db = d.settings, d.handle, d.db
    lbs = s["listenbrainz"]
    if not lbs["username"] or not lbs["token"]:
        raise ValueError("请先在设置里填写 ListenBrainz 用户名和 Token")
    cap = s["love"]["max_per_run"]

    starred = d.nd.starred_songs(None)                       # 所有音乐库里的红心
    by_item = lb_repo.item_mbids_by_nd_id(db)
    recorded = lb_repo.love_records(db)
    try:
        on_lb = {m.lower() for m in d.lb.loved_mbids()}      # LB 上已有的 love
    except LBError as e:
        raise ValueError(f"读取 ListenBrainz 已有红心失败：{e}")
    h.log(f"Navidrome 红心 {len(starred)} 首；ListenBrainz 上已有 {len(on_lb)} 个 love；{'预览模式，不会提交' if d.dry_run else f'本轮最多提交 {cap} 首'}")

    todo: List[Dict[str, Any]] = []
    seen: set = set()
    unresolved: List[str] = []
    for song in starred:
        h.add("starred")
        try:
            found = resolve_mbid(d, song, by_item)
        except LookupBudgetExhausted:
            h.add("lookup_deferred")                         # 本轮查询额度用完：这首留到下一轮
            continue
        except LBError as e:
            if e.status in (401, 403):
                raise ValueError(f"ListenBrainz Token 无效或权限不足：{e}")
            h.add("lookup_errors")                           # 这一首查询失败（网络/限流）：不缓存，下次再试
            h.log(f"MBID 查询失败，跳过（{song['artist']} - {song['title']}）：{e}", "WARNING")
            continue
        if found and found["source"] == "ignored":
            h.add("ignored")
            continue
        if not found:
            h.add("unresolved")
            unresolved.append(f"{song['artist']} - {song['title']}")
            row = lb_repo.cache_get_mbid(d.db, artist_key(song["artist"]), title_key(song["title"]))
            d.unresolved.append({"id": song["id"], "artist": song["artist"], "title": song["title"],
                                 "reason": (row["reason"] if row is not None else "") or "unknown",
                                 "cand": row["cand"] if row is not None else ""})
            continue
        h.add(f"via_{found['source']}")
        mbid = found["mbid"]
        if mbid in seen:                                     # 两首红心歌对应同一个录音：只处理一次
            continue
        seen.add(mbid)
        if mbid in on_lb:
            if recorded.get(mbid) not in ("submitted", "already") and not d.dry_run:
                lb_repo.record_love(db, mbid, nd_song_id=song["id"], artist=song["artist"], title=song["title"],
                                    source=found["source"], status="already", now=d.now())
            h.add("already")
            continue
        if recorded.get(mbid) == "submitted":                # 我们提交过，但 LB 的列表里还没看到（缓存延迟）：不重复提交
            h.add("already")
            continue
        todo.append({"mbid": mbid, "song": song, "source": found["source"]})

    if h.stats.get("lookup_deferred"):
        h.log(f"本轮名字查询已达上限 {d.lookup_budget} 次，另有 {h.stats['lookup_deferred']} 首留到下一轮（结果有缓存，不会重复查询）")
    if unresolved:
        shown = "；".join(unresolved[:LOG_UNRESOLVED_MAX])
        h.log(f"有 {len(unresolved)} 首解析不出可信的 MBID，已跳过：{shown}" + ("…" if len(unresolved) > LOG_UNRESOLVED_MAX else ""), "WARNING")

    batch, deferred = todo[:cap], max(0, len(todo) - cap)
    if deferred:
        h.add("deferred", deferred)
        h.log(f"待提交 {len(todo)} 首，超出本轮上限 {cap}，其余 {deferred} 首留待下次")
    if d.dry_run:
        d.plan = [{"artist": t["song"]["artist"], "title": t["song"]["title"], "mbid": t["mbid"], "source": t["source"]}
                  for t in batch]
        h.add("would_submit", len(batch))
        h.log(f"预览：本轮将提交 {len(batch)} 首（未实际提交）")
        return h.stats

    for t in batch:
        song = t["song"]
        try:
            d.lb.love(t["mbid"])
        except LBError as e:
            if e.status in (401, 403):                        # Token 失效：继续也只会全部失败
                h.log(f"ListenBrainz 拒绝了请求（{e}），终止本轮", "ERROR")
                raise ValueError(f"ListenBrainz Token 无效或权限不足：{e}")
            h.add("failed")
            h.log(f"提交失败（{song['artist']} - {song['title']}）：{e}", "WARNING")
            lb_repo.record_love(db, t["mbid"], nd_song_id=song["id"], artist=song["artist"], title=song["title"],
                                source=t["source"], status="failed", error=str(e), now=d.now())
            continue
        lb_repo.record_love(db, t["mbid"], nd_song_id=song["id"], artist=song["artist"], title=song["title"],
                            source=t["source"], status="submitted", now=d.now())
        h.add("submitted")
        h.log(f"已提交红心：{song['artist']} - {song['title']}（{t['source']}）")
        d.sleep(0.2)
    return h.stats
