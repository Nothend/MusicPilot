"""SQLite 数据库层：连接、WAL、版本化迁移、事务。

整个应用共用一个数据库文件（<数据目录>/database.sqlite），内含登录 cookie 等凭据，
因此文件创建时即为 600 权限（WAL/SHM 附属文件沿用主库权限）。

迁移用 PRAGMA user_version 记录版本：MIGRATIONS 的第 N 项把库从 N-1 升到 N，
只增不改；库的版本比代码新（降级运行）时拒绝启动，避免旧代码误写新结构。
"""

import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List

logger = logging.getLogger(__name__)


def _v1(conn: sqlite3.Connection) -> None:
    """v1：Web 控制台状态（原 web_state.json）。"""
    conn.execute(
        """CREATE TABLE settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,           -- JSON
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE job_history (
            id         TEXT PRIMARY KEY,
            seq        INTEGER NOT NULL,        -- 稳定的先后顺序（越大越新）
            data       TEXT NOT NULL,           -- JSON：任务汇总（逐首明细仍在 job_details/ 文件）
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX idx_job_history_seq ON job_history(seq)")


def _v2(conn: sqlite3.Connection) -> None:
    """v2：Navidrome 本地曲库索引（推荐联动用于查重/匹配）。

    注意 path_api 是 Navidrome 按标签拼出的路径，不是真实磁盘路径，仅供展示，不可用来定位文件。
    """
    conn.execute(
        """CREATE TABLE local_files (
            nd_song_id   TEXT PRIMARY KEY,
            library      TEXT NOT NULL CHECK (library IN ('main','discovery')),
            artist       TEXT NOT NULL DEFAULT '',
            title        TEXT NOT NULL DEFAULT '',
            album        TEXT NOT NULL DEFAULT '',
            album_artist TEXT NOT NULL DEFAULT '',
            duration     INTEGER,                 -- 秒
            size         INTEGER,
            suffix       TEXT NOT NULL DEFAULT '',
            mbid         TEXT NOT NULL DEFAULT '',
            starred      INTEGER NOT NULL DEFAULT 0,
            path_api     TEXT NOT NULL DEFAULT '',
            indexed_at   TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX idx_local_files_library ON local_files(library)")
    conn.execute("CREATE INDEX idx_local_files_mbid ON local_files(mbid) WHERE mbid <> ''")


def _v3(conn: sqlite3.Connection) -> None:
    """v3：推荐联动的业务数据——歌曲实体、推荐批次/条目（含状态与历史）、下载记录、任务与日志、歌单。

    时间一律存本地时区的 ISO 字符串（容器 TZ）。推荐历史只增不改：同一首歌每被推荐一次就是一条 item。
    """
    conn.execute(
        """CREATE TABLE tracks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_mbid TEXT NOT NULL DEFAULT '',
            artist         TEXT NOT NULL,
            title          TEXT NOT NULL,
            album          TEXT NOT NULL DEFAULT '',
            duration_ms    INTEGER,
            artists_json   TEXT NOT NULL DEFAULT '[]',   -- LB 歌单里的歌手拆分
            created_at     TEXT NOT NULL
        )""")
    conn.execute("CREATE UNIQUE INDEX uq_tracks_mbid ON tracks(recording_mbid) WHERE recording_mbid <> ''")
    conn.execute(
        """CREATE TABLE recommendation_batches (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            type             TEXT NOT NULL,              -- daily | weekly_exploration | weekly_jams
            lb_playlist_mbid TEXT NOT NULL UNIQUE,
            lb_title         TEXT NOT NULL DEFAULT '',
            lb_date          TEXT NOT NULL DEFAULT '',   -- LB 生成歌单的时间
            fetched_at       TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active',   -- active | done
            total            INTEGER NOT NULL DEFAULT 0
        )""")
    conn.execute(
        """CREATE TABLE recommendation_items (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id             INTEGER NOT NULL REFERENCES recommendation_batches(id),
            track_id             INTEGER NOT NULL REFERENCES tracks(id),
            position             INTEGER NOT NULL,
            state                TEXT NOT NULL DEFAULT 'PENDING',
            local_file_id        TEXT,                   -- Navidrome 歌曲 id（扫描入库后才有）
            local_path           TEXT,                   -- 真实磁盘路径（仅我们自己落地的试听库文件才知道）
            starred              INTEGER NOT NULL DEFAULT 0,
            keep                 INTEGER NOT NULL DEFAULT 0,   -- 用户明确保留：不自动清理
            entered_discovery_at TEXT,                   -- 首次进入试听库的时间（过期清理的起点）
            retry_count          INTEGER NOT NULL DEFAULT 0,
            next_retry_at        TEXT,
            reason_kind          TEXT NOT NULL DEFAULT '',
            reason               TEXT NOT NULL DEFAULT '',
            state_changed_at     TEXT NOT NULL,
            created_at           TEXT NOT NULL,
            UNIQUE (batch_id, position)
        )""")
    conn.execute("CREATE INDEX idx_items_batch_state ON recommendation_items(batch_id, state)")
    conn.execute("CREATE INDEX idx_items_track ON recommendation_items(track_id)")
    conn.execute("CREATE INDEX idx_items_state ON recommendation_items(state)")
    conn.execute(
        """CREATE TABLE item_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
            from_state TEXT NOT NULL,
            to_state   TEXT NOT NULL,
            at         TEXT NOT NULL,
            note       TEXT NOT NULL DEFAULT ''
        )""")
    conn.execute("CREATE INDEX idx_events_item ON item_events(item_id)")
    conn.execute(
        """CREATE TABLE match_candidates (
            item_id    INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
            nd_song_id TEXT NOT NULL,
            library    TEXT NOT NULL,
            PRIMARY KEY (item_id, nd_song_id)
        )""")
    conn.execute(
        """CREATE TABLE download_records (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
            attempt    INTEGER NOT NULL,
            platform_id INTEGER,
            ok         INTEGER NOT NULL,
            error_kind TEXT NOT NULL DEFAULT '',
            error      TEXT NOT NULL DEFAULT '',
            path       TEXT NOT NULL DEFAULT '',
            quality    TEXT NOT NULL DEFAULT '',
            file_type  TEXT NOT NULL DEFAULT '',
            at         TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX idx_downloads_item ON download_records(item_id)")
    conn.execute(
        """CREATE TABLE task_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL,
            trigger    TEXT NOT NULL,                    -- manual | schedule
            started_at TEXT NOT NULL,
            ended_at   TEXT,
            status     TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | paused | interrupted
            stats_json TEXT NOT NULL DEFAULT '{}',
            summary    TEXT NOT NULL DEFAULT ''
        )""")
    conn.execute(
        """CREATE TABLE task_logs (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
            ts     TEXT NOT NULL,
            level  TEXT NOT NULL,
            msg    TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX idx_task_logs_run ON task_logs(run_id)")
    conn.execute(
        """CREATE TABLE lb_playlists (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id       INTEGER NOT NULL REFERENCES recommendation_batches(id),
            name           TEXT NOT NULL,
            nd_playlist_id TEXT NOT NULL,
            song_count     INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL,
            deleted_at     TEXT
        )""")
    conn.execute("CREATE INDEX idx_lb_playlists_batch ON lb_playlists(batch_id)")


# 后续模块（推荐/下载记录/任务日志等）的表在各自的迁移里追加，保持只增不改
def _v4(conn: sqlite3.Connection) -> None:
    """v4：红心转正记录（试听库 → 主库的每次尝试）。status: moved | conflict | error | restarred"""
    conn.execute(
        """CREATE TABLE promotion_records (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id  INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
            src      TEXT NOT NULL DEFAULT '',
            dst      TEXT NOT NULL DEFAULT '',
            status   TEXT NOT NULL,
            restar   INTEGER NOT NULL DEFAULT 0,      -- 本次是否需要补星（Navidrome 里红心丢失时）
            error    TEXT NOT NULL DEFAULT '',
            at       TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX idx_promotions_item ON promotion_records(item_id)")


def _v5(conn: sqlite3.Connection) -> None:
    """v5：试听库软删除的回收站记录。只有这里登记过、且位于 .trash 内的文件才会被彻底删除。"""
    conn.execute(
        """CREATE TABLE trash_entries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id    INTEGER REFERENCES recommendation_items(id) ON DELETE SET NULL,
            src        TEXT NOT NULL DEFAULT '',        -- 原路径
            trash_path TEXT NOT NULL DEFAULT '',        -- 回收站里的路径（trash_days=0 时为空：直接删除）
            trashed_at TEXT NOT NULL,
            purged_at  TEXT                             -- 彻底删除的时间；为空表示仍在回收站
        )""")
    conn.execute("CREATE INDEX idx_trash_pending ON trash_entries(purged_at)")


def _v6(conn: sqlite3.Connection) -> None:
    """v6：红心同步（Navidrome 红心 → ListenBrainz love）。

    mbid_cache：按「标准化歌手+歌名」缓存 LB 元数据查询解析出的 recording MBID（含查不到的负缓存）。
    love_sync：每个 recording MBID 的同步结果；status: submitted（我们提交的）| already（LB 上本来就有）| failed（下次重试）。
    """
    conn.execute(
        """CREATE TABLE mbid_cache (
            norm_artist  TEXT NOT NULL,
            norm_title   TEXT NOT NULL,
            mbid         TEXT NOT NULL DEFAULT '',
            found        INTEGER NOT NULL,
            looked_up_at TEXT NOT NULL,
            PRIMARY KEY (norm_artist, norm_title)
        )""")
    conn.execute(
        """CREATE TABLE love_sync (
            mbid       TEXT PRIMARY KEY,
            nd_song_id TEXT NOT NULL DEFAULT '',
            artist     TEXT NOT NULL DEFAULT '',
            title      TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT '',     -- item | navidrome | lookup
            status     TEXT NOT NULL,
            error      TEXT NOT NULL DEFAULT '',
            synced_at  TEXT NOT NULL
        )""")


def _v7(conn: sqlite3.Connection) -> None:
    """v7：疑似与主库重复的试听库歌曲（供页面让用户选择保留哪一份）。

    每行 = 一个试听库条目 + 一个疑似重复的主库文件（Navidrome 歌曲 id）。dismissed=1 表示用户选了「两份都留」，
    以后不再提示；用户选定保留哪一份后，该条目的记录被删除。
    """
    conn.execute(
        """CREATE TABLE dup_suspects (
            item_id         INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
            main_nd_song_id TEXT NOT NULL,
            level           TEXT NOT NULL DEFAULT '',     -- 匹配级别；near = 歌名吻合但时长/专辑对不上
            dur_diff        REAL,
            dismissed       INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (item_id, main_nd_song_id)
        )""")


def _v8(conn: sqlite3.Connection) -> None:
    """v8：红心同步的「解析不出 MBID」可人工处理。

    mbid_cache 新增列：reason（no_result=LB 没查到 | mismatch=查到了但歌手/歌名对不上）、cand（对不上时 LB 返回了什么）、
    manual（1=用户手动指定的 MBID，永久有效，优先于一切自动结果）、ignored（1=用户选择「忽略这首」，不再提示、不提交）、
    artist/title（手动行的展示名，便于在页面上撤销）。
    """
    for ddl in ("ALTER TABLE mbid_cache ADD COLUMN reason  TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE mbid_cache ADD COLUMN cand    TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE mbid_cache ADD COLUMN manual  INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE mbid_cache ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE mbid_cache ADD COLUMN artist  TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE mbid_cache ADD COLUMN title   TEXT NOT NULL DEFAULT ''"):
        conn.execute(ddl)


def _v9(conn: sqlite3.Connection) -> None:
    """v9：批次日期改为 LB 标题里的日期（原先截 UTC 创建时间，北京时间 00:01 生成的"当日"歌单会差一天）。

    lb_playlists.rename_to：已建好的 Navidrome 歌单名里的旧日期要改正；这里只登记，下次同步时由 lb_playlist.apply_renames 去改。
    """
    conn.execute("ALTER TABLE lb_playlists ADD COLUMN rename_to TEXT")
    for b in conn.execute("SELECT id, lb_title, lb_date FROM recommendation_batches").fetchall():
        found = re.findall(r"\d{4}-\d{2}-\d{2}", b["lb_title"] or "")
        if not found:
            continue
        old, new = (b["lb_date"] or "")[:10], found[-1]
        conn.execute("UPDATE recommendation_batches SET lb_date=? WHERE id=?", (new, b["id"]))
        if old and old != new:
            for p in conn.execute("SELECT id, name FROM lb_playlists WHERE batch_id=? AND deleted_at IS NULL", (b["id"],)).fetchall():
                if old in p["name"]:
                    conn.execute("UPDATE lb_playlists SET rename_to=? WHERE id=?", (p["name"].replace(old, new), p["id"]))


def _v10(conn: sqlite3.Connection) -> None:
    """v10：download_records 的"音乐平台歌曲 id"列统一叫 platform_id（旧库里是更早的叫法，按位置取第 4 列改名）。"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(download_records)").fetchall()]
    if cols and "platform_id" not in cols:
        conn.execute(f'ALTER TABLE download_records RENAME COLUMN "{cols[3]}" TO platform_id')


MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [_v1, _v2, _v3, _v4, _v5, _v6, _v7, _v8, _v9, _v10]


class DatabaseVersionError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 先以 600 创建空文件，再让 sqlite 打开（避免出现过 644 的窗口）
        if not self.path.exists():
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        # isolation_level=None：自动提交模式，事务由 transaction() 显式 BEGIN/COMMIT
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")

    @property
    def version(self) -> int:
        with self.lock:
            return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        """升级到最新版本，返回当前版本。每个迁移各自在一个事务内完成。"""
        with self.lock:
            current = self.version
            target = len(MIGRATIONS)
            if current > target:
                raise DatabaseVersionError(
                    f"数据库版本 {current} 高于程序支持的 {target}（是否降级了镜像？）："
                    f"拒绝启动以免破坏数据，请使用不低于该版本的镜像"
                )
            for ver in range(current + 1, target + 1):
                with self.transaction():
                    MIGRATIONS[ver - 1](self.conn)
                    # PRAGMA user_version 不支持参数绑定；ver 为内部整数
                    self.conn.execute(f"PRAGMA user_version={int(ver)}")
                logger.info(f"数据库已迁移到 v{ver}")
            return self.version

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，正常退出提交，异常回滚。可重入（内层加入外层事务）。"""
        with self.lock:
            if self.conn.in_transaction:
                yield self.conn
                return
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.conn.close()
