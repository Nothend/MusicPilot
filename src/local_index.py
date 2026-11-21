"""本地曲库索引：把 Navidrome 两个音乐库（主库/试听库）的歌曲同步进 SQLite `local_files`。

只读 Navidrome，不碰文件系统。索引只用于「某首歌是否已有」的判断；Navidrome API 返回的
path 不是真实磁盘路径（见 nd_client.py），因此不用它定位文件。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List

from db import Database
from matcher import LocalFile, LocalIndex

logger = logging.getLogger(__name__)

MAIN_DROP_GUARD = 0.5   # 主库索引一次缩水超过这个比例（且基数较大）：疑似 Navidrome 异常，不删除旧记录
GUARD_MIN_ROWS = 100


def _row(song: Dict[str, Any], library: str, now: str) -> tuple:
    return (song["id"], library, song["artist"], song["title"], song["album"], song["album_artist"],
            song["duration"], song["size"], song["suffix"], song["mbid"], 1 if song["starred"] else 0,
            song["path_api"], now)


def sync_index(db: Database, client, main_folder_id: int, discovery_folder_id: int) -> Dict[str, Any]:
    """拉取两个库并更新索引。拉取阶段任何异常都会直接抛出，此时库里的旧索引保持不变。

    返回统计：{main:{total,added,removed}, discovery:{...}, skipped_delete:[...]}。
    """
    if main_folder_id is None or discovery_folder_id is None:
        raise ValueError("请先在设置里选择 Navidrome 的主库和试听库")
    fetched: Dict[str, List[Dict[str, Any]]] = {}
    for lib, fid in (("main", main_folder_id), ("discovery", discovery_folder_id)):
        fetched[lib] = list(client.iter_songs(fid))     # 先全部取到内存，再一次事务写入

    now = datetime.now().isoformat(timespec="seconds")
    stats: Dict[str, Any] = {"skipped_delete": []}
    with db.transaction() as conn:
        old = {r["nd_song_id"]: r["library"] for r in conn.execute("SELECT nd_song_id, library FROM local_files")}
        # 一首歌若同时出现在两个库（id 相同），以主库为准
        merged: Dict[str, tuple] = {}
        for lib in ("discovery", "main"):
            for s in fetched[lib]:
                merged[s["id"]] = _row(s, lib, now)
        for lib in ("main", "discovery"):
            old_ids = {i for i, l in old.items() if l == lib}
            new_ids = {i for i, r in merged.items() if r[1] == lib}
            removed = old_ids - new_ids
            # 移动到另一个库的不算删除（id 不变，下面的 upsert 会改 library）
            removed = {i for i in removed if i not in merged}
            if (lib == "main" and len(old_ids) >= GUARD_MIN_ROWS
                    and len(new_ids) < len(old_ids) * (1 - MAIN_DROP_GUARD)):
                logger.warning(f"主库索引从 {len(old_ids)} 首骤降到 {len(new_ids)} 首，疑似 Navidrome 异常，本次不删除旧记录")
                stats["skipped_delete"].append(lib)
                removed = set()
            for i in removed:
                conn.execute("DELETE FROM local_files WHERE nd_song_id=?", (i,))
            stats[lib] = {"total": len(new_ids), "added": len(new_ids - set(old)), "removed": len(removed)}
        conn.executemany(
            "INSERT INTO local_files(nd_song_id, library, artist, title, album, album_artist, duration, size, "
            "suffix, mbid, starred, path_api, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(nd_song_id) DO UPDATE SET library=excluded.library, artist=excluded.artist, "
            "title=excluded.title, album=excluded.album, album_artist=excluded.album_artist, "
            "duration=excluded.duration, size=excluded.size, suffix=excluded.suffix, mbid=excluded.mbid, "
            "starred=excluded.starred, path_api=excluded.path_api, indexed_at=excluded.indexed_at",
            list(merged.values()))
    logger.info(f"本地索引已同步：主库 {stats['main']['total']} 首，试听库 {stats['discovery']['total']} 首")
    return stats


def load_index(db: Database) -> LocalIndex:
    with db.lock:
        rows = db.conn.execute(
            "SELECT nd_song_id, library, artist, title, album, album_artist, duration, size, mbid, starred "
            "FROM local_files").fetchall()
    return LocalIndex([LocalFile(nd_song_id=r["nd_song_id"], library=r["library"], artist=r["artist"],
                                 title=r["title"], album=r["album"], album_artist=r["album_artist"],
                                 duration=r["duration"], size=r["size"], mbid=r["mbid"],
                                 starred=bool(r["starred"])) for r in rows])
