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
import lb_playlist
import lb_repo
import lb_states as ST
from db import Database
from lb_settings import REC_LABELS, REC_TYPES, STAGING_DIRNAME
from local_index import load_index, sync_index
from matcher import LocalIndex, MatchResult, Target, is_soft_edition, match
from task_manager import RunHandle

logger = logging.getLogger(__name__)

SEARCH_MIN_INTERVAL = 2.0     # 网易云搜索按 2 秒节奏最稳（实测 0.4 秒会被限流）
FAILED_RETRY_STEP_MIN = 30    # 下载失败后第 n 次重试的等待：30 分钟 × n
SCAN_TIMEOUT_S = 600


@dataclass
class SyncDeps:
    db: Database
    settings: Dict[str, Any]
    lb: Any                      # ListenBrainzClient
    nd: Any                      # NavidromeClient
    adapter: Any                 # NeteaseAdapter
    handle: RunHandle
    request_delay: float = SEARCH_MIN_INTERVAL
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = datetime.now
    types: Optional[List[str]] = None            # 只处理这些类型（手动按钮/每日 vs 每周调度用）；None=全部已启用的
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
def run_sync(d: SyncDeps) -> Dict[str, Any]:
    s, h = d.settings, d.handle
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

    # 1) 取最新歌单入库（不重新拉取已入库的批次）
    fetch_new_batches(d)

    # 2) 刷新本地索引（主库+试听库）
    st = sync_index(d.db, d.nd, nd_cfg["main_folder_id"], nd_cfg["discovery_folder_id"])
    h.log(f"本地索引：主库 {st['main']['total']} 首，试听库 {st['discovery']['total']} 首")
    index = load_index(d.db)

    # 3) 逐批次、逐首处理
    touched: List[int] = []
    for batch in lb_repo.active_batches(d.db):
        if d.types is not None and batch["type"] not in d.types:
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
        h.summary = "网易云搜索被限流，已暂停；未处理的歌曲保持待处理，下次运行继续"
        h.log(h.summary, "WARNING")
    return h.stats


# ---------------------------------------------------------------- 拉歌单
def fetch_new_batches(d: SyncDeps) -> None:
    h = d.handle
    headers = d.lb.created_for(25)       # 新的在前
    for rtype in REC_TYPES:
        if d.types is not None and rtype not in d.types:
            continue
        label = REC_LABELS[rtype]
        if not d.settings["strategy"][rtype]["enabled"]:
            continue
        hdr = next((x for x in headers if x["type"] == rtype), None)
        if hdr is None:
            h.log(f"{label}：ListenBrainz 暂无该类型歌单")
            continue
        if lb_repo.batch_known(d.db, hdr["mbid"]):
            h.log(f"{label}：最新一份（{hdr['date'][:10]}）已处理过")
            continue
        pl = d.lb.playlist(hdr["mbid"])
        if not pl["tracks"]:
            h.log(f"{label}：最新一份（{hdr['date'][:10]}）是空的，跳过")
            continue
        bid = lb_repo.create_batch(d.db, rtype, hdr, pl, d.now())
        h.log(f"{label}：获取到新歌单 {hdr['date'][:10]}，共 {len(pl['tracks'])} 首（批次 {bid}）")
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
        st = it["state"]
        if st == ST.DOWNLOAD_FAILED:       # 重试：次数未超限且已到时间
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

    # 1) 本地匹配（主库优先）
    r = match(index, target)
    if r.status == "main":
        lb_repo.set_state(db, it["id"], ST.LOCAL_MAIN, note=f"主库已有（{r.level}）", now=d.now(),
                          local_file_id=r.chosen.nd_song_id, starred=1 if r.chosen.starred else 0,
                          **_variant_hint(r, target))
        h.add("local_main"); h.log(f"主库已有：{who}")
        return 0
    if r.status == "discovery":
        _adopt_discovery(d, it, r, target)
        h.add("local_discovery"); h.log(f"试听库已有：{who}")
        return 0
    if r.status == "multiple":
        lb_repo.save_candidates(db, it["id"], r.candidates)
        lb_repo.set_state(db, it["id"], ST.MULTIPLE_MATCH, note=r.note, now=d.now(), reason_kind="multiple", reason=r.note)
        h.add("multiple"); h.log(f"本地有多个无法区分的候选，等待人工选择：{who}", "WARNING")
        return 0

    # 2) 同一首歌在更早的条目里已落地到试听库（索引还没刷新）：直接复用
    prior = lb_repo.last_item_for_track(db, it["track_id"], it["id"], (ST.LOCAL_DISCOVERY,))
    if prior and prior["local_path"] and Path(prior["local_path"]).is_file():
        lb_repo.set_state(db, it["id"], ST.LOCAL_DISCOVERY, note="复用更早推荐已下载的文件", now=d.now(),
                          local_file_id=prior["local_file_id"], local_path=prior["local_path"],
                          entered_discovery_at=prior["entered_discovery_at"] or lb_repo.now_iso(d.now()))
        h.add("local_discovery"); h.log(f"复用已下载：{who}")
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
    res = d.adapter.download_song(it["artist"], it["title"], s["dirs"]["discovery"],
                                  artists=[a for a in json.loads(it["artists_json"] or "[]") if a],
                                  album=it["album"], duration_ms=it["duration_ms"])
    lb_repo.record_download(db, it["id"], attempt, ok=res.success, netease_id=res.netease_id, error_kind=res.error_kind,
                            error=res.error, path=res.path or "", quality=res.quality, file_type=res.file_type, now=d.now())
    d.sleep(max(d.request_delay, SEARCH_MIN_INTERVAL))     # 每首搜索之间留足间隔

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
