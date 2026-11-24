"""推荐同步流水线：拉 LB 歌单 → 入库 → 本地匹配 → 下载 → 逐首写库 → 扫描认领 → 建 Navidrome 歌单。

设计要点（见 DESIGN-LB.md）：
- 每首歌处理完**立刻**写库（状态+事件同事务），进程被杀后可从库里的状态继续，不重新拉取歌单；
- 单首失败不中断整轮；搜索被限流则**暂停整轮**，当前这首退回 PENDING（绝不记为 NOT_FOUND）；
- 同步只处理每种类型最新的一份 LB 歌单，且同一份歌单（按 MBID）只处理一次；
- 「最大新下载数」只统计成功下载的歌，本地已有的不计入，0 = 不限；
- 主库/试听库都只读判断；本流水线只会向试听库写入新文件，绝不改主库。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import lb_dedup
import lb_folder
import lb_period
import lb_playlist
import lb_repo
import lb_states as ST
from db import Database
from lb_client import batch_date
from lb_settings import NETEASE_DAILY, REC_LABELS, STAGING_DIRNAME, types_of
from local_index import load_index, sync_index
from matcher import LocalFile, LocalIndex, MatchResult, Target, is_soft_edition, match
from task_manager import RunHandle

logger = logging.getLogger(__name__)

SEARCH_MIN_INTERVAL = 2.0     # 音乐平台搜索按 2 秒节奏最稳（实测 0.4 秒会被限流）
FAILED_RETRY_STEP_MIN = 30    # 下载失败后第 n 次重试的等待：30 分钟 × n
SCAN_TIMEOUT_S = 600


@dataclass
class SyncDeps:
    db: Database
    settings: Dict[str, Any]
    lb: Any                      # ListenBrainzClient
    nd: Any                      # NavidromeClient
    adapter: Any                 # PlatformAdapter
    handle: RunHandle
    request_delay: float = SEARCH_MIN_INTERVAL
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = datetime.now
    types: Optional[List[str]] = None            # 只处理这些类型（手动按钮/每日 vs 每周调度用）；None=当前推荐来源的全部列表
    netease_daily: Optional[Callable[[], List[Dict[str, Any]]]] = None   # 来源=音乐平台时：取今日每日推荐（见 lb_service）
    download: bool = True                        # False = 只获取推荐并与本地比对，不下载（没有的歌保持待处理）
    only_items: Optional[List[int]] = None       # 只处理这些条目（页面上点某首的「下载」）；None=全部
    library_checker: Any = None                  # 仅下载模式：可选的外部音乐库去重（Navidrome / music-tag-web，只读查询）
    kv: Any = None                               # 键值存储：记录每个列表最近一次获取的结果（空列表时页面据此解释原因）
    paused: bool = field(default=False, init=False)


# ---------------------------------------------------------------- 小工具
_target = lb_repo.target_of


def clean_staging(discovery_dir: str) -> int:
    """清掉试听库暂存区里的残留（只在持有任务锁时调用）。只删普通文件。"""
    n = 0
    staging = Path(discovery_dir) / STAGING_DIRNAME
    if staging.is_dir():
        for p in staging.iterdir():
            if p.is_file():
                try:
                    p.unlink()
                    n += 1
                except OSError:
                    pass
    return n


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


# ---------------------------------------------------------------- 入口
def run_sync_folder(d: SyncDeps) -> Dict[str, Any]:
    """仅下载模式：不连接 Navidrome。取推荐 → 与下载目录（及可选的外部音乐库）去重 → 下载到下载目录 → 记 DOWNLOADED。
    没有试听库、歌单、转正、过期清理：下载下来的文件就是最终结果，本流水线绝不删除或改动目录里已有的任何文件。"""
    s, h = d.settings, d.handle
    out = s["dirs"]["output"]
    Path(out).mkdir(parents=True, exist_ok=True)
    reset = lb_repo.recover_interrupted(d.db, d.now())
    if reset:
        h.log(f"上次运行被中断，已把 {reset} 首「下载中」的歌复位为待处理")
    removed = clean_staging(out)
    if removed:
        h.log(f"已清理暂存区残留 {removed} 个文件")
    fetch_new_batches(d)
    index, n = lb_folder.build_index(out)
    h.log(f"下载目录 {out} 里已有 {n} 首（读取标签用于去重）")
    wanted = list(d.types) if d.types is not None else list(types_of(s["source"]))
    touched: List[int] = []
    for batch in lb_repo.active_batches(d.db):
        if batch["type"] not in wanted or _stale(d, batch):
            continue
        touched.append(batch["id"])
        process_batch(d, batch, index)
        if d.paused:
            break
    for bid in touched:
        _maybe_finish_batch(d, bid)
    if d.paused:
        h.status = "paused"
        h.summary = "下载渠道被限流，已暂停；未处理的歌曲保持待处理，下次运行继续"
        h.log(h.summary, "WARNING")
    return h.stats


def run_sync(d: SyncDeps) -> Dict[str, Any]:
    s, h = d.settings, d.handle
    if s["mode"] == "folder":
        return run_sync_folder(d)
    nd_cfg = s["navidrome"]
    if nd_cfg["main_folder_id"] is None or nd_cfg["discovery_folder_id"] is None:
        raise ValueError("请先在设置里选择 Navidrome 的主库和试听库")
    discovery_dir = s["dirs"]["discovery"]

    # 0) 预检：Navidrome 不可达就终止（避免盲目下载后没法入库/建歌单）
    info = d.nd.ping()
    h.log(f"Navidrome 连接正常（{info.get('version', '')}）")
    reset = lb_repo.recover_interrupted(d.db, d.now())
    if reset:
        h.log(f"上次运行被中断，已把 {reset} 首「下载中」的歌复位为待处理")
    removed = clean_staging(discovery_dir)
    if removed:
        h.log(f"已清理暂存区残留 {removed} 个文件")

    lb_playlist.apply_renames(d.db, d.nd, h.log)

    # 1) 取最新歌单入库（不重新拉取已入库的批次）
    fetch_new_batches(d)

    # 2) 刷新本地索引（主库+试听库）
    st = sync_index(d.db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
    h.log(f"本地索引：主库 {st['main']['total']} 首，试听库 {st['discovery']['total']} 首")
    index = load_index(d.db)

    # 3) 逐批次、逐首处理（只处理当前推荐来源的批次；切换来源前遗留的批次切回来再继续）
    touched: List[int] = []
    wanted = list(d.types) if d.types is not None else list(types_of(s["source"]))
    for batch in lb_repo.active_batches(d.db):
        if batch["type"] not in wanted or _stale(d, batch):
            continue
        touched.append(batch["id"])
        process_batch(d, batch, index)
        if d.paused:
            break

    # 4) 新下载的文件：触发扫描，认领 Navidrome 歌曲 id（认领到的条目所在批次也要重新发布歌单）
    for bid in resolve_new_files(d):
        if bid not in touched:
            touched.append(bid)

    # 5) 建/更新 Navidrome 歌单；收尾已处理完的批次
    for bid in touched:
        lb_playlist.publish_batch(d.db, d.nd, bid, h.log, d.now(), d.settings.get("playlist_names"))
        _maybe_finish_batch(d, bid)

    # 6) 试听库里有哪些歌疑似和主库重复（只检测提示，不动文件）
    n_dup = lb_dedup.detect(d.db, load_index(d.db), d.now())
    if n_dup:
        h.add("dup_suspects", n_dup)
        h.log(f"发现 {n_dup} 处疑似与主库重复的歌曲，请到「推荐」页选择保留哪一份", "WARNING")

    if d.paused:
        h.status = "paused"
        h.summary = "音乐平台搜索被限流，已暂停；未处理的歌曲保持待处理，下次运行继续"
        h.log(h.summary, "WARNING")
    return h.stats


def _stale(d: SyncDeps, batch) -> bool:
    """已过了当前周期的批次不再自动处理（不下载、不重试）；只有用户在页面上点某一首的操作（only_items）才会处理。"""
    return d.only_items is None and not lb_period.is_current(batch["type"], (batch["lb_date"] or "")[:10], d.now())


# ---------------------------------------------------------------- 拉歌单
def fetch_new_batches(d: SyncDeps) -> None:
    """按当前推荐来源取新的推荐列表入库（来源注册表见 lb_settings.SOURCES）。"""
    if d.settings["source"] == "netease":
        _fetch_netease_daily(d)
    else:
        _fetch_listenbrainz(d)


def _fetch_netease_daily(d: SyncDeps) -> None:
    """音乐平台每日推荐：一天一批，歌曲自带平台歌曲 id（下载时不再搜索）。"""
    h = d.handle
    if d.types is not None and NETEASE_DAILY not in d.types:
        return
    label = REC_LABELS[NETEASE_DAILY]
    if not d.settings["strategy"][NETEASE_DAILY]["enabled"]:
        return
    if d.netease_daily is None:
        raise ValueError("没有可用的音乐平台账号来获取每日推荐")
    # 平台每天约 6 点刷新：6 点前取到的仍是前一天的推荐，按前一天记，避免占用「今天」这个批次名
    day = (d.now() - timedelta(hours=6)).strftime("%Y-%m-%d")
    key = f"netease:daily:{day}"
    if lb_repo.batch_known(d.db, key):
        h.log(f"{label}：{day} 的已处理过")
        lb_period.record_status(d.kv, NETEASE_DAILY, "ok", d.now(), day)
        return
    tracks = d.netease_daily()
    if not tracks:
        h.log(f"{label}：今天没有取到推荐歌曲，跳过")
        lb_period.record_status(d.kv, NETEASE_DAILY, "none", d.now(), day)
        return
    pl = {"tracks": [{
        "recording_mbid": "", "title": t["name"] or "", "artist": ", ".join(a for a in t["artists"] if a),
        "artists": [{"name": a} for a in t["artists"] if a], "album": t.get("album") or "",
        "duration_ms": t.get("duration_ms"), "platform_song_id": t["id"],
    } for t in tracks if t.get("id") and t.get("name")]}
    bid = lb_repo.create_batch(d.db, NETEASE_DAILY, {"mbid": key, "title": f"{label} {day}", "date": day}, pl, d.now())
    lb_period.record_status(d.kv, NETEASE_DAILY, "ok", d.now(), day)
    h.log(f"{label}：获取到 {day} 的推荐，共 {len(pl['tracks'])} 首（批次 {bid}）")
    h.add("recommended", len(pl["tracks"]))


def _fetch_listenbrainz(d: SyncDeps) -> None:
    """只取「当前周期」的歌单：每日 = 今天，每周 = 本周。LB 上最新的一份已过期就不入库，只记下结果供页面解释。"""
    h = d.handle
    headers = d.lb.created_for(25)       # 新的在前
    for rtype in types_of("listenbrainz"):
        if d.types is not None and rtype not in d.types:
            continue
        label = REC_LABELS[rtype]
        if not d.settings["strategy"][rtype]["enabled"]:
            continue
        hdr = next((x for x in headers if x["type"] == rtype), None)
        if hdr is None:
            h.log(f"{label}：ListenBrainz 上暂无该类型歌单")
            lb_period.record_status(d.kv, rtype, "none", d.now())
            continue
        hdate = batch_date(hdr.get("title", ""), hdr.get("date", ""))
        if not lb_period.is_current(rtype, hdate, d.now()):
            h.log(f"{label}：ListenBrainz {lb_period.period_word(rtype)}还没有生成（最近一份是 {hdate}，已过期，不使用）", "WARNING")
            lb_period.record_status(d.kv, rtype, "stale", d.now(), hdate)
            continue
        if lb_repo.batch_known(d.db, hdr["mbid"]):
            h.log(f"{label}：{hdate} 的歌单已处理过")
            lb_period.record_status(d.kv, rtype, "ok", d.now(), hdate)
            continue
        pl = d.lb.playlist(hdr["mbid"])
        if not pl["tracks"]:
            h.log(f"{label}：{hdate} 的歌单是空的，跳过")
            lb_period.record_status(d.kv, rtype, "empty", d.now(), hdate)
            continue
        bid = lb_repo.create_batch(d.db, rtype, hdr, pl, d.now())
        lb_period.record_status(d.kv, rtype, "ok", d.now(), hdate)
        h.log(f"{label}：获取到新歌单 {hdate}，共 {len(pl['tracks'])} 首（批次 {bid}）")
        h.add("recommended", len(pl["tracks"]))


# ---------------------------------------------------------------- 处理一个批次
def process_batch(d: SyncDeps, batch, index: LocalIndex) -> None:
    h, s = d.handle, d.settings
    label = REC_LABELS[batch["type"]]
    cap = s["strategy"][batch["type"]]["max_new"]
    max_retries = s["retry"]["download_max_retries"]
    items = lb_repo.items_of_batch(d.db, batch["id"])
    h.log(f"处理 {label}（{batch['lb_date'][:10]}）：{len(items)} 首")

    with d.db.lock:
        new_ok = d.db.conn.execute(
            "SELECT COUNT(DISTINCT r.item_id) FROM download_records r JOIN recommendation_items i ON i.id=r.item_id "
            "WHERE i.batch_id=? AND r.ok=1", (batch["id"],)).fetchone()[0]

    for it in items:
        if d.paused:
            return
        if d.only_items is not None and it["id"] not in d.only_items:
            continue
        st = it["state"]
        if st == ST.DOWNLOAD_FAILED:       # 重试：次数未超限且已到时间
            if not d.download:             # 只获取推荐时不动失败项（保持「下载失败」可见，等下次真正下载时再重试）
                continue
            due = _parse_ts(it["next_retry_at"])
            if it["retry_count"] > max_retries or (due and due > d.now()):
                continue
            lb_repo.set_state(d.db, it["id"], ST.PENDING, note=f"第 {it['retry_count']} 次重试", now=d.now())
        elif st != ST.PENDING:
            continue
        try:
            new_ok += _process_item(d, lb_repo.get_item(d.db, it["id"]), index, cap, new_ok)
        except Exception as e:             # 单首失败不中断整轮
            h.add("errors")
            h.log(f"处理出错（{it['artist']} - {it['title']}）：{type(e).__name__}: {e}", "ERROR")
            logger.exception("处理条目异常")
            _rescue_downloading(d, it["id"], e)


def _rescue_downloading(d: SyncDeps, item_id: int, err: Exception) -> None:
    """异常时若条目还卡在 DOWNLOADING，标为下载失败（可重试），避免留下中间态。"""
    row = lb_repo.get_item(d.db, item_id)
    if row and row["state"] == ST.DOWNLOADING:
        n = row["retry_count"] + 1
        lb_repo.set_state(d.db, item_id, ST.DOWNLOAD_FAILED, note="处理异常", now=d.now(), retry_count=n,
                          next_retry_at=lb_repo.now_iso(d.now() + timedelta(minutes=FAILED_RETRY_STEP_MIN * n)),
                          reason_kind="exception", reason=f"{type(err).__name__}: {err}")


def _variant_hint(r: MatchResult, target: Target) -> dict:
    """库里只有电台版/单曲版等剪辑版、而推荐的是原版时：提示用户尽快换成原版（我们不会覆盖主库文件）。"""
    f = r.chosen
    if r.level == "variant" and f and is_soft_edition(f.title) and not is_soft_edition(target.title):
        return {"reason_kind": "variant", "reason": f"库里是「{f.title}」，建议尽快替换为原版"}
    return {}


def _process_item(d: SyncDeps, it, index: LocalIndex, cap: int, new_ok: int) -> int:
    """处理一首 PENDING 的歌，返回本次新增的成功下载数（0/1）。"""
    h, s, db = d.handle, d.settings, d.db
    who = f"{it['artist']} - {it['title']}"
    target = _target(it)
    folder = s["mode"] == "folder"

    if folder:
        if _folder_existing(d, it, target, index, who):
            return 0
    elif _local_existing(d, it, index, target, who):
        return 0
    return _download_or_defer(d, it, target, cap, new_ok, who, folder, index)


def _folder_existing(d: SyncDeps, it, target: Target, index: LocalIndex, who: str) -> bool:
    """仅下载模式的「已有」判断：先看下载目录（读标签，按联动模式同样的规则匹配），再看可选的外部音乐库。"""
    h, db = d.handle, d.db
    r = match(index, target)
    if r.status in ("main", "multiple"):  # 目录里有多个候选时也算已有
        lb_repo.set_state(db, it["id"], ST.LOCAL_MAIN, note=f"下载目录里已有（{r.level or '多个候选'}）", now=d.now(),
                          reason_kind="exists", reason="下载目录里已有")
        h.add("local_main"); h.log(f"目录已有：{who}")
        return True
    if d.library_checker is not None:
        try:
            if d.library_checker.exists(it["title"], [a for a in json.loads(it["artists_json"] or "[]") if a], it["album"] or ""):
                lb_repo.set_state(db, it["id"], ST.LOCAL_MAIN, note="音乐库中已有", now=d.now(),
                                  reason_kind="exists", reason="音乐库中已有")
                h.add("local_main"); h.log(f"音乐库已有：{who}")
                return True
        except Exception as e:                   # 外部库临时不可用：当作没有，继续下载（最坏是多下一首，不会少下）
            h.log(f"音乐库去重查询失败（{type(e).__name__}），按未收录处理：{who}", "WARNING")
    return False


def _local_existing(d: SyncDeps, it, index: LocalIndex, target: Target, who: str) -> bool:
    """联动模式的本地判断：主库/试听库已有 → 直接复用；多个候选 → 待人工选择；更早条目已下载 → 复用。True = 已处理。
    """
    h, db = d.handle, d.db
    r = match(index, target)
    if r.status == "main":
        lb_repo.set_state(db, it["id"], ST.LOCAL_MAIN, note=f"主库已有（{r.level}）", now=d.now(),
                          local_file_id=r.chosen.nd_song_id, starred=1 if r.chosen.starred else 0,
                          **_variant_hint(r, target))
        h.add("local_main"); h.log(f"主库已有：{who}")
        return True
    if r.status == "discovery":
        _adopt_discovery(d, it, r, target)
        h.add("local_discovery"); h.log(f"试听库已有：{who}")
        return True
    if r.status == "multiple":
        lb_repo.save_candidates(db, it["id"], r.candidates)
        lb_repo.set_state(db, it["id"], ST.MULTIPLE_MATCH, note=r.note, now=d.now(), reason_kind="multiple", reason=r.note)
        h.add("multiple"); h.log(f"本地有多个无法区分的候选，等待人工选择：{who}", "WARNING")
        return True

    # 2) 同一首歌在更早的条目里已落地到试听库（索引还没刷新）：直接复用
    prior = lb_repo.last_item_for_track(db, it["track_id"], it["id"], (ST.LOCAL_DISCOVERY,))
    if prior and prior["local_path"] and Path(prior["local_path"]).is_file():
        lb_repo.set_state(db, it["id"], ST.LOCAL_DISCOVERY, note="复用更早推荐已下载的文件", now=d.now(),
                          local_file_id=prior["local_file_id"], local_path=prior["local_path"],
                          entered_discovery_at=prior["entered_discovery_at"] or lb_repo.now_iso(d.now()))
        h.add("local_discovery"); h.log(f"复用已下载：{who}")
        return True
    return False


def _download_or_defer(d: SyncDeps, it, target: Target, cap: int, new_ok: int, who: str, folder: bool,
                       index: Optional[LocalIndex]) -> int:
    """本地没有这首：冷却期 / 数量上限 / 下载。返回本次新增的成功下载数（0/1）。"""
    h, s, db = d.handle, d.settings, d.db
    if not d.download:                       # 只获取推荐：本地没有的歌保持待处理，不下载（页面上显示「待下载」）
        h.add("to_download")
        return 0
    # 3) 未找到的冷却期内不重复搜索
    nf = lb_repo.last_item_for_track(db, it["track_id"], it["id"], (ST.NOT_FOUND,))
    if nf and (_parse_ts(nf["next_retry_at"]) or datetime.min) > d.now():
        lb_repo.set_state(db, it["id"], ST.NOT_FOUND, note="冷却期内，不重复搜索", now=d.now(),
                          next_retry_at=nf["next_retry_at"], reason_kind="cooldown", reason=nf["reason"])
        h.add("not_found"); h.log(f"未找到（冷却期内，不重复搜索）：{who}")
        return 0

    # 4) 最大新下载数（只数成功下载的；本地已有的不占名额）
    if cap > 0 and new_ok >= cap:
        lb_repo.set_state(db, it["id"], ST.SKIPPED, note=f"已达最大新下载数 {cap}", now=d.now(),
                          reason_kind="over_limit", reason=f"超出最大新下载数 {cap}")
        h.add("skipped"); h.log(f"跳过（已达最大新下载数 {cap}）：{who}")
        return 0

    # 5) 下载
    attempt = lb_repo.download_attempts(db, it["id"]) + 1
    lb_repo.set_state(db, it["id"], ST.DOWNLOADING, note=f"第 {attempt} 次尝试", now=d.now())
    h.log(f"开始下载：{who}")
    psid = it["platform_song_id"]
    res = d.adapter.download_song(it["artist"], it["title"], s["dirs"]["output" if folder else "discovery"],
                                  artists=[a for a in json.loads(it["artists_json"] or "[]") if a],
                                  album=it["album"], duration_ms=it["duration_ms"],
                                  **({"platform_id": int(psid)} if psid else {}),
                                  **({"recording_mbid": it["recording_mbid"]} if it["recording_mbid"] else {}))
    lb_repo.record_download(db, it["id"], attempt, ok=res.success, platform_id=res.platform_id, error_kind=res.error_kind,
                            error=res.error, path=res.path or "", quality=res.quality, file_type=res.file_type, now=d.now())
    # 每首搜索之间留足间隔；按平台歌曲 id 直接下载的没有搜索，只按用户设置的请求间隔
    d.sleep(d.request_delay if psid else max(d.request_delay, SEARCH_MIN_INTERVAL))

    if res.success and res.review:               # 网盘来的文件：标签不可信，先放进 .review/，等人工对比后再入库
        lb_repo.set_state(db, it["id"], ST.REVIEW, note="网盘文件等待审核元数据", now=d.now(), local_path=res.path,
                          reason_kind="", reason="")
        lb_repo.save_review(db, it["id"], res.path, res.review.get("cover_path", ""), res.review, d.now())
        h.add("to_review"); h.log(f"已下载，等你审核元数据：{who}（在「推荐」页点「审核」）")
        return 1
    if res.success and folder:
        lb_repo.set_state(db, it["id"], ST.DOWNLOADED, note="下载成功" + ("（复用已存在文件）" if res.reused else ""),
                          now=d.now(), local_path=res.path, reason_kind="", reason="")
        lf = lb_folder.read_local_file(Path(res.path))       # 同一轮里再遇到同一首：算已有
        index.add(lf or LocalFile(nd_song_id=res.path, library=lb_folder.FOLDER_LIBRARY, artist=target.artist, title=target.title))
        h.add("downloaded"); h.log(f"下载成功：{who}（{res.file_type}）")
        return 1
    if res.success:
        lb_repo.set_state(db, it["id"], ST.LOCAL_DISCOVERY, note="下载成功" + ("（复用已存在文件）" if res.reused else ""),
                          now=d.now(), local_path=res.path, entered_discovery_at=lb_repo.now_iso(d.now()),
                          reason_kind="", reason="")
        h.add("downloaded"); h.log(f"下载成功：{who}（{res.file_type}）")
        return 1
    kind = res.error_kind
    if kind == "rate_limited":
        lb_repo.set_state(db, it["id"], ST.PENDING, note="搜索被限流，暂停", now=d.now())
        d.paused = True
        h.log(f"搜索被限流，暂停本轮：{res.error}", "WARNING")
        return 0
    if kind in ("not_found", "only_mp3", "no_copyright"):
        days = s["retry"]["not_found_cooldown_days"]
        lb_repo.set_state(db, it["id"], ST.NOT_FOUND, note=res.error, now=d.now(), reason_kind=kind, reason=res.error,
                          next_retry_at=lb_repo.now_iso(d.now() + timedelta(days=days)))
        h.add("not_found"); h.log(f"未找到（{kind}）：{who} — {res.error}")
        return 0
    n = it["retry_count"] + 1
    lb_repo.set_state(db, it["id"], ST.DOWNLOAD_FAILED, note=res.error, now=d.now(), reason_kind=kind, reason=res.error,
                      retry_count=n, next_retry_at=lb_repo.now_iso(d.now() + timedelta(minutes=FAILED_RETRY_STEP_MIN * n)))
    h.add("failed"); h.log(f"下载失败（{kind}）：{who} — {res.error}", "WARNING")
    return 0


def _adopt_discovery(d: SyncDeps, it, r: MatchResult, target: Optional[Target] = None) -> None:
    """匹配到试听库里已有的歌：继承该文件已知的真实路径与入库时间（来自更早的条目），否则以此刻为入库时间。"""
    f = r.chosen
    prior = lb_repo.item_with_nd_id(d.db, f.nd_song_id, it["id"])
    lb_repo.set_state(d.db, it["id"], ST.LOCAL_DISCOVERY, note=f"试听库已有（{r.level}）", now=d.now(),
                      local_file_id=f.nd_song_id, starred=1 if f.starred else 0,
                      local_path=prior["local_path"] if prior else None,
                      entered_discovery_at=(prior["entered_discovery_at"] if prior and prior["entered_discovery_at"]
                                            else lb_repo.now_iso(d.now())),
                      **(_variant_hint(r, target) if target else {}))


# ---------------------------------------------------------------- 扫描认领与收尾
def resolve_new_files(d: SyncDeps) -> List[int]:
    """已落地试听库、但还没有 Navidrome 歌曲 id 的条目：触发扫描 → 刷新索引 → 认领 id。返回认领到 id 的条目所在批次。"""
    h, db = d.handle, d.db
    with db.lock:
        pending = db.conn.execute("SELECT id FROM recommendation_items WHERE state=? AND local_file_id IS NULL",
                                  (ST.LOCAL_DISCOVERY,)).fetchall()
    if not pending:
        return []
    h.log(f"有 {len(pending)} 首新歌待 Navidrome 收录，触发扫描")
    d.nd.scan_and_wait(timeout=SCAN_TIMEOUT_S)
    nd_cfg = d.settings["navidrome"]
    sync_index(db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
    index = load_index(db)
    resolved: List[int] = []
    for row in pending:
        it = lb_repo.get_item(db, row["id"])
        r = match(index, _target(it), libraries=("discovery",))
        if r.status == "discovery":
            lb_repo.update_fields(db, it["id"], local_file_id=r.chosen.nd_song_id, starred=1 if r.chosen.starred else 0)
            if it["batch_id"] not in resolved:
                resolved.append(it["batch_id"])
        else:
            h.log(f"扫描后仍未在 Navidrome 找到：{it['artist']} - {it['title']}（下次运行会再认领）", "WARNING")
            h.add("unresolved")
    return resolved


def _maybe_finish_batch(d: SyncDeps, batch_id: int) -> None:
    """批次里没有待处理项、也没有还能重试的失败项，就标记为 done。"""
    max_retries = d.settings["retry"]["download_max_retries"]
    counts = lb_repo.counts_by_state(d.db, batch_id)
    if counts.get(ST.PENDING) or counts.get(ST.DOWNLOADING):
        return
    if counts.get(ST.DOWNLOAD_FAILED):
        with d.db.lock:
            retryable = d.db.conn.execute(
                "SELECT COUNT(*) FROM recommendation_items WHERE batch_id=? AND state=? AND retry_count<=?",
                (batch_id, ST.DOWNLOAD_FAILED, max_retries)).fetchone()[0]
        if retryable:
            return
    lb_repo.finish_batch(d.db, batch_id)
