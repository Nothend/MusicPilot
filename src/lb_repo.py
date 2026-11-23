"""推荐联动的数据访问层（SQLite）。

约定：
- 每次状态变化都在**同一个事务**里更新条目并写一条 item_events，所以无论何时被中断，
  库里的状态与历史都是自洽的；
- 状态转换必须符合 lb_states.TRANSITIONS，否则抛 InvalidTransition（写错状态会立刻暴露，而不是悄悄入库）；
- 时间一律是本地时区的 ISO 字符串（容器 TZ）。
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import lb_states as ST
from db import Database
from lb_client import batch_date
from matcher import Target

# set_state 允许一并更新的字段（白名单，防止拼写错误悄悄写进不存在的列）
_FIELDS = {"local_file_id", "local_path", "starred", "keep", "entered_discovery_at",
           "retry_count", "next_retry_at", "reason_kind", "reason"}


def now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 歌曲与批次
def _upsert_track(conn: sqlite3.Connection, t: Dict[str, Any], now: str) -> int:
    mbid = (t.get("recording_mbid") or "").lower()
    psid = str(t.get("platform_song_id") or "")
    names = json.dumps([a.get("name", "") for a in t.get("artists", [])], ensure_ascii=False)
    if mbid:
        row = conn.execute("SELECT id FROM tracks WHERE recording_mbid=?", (mbid,)).fetchone()
        if row:
            return row["id"]
    else:   # 没有 MBID：按「歌手+歌名」归并
        row = conn.execute("SELECT id FROM tracks WHERE recording_mbid='' AND artist=? AND title=?",
                           (t["artist"], t["title"])).fetchone()
        if row:
            if psid:        # 归并到老记录时补上来源给的平台歌曲 id（已有的不改）
                conn.execute("UPDATE tracks SET platform_song_id=? WHERE id=? AND platform_song_id=''", (psid, row["id"]))
            return row["id"]
    cur = conn.execute(
        "INSERT INTO tracks(recording_mbid, artist, title, album, duration_ms, artists_json, platform_song_id, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (mbid, t["artist"], t["title"], t.get("album") or "", t.get("duration_ms"), names, psid, now))
    return cur.lastrowid


def batch_known(db: Database, playlist_mbid: str) -> bool:
    with db.lock:
        return db.conn.execute("SELECT 1 FROM recommendation_batches WHERE lb_playlist_mbid=?",
                               (playlist_mbid,)).fetchone() is not None


def create_batch(db: Database, rec_type: str, header: Dict[str, Any], playlist: Dict[str, Any],
                 now: Optional[datetime] = None) -> int:
    """入库一份新的推荐歌单：批次 + 歌曲实体 + 全部条目（PENDING）。一个事务，要么全有要么全无。"""
    ts = now_iso(now)
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO recommendation_batches(type, lb_playlist_mbid, lb_title, lb_date, fetched_at, status, total) "
            "VALUES(?,?,?,?,?, 'active', ?)",
            (rec_type, header["mbid"], header.get("title", ""),
             batch_date(header.get("title", ""), header.get("date", "")), ts, len(playlist["tracks"])))
        batch_id = cur.lastrowid
        for pos, tr in enumerate(playlist["tracks"], start=1):
            tid = _upsert_track(conn, tr, ts)
            conn.execute(
                "INSERT INTO recommendation_items(batch_id, track_id, position, state, state_changed_at, created_at) "
                "VALUES(?,?,?,?,?,?)", (batch_id, tid, pos, ST.PENDING, ts, ts))
    return batch_id


def active_batches(db: Database) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute("SELECT * FROM recommendation_batches WHERE status='active' ORDER BY id").fetchall()


def finish_batch(db: Database, batch_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE recommendation_batches SET status='done' WHERE id=?", (batch_id,))


# ---------------------------------------------------------------- 条目
_ITEM_SELECT = (
    "SELECT i.*, t.recording_mbid, t.artist, t.title, t.album, t.duration_ms, t.artists_json, t.platform_song_id, "
    "b.type AS batch_type, b.lb_title, b.lb_date "
    "FROM recommendation_items i JOIN tracks t ON t.id=i.track_id JOIN recommendation_batches b ON b.id=i.batch_id ")


def items_of_batch(db: Database, batch_id: int) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute(_ITEM_SELECT + "WHERE i.batch_id=? ORDER BY i.position", (batch_id,)).fetchall()


def get_item(db: Database, item_id: int) -> Optional[sqlite3.Row]:
    with db.lock:
        return db.conn.execute(_ITEM_SELECT + "WHERE i.id=?", (item_id,)).fetchone()


def set_state(db: Database, item_id: int, to: str, *, note: str = "", now: Optional[datetime] = None,
              **fields: Any) -> None:
    """原子地转换状态（含合法性校验）并记录事件；fields 里可同时更新白名单内的其它列。"""
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"set_state 不允许更新这些字段: {sorted(bad)}")
    ts = now_iso(now)
    with db.transaction() as conn:
        row = conn.execute("SELECT state FROM recommendation_items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"条目 {item_id} 不存在")
        cur = row["state"]
        if not ST.can_transition(cur, to):
            raise ST.InvalidTransition(f"条目 {item_id}：不允许 {cur} → {to}")
        sets = ["state=?", "state_changed_at=?"] + [f"{k}=?" for k in fields]
        conn.execute(f"UPDATE recommendation_items SET {', '.join(sets)} WHERE id=?",
                     [to, ts, *fields.values(), item_id])
        conn.execute("INSERT INTO item_events(item_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
                     (item_id, cur, to, ts, note))


def update_fields(db: Database, item_id: int, **fields: Any) -> None:
    """不改状态，只更新白名单字段（如补上扫描后才有的 Navidrome id）。"""
    bad = set(fields) - _FIELDS
    if bad or not fields:
        raise ValueError(f"update_fields 字段不合法: {sorted(bad)}")
    with db.transaction() as conn:
        conn.execute(f"UPDATE recommendation_items SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                     [*fields.values(), item_id])


def save_candidates(db: Database, item_id: int, files: Iterable[Any]) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM match_candidates WHERE item_id=?", (item_id,))
        conn.executemany("INSERT INTO match_candidates(item_id, nd_song_id, library) VALUES(?,?,?)",
                         [(item_id, f.nd_song_id, f.library) for f in files])


def record_download(db: Database, item_id: int, attempt: int, *, ok: bool, platform_id: Optional[int] = None,
                    error_kind: str = "", error: str = "", path: str = "", quality: str = "",
                    file_type: str = "", now: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO download_records(item_id, attempt, platform_id, ok, error_kind, error, path, quality, file_type, at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (item_id, attempt, platform_id, 1 if ok else 0, error_kind, error, path, quality, file_type, now_iso(now)))


def download_attempts(db: Database, item_id: int) -> int:
    with db.lock:
        return db.conn.execute("SELECT COUNT(*) FROM download_records WHERE item_id=?", (item_id,)).fetchone()[0]


def last_item_for_track(db: Database, track_id: int, exclude_item_id: int, states: Iterable[str]) -> Optional[sqlite3.Row]:
    """同一首歌在其它（更早的）条目里、处于给定状态的最近一条。"""
    states = tuple(states)
    with db.lock:
        return db.conn.execute(
            f"SELECT * FROM recommendation_items WHERE track_id=? AND id<>? AND state IN ({','.join('?' * len(states))}) "
            "ORDER BY id DESC LIMIT 1", (track_id, exclude_item_id, *states)).fetchone()


def item_with_nd_id(db: Database, nd_song_id: str, exclude_item_id: int) -> Optional[sqlite3.Row]:
    """已关联到同一个 Navidrome 歌曲、且知道真实磁盘路径或入库时间的最近一条（用于继承 local_path/entered_discovery_at）。"""
    with db.lock:
        return db.conn.execute(
            "SELECT * FROM recommendation_items WHERE local_file_id=? AND id<>? AND "
            "(local_path IS NOT NULL OR entered_discovery_at IS NOT NULL) ORDER BY id DESC LIMIT 1",
            (nd_song_id, exclude_item_id)).fetchone()


def counts_by_state(db: Database, batch_id: int) -> Dict[str, int]:
    with db.lock:
        rows = db.conn.execute("SELECT state, COUNT(*) AS n FROM recommendation_items WHERE batch_id=? GROUP BY state",
                               (batch_id,)).fetchall()
    return {r["state"]: r["n"] for r in rows}


# ---------------------------------------------------------------- 中断恢复
def recover_interrupted(db: Database, now: Optional[datetime] = None) -> int:
    """进程被杀后的善后：DOWNLOADING → PENDING。返回复位的条目数。"""
    with db.lock:
        ids = [r["id"] for r in db.conn.execute("SELECT id FROM recommendation_items WHERE state=?", (ST.DOWNLOADING,))]
    for i in ids:
        set_state(db, i, ST.PENDING, note="上次运行被中断，重新处理", now=now)
    return len(ids)


# ---------------------------------------------------------------- 歌单记录
def record_playlist(db: Database, batch_id: int, name: str, nd_playlist_id: str, song_count: int,
                    now: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO lb_playlists(batch_id, name, nd_playlist_id, song_count, created_at) VALUES(?,?,?,?,?)",
                     (batch_id, name, nd_playlist_id, song_count, now_iso(now)))


def pending_renames(db: Database) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute("SELECT * FROM lb_playlists WHERE rename_to IS NOT NULL AND deleted_at IS NULL").fetchall()


def finish_rename(db: Database, row_id: int, new_name: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE lb_playlists SET name=?, rename_to=NULL WHERE id=?", (new_name, row_id))


def live_playlists_of_batch(db: Database, batch_id: int) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute("SELECT * FROM lb_playlists WHERE batch_id=? AND deleted_at IS NULL", (batch_id,)).fetchall()


def mark_playlist_deleted(db: Database, row_id: int, now: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE lb_playlists SET deleted_at=? WHERE id=?", (now_iso(now), row_id))


# ---------------------------------------------------------------- 转正
def items_in_state(db: Database, *states: str) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute(_ITEM_SELECT + f"WHERE i.state IN ({','.join('?' * len(states))}) ORDER BY i.id",
                               states).fetchall()


def record_promotion(db: Database, item_id: int, *, src: str = "", dst: str = "", status: str, restar: bool = False,
                     error: str = "", now: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO promotion_records(item_id, src, dst, status, restar, error, at) VALUES(?,?,?,?,?,?,?)",
                     (item_id, src, dst, status, 1 if restar else 0, error, now_iso(now)))


def target_of(row) -> Target:
    """条目 → 匹配用的 Target（来自 ListenBrainz 的歌手/歌名/MBID/时长）。"""
    return Target(artist=row["artist"], title=row["title"],
                  artists=[a for a in json.loads(row["artists_json"] or "[]") if a],
                  mbid=row["recording_mbid"] or "",
                  duration_s=(row["duration_ms"] / 1000.0) if row["duration_ms"] else None,
                  album=row["album"] or "")


# ---------------------------------------------------------------- 清理 / 回收站
def items_sharing_file(db: Database, nd_song_id: str) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute(_ITEM_SELECT + "WHERE i.local_file_id=? ORDER BY i.id", (nd_song_id,)).fetchall()


def record_trash(db: Database, item_id: Optional[int], src: str, trash_path: str, *, purged: bool = False,
                 now: Optional[datetime] = None) -> int:
    ts = now_iso(now)
    with db.transaction() as conn:
        return conn.execute("INSERT INTO trash_entries(item_id, src, trash_path, trashed_at, purged_at) VALUES(?,?,?,?,?)",
                            (item_id, src, trash_path, ts, ts if purged else None)).lastrowid


def trash_pending(db: Database) -> List[sqlite3.Row]:
    with db.lock:
        return db.conn.execute("SELECT * FROM trash_entries WHERE purged_at IS NULL ORDER BY id").fetchall()


def mark_trash_purged(db: Database, entry_id: int, now: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE trash_entries SET purged_at=? WHERE id=?", (now_iso(now), entry_id))


def live_playlists_older_than(db: Database, cutoff_iso: str) -> List[sqlite3.Row]:
    """我们建过、尚未删除、且所属批次入库时间早于 cutoff 的歌单。"""
    with db.lock:
        return db.conn.execute(
            "SELECT p.*, b.fetched_at FROM lb_playlists p JOIN recommendation_batches b ON b.id=p.batch_id "
            "WHERE p.deleted_at IS NULL AND b.fetched_at < ? ORDER BY p.id", (cutoff_iso,)).fetchall()


# ---------------------------------------------------------------- 红心同步
def cache_get_mbid(db: Database, norm_artist: str, norm_title: str) -> Optional[sqlite3.Row]:
    with db.lock:
        return db.conn.execute("SELECT * FROM mbid_cache WHERE norm_artist=? AND norm_title=?",
                               (norm_artist, norm_title)).fetchone()


def cache_put_mbid(db: Database, norm_artist: str, norm_title: str, mbid: str, found: bool,
                   now: Optional[datetime] = None, *, reason: str = "", cand: str = "") -> None:
    """写自动查询结果。绝不覆盖用户手动处理过的行（manual 行永远优先）。"""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mbid_cache(norm_artist, norm_title, mbid, found, looked_up_at, reason, cand) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(norm_artist, norm_title) DO UPDATE SET mbid=excluded.mbid, found=excluded.found, "
            "looked_up_at=excluded.looked_up_at, reason=excluded.reason, cand=excluded.cand WHERE mbid_cache.manual=0 "
            "AND mbid_cache.ignored=0", (norm_artist, norm_title, mbid, 1 if found else 0, now_iso(now), reason, cand))


def cache_set_manual(db: Database, norm_artist: str, norm_title: str, *, artist: str, title: str, mbid: str = "",
                     ignored: bool = False, now: Optional[datetime] = None) -> None:
    """用户手动指定 MBID（或标记忽略）。mbid 与 ignored 二选一。"""
    if bool(mbid) == bool(ignored):
        raise ValueError("手动处理必须二选一：指定 MBID，或标记忽略")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mbid_cache(norm_artist, norm_title, mbid, found, looked_up_at, manual, ignored, artist, title) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(norm_artist, norm_title) DO UPDATE SET mbid=excluded.mbid, "
            "found=excluded.found, looked_up_at=excluded.looked_up_at, manual=excluded.manual, ignored=excluded.ignored, "
            "artist=excluded.artist, title=excluded.title, reason='', cand=''",
            (norm_artist, norm_title, mbid, 1 if mbid else 0, now_iso(now), 1 if mbid else 0, 1 if ignored else 0, artist, title))


def cache_clear_manual(db: Database, norm_artist: str, norm_title: str) -> bool:
    """撤销手动处理：删除该行，下次同步会重新自动解析。返回是否真的删了。"""
    with db.transaction() as conn:
        cur = conn.execute("DELETE FROM mbid_cache WHERE norm_artist=? AND norm_title=? AND (manual=1 OR ignored=1)",
                           (norm_artist, norm_title))
        return cur.rowcount > 0


def cache_manual_rows(db: Database) -> List[Dict[str, Any]]:
    with db.lock:
        rows = db.conn.execute("SELECT norm_artist, norm_title, mbid, ignored, artist, title, looked_up_at FROM mbid_cache "
                               "WHERE manual=1 OR ignored=1 ORDER BY looked_up_at DESC").fetchall()
    return [dict(r) for r in rows]


def love_records(db: Database) -> Dict[str, str]:
    """mbid -> status"""
    with db.lock:
        return {r["mbid"]: r["status"] for r in db.conn.execute("SELECT mbid, status FROM love_sync")}


def record_love(db: Database, mbid: str, *, nd_song_id: str = "", artist: str = "", title: str = "", source: str = "",
                status: str, error: str = "", now: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO love_sync(mbid, nd_song_id, artist, title, source, status, error, synced_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(mbid) DO UPDATE SET nd_song_id=excluded.nd_song_id, artist=excluded.artist, title=excluded.title, "
            "source=excluded.source, status=excluded.status, error=excluded.error, synced_at=excluded.synced_at",
            (mbid, nd_song_id, artist, title, source, status, error, now_iso(now)))


def item_mbids_by_nd_id(db: Database) -> Dict[str, str]:
    """已关联到 Navidrome 歌曲、且 LB 歌单给出了 recording MBID 的条目：nd_song_id -> recording_mbid（最可靠的来源）。"""
    with db.lock:
        rows = db.conn.execute(
            "SELECT i.local_file_id AS fid, t.recording_mbid AS mbid FROM recommendation_items i "
            "JOIN tracks t ON t.id=i.track_id WHERE i.local_file_id IS NOT NULL AND t.recording_mbid <> '' "
            "AND i.state IN ('LOCAL_MAIN','LOCAL_DISCOVERY','PROMOTING','PROMOTED')").fetchall()
    return {r["fid"]: r["mbid"] for r in rows}


# ---------------------------------------------------------------- 疑似与主库重复
def add_dup_suspects(db: Database, item_id: int, rows: Iterable[tuple], now: Optional[datetime] = None) -> int:
    """rows: (主库歌曲 id, level, 时长差)。已存在的（含已忽略的）不动。返回新增条数。"""
    ts, n = now_iso(now), 0
    with db.transaction() as conn:
        for sid, level, dur_diff in rows:
            n += conn.execute("INSERT OR IGNORE INTO dup_suspects(item_id, main_nd_song_id, level, dur_diff, created_at) "
                              "VALUES(?,?,?,?,?)", (item_id, sid, level, dur_diff, ts)).rowcount
    return n


def dup_suspects(db: Database, item_id: int) -> List[sqlite3.Row]:
    """条目未被忽略的疑似重复（主库文件信息来自本地索引）。"""
    with db.lock:
        return db.conn.execute(
            "SELECT d.main_nd_song_id, d.level, d.dur_diff, f.artist, f.title, f.album, f.duration, f.size, f.suffix, f.starred "
            "FROM dup_suspects d JOIN local_files f ON f.nd_song_id=d.main_nd_song_id "
            "WHERE d.item_id=? AND d.dismissed=0 AND f.library='main' ORDER BY d.dur_diff IS NULL, d.dur_diff, d.main_nd_song_id",
            (item_id,)).fetchall()


def dismiss_dup_suspects(db: Database, item_ids: Iterable[int]) -> None:
    with db.transaction() as conn:
        for i in item_ids:
            conn.execute("UPDATE dup_suspects SET dismissed=1 WHERE item_id=?", (i,))


def delete_dup_suspects(db: Database, item_ids: Iterable[int]) -> None:
    with db.transaction() as conn:
        for i in item_ids:
            conn.execute("DELETE FROM dup_suspects WHERE item_id=?", (i,))
