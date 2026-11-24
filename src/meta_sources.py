"""网盘等「文件本身可能没标签」的渠道用的外部元数据来源（都是公开免费、不需要 key 的只读接口）：

  MusicBrainz     录音 MBID → 发行专辑、发行日期、发行 MBID（推荐歌单里每首歌本来就带录音 MBID）
  Cover Art Archive  发行 MBID → 封面
  LRCLIB          歌手 + 歌名（+ 专辑、时长）→ 歌词（优先带时间轴的）

每个来源独立 try：任何一个失败/没结果都只是少补一项，绝不让下载失败。MusicBrainz 限速约 1 次/秒（与 mb_client 共用节流）。
局限（实话）：中文歌在 MusicBrainz 里发行信息常常不全，补不到的就留空；歌词的覆盖率也不保证。
"""

import logging
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import requests

from matcher import compact, fold
from mb_client import BASE as MB_BASE, MBID_RE, UA, _lock as _mb_lock, _last as _mb_last

logger = logging.getLogger(__name__)

CAA = "https://coverartarchive.org/release/{}/front-500"
LRCLIB = "https://lrclib.net/api"
TIMEOUT = 15
MAX_RELEASES_FOR_COVER = 3
LYRIC_DURATION_TOLERANCE = 5     # 秒：LRCLIB 搜到的歌词时长与推荐时长相差超过这个就不用（多半是别的版本）


class ExternalMeta:
    def __init__(self, session: Optional[requests.Session] = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.s = session or requests.Session()
        self._sleep, self._clock = sleep, clock

    # ------------------------------------------------------------ 对外：一首歌能补到什么
    def lookup(self, *, recording_mbid: str, artist: str, title: str, album: str = "",
               duration_s: Optional[float] = None) -> SimpleNamespace:
        """返回 SimpleNamespace(album, publishTime, lyric, cover(bytes|None), release_mbid, recording_mbid)；补不到的字段为空。"""
        out = SimpleNamespace(album="", publishTime="", lyric="", tlyric="", track_number=0, cover=None, pic_url="",
                              release_mbid="", recording_mbid=(recording_mbid or "").lower())
        mbid = out.recording_mbid
        if mbid and MBID_RE.match(mbid):
            try:
                rels = self.releases(mbid, album)
                if rels:
                    out.album, out.publishTime, out.release_mbid = rels[0]["title"], rels[0]["date"], rels[0]["id"]
                    for r in rels[:MAX_RELEASES_FOR_COVER]:
                        c = self.cover(r["id"])
                        if c:
                            out.cover = c
                            break
            except Exception as e:
                logger.warning(f"MusicBrainz 取发行信息失败（已忽略）：{type(e).__name__}")
        try:
            out.lyric = self.lyrics(artist, title, out.album or album, duration_s)
        except Exception as e:
            logger.warning(f"取歌词失败（已忽略）：{type(e).__name__}")
        return out

    # ------------------------------------------------------------ MusicBrainz
    def releases(self, recording_mbid: str, album: str = "") -> List[Dict[str, str]]:
        """录音所在的发行，最合适的在前：专辑名对得上 > 正式发行且日期最早 > 其它有日期的。"""
        with _mb_lock:
            wait = 1.1 - (self._clock() - _mb_last[0])
            if wait > 0:
                self._sleep(wait)
            try:
                r = self.s.get(f"{MB_BASE}/recording/{recording_mbid}", params={"inc": "releases", "fmt": "json"},
                               headers={"User-Agent": UA, "Accept": "application/json"}, timeout=TIMEOUT)
            finally:
                _mb_last[0] = self._clock()
        if r.status_code != 200:
            return []
        rels = [{"id": str(x.get("id", "")).lower(), "title": x.get("title", ""), "date": x.get("date", "") or "",
                 "official": x.get("status") == "Official"} for x in (r.json().get("releases") or [])]
        rels = [x for x in rels if MBID_RE.match(x["id"])]
        want = compact(fold(album)) if album else ""

        def key(x):
            return (0 if want and compact(fold(x["title"])) == want else 1, 0 if x["official"] else 1,
                    x["date"] or "9999")
        return sorted(rels, key=key)

    def cover(self, release_mbid: str) -> Optional[bytes]:
        r = self.s.get(CAA.format(release_mbid), headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or not (r.content.startswith(b"\xff\xd8\xff") or r.content.startswith(b"\x89PNG")):
            return None
        return r.content

    # ------------------------------------------------------------ LRCLIB
    def lyrics(self, artist: str, title: str, album: str = "", duration_s: Optional[float] = None) -> str:
        hdr = {"User-Agent": UA}
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration_s:
            params["duration"] = int(round(duration_s))
        r = self.s.get(f"{LRCLIB}/get", params=params, headers=hdr, timeout=TIMEOUT)
        if r.status_code == 200:
            d = r.json()
            if self._dur_ok(d, duration_s):
                return self._pick(d)
        r = self.s.get(f"{LRCLIB}/search", params={"artist_name": artist, "track_name": title}, headers=hdr, timeout=TIMEOUT)
        if r.status_code == 200:
            for d in r.json() or []:
                if self._dur_ok(d, duration_s) and self._pick(d):
                    return self._pick(d)
        return ""

    @staticmethod
    def _dur_ok(d: Dict[str, Any], want: Optional[float]) -> bool:
        have = d.get("duration")
        return not (want and have) or abs(float(have) - float(want)) <= LYRIC_DURATION_TOLERANCE

    @staticmethod
    def _pick(d: Dict[str, Any]) -> str:
        return (d.get("syncedLyrics") or d.get("plainLyrics") or "").strip()


def merge(primary, secondary) -> Optional[SimpleNamespace]:
    """把两份元数据合成一份（primary 优先，只用 secondary 补 primary 缺的）。任一为 None 就返回另一个。"""
    if primary is None or secondary is None:
        return primary or secondary
    g = lambda o, k, d="": getattr(o, k, d) or d
    return SimpleNamespace(
        album=g(primary, "album") or g(secondary, "album"), publishTime=g(primary, "publishTime") or g(secondary, "publishTime"),
        lyric=g(primary, "lyric") or g(secondary, "lyric"), tlyric=g(primary, "tlyric"),
        track_number=g(primary, "track_number", 0) or g(secondary, "track_number", 0),
        pic_url=g(primary, "pic_url"), cover=g(secondary, "cover", None) if not g(primary, "pic_url") else None,
        release_mbid=g(secondary, "release_mbid"), recording_mbid=g(secondary, "recording_mbid"))
