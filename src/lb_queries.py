"""页面用的只读查询与条目操作（Dashboard、推荐歌单、任务记录）。

只读函数不改任何数据；条目操作（保留/重试/手动选择候选）都经过 lb_repo.set_state，受状态机约束。
"""

import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import lb_cleanup
import lb_period
import lb_repo
import lb_states as ST
from db import Database
from lb_settings import REC_LABELS, SOURCES, types_of

SHOWN_STATES = ST.ALL_STATES


# ---------------------------------------------------------------- Dashboard
def current_lists(db: Database, settings: Dict[str, Any], kv: Any, now: datetime) -> List[Dict[str, Any]]:
    """推荐页要展示的列表：当前推荐来源的每个类型各一项，只含「当前周期」（每日=今天，每周=本周）的歌单。

    没有当前周期的歌单时 batch_id 为 None，并给出 `reason`（为什么是空的）；绝不拿前期的歌单顶替。
    """
    status = (kv.get(lb_period.FETCH_STATUS_KEY) if kv is not None else None) or {}
    out = []
    with db.lock:
        for t in types_of(settings["source"]):
            start = lb_period.period_start(t, now)
            b = db.conn.execute("SELECT * FROM recommendation_batches WHERE type=? AND lb_date>=? ORDER BY id DESC LIMIT 1",
                                (t, start)).fetchone()
            row = {"type": t, "label": REC_LABELS[t], "period_start": start, "batch_id": None, "date": "", "total": 0,
                   "status": "", "counts": {}, "reason": "", "fetch": status.get(t)}
            if b:
                row.update(batch_id=b["id"], date=(b["lb_date"] or "")[:10], total=b["total"], status=b["status"], counts={
                    r["state"]: r["n"] for r in db.conn.execute(
                        "SELECT state, COUNT(*) AS n FROM recommendation_items WHERE batch_id=? GROUP BY state", (b["id"],))})
            else:
                row["reason"] = lb_period.empty_reason(t, status.get(t), settings["source"])
            out.append(row)
    return out


def dashboard(db: Database, settings: Dict[str, Any], current_task: Optional[Dict[str, Any]],
              now: Optional[datetime] = None, kv: Any = None) -> Dict[str, Any]:
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    lists = current_lists(db, settings, kv, now)
    with db.lock:
        c = db.conn
        recommended_today = c.execute("SELECT COALESCE(SUM(total),0) FROM recommendation_batches WHERE fetched_at LIKE ?",
                                      (today + "%",)).fetchone()[0]
        downloaded_today = c.execute("SELECT COUNT(*) FROM download_records WHERE ok=1 AND at LIKE ?", (today + "%",)).fetchone()[0]
        state_totals = {r["state"]: r["n"] for r in c.execute("SELECT state, COUNT(*) AS n FROM recommendation_items GROUP BY state")}
        last_sync = c.execute("SELECT * FROM task_runs WHERE type='sync' ORDER BY id DESC LIMIT 1").fetchone()
        last_run = c.execute("SELECT * FROM task_runs ORDER BY id DESC LIMIT 1").fetchone()
        playlists = c.execute("SELECT COUNT(*) FROM lb_playlists WHERE deleted_at IS NULL").fetchone()[0]
        loved = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM love_sync GROUP BY status")}
    pv = lb_cleanup.preview(db, settings, now=now)
    return {
        "mode": settings["mode"], "channel": settings["channel"],
        "source": settings["source"], "source_label": SOURCES[settings["source"]]["label"],
        "current_task": current_task,
        "recommended_today": recommended_today,
        "downloaded_today": downloaded_today,
        "current": lists,
        "last_sync": _run_row(last_sync),
        "last_run": _run_row(last_run),
        "totals": {"promoted": state_totals.get(ST.PROMOTED, 0), "in_discovery": state_totals.get(ST.LOCAL_DISCOVERY, 0),
                   "expired": state_totals.get(ST.EXPIRED, 0), "not_found": state_totals.get(ST.NOT_FOUND, 0),
                   "download_failed": state_totals.get(ST.DOWNLOAD_FAILED, 0), "multiple_match": state_totals.get(ST.MULTIPLE_MATCH, 0)},
        "pending_cleanup": {"files": pv["due_files"], "items": pv["due_items"], "protected": pv["protected"],
                            "retention_days": pv["retention_days"]},
        "playlists": playlists,
        "love": {"submitted": loved.get("submitted", 0), "already": loved.get("already", 0), "failed": loved.get("failed", 0)},
    }


