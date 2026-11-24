"""OpenList 网盘下载渠道：在挂载的网盘音乐目录里按「歌手 + 歌名」搜索，找到无损文件就下载并补全标签。

流程：搜索（OpenList 索引）→ 从文件名/所在目录推出「歌手、歌名」→ 用与音乐平台渠道相同的 matcher 规则选歌
→ 下载到 .staging（.part 原子写）→ 补全缺失的标签 → 校验 → 原子移入目标目录。契约见 download_channels.py。

只要无损：flac / ape / wv。只有 mp3 等有损文件时返回 only_mp3；wav / m4a 标签与无损判断都不可靠，不用。

元数据：网盘文件的标签、封面、歌词不可信（可能夹带广告），所以**不直接入库**：校验后连同「推荐写入的内容」放进 `<目标目录>/.review/`，
条目进入 REVIEW 状态，由用户在页面上对比后决定（lb_review.py）。推荐写入的内容：
  · 歌手 / 歌名 / 专辑 / 录音 MBID：用推荐条目里已知的（ListenBrainz 的推荐自带 MBID）；
  · 封面 / 歌词 / 年份：先用音乐平台里同一首歌（平台有但下不了，如只有 MP3、无版权）；仍缺的再用外部来源——
    MusicBrainz（按录音 MBID 取发行日期与发行 id）→ Cover Art Archive（封面）→ LRCLIB（歌词），见 meta_sources.py。都没有就留空。

组合：`FallbackAdapter` 先用音乐平台，平台返回「没找到 / 只有 MP3 / 无版权」才改用网盘；
平台自己的临时错误（接口、下载失败、限流）原样返回，不去网盘凑数。
"""

import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import mutagen

from fsutil import safe_move
from lb_adapter import AdapterResult, validate_audio
from lb_localfs import safe_component
from lb_review import describe
from lb_settings import REVIEW_DIRNAME, STAGING_DIRNAME
from matcher import Target
from openlist_client import OpenListClient, OpenListError
from platform_search import Candidate, rank_candidates

logger = logging.getLogger(__name__)

LOSSLESS_EXT = {".flac": 0, ".ape": 1, ".wv": 2}          # 值越小越优先
LOSSY_EXT = {".mp3", ".aac", ".ogg", ".opus", ".wma", ".m4a", ".wav"}
FALLBACK_KINDS = ("not_found", "only_mp3", "no_copyright")
MAX_DURATION_DIFF = 20.0        # 下载后文件时长与推荐时长相差超过这么多秒（且超过 10%）就当成别的版本

_TRACKNO = re.compile(r"^\s*\(?\d{1,3}\)?\s*[.\-_、)\s]\s*")


_S2T = None


def script_variants(text: str) -> List[str]:
    """同一段文字的 原文 / 简体 / 繁体 写法（去重，原文在前）。"""
    global _S2T
    from matcher import _t2s
    if _S2T is None:
        from opencc import OpenCC
        _S2T = OpenCC("s2t")
    out: List[str] = []
    for v in (text, _t2s(text), _S2T.convert(text)):
        if v and v not in out:
            out.append(v)
    return out


def read_length(path: Path) -> float:
    f = mutagen.File(str(path))
    if f is None:
        raise ValueError("无法识别的音频格式")
    return float(getattr(f.info, "length", 0) or 0)


# ---------------------------------------------------------------- 文件名解析
def parse_names(path: str) -> List[Tuple[str, str]]:
    """从网盘路径猜「(歌手, 歌名)」，返回所有可能的读法（顺序不确定时两种都给，交给 matcher 过滤）。"""
    p = Path(path)
    stem = _TRACKNO.sub("", p.stem).strip()
    dirs = [d for d in p.parent.parts[-2:] if d not in ("/", "")]
    out: List[Tuple[str, str]] = []
    parts = [x.strip() for x in re.split(r"\s+[-–—]\s+", stem) if x.strip()]
    if len(parts) < 2:
        parts = [x.strip() for x in re.split(r"\s*[-–—]\s*", stem, maxsplit=1) if x.strip()]
    if len(parts) >= 2:
        a, t = parts[0], " - ".join(parts[1:])
        out += [(a, t), (t, a)]
        if len(parts) == 3:                                    # 「歌手 - 专辑 - 歌名」
            out += [(parts[0], parts[2])]
    else:
        for d in reversed(dirs):                                # 只有歌名：歌手取所在的上两级目录
            out.append((d, stem))
    seen, uniq = set(), []
    for x in out:
        if x not in seen and all(x):
            seen.add(x)
            uniq.append(x)
    return uniq


