"""MusicBrainz 搜索客户端：红心同步里「解析不出 MBID」的歌，让用户在页面上搜索并手动选定。

只用于人工指定 MBID 的搜索（读公开接口，不需要账号）。MusicBrainz 要求带可识别的 User-Agent，
且匿名访问限速约 1 次/秒，这里串行 + 间隔 1.1 秒。
"""

import re
import threading
import time
from typing import Any, Dict, List, Optional

import requests

UA = "MusicPilot/1.0 (+https://github.com/Nothend/MusicPilot)"
BASE = "https://musicbrainz.org/ws/2"
MBID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_lock = threading.Lock()
_last = [0.0]


class MBError(Exception):
    pass


def _lucene(s: str) -> str:
    """转义 Lucene 查询里的特殊字符与引号，避免用户输入破坏查询语法。"""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s or "").strip()


def search_recordings(artist: str, title: str, *, limit: int = 8, session: Optional[requests.Session] = None,
                      sleep=time.sleep, clock=time.monotonic) -> List[Dict[str, Any]]:
    """按歌手+歌名搜索录音，返回 [{mbid, title, artist, release, length_ms, score}]。"""
    if not (title or "").strip():
        raise MBError("歌名不能为空")
    q = f'recording:"{_lucene(title)}"' + (f' AND artist:"{_lucene(artist)}"' if (artist or "").strip() else "")
    sess = session or requests.Session()
    with _lock:                                              # 串行并限速
        wait = 1.1 - (clock() - _last[0])
        if wait > 0:
            sleep(wait)
        try:
            r = sess.get(BASE + "/recording", params={"query": q, "fmt": "json", "limit": limit},
                         headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20)
        except requests.RequestException as e:
            raise MBError(f"连接 MusicBrainz 失败（{type(e).__name__}）")
        finally:
            _last[0] = clock()
    if r.status_code == 503 or r.status_code == 429:
        raise MBError("MusicBrainz 限流中，请稍后再试")
    if r.status_code != 200:
        raise MBError(f"MusicBrainz 返回 HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise MBError("MusicBrainz 返回了无法解析的内容")
    out = []
    for rec in data.get("recordings", []):
        credit = "".join((c.get("name", "") + c.get("joinphrase", "")) for c in rec.get("artist-credit", []) if isinstance(c, dict))
        rel = (rec.get("releases") or [{}])[0].get("title", "")
        out.append({"mbid": str(rec.get("id", "")).lower(), "title": rec.get("title", ""), "artist": credit, "release": rel,
                    "length_ms": rec.get("length"), "score": rec.get("score")})
    return [o for o in out if MBID_RE.match(o["mbid"])]
