"""把一个推荐批次发布成 Navidrome 歌单（按歌曲 id 建，不依赖磁盘路径）。

名称由设置里的模板决定（默认如「LB Weekly Jams 2026-09-14」），保留历史；同一批次重复发布时先删掉我们自己建的旧歌单再重建，
绝不碰不是我们创建的歌单。
"""

import logging
from datetime import datetime
from typing import Callable, List, Optional

import lb_repo
import lb_states as ST
from typing import Dict
from lb_settings import DEFAULT_PLAYLIST_NAMES
from nd_client import NavidromeError

logger = logging.getLogger(__name__)


def playlist_name(rec_type: str, lb_date: str, names: Optional[Dict[str, str]] = None) -> str:
    """names = 设置里的 playlist_names（每类一个模板，{date} 换成日期）；缺省用默认模板。"""
    tpl = (names or {}).get(rec_type) or DEFAULT_PLAYLIST_NAMES.get(rec_type) or f"LB {rec_type} {{date}}"
    return tpl.replace("{date}", (lb_date or "")[:10]).strip()


def apply_renames(db, nd, log: Callable[..., None]) -> int:
    """把 v9 迁移登记的"待改名"歌单在 Navidrome 里改名（失败只记日志，下次同步再试）。"""
    n = 0
    for row in lb_repo.pending_renames(db):
        try:
            nd.rename_playlist(row["nd_playlist_id"], row["rename_to"])
        except NavidromeError as e:
            log(f"歌单改名失败：{row['name']} → {row['rename_to']} — {e}", "WARNING")
            continue
        lb_repo.finish_rename(db, row["id"], row["rename_to"])
        log(f"歌单已改名（推荐歌单的日期按标题里的日期校正，一次性）：{row['name']} → {row['rename_to']}")
        n += 1
    return n


def publish_batch(db, nd, batch_id: int, log: Callable[..., None], now: Optional[datetime] = None,
                  names: Optional[Dict[str, str]] = None) -> Optional[str]:
    """返回新歌单的 Navidrome id；没有可放的歌或失败返回 None（失败只记日志，不影响任务）。"""
    with db.lock:
        batch = db.conn.execute("SELECT * FROM recommendation_batches WHERE id=?", (batch_id,)).fetchone()
    items = lb_repo.items_of_batch(db, batch_id)
    song_ids: List[str] = [i["local_file_id"] for i in items if i["state"] in ST.PLAYABLE and i["local_file_id"]]
    name = playlist_name(batch["type"], batch["lb_date"], names)

    # 先删我们自己建过的旧版本（重跑/补全后重建）
    for row in lb_repo.live_playlists_of_batch(db, batch_id):
        try:
            nd.delete_playlist(row["nd_playlist_id"])
        except NavidromeError as e:
            log(f"删除旧歌单失败（可能已被手动删除）：{e}", "WARNING")
        lb_repo.mark_playlist_deleted(db, row["id"], now)

    if not song_ids:
        log(f"歌单 {name}：没有可放入的歌曲，不创建")
        return None
    try:
        pid = nd.create_playlist(name, song_ids)
    except NavidromeError as e:
        log(f"创建歌单失败：{name} — {e}", "ERROR")
        return None
    lb_repo.record_playlist(db, batch_id, name, pid, len(song_ids), now)
    log(f"歌单已创建：{name}（{len(song_ids)} 首）")
    return pid
