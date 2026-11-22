"""红心同步里「解析不出可信 MBID」的歌：页面上人工处理（指定 MBID / 忽略 / 撤销）。

清单来自最近一次红心同步（或预览）存下的快照；处理结果写进 mbid_cache（manual/ignored 行，见 db v8），
之后每次同步都优先使用，永不过期。名字一律取自快照里的 Navidrome 歌曲，不信任页面传来的歌名。
"""

from datetime import datetime
from typing import Any, Dict, Optional

import lb_repo
from db import Database
from matcher import artist_key, title_key
from mb_client import MBID_RE
from settings_store import SettingsStore

KEY = "lb.love_unresolved"
REASON_ZH = {"no_result": "ListenBrainz 没查到这首歌", "mismatch": "查到了但歌手/歌名对不上",
             "unknown": "原因未记录（旧缓存，重新同步一次即可）"}


def view(db: Database, kv: SettingsStore) -> Dict[str, Any]:
    snap = kv.get(KEY) or {}
    items = [dict(i, reason_zh=REASON_ZH.get(i.get("reason", ""), REASON_ZH["unknown"])) for i in snap.get("items", [])]
    manual = lb_repo.cache_manual_rows(db)
    return {"at": snap.get("at"), "items": items, "manual": manual}


def resolve(db: Database, kv: SettingsStore, nd_id: str, *, mbid: str = "", ignore: bool = False,
            now: Optional[datetime] = None) -> Dict[str, Any]:
    snap = kv.get(KEY) or {}
    item = next((i for i in snap.get("items", []) if i["id"] == nd_id), None)
    if item is None:
        raise ValueError("这首歌已不在未解析清单里，请刷新页面")
    mbid = (mbid or "").strip().lower()
    if ignore:
        mbid = ""
    elif not MBID_RE.match(mbid):
        raise ValueError("MBID 格式不对（应为形如 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx 的 36 位编号）")
    na, nt = artist_key(item["artist"]), title_key(item["title"])
    lb_repo.cache_set_manual(db, na, nt, artist=item["artist"], title=item["title"], mbid=mbid, ignored=ignore, now=now)
    snap["items"] = [i for i in snap["items"] if (artist_key(i["artist"]), title_key(i["title"])) != (na, nt)]
    kv.set(KEY, snap)
    return {"ok": True, "remaining": len(snap["items"])}


def undo(db: Database, norm_artist: str, norm_title: str) -> Dict[str, Any]:
    if not lb_repo.cache_clear_manual(db, norm_artist, norm_title):
        raise ValueError("没有找到这条手动处理记录")
    return {"ok": True}