def todo(db: Database, kv, settings: Dict[str, Any]) -> Dict[str, Any]:
    """需要用户人工处理的事项数量（页面「需要你处理」卡片与「推荐」标签上的角标）。

    multiple    本地有多个难以区分的候选，等你选择是哪一首
    dup         试听库歌曲与主库文件疑似重复，等你选择保留哪一份（只算尚未处理、主库文件还在的）
    review      网盘下载的文件，等你对比原有标签与推荐标签后决定入库方式
    love_unresolved  红心同步解析不出可信 MBID 的歌（最近一次同步/预览的快照）
    """
    with db.lock:
        multiple = db.conn.execute("SELECT COUNT(*) FROM recommendation_items WHERE state=?", (ST.MULTIPLE_MATCH,)).fetchone()[0]
        dup_sql = ("FROM dup_suspects d JOIN local_files f ON f.nd_song_id=d.main_nd_song_id "
                   "JOIN recommendation_items i ON i.id=d.item_id JOIN recommendation_batches b ON b.id=i.batch_id "
                   "WHERE d.dismissed=0 AND f.library='main'")
        dup = db.conn.execute("SELECT COUNT(DISTINCT d.item_id) " + dup_sql).fetchone()[0]
        dup_b = db.conn.execute("SELECT b.id, b.type " + dup_sql + " ORDER BY b.id DESC LIMIT 1").fetchone()
        mul_b = db.conn.execute(
            "SELECT b.id, b.type FROM recommendation_items i JOIN recommendation_batches b ON b.id=i.batch_id "
            "WHERE i.state=? ORDER BY b.id DESC LIMIT 1", (ST.MULTIPLE_MATCH,)).fetchone()
    unresolved = len((kv.get("lb.love_unresolved") or {}).get("items", []))
    with db.lock:
        review = db.conn.execute("SELECT COUNT(*) FROM recommendation_items WHERE state=?", (ST.REVIEW,)).fetchone()[0]
        rev_b = db.conn.execute("SELECT b.id, b.type FROM recommendation_items i JOIN recommendation_batches b ON b.id=i.batch_id "
                                "WHERE i.state=? ORDER BY b.id DESC LIMIT 1", (ST.REVIEW,)).fetchone()
    return {"multiple": multiple, "dup": dup, "love_unresolved": unresolved, "review": review,
            "review_batch": dict(rev_b) if rev_b else None,
            "total": multiple + dup + unresolved + review,
            # 点「去处理」时直达最新的那一批（没有则为 None）
            "multiple_batch": dict(mul_b) if mul_b else None, "dup_batch": dict(dup_b) if dup_b else None}


def review_detail(db: Database, item_id: int) -> Dict[str, Any]:
    """待审核的网盘文件：原有标签/封面 与 推荐写入的内容对照（页面「审核」对话框用）。"""
    it = lb_repo.get_item(db, item_id)
    rv = lb_repo.get_review(db, item_id)
    if it is None or rv is None:
        raise ValueError("这首歌没有待审核的内容")
    data = json.loads(rv["data_json"])
    return {"item": {"id": item_id, "artist": it["artist"], "title": it["title"], "album": it["album"], "state": it["state"]},
            "held_name": Path(rv["held_path"]).name, "exists": Path(rv["held_path"]).is_file(),
            "original": data["original"], "proposed": data["proposed"]}


def _run_row(r) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    d = dict(r)
    d["stats"] = json.loads(d.pop("stats_json") or "{}")
    return d