# ---------------------------------------------------------------- 补全标签
def fill_tags(path: Path, artist: str, title: str, album: str, artists: List[str], info=None,
              fetch_cover: Optional[Callable[[str], Optional[bytes]]] = None, recording_mbid: str = "") -> bool:
    """只补缺的标签，返回是否改了文件。info 是音乐平台的 MusicInfo（可选，用来补封面/歌词/年份）。"""
    suffix = path.suffix.lower()
    if suffix == ".flac":
        return _fill_flac(path, artist, title, album, artists, info, fetch_cover, recording_mbid)
    audio = mutagen.File(str(path))
    if audio is None:
        return False
    if audio.tags is None:
        audio.add_tags()
    changed = False
    for key, val in (("Artist", ", ".join(artists) or artist), ("Title", title), ("Album", album)):
        if val and not audio.tags.get(key):
            audio.tags[key] = val
            changed = True
    if recording_mbid and not audio.tags.get("MUSICBRAINZ_TRACKID"):
        audio.tags["MUSICBRAINZ_TRACKID"] = recording_mbid
        changed = True
    if info is not None and getattr(info, "publishTime", "") and not audio.tags.get("Year"):
        audio.tags["Year"] = info.publishTime.split("-")[0]
        changed = True
    if changed:
        audio.save()
    return changed


def _fill_flac(path, artist, title, album, artists, info, fetch_cover, recording_mbid="") -> bool:
    from mutagen.flac import FLAC, Picture
    audio = FLAC(str(path))
    changed = False

    def put(key: str, val) -> None:
        nonlocal changed
        if val and not audio.get(key):
            audio[key] = val
            changed = True

    put("ARTIST", list(artists) or [artist])
    put("TITLE", title)
    put("ALBUM", album or (getattr(info, "album", "") if info else ""))
    put("MUSICBRAINZ_TRACKID", recording_mbid or (getattr(info, "recording_mbid", "") if info else ""))   # 录音 MBID（Navidrome 的 musicbrainz_recordingid）
    put("MUSICBRAINZ_ALBUMID", getattr(info, "release_mbid", "") if info else "")
    if info is not None:
        date = getattr(info, "publishTime", "") or ""
        if date:
            put("DATE", date)
            put("YEAR", date.split("-")[0])
        if getattr(info, "lyric", ""):
            put("LYRICS", info.lyric.strip())
        if getattr(info, "tlyric", ""):
            put("TRANSLATEDLYRICS", info.tlyric.strip())
        if getattr(info, "track_number", 0) and info.track_number > 0:
            put("TRACKNUMBER", str(info.track_number))
        if (getattr(info, "pic_url", "") or getattr(info, "cover", None)) and not audio.pictures:
            cover = getattr(info, "cover", None) or (fetch_cover or _default_cover)(info.pic_url)
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg" if cover[:2] == b"\xff\xd8" else "image/png"
                pic.desc = "Cover"
                pic.data = cover
                audio.add_picture(pic)
                changed = True
    if changed:
        audio.save()
    return changed


def _default_cover(url: str) -> Optional[bytes]:
    from tagger import _fetch_cover
    return _fetch_cover(url)


