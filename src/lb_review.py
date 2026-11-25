"""网盘文件的元数据人工审核：下载后先放在 `<目标目录>/.review/`（点号目录：Navidrome 与下载目录扫描都会忽略），
由你在页面上对比「文件里原有的标签/封面」和「我们打算写入的（来自 ListenBrainz / MusicBrainz / 封面库 / 歌词库）」后决定：

  use_proposed   清空文件里所有原有标签与图片，只写入推荐的那几项（原标签里可能夹带广告）
  keep_original  文件原样保留，只补缺的歌手/歌名/专辑/MBID（你看过了，没问题）
  reject         放弃这个版本：删掉暂存文件，条目回到「未找到」（有冷却期，之后会再尝试）

三种选择之后：下载目录模式 → 移入下载目录，状态 DOWNLOADED；联动模式 → 移入试听库，状态 LOCAL_DISCOVERY
（下次同步时由 Navidrome 收录并加入歌单）。标签的任何改动都在文件进入下载目录/试听库之前完成。
"""

import base64
import io
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import mutagen

import lb_repo
import lb_states as ST
from fsutil import safe_move
from lb_settings import REVIEW_DIRNAME
from task_manager import RunHandle

logger = logging.getLogger(__name__)

CHOICES = ("use_proposed", "keep_original", "reject")
MAX_VALUE_CHARS = 300
THUMB_PX = 160


def simplified_proposal(p: Dict[str, Any]) -> Dict[str, Any]:
    """推荐写入的内容里，中文一律转简体（页面显示与实际写入用同一份，所以对话框里看到的就是会写的）。"""
    from zh import simplify
    q = dict(p)
    for k in ("artist", "title", "album", "lyric"):
        q[k] = simplify(p.get(k, "") or "")
    q["artists"] = [simplify(a) for a in (p.get("artists") or [])]
    return q