# ---------------------------------------------------------------- 推荐歌单
def batches(db: Database, rec_type: str = "", limit: int = 30, now: Optional[datetime] = None,
            types: Optional[tuple] = None) -> List[Dict[str, Any]]:
    """历史批次（新的在前，记录页「往期推荐」用）：带各状态数量，以及是否仍是当前周期的歌单。"""
    now = now or datetime.now()
    q, args = "SELECT * FROM recommendation_batches", []
    if rec_type:
        q += " WHERE type=?"
        args.append(rec_type)
    elif types:                                  # 不指定类型时只列这些类型（当前推荐来源的列表）
        q += " WHERE type IN (%s)" % ",".join("?" * len(types))
        args.extend(types)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with db.lock:
        rows = db.conn.execute(q, args).fetchall()
        counts: Dict[int, Dict[str, int]] = {}
        ids = [r["id"] for r in rows]
        if ids:
            for r in db.conn.execute("SELECT batch_id, state, COUNT(*) AS n FROM recommendation_items WHERE batch_id IN (%s) "
                                     "GROUP BY batch_id, state" % ",".join("?" * len(ids)), ids):
                counts.setdefault(r["batch_id"], {})[r["state"]] = r["n"]
    return [{"id": r["id"], "type": r["type"], "label": REC_LABELS.get(r["type"], r["type"]), "date": (r["lb_date"] or "")[:10],
             "total": r["total"], "status": r["status"], "fetched_at": r["fetched_at"], "counts": counts.get(r["id"], {}),
             "current": lb_period.is_current(r["type"], r["lb_date"] or "", now)} for r in rows]


def items(db: Database, batch_id: int, state: str = "") -> Dict[str, Any]:
    """一个批次里的全部条目（可按状态筛选），含 MULTIPLE_MATCH 的候选。"""
    with db.lock:
        b = db.conn.execute("SELECT * FROM recommendation_batches WHERE id=?", (batch_id,)).fetchone()
    if b is None:
        raise ValueError("批次不存在")
    rows = lb_repo.items_of_batch(db, batch_id)
    counts: Dict[str, int] = {}
    out = []
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        if state and r["state"] != state:
            continue
        out.append({
            "id": r["id"], "position": r["position"], "artist": r["artist"], "title": r["title"], "album": r["album"],
            "recommended_at": (b["lb_date"] or "")[:10], "state": r["state"], "reason_kind": r["reason_kind"],
            "reason": r["reason"], "path": r["local_path"] or "", "starred": bool(r["starred"]), "keep": bool(r["keep"]),
            "promoted": r["state"] == ST.PROMOTED, "entered_discovery_at": r["entered_discovery_at"],
            "retry_count": r["retry_count"], "next_retry_at": r["next_retry_at"],
            "candidates": candidates(db, r["id"]) if r["state"] == ST.MULTIPLE_MATCH else [],
            "dups": dup_view(db, r) if r["state"] == ST.LOCAL_DISCOVERY and r["local_file_id"] else [],
        })
    return {"batch": {"id": b["id"], "type": b["type"], "label": REC_LABELS.get(b["type"], b["type"]),
                      "date": (b["lb_date"] or "")[:10], "total": b["total"], "status": b["status"]},
            "counts": counts, "items": out}


def dup_view(db: Database, it) -> List[Dict[str, Any]]:
    """试听库这份与主库疑似重复的文件，两份的格式/时长/大小并排给页面比较。"""
    sus = lb_repo.dup_suspects(db, it["id"])
    if not sus:
        return []
    with db.lock:
        mine = db.conn.execute("SELECT duration, size, suffix FROM local_files WHERE nd_song_id=?", (it["local_file_id"],)).fetchone()
    new = {"duration": mine["duration"], "size": mine["size"], "suffix": mine["suffix"]} if mine else {}
    return [{"main_id": r["main_nd_song_id"], "level": r["level"], "dur_diff": r["dur_diff"],
             "main": {"artist": r["artist"], "title": r["title"], "album": r["album"], "duration": r["duration"],
                      "size": r["size"], "suffix": r["suffix"], "starred": bool(r["starred"])},
             "new": new} for r in sus]


