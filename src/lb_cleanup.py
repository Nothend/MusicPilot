"""试听库过期清理（软删除）+ 回收站彻底删除 + 过期歌单清理。

对象与底线（PRD 27）：
- **只动试听库**：被清理的文件必须是 LOCAL_DISCOVERY 条目的文件，且真实路径在试听库目录之内、
  不在 `.trash` / `.staging` 里。主库永远不碰；
- 受保护（永不自动清理）：已标星（以 Navidrome 当前状态为准，清理前先刷新）、已转正/转正中、用户标记「保留」；
  多个批次共用同一个文件时，只要有一个条目受保护，整个文件都受保护；
- 过期按「首次进入试听库的时间」计算（entered_discovery_at），不看文件修改时间；
- 软删除：先移入 `.trash/<日期>/`，状态记为 EXPIRED 并从歌单去掉；回收站里的文件满 trash_days 才真正删除，
  而且只会删「回收站记录表里登记过、且位于 .trash 内」的文件。trash_days=0 表示不留回收站，直接删除。
- 歌单：超过 playlist_keep_days 的、由我们创建的 Navidrome 歌单会被删除（不碰别人的歌单）。
"""

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import lb_localfs
import lb_playlist
import lb_repo
import lb_states as ST
from db import Database
from fsutil import safe_move
from lb_promote import sync_starred_flags
from lb_settings import STAGING_DIRNAME, TRASH_DIRNAME
from local_index import sync_index
from nd_client import NavidromeError
from task_manager import RunHandle

logger = logging.getLogger(__name__)

SCAN_TIMEOUT_S = 600


@dataclass
class CleanupDeps:
    db: Database
    settings: Dict[str, Any]
    nd: Any
    handle: RunHandle
    now: Callable[[], datetime] = datetime.now


@dataclass
class Group:
    """同一个文件对应的一组条目。eligible=True 才会被清理。"""
    key: str
    items: List[Any]
    eligible: bool
    reason: str = ""            # 不可清理时的原因（protected_*）
    entered: Optional[datetime] = None


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


# ---------------------------------------------------------------- 选出候选
def collect_groups(db: Database, settings: Dict[str, Any], now: datetime) -> List[Group]:
    """把试听库条目按文件分组并判定是否过期、是否受保护。纯读，无副作用。"""
    retention = timedelta(days=settings["discovery"]["retention_days"])
    by_key: Dict[str, List[Any]] = {}
    for it in lb_repo.items_in_state(db, ST.LOCAL_DISCOVERY):
        key = it["local_file_id"] or f"item-{it['id']}"
        by_key.setdefault(key, []).append(it)

    groups: List[Group] = []
    for key, items in by_key.items():
        # 共用同一文件的其它条目（不同状态）也要看：已转正/转正中/主库的，整个文件受保护
        others = lb_repo.items_sharing_file(db, key) if items[0]["local_file_id"] else []
        everyone = {i["id"]: i for i in [*items, *others]}.values()
        if any(i["state"] in (ST.PROMOTING, ST.PROMOTED, ST.LOCAL_MAIN) for i in everyone):
            groups.append(Group(key, items, False, "protected_promoted"))
        elif any(i["starred"] for i in everyone):
            groups.append(Group(key, items, False, "protected_starred"))
        elif any(i["keep"] for i in everyone):
            groups.append(Group(key, items, False, "protected_keep"))
        else:
            entered = [t for t in (_parse(i["entered_discovery_at"]) for i in items) if t]
            if not entered:                                   # 不知道何时入库：绝不猜，不清理
                groups.append(Group(key, items, False, "unknown_entry_time"))
                continue
            first = min(entered)
            groups.append(Group(key, items, first <= now - retention, "" if first <= now - retention else "not_expired", first))
    return groups