# ---------------------------------------------------------------- 读：文件里现有的标签
def _thumb(data: bytes) -> str:
    """缩略图（data URI），供页面直接显示；解码失败返回空串。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def describe(path: Path) -> Dict[str, Any]:
    """{tags:[[键, 值]], pictures:[{mime,size,thumb}], length}。值过长会截断（歌词、注释里常夹带大段文字）。"""
    out: Dict[str, Any] = {"tags": [], "pictures": [], "length": 0.0}
    audio = mutagen.File(str(path))
    if audio is None:
        return out
    out["length"] = float(getattr(audio.info, "length", 0) or 0)
    tags = getattr(audio, "tags", None)
    if tags is not None:
        for k, v in tags.items():
            vals = v if isinstance(v, (list, tuple)) else [v]
            for x in vals:
                if isinstance(x, (bytes, bytearray)):
                    continue
                s = str(x)
                if len(s) > MAX_VALUE_CHARS:
                    s = s[:MAX_VALUE_CHARS] + f"…（共 {len(str(x))} 字）"
                out["tags"].append([str(k), s])
    for p in getattr(audio, "pictures", None) or []:
        out["pictures"].append({"mime": p.mime, "size": len(p.data), "thumb": _thumb(p.data)})
    if tags is not None and hasattr(tags, "getall"):                   # MP3/ID3 的内嵌图片
        for fr in tags.getall("APIC"):
            out["pictures"].append({"mime": fr.mime, "size": len(fr.data), "thumb": _thumb(fr.data)})
    return out


# ---------------------------------------------------------------- 写：重写 / 补缺
def rewrite_tags(path: Path, p: Dict[str, Any], cover: Optional[bytes]) -> None:
    """清空所有原有标签与图片，只写入 p 里的白名单字段。"""
    p = simplified_proposal(p)
    artists = [a for a in (p.get("artists") or []) if a] or [p.get("artist", "")]
    fields = {"ARTIST": artists, "TITLE": [p.get("title", "")], "ALBUM": [p.get("album", "")],
              "DATE": [p.get("date", "")], "YEAR": [(p.get("date", "") or "").split("-")[0]],
              "LYRICS": [p.get("lyric", "")],
              "MUSICBRAINZ_TRACKID": [p.get("recording_mbid", "")], "MUSICBRAINZ_ALBUMID": [p.get("release_mbid", "")]}
    fields = {k: [x for x in v if x] for k, v in fields.items()}
    if path.suffix.lower() == ".flac":
        from mutagen.flac import FLAC, Picture
        a = FLAC(str(path))
        a.clear_pictures()
        if a.tags is None:
            a.add_tags()
        a.tags.clear()
        for k, v in fields.items():
            if v:
                a[k] = v
        if cover:
            pic = Picture()
            pic.type, pic.desc = 3, "Cover"
            pic.mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
            pic.data = cover
            a.add_picture(pic)
        a.save()
        return
    a = mutagen.File(str(path))
    if a is None:
        raise ValueError("无法识别的音频格式")
    a.delete()
    a = mutagen.File(str(path))
    if a.tags is None:
        a.add_tags()
    for k, v in fields.items():
        if v:
            a.tags[{"ARTIST": "Artist", "TITLE": "Title", "ALBUM": "Album", "DATE": "Year", "LYRICS": "Lyrics",
                    "MUSICBRAINZ_TRACKID": "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_ALBUMID": "MUSICBRAINZ_ALBUMID"}.get(k, k)] = ", ".join(v) if k == "ARTIST" else v[0]
    a.save()


# ---------------------------------------------------------------- 执行（后台任务）
@dataclass
class ReviewDeps:
    db: Any
    settings: Dict[str, Any]
    handle: RunHandle
    item_id: int
    choice: str
    now: Callable[[], datetime] = datetime.now


def validate(db, item_id: int, choice: str) -> None:
    """同步校验（出错立刻回给页面）：条目在待审核、选择合法、暂存文件还在。"""
    if choice not in CHOICES:
        raise ValueError("未知的选择")
    it = lb_repo.get_item(db, item_id)
    if it is None or it["state"] != ST.REVIEW:
        raise ValueError("这首歌当前不在「待审核」状态")
    rv = lb_repo.get_review(db, item_id)
    if rv is None:
        raise ValueError("找不到这首歌的审核记录")
    if choice != "reject" and not Path(rv["held_path"]).is_file():
        raise ValueError("暂存文件已经不在了，请选「放弃」后重新下载")


def _cleanup_files(rv) -> None:
    for p in (rv["held_path"], rv["cover_path"]):
        if p:
            try:
                Path(p).unlink()
            except OSError:
                pass


def run_review(d: ReviewDeps) -> Dict[str, Any]:
    h, db, s = d.handle, d.db, d.settings
    validate(db, d.item_id, d.choice)
    it = lb_repo.get_item(db, d.item_id)
    rv = lb_repo.get_review(db, d.item_id)
    who = f"{it['artist']} - {it['title']}"
    folder = s["mode"] == "folder"

    if d.choice == "reject":
        _cleanup_files(rv)
        days = s["retry"]["not_found_cooldown_days"]
        lb_repo.set_state(db, d.item_id, ST.NOT_FOUND, note="用户放弃了网盘里的这个版本", now=d.now(), reason_kind="rejected",
                          reason="你放弃了网盘里的版本", local_path=None,
                          next_retry_at=lb_repo.now_iso(d.now() + timedelta(days=days)))
        lb_repo.delete_review(db, d.item_id)
        h.add("review_rejected"); h.log(f"已放弃网盘里的版本：{who}")
        return h.stats

    held = Path(rv["held_path"])
    data = json.loads(rv["data_json"])
    from lb_adapter import validate_audio
    from openlist_channel import fill_tags
    if d.choice == "use_proposed":
        cover = Path(rv["cover_path"]).read_bytes() if rv["cover_path"] and Path(rv["cover_path"]).is_file() else None
        rewrite_tags(held, data["proposed"], cover)
        h.add("review_rewritten")
    else:
        pr = simplified_proposal(data["proposed"])
        fill_tags(held, pr.get("artist", ""), pr.get("title", ""), pr.get("album", ""), pr.get("artists") or [],
                  None, None, pr.get("recording_mbid", ""))
        h.add("review_kept")
    why = validate_audio(held)
    if why:
        raise ValueError(f"处理后文件校验失败：{why}")
    out_dir = Path(s["dirs"]["output" if folder else "discovery"])
    final = out_dir / held.name
    if final.exists():
        raise ValueError(f"目标目录里已有同名文件，未覆盖：{final.name}")
    safe_move(held, final)
    if rv["cover_path"]:
        try:
            Path(rv["cover_path"]).unlink()
        except OSError:
            pass
    if folder:
        lb_repo.set_state(db, d.item_id, ST.DOWNLOADED, note="审核通过（网盘）", now=d.now(), local_path=str(final),
                          reason_kind="", reason="")
    else:
        lb_repo.set_state(db, d.item_id, ST.LOCAL_DISCOVERY, note="审核通过（网盘）", now=d.now(), local_path=str(final),
                          entered_discovery_at=lb_repo.now_iso(d.now()), reason_kind="", reason="")
    lb_repo.delete_review(db, d.item_id)
    h.log(f"审核完成（{'已重写标签' if d.choice == 'use_proposed' else '保留原标签'}）：{who} → {final.name}"
          + ("" if folder else "；下次同步时 Navidrome 会收录并加入歌单"))
    return h.stats