# ---------------------------------------------------------------- 渠道
class OpenListAdapter:
    def __init__(self, client: OpenListClient, music_dir: str, *, max_candidates: int = 3, request_delay: float = 0.0,
                 meta_lookup: Optional[Callable[[int], Any]] = None, sleep: Callable[[float], None] = time.sleep,
                 fetch_cover: Optional[Callable[[str], Optional[bytes]]] = None, external: Any = None):
        self.external = external                # meta_sources.ExternalMeta：平台没有这首歌时，用 MusicBrainz / 封面库 / 歌词库补
        self.client, self.music_dir = client, "/" + (music_dir or "/").strip("/")
        self.max_candidates, self.request_delay, self._sleep = max_candidates, request_delay, sleep
        self.meta_lookup = meta_lookup          # 音乐平台歌曲 id → MusicInfo（取不到返回 None）
        self.fetch_cover = fetch_cover

    def download_song(self, artist: str, title: str, output_dir, *, artists: Optional[List[str]] = None,
                      album: str = "", duration_ms: Optional[int] = None, candidate_hint: Optional[int] = None,
                      recording_mbid: str = "", **_ignored) -> AdapterResult:
        out_dir = Path(output_dir)
        target = Target(artist=artist, title=title, artists=list(artists or []),
                        duration_s=(duration_ms / 1000.0) if duration_ms else None)
        try:
            files = self._search(target)
        except OpenListError as e:
            return AdapterResult(False, error=str(e), error_kind="api")
        lossless = [f for f in files if Path(f["name"]).suffix.lower() in LOSSLESS_EXT]
        ranked = self._rank(target, album, lossless)
        if not ranked:
            lossy = self._rank(target, album, [f for f in files if Path(f["name"]).suffix.lower() in LOSSY_EXT])
            if lossy:
                return AdapterResult(False, error="网盘里只有 MP3 等有损版本，库只要无损", error_kind="only_mp3")
            return AdapterResult(False, error=f"网盘里没有「{artist} - {title}」的吻合版本", error_kind="not_found")

        staging = out_dir / STAGING_DIRNAME
        try:
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return AdapterResult(False, error=f"无法创建暂存目录: {e}", error_kind="download")
        last_err, tried = "", 0
        for f in ranked[: self.max_candidates]:
            tried += 1
            ext = Path(f["name"]).suffix.lower()
            staged = staging / (safe_component(f"{artist} - {title}") + ext)
            self._unlink(staged)
            try:
                self.client.download(f["path"], staged)
            except OpenListError as e:
                self._unlink(staged)
                return AdapterResult(False, error=str(e), error_kind="download", tried=tried)
            res = self._finish(staged, out_dir, target, artist, title, album, artists or [], candidate_hint, ext, tried, recording_mbid)
            if res.success or res.error_kind != "wrong_version":
                return res
            last_err = res.error
        return AdapterResult(False, error=last_err or "候选都不是想要的版本", error_kind="not_found", tried=tried)

    # ------------------------------------------------------------ 内部
    def _search(self, target: Target) -> List[Dict[str, Any]]:
        """按「歌手 歌名」搜索；没结果再只用歌名。OpenList 的索引按字面匹配，不认繁简体，
        所以每个关键词都用 原文 / 简体 / 繁体 各搜一遍（网盘里的文件名可能是任一种），结果合并后交给 matcher（它会折叠繁简）。"""
        first = (target.artists[0] if target.artists else target.artist) or ""
        seen, out = set(), []
        tries = ([f"{first} {target.title}".strip()] if first else []) + [target.title]
        for kw in tries:
            for q in script_variants(kw):
                for it in self.client.search(self.music_dir, q):
                    if it["path"] not in seen:
                        seen.add(it["path"])
                        out.append(it)
                self._sleep(self.request_delay)
            if out:
                break
        return out

    def _rank(self, target: Target, album: str, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        files = sorted(files, key=lambda f: (LOSSLESS_EXT.get(Path(f["name"]).suffix.lower(), 9), -f["size"]))
        cands, by_id = [], {}
        for i, f in enumerate(files):
            for j, (a, t) in enumerate(parse_names(f["path"])):
                cid = i * 10 + j
                by_id[cid] = f
                cands.append(Candidate(id=cid, name=t, artists=[a], album=Path(f["path"]).parent.name, duration_s=None, rank=i))
        picked, seen = [], set()
        for c in rank_candidates(target, album, cands):
            f = by_id[c.id]
            if f["path"] not in seen:
                seen.add(f["path"])
                picked.append(f)
        return picked

    def _finish(self, staged: Path, out_dir: Path, target: Target, artist: str, title: str, album: str,
                artists: List[str], hint: Optional[int], ext: str, tried: int,
                recording_mbid: str = "") -> AdapterResult:
        """校验 → 凑出「推荐写入的标签」→ 把文件连同对照数据放进 `.review/` 等人工审核（不直接入库，见 lb_review.py）。"""
        try:
            length = read_length(staged)
        except Exception as e:
            self._unlink(staged)
            return AdapterResult(False, error=f"下载后校验失败：无法读取音频（{type(e).__name__}）", error_kind="invalid", tried=tried)
        if length <= 0:
            self._unlink(staged)
            return AdapterResult(False, error="下载后校验失败：音频时长为 0", error_kind="invalid", tried=tried)
        if target.duration_s:
            diff = abs(length - target.duration_s)
            if diff > MAX_DURATION_DIFF and diff > 0.1 * target.duration_s:
                self._unlink(staged)
                return AdapterResult(False, error=f"时长相差 {diff:.0f} 秒，不是同一个版本", error_kind="wrong_version", tried=tried)
        final = out_dir / staged.name
        if final.exists():                        # 之前已经审核入库过：直接复用
            self._unlink(staged)
            if validate_audio(final) is None:
                return AdapterResult(True, str(final), quality="lossless", file_type=ext.lstrip("."), reused=True, tried=tried)
            return AdapterResult(False, error=f"目标文件已存在但校验失败，未覆盖：{final.name}", error_kind="invalid", tried=tried)

        info = None
        if hint and self.meta_lookup:
            try:
                info = self.meta_lookup(hint)
            except Exception as e:
                logger.warning(f"取音乐平台元数据失败（已忽略）：{type(e).__name__}")
        if self.external is not None and not (info and getattr(info, "pic_url", "") and getattr(info, "lyric", "")
                                              and getattr(info, "publishTime", "")):
            try:                                 # 平台没有（或不全）：用 MusicBrainz / 封面库 / 歌词库补缺的
                from meta_sources import merge
                info = merge(info, self.external.lookup(recording_mbid=recording_mbid, artist=artist, title=title,
                                                        album=album, duration_s=target.duration_s))
            except Exception as e:
                logger.warning(f"外部元数据来源失败（已忽略）：{type(e).__name__}")
        g = lambda k: (getattr(info, k, "") or "") if info is not None else ""
        cover = (getattr(info, "cover", None) if info is not None else None)
        if not cover and g("pic_url"):
            try:
                cover = (self.fetch_cover or _default_cover)(g("pic_url"))
            except Exception:
                cover = None
        proposed = {"artist": artist, "artists": list(artists) or [artist], "title": title, "album": album or g("album"),
                    "date": g("publishTime"), "lyric": g("lyric"), "recording_mbid": recording_mbid or g("recording_mbid"),
                    "release_mbid": g("release_mbid"), "has_cover": bool(cover)}
        if cover:
            from lb_review import _thumb
            proposed["cover_thumb"] = _thumb(cover)
        try:
            original = describe(staged)
            hold_dir = out_dir / REVIEW_DIRNAME
            hold_dir.mkdir(parents=True, exist_ok=True)
            held = hold_dir / staged.name
            n = 1
            while held.exists():
                n += 1
                held = hold_dir / f"{staged.stem} ({n}){staged.suffix}"
            safe_move(staged, held)
            cover_path = ""
            if cover:
                cp = held.with_name(held.name + ".cover")
                cp.write_bytes(cover)
                cover_path = str(cp)
        except OSError as e:
            self._unlink(staged)
            return AdapterResult(False, error=f"放入审核目录失败: {e}", error_kind="download", tried=tried)
        return AdapterResult(True, str(held), quality="lossless", file_type=ext.lstrip("."), tried=tried,
                             review={"original": original, "proposed": proposed, "cover_path": cover_path})

    @staticmethod
    def _unlink(p: Path) -> None:
        try:
            Path(p).unlink()
        except OSError:
            pass


class FallbackAdapter:
    """先用 primary（音乐平台），它说「没有 / 只有 MP3 / 无版权」才用 secondary（网盘）。"""

    def __init__(self, primary, secondary, log: Optional[Callable[[str], None]] = None):
        self.primary, self.secondary = primary, secondary
        self.log = log or (lambda m: logger.info(m))

    def download_song(self, artist: str, title: str, output_dir, **kw) -> AdapterResult:
        r = self.primary.download_song(artist, title, output_dir, **kw)
        if r.success or r.error_kind not in FALLBACK_KINDS:
            return r
        self.log(f"音乐平台没有可用的无损（{r.error_kind}），改去网盘找：{artist} - {title}")
        r2 = self.secondary.download_song(artist, title, output_dir, candidate_hint=r.candidate_id, **kw)
        if r2.success:
            return r2
        if r2.error_kind in ("not_found", "only_mp3"):
            r.error = f"{r.error}；网盘：{r2.error}"        # 两边都没有：保留平台的结论（进入「未找到」冷却）
            return r
        return r2                                            # 网盘临时出错：可重试，不记成「未找到」


def wrap_with_openlist(primary, settings: Dict[str, Any]):
    """设置里启用了网盘渠道就把 primary 包成 FallbackAdapter，否则原样返回。"""
    ol = settings.get("openlist") or {}
    if not ol.get("enabled") or not ol.get("url") or not ol.get("music_dir"):
        return primary
    client = OpenListClient(ol["url"], ol["username"], ol["password"])
    meta = None
    dl = getattr(primary, "downloader", None)
    if dl is not None:
        def meta(cid: int):
            return dl.get_music_info(cid, primary.quality, allow_mp3=True)
    from meta_sources import ExternalMeta
    return FallbackAdapter(primary, OpenListAdapter(client, ol["music_dir"], request_delay=getattr(primary, "request_delay", 0.0),
                                                    meta_lookup=meta, external=ExternalMeta()))