def preview(db: Database, settings: Dict[str, Any], nd=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """「待清理」预览：只读（给了 nd 才会先读一次 Navidrome 的红心来刷新标记）。"""
    now = now or datetime.now()
    if nd is not None:
        sync_starred_flags(db, nd, settings["navidrome"]["discovery_folder_id"])
    groups = collect_groups(db, settings, now)
    due = [g for g in groups if g.eligible]
    protected = {r: sum(1 for g in groups if g.reason == r) for r in
                 ("protected_starred", "protected_keep", "protected_promoted", "unknown_entry_time")}
    return {"due_files": len(due), "due_items": sum(len(g.items) for g in due), "protected": protected,
            "retention_days": settings["discovery"]["retention_days"],
            "sample": [{"artist": g.items[0]["artist"], "title": g.items[0]["title"], "entered": g.items[0]["entered_discovery_at"]}
                       for g in due[:20]]}


# ---------------------------------------------------------------- 清理
def run_cleanup(d: CleanupDeps) -> Dict[str, Any]:
    s, h, db = d.settings, d.handle, d.db
    nd_cfg = s["navidrome"]
    if nd_cfg["discovery_folder_id"] is None or nd_cfg["main_folder_id"] is None:
        raise ValueError("请先在设置里选择 Navidrome 的主库和试听库")
    disc_dir = Path(s["dirs"]["discovery"])
    d.nd.ping()

    sync_starred_flags(db, d.nd, nd_cfg["discovery_folder_id"])        # 红心以 Navidrome 当前状态为准
    groups = collect_groups(db, s, d.now())
    due = [g for g in groups if g.eligible]
    h.log(f"试听库 {len(groups)} 个文件，其中 {len(due)} 个已过期（保留 {s['discovery']['retention_days']} 天）；"
          f"受保护：红心 {sum(g.reason == 'protected_starred' for g in groups)}、已转正 "
          f"{sum(g.reason == 'protected_promoted' for g in groups)}、保留 {sum(g.reason == 'protected_keep' for g in groups)}")

    affected: List[int] = []
    for g in due:
        try:
            if _expire_group(d, g, disc_dir):
                for it in g.items:
                    if it["batch_id"] not in affected:
                        affected.append(it["batch_id"])
        except Exception as e:
            h.add("errors")
            h.log(f"清理出错（{g.items[0]['artist']} - {g.items[0]['title']}）：{type(e).__name__}: {e}", "ERROR")
            logger.exception("清理异常")

    if affected:                                    # 让 Navidrome 丢掉已移走的歌，并把它们从歌单里去掉
        d.nd.scan_and_wait(timeout=SCAN_TIMEOUT_S)
        sync_index(db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
        for bid in affected:
            lb_playlist.publish_batch(db, d.nd, bid, h.log, d.now())

    purge_trash(d, disc_dir)
    purge_old_playlists(d)
    return h.stats


def _real_path(d: CleanupDeps, g: Group, disc_dir: Path) -> Optional[Path]:
    """文件的真实磁盘路径：优先用我们记录的；没有就按标签在试听库里反查。找不到返回 None。"""
    first = g.items[0]
    p = Path(first["local_path"]) if first["local_path"] else None
    if p and p.is_file():
        return p
    return lb_localfs.find_file_by_tags(disc_dir, first["artist"], first["title"],
                                        [a for a in json.loads(first["artists_json"] or "[]") if a])


def _deletable(p: Path, disc_dir: Path) -> bool:
    """只允许处理试听库内的普通文件，且不在以 . 开头的目录（.trash/.staging）里。"""
    if not lb_localfs.inside(p, disc_dir) or not p.is_file():
        return False
    rel = Path(p).resolve().relative_to(Path(disc_dir).resolve())
    return not any(part.startswith(".") for part in rel.parts[:-1]) and not rel.name.startswith(".")


def _expire_group(d: CleanupDeps, g: Group, disc_dir: Path) -> bool:
    h, db, s = d.handle, d.db, d.settings
    first = g.items[0]
    who = f"{first['artist']} - {first['title']}"
    src = _real_path(d, g, disc_dir)
    trash_days = s["discovery"]["trash_days"]
    note = f"试听 {s['discovery']['retention_days']} 天未红心，已过期"

    if src is not None and not _deletable(src, disc_dir):
        h.add("errors"); h.log(f"拒绝清理：文件不在试听库内或位于 .trash/.staging（{src}）", "ERROR")
        return False

    if src is None:                                  # 文件已经不在了（手动删了/上次已移走）：只更新状态
        h.log(f"文件已不存在，仅标记过期：{who}", "WARNING")
    elif trash_days == 0:
        src.unlink()
        lb_repo.record_trash(db, first["id"], str(src), "", purged=True, now=d.now())
        h.log(f"已删除（不留回收站）：{who}")
    else:
        dst = _unique_trash_path(disc_dir / TRASH_DIRNAME / d.now().strftime("%Y%m%d"), src.name)
        safe_move(src, dst)
        lb_repo.record_trash(db, first["id"], str(src), str(dst), now=d.now())
        h.log(f"已移入回收站：{who} → {dst.relative_to(disc_dir)}")

    for it in g.items:
        lb_repo.set_state(db, it["id"], ST.EXPIRED, note=note, now=d.now(), reason_kind="expired", reason=note)
    h.add("expired", len(g.items))
    return True


def _unique_trash_path(day_dir: Path, name: str) -> Path:
    p = day_dir / name
    n = 1
    while p.exists():
        p = day_dir / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return p


# ---------------------------------------------------------------- 回收站与歌单
def purge_trash(d: CleanupDeps, disc_dir: Path) -> int:
    """彻底删除回收站里满 trash_days 的文件。只删「登记过、且位于 .trash 内」的文件。"""
    h, db = d.handle, d.db
    trash_root = disc_dir / TRASH_DIRNAME
    cutoff = d.now() - timedelta(days=d.settings["discovery"]["trash_days"])
    n = 0
    for row in lb_repo.trash_pending(db):
        trashed = _parse(row["trashed_at"])
        if not trashed or trashed > cutoff:
            continue
        p = Path(row["trash_path"]) if row["trash_path"] else None
        try:
            if p is not None and p.exists():
                if not (lb_localfs.inside(p, trash_root) and p.is_file()):
                    h.add("errors"); h.log(f"回收站记录指向了 .trash 之外，拒绝删除：{p}", "ERROR")
                    continue
                p.unlink()
                n += 1
            lb_repo.mark_trash_purged(db, row["id"], d.now())
        except OSError as e:
            h.add("errors"); h.log(f"回收站文件删除失败：{p} — {e}", "ERROR")
    if trash_root.is_dir():                          # 清掉空的日期目录
        for sub in trash_root.iterdir():
            if sub.is_dir() and not any(sub.iterdir()):
                shutil.rmtree(sub, ignore_errors=True)
    if n:
        h.add("purged", n); h.log(f"回收站：彻底删除 {n} 个已满 {d.settings['discovery']['trash_days']} 天的文件")
    return n


def purge_old_playlists(d: CleanupDeps) -> int:
    """删除超过保留天数的、由我们创建的 Navidrome 歌单。"""
    h, db = d.handle, d.db
    cutoff = lb_repo.now_iso(d.now() - timedelta(days=d.settings["discovery"]["playlist_keep_days"]))
    n = 0
    for row in lb_repo.live_playlists_older_than(db, cutoff):
        try:
            d.nd.delete_playlist(row["nd_playlist_id"])
        except NavidromeError as e:                  # 可能已被手动删除：记录一下，仍标记为已删，避免反复尝试
            h.log(f"删除旧歌单时 Navidrome 报错（可能已不存在）：{row['name']} — {e}", "WARNING")
        lb_repo.mark_playlist_deleted(db, row["id"], d.now())
        n += 1
    if n:
        h.add("playlists_deleted", n); h.log(f"已删除 {n} 个超过 {d.settings['discovery']['playlist_keep_days']} 天的旧歌单")
    return n
