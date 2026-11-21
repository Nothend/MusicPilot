"""Navidrome（Subsonic / OpenSubsonic）API 客户端。

- 认证：token = md5(密码 + salt)，每次请求换新 salt，密码不出现在 URL；
- 日志与异常里不带完整 URL/参数；
- 实测要点（Navidrome 0.64.2）：search3 空查询可分页拉全库；返回的 `path` 不是真实磁盘路径
  （按标签拼出），不可用来定位文件；song id 由标签派生，移动文件后 id 与红心保持不变。
"""

import hashlib
import logging
import secrets
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

logger = logging.getLogger(__name__)

API_VERSION = "1.16.1"
CLIENT_NAME = "autoMusicDown"


class NavidromeError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class NavidromeClient:
    def __init__(self, url: str, user: str, password: str,
                 session: Optional[requests.Session] = None, timeout: float = 30, sleep=time.sleep):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sleep = sleep

    def _call(self, endpoint: str, **params) -> Dict[str, Any]:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        q: Dict[str, Any] = {"u": self.user, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT_NAME, "f": "json"}
        q.update({k: v for k, v in params.items() if v is not None})
        try:
            r = self.session.get(f"{self.url}/rest/{endpoint}", params=q, timeout=self.timeout)
        except requests.RequestException as e:
            raise NavidromeError(f"无法连接 Navidrome（{type(e).__name__}）")
        if r.status_code != 200:
            raise NavidromeError(f"Navidrome 返回 HTTP {r.status_code}")
        try:
            resp = r.json()["subsonic-response"]
        except (ValueError, KeyError):
            raise NavidromeError("Navidrome 响应格式异常（地址是否填成了其他服务？）")
        if resp.get("status") != "ok":
            err = resp.get("error", {})
            raise NavidromeError(err.get("message", "未知错误"), err.get("code"))
        return resp

    # ------------------------------------------------------------ 基础
    def ping(self) -> Dict[str, Any]:
        r = self._call("ping")
        return {"version": r.get("serverVersion", ""), "type": r.get("type", ""),
                "open_subsonic": bool(r.get("openSubsonic"))}

    def music_folders(self) -> List[Dict[str, Any]]:
        r = self._call("getMusicFolders")
        return [{"id": f["id"], "name": f.get("name", "")} for f in r.get("musicFolders", {}).get("musicFolder", [])]

    # ------------------------------------------------------------ 扫描
    def start_scan(self) -> None:
        self._call("startScan")

    def scan_status(self) -> Dict[str, Any]:
        s = self._call("getScanStatus").get("scanStatus", {})
        return {"scanning": bool(s.get("scanning")), "count": s.get("count", 0), "last_scan": s.get("lastScan", "")}

    def scan_and_wait(self, timeout: float = 600, poll: float = 2.0) -> Dict[str, Any]:
        """触发扫描并等待结束。startScan 返回后状态可能尚未翻转，先短暂等待再轮询。"""
        self.start_scan()
        self._sleep(min(3.0, poll * 1.5))
        deadline = time.monotonic() + timeout
        while True:
            st = self.scan_status()
            if not st["scanning"]:
                return st
            if time.monotonic() > deadline:
                raise NavidromeError("等待 Navidrome 扫描超时")
            self._sleep(poll)

    # ------------------------------------------------------------ 曲库
    @staticmethod
    def _song(x: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": x["id"], "title": x.get("title", ""), "artist": x.get("artist", ""),
                "album": x.get("album", ""), "album_artist": x.get("albumArtist", "") or x.get("artist", ""),
                "duration": x.get("duration"), "size": x.get("size"), "suffix": x.get("suffix", ""),
                "mbid": x.get("musicBrainzId", "") or "", "starred": bool(x.get("starred")),
                "path_api": x.get("path", "")}  # 仅供展示：不是真实磁盘路径

    def iter_songs(self, folder_id: Optional[int] = None, page: int = 500) -> Iterator[Dict[str, Any]]:
        """分页遍历某个库的全部歌曲（空查询）。"""
        offset = 0
        while True:
            r = self._call("search3", query="", songCount=page, songOffset=offset,
                           artistCount=0, albumCount=0, musicFolderId=folder_id)
            rows = r.get("searchResult3", {}).get("song", [])
            for x in rows:
                yield self._song(x)
            if len(rows) < page:
                return
            offset += page

    def starred_songs(self, folder_id: Optional[int] = None) -> List[Dict[str, Any]]:
        r = self._call("getStarred2", musicFolderId=folder_id)
        return [self._song(x) for x in r.get("starred2", {}).get("song", [])]

    def star(self, song_id: str) -> None:
        self._call("star", id=song_id)

    def unstar(self, song_id: str) -> None:
        self._call("unstar", id=song_id)

    # ------------------------------------------------------------ 歌单（按歌曲 id，不依赖磁盘路径）
    def playlists(self) -> List[Dict[str, Any]]:
        r = self._call("getPlaylists")
        return [{"id": p["id"], "name": p.get("name", ""), "count": p.get("songCount", 0)}
                for p in r.get("playlists", {}).get("playlist", [])]

    def create_playlist(self, name: str, song_ids: List[str]) -> str:
        r = self._call("createPlaylist", name=name, songId=song_ids or None)
        return r.get("playlist", {}).get("id", "")

    def delete_playlist(self, playlist_id: str) -> None:
        self._call("deletePlaylist", id=playlist_id)