def candidates(db: Database, item_id: int) -> List[Dict[str, Any]]:
    with db.lock:
        rows = db.conn.execute(
            "SELECT m.nd_song_id, m.library, f.artist, f.title, f.album, f.duration FROM match_candidates m "
            "LEFT JOIN local_files f ON f.nd_song_id=m.nd_song_id WHERE m.item_id=? ORDER BY m.library, m.nd_song_id",
            (item_id,)).fetchall()
    return [{"nd_song_id": r["nd_song_id"], "library": r["library"], "artist": r["artist"] or "", "title": r["title"] or "",
             "album": r["album"] or "", "duration": r["duration"]} for r in rows]


# ---------------------------------------------------------------- 任务记录
def runs(db: Database, limit: int = 30) -> List[Dict[str, Any]]:
    with db.lock:
        rows = db.conn.execute("SELECT * FROM task_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_run_row(r) for r in rows]


# ---------------------------------------------------------------- 条目操作
RETRYABLE = (ST.NOT_FOUND, ST.DOWNLOAD_FAILED, ST.SKIPPED)


def item_action(db: Database, item_id: int, action: str, arg: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
    """keep / unkeep / retry / choose。出错抛 ValueError（页面显示）。返回更新后的条目摘要。"""
    it = lb_repo.get_item(db, item_id)
    if it is None:
        raise ValueError("条目不存在")
    now = now or datetime.now()

    if action in ("keep", "unkeep"):
        if it["state"] not in (ST.LOCAL_DISCOVERY, ST.PROMOTING):
            raise ValueError("只有试听库里的歌曲才需要「保留」")
        lb_repo.update_fields(db, item_id, keep=1 if action == "keep" else 0)

    elif action == "dup_dismiss":
        if not lb_repo.dup_suspects(db, item_id):
            raise ValueError("这首歌没有待处理的疑似重复")
        lb_repo.dismiss_dup_suspects(db, [g["id"] for g in lb_repo.items_sharing_file(db, it["local_file_id"])]
                                     if it["local_file_id"] else [item_id])

    elif action == "retry":
        if it["state"] in RETRYABLE:
            lb_repo.set_state(db, item_id, ST.PENDING, note="手动重试", now=now, retry_count=0, next_retry_at=None,
                              reason_kind="", reason="")
            with db.transaction() as c:          # 让下次同步重新处理该批次
                c.execute("UPDATE recommendation_batches SET status='active' WHERE id=?", (it["batch_id"],))
        elif it["state"] == ST.LOCAL_DISCOVERY and it["reason_kind"] in ("promote_conflict", "promote_error"):
            lb_repo.update_fields(db, item_id, reason_kind="", reason="")   # 让自动转正再试一次
        else:
            raise ValueError(f"当前状态（{it['state']}）无需重试")

    elif action == "choose":
        if it["state"] != ST.MULTIPLE_MATCH:
            raise ValueError("只有「多个候选」的条目需要手动选择")
        cand = {c["nd_song_id"]: c for c in candidates(db, item_id)}
        if arg not in cand:
            raise ValueError("所选歌曲不在候选列表里")
        lib = cand[arg]["library"]
        if lib == "main":
            lb_repo.set_state(db, item_id, ST.LOCAL_MAIN, note="手动选择候选", now=now, local_file_id=arg,
                              reason_kind="", reason="")
        else:
            prior = lb_repo.item_with_nd_id(db, arg, item_id)
            lb_repo.set_state(db, item_id, ST.LOCAL_DISCOVERY, note="手动选择候选", now=now, local_file_id=arg,
                              local_path=prior["local_path"] if prior else None,
                              entered_discovery_at=(prior["entered_discovery_at"] if prior and prior["entered_discovery_at"]
                                                    else lb_repo.now_iso(now)),
                              reason_kind="", reason="")
        with db.transaction() as c:
            c.execute("DELETE FROM match_candidates WHERE item_id=?", (item_id,))
    else:
        raise ValueError("未知的操作")

    r = lb_repo.get_item(db, item_id)
    return {"id": r["id"], "state": r["state"], "keep": bool(r["keep"]), "reason_kind": r["reason_kind"]}
