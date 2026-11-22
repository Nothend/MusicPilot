"""ListenBrainz API 客户端（只含推荐联动用到的接口）。

- 认证：Authorization: Token <token>；
- 限流：遵守响应头 X-RateLimit-*，429 / 5xx / "200 但响应体为空"（实测偶发）时退避重试；
- 凭据不进日志、不进异常信息。
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.listenbrainz.org"
USER_AGENT = "MusicPilot/1.0 (+https://github.com/Nothend/MusicPilot)"

# createdfor 歌单的 source_patch -> 推荐类型。daily 的取值在账号出现 Daily Jams 前未实测，
# 因此另按标题兜底（见 classify）
_PATCH_TYPES = {"weekly-jams": "weekly_jams", "weekly-exploration": "weekly_exploration", "daily-jams": "daily"}


class LBError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def classify(title: str, source_patch: str) -> str:
    if source_patch in _PATCH_TYPES:
        return _PATCH_TYPES[source_patch]
    t = (title or "").lower()
    if t.startswith("daily jams"):
        return "daily"
    if t.startswith("weekly jams"):
        return "weekly_jams"
    if t.startswith("weekly exploration"):
        return "weekly_exploration"
    return "other"


def _mbid_from_identifier(identifier: Any) -> str:
    """JSPF 的 identifier 是 URL（或其列表），取最后一段 MBID。"""
    if isinstance(identifier, list):
        identifier = identifier[0] if identifier else ""
    return str(identifier or "").rstrip("/").rsplit("/", 1)[-1]


class ListenBrainzClient:
    def __init__(self, token: str = "", username: str = "", base_url: str = BASE_URL,
                 session: Optional[requests.Session] = None, timeout: float = 30,
                 max_tries: int = 5, sleep=time.sleep):
        self.token = token
        self.username = username
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_tries = max_tries
        self._sleep = sleep

    # ------------------------------------------------------------ 底层
    def _request(self, method: str, path: str, *, params=None, json_body=None, auth=True) -> Dict[str, Any]:
        headers = {"User-Agent": USER_AGENT}
        if auth and self.token:
            headers["Authorization"] = f"Token {self.token}"
        last = "未知错误"
        for attempt in range(1, self.max_tries + 1):
            try:
                r = self.session.request(method, self.base_url + path, params=params, json=json_body,
                                         headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                last = f"网络错误: {type(e).__name__}"
            else:
                wait = self._rate_wait(r)
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                elif r.status_code == 204:
                    return {}
                elif r.status_code >= 400:
                    raise LBError(self._error_text(r), r.status_code)
                else:
                    try:
                        data = r.json()
                    except ValueError:
                        last = "响应不是合法 JSON（空响应）"   # 实测偶发：200 但无响应体
                    else:
                        if wait:  # 额度用尽时在返回前就先等够，避免下一个请求撞限流
                            self._sleep(wait)
                        return data if isinstance(data, dict) else {"data": data}
                if wait:
                    self._sleep(wait)
                    continue
            if attempt < self.max_tries:
                self._sleep(2 ** attempt)
        raise LBError(f"ListenBrainz 请求失败（{last}）：{path}")

    @staticmethod
    def _rate_wait(r: requests.Response) -> float:
        """额度用尽（Remaining=0）或 429 时，按 Reset-In 等待，最多 15 秒。"""
        try:
            remaining = int(r.headers.get("X-RateLimit-Remaining", "1"))
            reset_in = float(r.headers.get("X-RateLimit-Reset-In", "0"))
        except ValueError:
            return 0.0
        if r.status_code == 429 or remaining <= 0:
            return min(max(reset_in, 1.0), 15.0)
        return 0.0

    @staticmethod
    def _error_text(r: requests.Response) -> str:
        try:
            body = r.json()
            msg = body.get("error") or body.get("message") or ""
        except ValueError:
            msg = ""
        return f"ListenBrainz 返回 HTTP {r.status_code}" + (f"：{msg}" if msg else "")

    # ------------------------------------------------------------ 接口
    def validate_token(self) -> Dict[str, Any]:
        """返回 {valid, user_name}。"""
        d = self._request("GET", "/1/validate-token")
        return {"valid": bool(d.get("valid")), "user_name": d.get("user_name") or ""}

    def created_for(self, count: int = 25) -> List[Dict[str, Any]]:
        """"为你生成"的歌单列表（只有歌单头，不含曲目）。新的在前。"""
        d = self._request("GET", f"/1/user/{self.username}/playlists/createdfor", params={"count": count})
        out = []
        for item in d.get("playlists", []):
            p = item.get("playlist", {})
            ext = (p.get("extension") or {}).get("https://musicbrainz.org/doc/jspf#playlist", {})
            meta = ext.get("additional_metadata", {})
            patch = (meta.get("algorithm_metadata") or {}).get("source_patch", "")
            out.append({
                "mbid": _mbid_from_identifier(p.get("identifier")),
                "title": p.get("title", ""),
                "date": p.get("date", ""),
                "expires_at": meta.get("expires_at", ""),
                "source_patch": patch,
                "type": classify(p.get("title", ""), patch),
            })
        return out

    def playlist(self, mbid: str) -> Dict[str, Any]:
        """歌单详情：{mbid, title, date, tracks:[{recording_mbid,title,artist,artists,album,duration_ms}]}。"""
        p = self._request("GET", f"/1/playlist/{mbid}").get("playlist", {})
        tracks = []
        for t in p.get("track", []):
            tm = (t.get("extension") or {}).get("https://musicbrainz.org/doc/jspf#track", {})
            arts = (tm.get("additional_metadata") or {}).get("artists") or []
            tracks.append({
                "recording_mbid": _mbid_from_identifier(t.get("identifier")),
                "title": t.get("title", ""),
                "artist": t.get("creator", ""),
                "artists": [{"name": a.get("artist_credit_name", ""), "mbid": a.get("artist_mbid", ""),
                             "join": a.get("join_phrase", "")} for a in arts],
                "album": t.get("album", "") or "",
                "duration_ms": t.get("duration") or None,
            })
        return {"mbid": mbid, "title": p.get("title", ""), "date": p.get("date", ""), "tracks": tracks}

    def lookup(self, artist: str, title: str) -> Optional[Dict[str, Any]]:
        """按歌手+歌名解析 recording MBID（繁简体输入都能命中同一条记录）。查不到返回 None。"""
        d = self._request("GET", "/1/metadata/lookup/", params={"artist_name": artist, "recording_name": title})
        if not d.get("recording_mbid"):
            return None
        return {"recording_mbid": d["recording_mbid"], "artist": d.get("artist_credit_name", ""),
                "title": d.get("recording_name", ""), "release": d.get("release_name", ""),
                "artist_mbids": d.get("artist_mbids", [])}

    def loved_mbids(self, page_size: int = 100) -> List[str]:
        """已提交的红心（score=1）recording MBID 全量。"""
        out, offset = [], 0
        while True:
            d = self._request("GET", f"/1/feedback/user/{self.username}/get-feedback",
                              params={"score": 1, "count": page_size, "offset": offset})
            rows = d.get("feedback", [])
            out += [r["recording_mbid"] for r in rows if r.get("recording_mbid")]
            offset += len(rows)
            if not rows or offset >= int(d.get("total_count", 0)):
                return out

    def love(self, recording_mbid: str) -> None:
        """提交红心。会真实改变推荐数据，仅在红心同步流程里调用。"""
        self._request("POST", "/1/feedback/recording-feedback",
                      json_body={"recording_mbid": recording_mbid, "score": 1})
