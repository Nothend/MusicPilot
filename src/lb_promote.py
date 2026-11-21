"""红心转正：试听库里被标了红心的歌 → 移入主库。

流程：Navidrome 已标星（getStarred2，限定试听库）→ 命中 DB 里 LOCAL_DISCOVERY 的条目 → 条目置 PROMOTING →
按 MoviePilot 命名规则用标签拼出主库路径 → `safe_move`（绝不覆盖；目标已存在则跳过并记录）→
触发扫描并刷新索引 → 校验红心仍在（实测：标签不变时歌曲 id 与红心都会保留；缺失才补星）→ PROMOTED。

安全约束（PRD 27）：对主库只做「新建文件」，不删除、不覆盖、不修改已有文件；任何写主库的路径都先确认在主库根目录之内。
可恢复：PROMOTING 是持久状态，进程中途被杀后，下次运行先把遗留的 PROMOTING 收尾。
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import lb_localfs
import lb_playlist
import lb_repo
import lb_states as ST
from db import Database
from fsutil import safe_move
from local_index import load_index, sync_index
from matcher import match
from task_manager import RunHandle

logger = logging.getLogger(__name__)

CONFLICT = "promote_conflict"   # reason_kind：主库已有同名文件。自动转正不再重试（需要人处理）
ERROR = "promote_error"         # reason_kind：其它失败，下次运行会再试
SCAN_TIMEOUT_S = 600


@dataclass
class PromoteDeps:
    db: Database
    settings: Dict[str, Any]
    nd: Any
    handle: RunHandle
    now: Callable[[], datetime] = datetime.now
    restar: bool = True      # 转正后红心缺失是否补星。去重换文件（lb_dedup）时只有被替换的主库文件原本有红心才补


def sync_starred_flags(db: Database, nd, discovery_folder_id: int) -> List[str]:
    """把 Navidrome 里试听库的红心同步到 DB 条目的 starred 标记（取消红心也会取消保护）。返回已标星的歌曲 id。"""
    starred = {s["id"] for s in nd.starred_songs(discovery_folder_id)}
    for it in lb_repo.items_in_state(db, ST.LOCAL_DISCOVERY):
        if it["local_file_id"]:
            want = 1 if it["local_file_id"] in starred else 0
            if it["starred"] != want:
                lb_repo.update_fields(db, it["id"], starred=want)
    return sorted(starred)


def run_promote(d: PromoteDeps) -> Dict[str, Any]:
    s, h, db = d.settings, d.handle, d.db
    nd_cfg = s["navidrome"]
    if nd_cfg["main_folder_id"] is None or nd_cfg["discovery_folder_id"] is None:
        raise ValueError("请先在设置里选择 Navidrome 的主库和试听库")
    main_dir, disc_dir = Path(s["dirs"]["main"]), Path(s["dirs"]["discovery"])
    d.nd.ping()

    starred = set(sync_starred_flags(db, d.nd, nd_cfg["discovery_folder_id"]))
    # 以文件为单位：同一个文件可能被多个批次的条目引用，一起转正
    groups: Dict[str, List[Any]] = {}
    for it in lb_repo.items_in_state(db, ST.LOCAL_DISCOVERY):
        if it["local_file_id"] in starred and it["reason_kind"] != CONFLICT:
            groups.setdefault(it["local_file_id"], []).append(it)
    h.log(f"试听库里有 {len(starred)} 首已标红心，其中 {len(groups)} 首待转正")

    moved = 0
    for fid, items in groups.items():
        try:
            moved += _move_group(d, fid, items, main_dir, disc_dir)
        except Exception as e:
            h.add("errors")
            h.log(f"转正出错（{items[0]['artist']} - {items[0]['title']}）：{type(e).__name__}: {e}", "ERROR")
            logger.exception("转正异常")
            _revert(d, items, ERROR, f"{type(e).__name__}: {e}")

    finalize_promoting(d, main_dir, force_scan=moved > 0)
    return h.stats


def _revert(d: PromoteDeps, items, kind: str, reason: str) -> None:
    for it in items:
        row = lb_repo.get_item(d.db, it["id"])
        if row["state"] == ST.PROMOTING:
            lb_repo.set_state(d.db, it["id"], ST.LOCAL_DISCOVERY, note=reason, now=d.now(), reason_kind=kind, reason=reason)
        else:
            lb_repo.update_fields(d.db, it["id"], reason_kind=kind, reason=reason)


def _move_group(d: PromoteDeps, fid: str, items, main_dir: Path, disc_dir: Path) -> int:
    h, db = d.handle, d.db
    first = items[0]
    who = f"{first['artist']} - {first['title']}"

    # 找到真实文件：优先用我们记录的路径；否则按标签在试听库里反查
    src: Optional[Path] = Path(first["local_path"]) if first["local_path"] else None
    if not (src and src.is_file()):
        src = lb_localfs.find_file_by_tags(disc_dir, first["artist"], first["title"],
                                           [a for a in json.loads(first["artists_json"] or "[]") if a])
    if not src or not lb_localfs.inside(src, disc_dir):
        h.add("errors"); h.log(f"找不到试听库里的文件，无法转正：{who}", "WARNING")
        for it in items:
            lb_repo.record_promotion(db, it["id"], status="error", error="试听库里找不到文件", now=d.now())
        _revert(d, items, ERROR, "试听库里找不到对应文件")
        return 0

    tags = lb_localfs.read_file_tags(src)
    dst = main_dir / lb_localfs.main_relpath(tags, src.suffix, d.settings["naming"]["artist_separator"])
    if not lb_localfs.inside(dst, main_dir):             # 理论上不会发生（路径各层已清洗），仍把它当作硬性保险
        raise ValueError(f"目标路径越出主库目录：{dst}")

    for it in items:
        lb_repo.set_state(db, it["id"], ST.PROMOTING, note="红心转正", now=d.now(), local_path=str(src))

    if dst.exists():                                      # 绝不覆盖：跳过并记录
        h.add("conflicts"); h.log(f"主库已有同名文件，跳过转正：{dst}", "WARNING")
        for it in items:
            lb_repo.record_promotion(db, it["id"], src=str(src), dst=str(dst), status="conflict",
                                     error="主库已有同名文件", now=d.now())
        _revert(d, items, CONFLICT, f"主库已有同名文件：{dst.name}")
        return 0

    try:
        how = safe_move(src, dst)
    except FileExistsError:                               # 竞态：检查后被别人创建了
        h.add("conflicts")
        for it in items:
            lb_repo.record_promotion(db, it["id"], src=str(src), dst=str(dst), status="conflict", now=d.now())
        _revert(d, items, CONFLICT, f"主库已有同名文件：{dst.name}")
        return 0
    except OSError as e:
        h.add("errors"); h.log(f"移动失败（{who}）：{e}", "ERROR")
        for it in items:
            lb_repo.record_promotion(db, it["id"], src=str(src), dst=str(dst), status="error", error=str(e), now=d.now())
        _revert(d, items, ERROR, f"移动失败：{e}")
        return 0

    for it in items:
        lb_repo.update_fields(db, it["id"], local_path=str(dst))
        lb_repo.record_promotion(db, it["id"], src=str(src), dst=str(dst), status="moved", now=d.now())
    h.add("moved"); h.log(f"已移入主库（{how}）：{dst.relative_to(main_dir)}")
    return 1


def finalize_promoting(d: PromoteDeps, main_dir: Path, force_scan: bool = False) -> None:
    """收尾所有 PROMOTING 的条目：确认文件已在主库、Navidrome 已收录、红心仍在，然后置为 PROMOTED。"""
    h, db = d.handle, d.db
    pending = lb_repo.items_in_state(db, ST.PROMOTING)
    if not pending:
        return
    nd_cfg = d.settings["navidrome"]
    h.log(f"收尾 {len(pending)} 条转正：触发 Navidrome 扫描")
    d.nd.scan_and_wait(timeout=SCAN_TIMEOUT_S)
    sync_index(db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
    index = load_index(db)
    by_id = {f.nd_song_id: f for f in index.files}

    groups: Dict[str, List[Any]] = {}
    for it in pending:
        groups.setdefault(it["local_file_id"] or f"item-{it['id']}", []).append(it)
    affected_batches: List[int] = []
    for fid, items in groups.items():
        first = items[0]
        dst = Path(first["local_path"] or "")
        who = f"{first['artist']} - {first['title']}"
        if not (dst.is_file() and lb_localfs.inside(dst, main_dir)):
            h.log(f"转正文件不在主库里（{who}），退回试听库状态待人工检查", "ERROR")
            _revert(d, items, ERROR, "转正中断且文件不在主库")
            continue
        song = by_id.get(fid)
        if song is None or song.library != "main":
            # id 变了（标签被改过）或还没被收录：按歌手+歌名在主库里认领
            r = match(index, lb_repo.target_of(first), libraries=("main",))
            song = r.chosen if r.status == "main" else None
        if song is None:
            h.add("unresolved"); h.log(f"Navidrome 还没收录主库里的新文件：{who}（下次运行继续）", "WARNING")
            continue
        restar = d.restar and not song.starred
        if restar:                                         # 实测通常不会发生；缺失才补星
            d.nd.star(song.nd_song_id)
            h.add("restarred"); h.log(f"红心丢失，已补星：{who}", "WARNING")
        for it in items:
            if restar:
                lb_repo.record_promotion(db, it["id"], dst=str(dst), status="restarred", restar=True, now=d.now())
            lb_repo.set_state(db, it["id"], ST.PROMOTED, note="转正完成", now=d.now(), local_file_id=song.nd_song_id,
                              starred=1 if (d.restar or song.starred) else 0, reason_kind="", reason="")
            if it["batch_id"] not in affected_batches:
                affected_batches.append(it["batch_id"])
        _release_siblings(d, fid, song.nd_song_id, dst)
        h.add("promoted"); h.log(f"转正完成：{who}")
    for bid in affected_batches:                            # id 若变化，歌单要重发
        lb_playlist.publish_batch(db, d.nd, bid, h.log, d.now())


def _release_siblings(d: PromoteDeps, old_fid: str, new_fid: str, dst: Path) -> None:
    """其它批次里引用同一个文件、但没被一起转正的条目：改为引用主库里的文件（LOCAL_MAIN）。"""
    for it in lb_repo.items_in_state(d.db, ST.LOCAL_DISCOVERY):
        if it["local_file_id"] in (old_fid, new_fid):
            lb_repo.set_state(d.db, it["id"], ST.LOCAL_MAIN, note="该文件已转正进入主库", now=d.now(),
                              local_file_id=new_fid, local_path=str(dst))
