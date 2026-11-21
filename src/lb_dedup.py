"""试听库里的歌与主库「疑似重复」：检测，以及让用户选定保留哪一份后的执行。

检测（`detect`）：对每个已落地试听库的条目，用**试听库文件自己的标签**（歌手/歌名/专辑/时长）去主库里找同一首歌
（matcher 的全部规则，含「同专辑时长容差 60 秒」）；命中的主库文件、以及「歌名吻合但时长差太多」的近似项，都记为疑似重复，
页面上列出两份的格式/时长/大小，让用户选。检测只读，不动任何文件。

执行（`run_dedup`，作为后台任务运行）：
- keep_main：试听库这份移入 `.trash`（可恢复，满 trash_days 才真删），条目改为引用主库文件；
- keep_new：**把主库那份移入试听库的 `.trash`**（可恢复，不是直接删除），再把试听库这份按命名规则转正进主库；
  转正失败（目标已存在/IO 错误）就把主库那份原样移回，不留半截；
- dismiss：两份都留，不再提示（在 lb_queries.item_action 里同步完成）。
主库文件的真实路径不能信 Navidrome 的 path（那是标签拼出来的），所以按「索引里的大小 + 扩展名」在主库目录里找，
必须恰好唯一才动手。主库里的 .lrc/封面等附属文件不动。
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import lb_cleanup
import lb_localfs
import lb_playlist
import lb_promote
import lb_repo
import lb_states as ST
from db import Database
from fsutil import safe_move
from lb_settings import TRASH_DIRNAME
from local_index import load_index, sync_index
from matcher import LocalIndex, Target, match
from task_manager import RunHandle

logger = logging.getLogger(__name__)

CHOICES = ("keep_main", "keep_new")
SCAN_TIMEOUT_S = 600


# ---------------------------------------------------------------- 检测
def detect(db: Database, index: LocalIndex, now: Optional[datetime] = None) -> int:
    """返回新发现的疑似重复条数（条目×主库文件）。"""
    by_id = {f.nd_song_id: f for f in index.files}
    found = 0
    for it in lb_repo.items_in_state(db, ST.LOCAL_DISCOVERY):
        f = by_id.get(it["local_file_id"] or "")
        if f is None or f.library != "discovery":
            continue
        t = Target(artist=f.artist, title=f.title, artists=[f.artist], album=f.album, mbid=f.mbid,
                   duration_s=float(f.duration) if f.duration is not None else None)
        r = match(index, t, libraries=("main",))
        if r.status in ("main", "multiple"):
            hits, level = r.candidates or ([r.chosen] if r.chosen else []), r.level or "exact"
        else:
            hits, level = r.near_misses, "near"
        rows = [(h.nd_song_id, level, abs(h.duration - f.duration) if h.duration is not None and f.duration is not None else None)
                for h in hits if h.library == "main"]
        if rows:
            found += lb_repo.add_dup_suspects(db, it["id"], rows, now)
    return found


# ---------------------------------------------------------------- 校验与定位
def validate(db: Database, item_id: int, choice: str, main_id: str = "") -> str:
    """启动任务前的同步校验；返回要处理的主库歌曲 id。出错抛 ValueError（页面直接显示）。"""
    if choice not in CHOICES:
        raise ValueError("未知的选择")
    it = lb_repo.get_item(db, item_id)
    if it is None:
        raise ValueError("条目不存在")
    if it["state"] != ST.LOCAL_DISCOVERY or not it["local_file_id"]:
        raise ValueError("只有已在试听库里的歌曲才能处理疑似重复")
    sus = {r["main_nd_song_id"] for r in lb_repo.dup_suspects(db, item_id)}
    if not sus:
        raise ValueError("这首歌没有待处理的疑似重复")
    if not main_id:
        if len(sus) != 1:
            raise ValueError("有多个疑似重复的主库文件，请指定要对比的那一个")
        main_id = next(iter(sus))
    if main_id not in sus:
        raise ValueError("所选主库文件不在疑似重复列表里")
    return main_id


def locate_main_file(db: Database, main_dir: Path, nd_song_id: str) -> Path:
    """按索引里的「大小+扩展名」在主库目录里找到那个文件；必须恰好一个，否则抛 ValueError。"""
    with db.lock:
        row = db.conn.execute("SELECT size, suffix FROM local_files WHERE nd_song_id=? AND library='main'",
                              (nd_song_id,)).fetchone()
    if row is None or not row["size"] or not row["suffix"]:
        raise ValueError("本地索引里缺少这个主库文件的大小/格式信息，请先「同步索引」")
    want_ext, want_size = "." + row["suffix"].lower().lstrip("."), int(row["size"])
    hits: List[Path] = []
    for cur, dirs, files in os.walk(main_dir):
        dirs[:] = [x for x in dirs if not x.startswith(".")]            # 与 Navidrome 一致：忽略以 . 开头的目录
        for name in files:
            if name.startswith(".") or not name.lower().endswith(want_ext):
                continue
            p = Path(cur) / name
            try:
                if p.stat().st_size == want_size:
                    hits.append(p)
            except OSError:
                continue
    if not hits:
        raise ValueError("在主库目录里找不到对应的文件（可能已被移动或删除），请先「同步索引」")
    if len(hits) > 1:
        raise ValueError(f"主库里有 {len(hits)} 个大小和格式完全相同的文件，无法确定是哪一个，已放弃（请手动处理）")
    if not lb_localfs.inside(hits[0], main_dir):
        raise ValueError("文件不在主库目录内，拒绝操作")
    return hits[0]


# ---------------------------------------------------------------- 执行
@dataclass
class DedupDeps:
    db: Database
    settings: Dict[str, Any]
    nd: Any
    handle: RunHandle
    item_id: int
    choice: str
    main_id: str = ""
    now: Callable[[], datetime] = datetime.now


def _group(db: Database, it) -> list:
    """引用同一个试听库文件、且还在试听库的全部条目（多个批次可能推荐了同一首）。"""
    return [r for r in lb_repo.items_sharing_file(db, it["local_file_id"]) if r["state"] == ST.LOCAL_DISCOVERY]


def _disc_file(it, disc_dir: Path) -> Path:
    p = Path(it["local_path"]) if it["local_path"] else None
    if not (p and p.is_file()):
        p = lb_localfs.find_file_by_tags(disc_dir, it["artist"], it["title"],
                                         [a for a in json.loads(it["artists_json"] or "[]") if a])
    if p is None:
        raise ValueError("在试听库里找不到这首歌的文件")
    return p


def run_dedup(d: DedupDeps) -> Dict[str, Any]:
    h, db, s = d.handle, d.db, d.settings
    main_id = validate(db, d.item_id, d.choice, d.main_id)
    it = lb_repo.get_item(db, d.item_id)
    group = _group(db, it)
    main_dir, disc_dir = Path(s["dirs"]["main"]), Path(s["dirs"]["discovery"])
    d.nd.ping()
    with db.lock:
        mrow = db.conn.execute("SELECT * FROM local_files WHERE nd_song_id=?", (main_id,)).fetchone()
    who = f"{it['artist']} - {it['title']}"
    nd_cfg = s["navidrome"]

    if d.choice == "keep_main":
        src = _disc_file(it, disc_dir)
        if not lb_cleanup._deletable(src, disc_dir):
            raise ValueError(f"拒绝处理：文件不在试听库内或位于 .trash/.staging（{src}）")
        dst = lb_cleanup._unique_trash_path(disc_dir / TRASH_DIRNAME / d.now().strftime("%Y%m%d"), src.name)
        safe_move(src, dst)
        lb_repo.record_trash(db, it["id"], str(src), str(dst), now=d.now())
        h.log(f"试听库这份已移入回收站（可恢复）：{who} → {dst.relative_to(disc_dir)}")
        for g in group:
            lb_repo.set_state(db, g["id"], ST.LOCAL_MAIN, note="用户选择保留主库那份", now=d.now(), local_file_id=main_id,
                              local_path=None, starred=1 if mrow["starred"] else 0,
                              reason_kind="dup_resolved", reason="与主库重复，已保留主库那份")
        lb_repo.delete_dup_suspects(db, [g["id"] for g in group])
        h.add("dedup_kept_main")
        for bid in sorted({g["batch_id"] for g in group}):
            lb_playlist.publish_batch(db, d.nd, bid, h.log, d.now())
        _rescan(d, nd_cfg)
        return h.stats

    # keep_new：先把主库那份移进回收站，再转正试听库这份；转正没成功就原样放回去
    old = locate_main_file(db, main_dir, main_id)
    trash = lb_cleanup._unique_trash_path(disc_dir / TRASH_DIRNAME / d.now().strftime("%Y%m%d"), old.name)
    safe_move(old, trash)
    tid = lb_repo.record_trash(db, it["id"], str(old), str(trash), now=d.now())
    h.log(f"主库那份已移入回收站（可恢复，满 {s['discovery']['trash_days']} 天才会真正删除）：{old.relative_to(main_dir)}")
    pd = lb_promote.PromoteDeps(db, s, d.nd, h, now=d.now, restar=bool(mrow["starred"]))
    try:
        moved = lb_promote._move_group(pd, it["local_file_id"], group, main_dir, disc_dir)
    except Exception:
        _restore_main(d, old, trash, tid, main_dir)
        raise
    if not moved:
        _restore_main(d, old, trash, tid, main_dir)
        cur = lb_repo.get_item(db, it["id"])
        raise ValueError(f"转正失败，主库那份已原样放回：{cur['reason'] or '见任务日志'}")
    lb_repo.delete_dup_suspects(db, [g["id"] for g in group])
    lb_promote.finalize_promoting(pd, main_dir, force_scan=True)
    sync_index(db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
    h.add("dedup_replaced")
    h.log(f"已用试听库这份替换主库那份：{who}")
    return h.stats


def _restore_main(d: DedupDeps, old: Path, trash: Path, tid: int, main_dir: Path) -> None:
    h = d.handle
    if old.exists():
        h.log(f"无法放回主库那份（原位置已有文件），请到回收站手动恢复：{trash}", "ERROR")
        return
    safe_move(trash, old)
    lb_repo.mark_trash_purged(d.db, tid, d.now())          # 不再是待清理的回收站记录
    h.log(f"已把主库那份原样放回：{old.relative_to(main_dir)}", "WARNING")


def _rescan(d: DedupDeps, nd_cfg: Dict[str, Any]) -> None:
    try:
        d.nd.scan_and_wait(timeout=SCAN_TIMEOUT_S)
        sync_index(d.db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
    except Exception as e:                                  # 扫描失败不影响已完成的文件操作，下次同步会修正索引
        d.handle.log(f"Navidrome 扫描失败（不影响已完成的操作）：{e}", "WARNING")
