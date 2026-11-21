"""网易云歌曲搜索 + 选歌（新增模块，不改 netease.py）。

搜索接口 `cloudsearch/pc` 是普通表单 POST（无需 eapi 签名），与现有取歌单/歌曲详情同一路数，
复用共享 SESSION（带重试退避与设备 cookie）。

选歌规则与「本地查重」完全一致——直接复用 matcher.match：歌手有交集、歌名标准化后相同、
版本标记（Live/Remix/…）一致、时长相差不超过容差。同一首歌常在多张专辑里重复出现，
候选之间的先后用「专辑是否吻合 → 时长最接近 → 搜索相关度」打破平局。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from constants import APIConstants
from http_client import SESSION, APIException, netease_headers
from matcher import LocalFile, LocalIndex, Target, compact, fold, match

logger = logging.getLogger(__name__)

# 搜索接口限流比其他接口严格（实测按 0.4 秒间隔连续搜会返回 405「操作频繁」）：
# 遇到这类 code 要退避重试，绝不能当成"没搜到"（否则会把歌误记为 NOT_FOUND 并进入冷却）
RATE_LIMIT_CODES = (405, -460, 460)


class SearchRateLimited(APIException):
    """搜索被限流（重试后仍被拒）。调用方应暂停一段时间，而不是判定歌曲不存在。"""


@dataclass
class Candidate:
    id: int
    name: str
    artists: List[str]
    album: str
    duration_s: Optional[int]
    fee: int = 0
    alias: List[str] = field(default_factory=list)
    rank: int = 0                      # 在搜索结果里的先后（相关度）


def search_songs(cookies: Dict[str, str], keyword: str, limit: int = 20,
                 session: requests.Session = None, retries: int = 2, backoff: float = 10.0,
                 sleep=time.sleep) -> List[Candidate]:
    """按关键词搜索单曲。被限流时按 backoff×次数 退避重试，仍被拒则抛 SearchRateLimited；其余失败抛 APIException。"""
    for attempt in range(retries + 1):
        try:
            return _search_once(cookies, keyword, limit, session or SESSION)
        except SearchRateLimited:
            if attempt >= retries:
                raise
            wait = backoff * (attempt + 1)
            logger.warning(f"搜索被限流，{wait:.0f} 秒后重试（{attempt + 1}/{retries}）")
            sleep(wait)
    raise AssertionError("unreachable")


def _search_once(cookies: Dict[str, str], keyword: str, limit: int, session: requests.Session) -> List[Candidate]:
    try:
        r = session.post(APIConstants.SEARCH_API,
                         data={"s": keyword, "type": 1, "limit": limit, "offset": 0, "total": "true"},
                         headers=netease_headers(),
                         cookies={**APIConstants.DEFAULT_COOKIES, **(cookies or {})}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        raise APIException(f"搜索请求失败: {type(e).__name__}")
    except ValueError:
        raise APIException("搜索响应不是合法 JSON")
    if data.get("code") in RATE_LIMIT_CODES:
        raise SearchRateLimited(f"搜索被限流: code={data.get('code')} {data.get('message', '')}".strip())
    if data.get("code") != 200:
        raise APIException(f"搜索失败: code={data.get('code')} {data.get('message', '')}".strip())
    out: List[Candidate] = []
    for i, s in enumerate((data.get("result") or {}).get("songs") or []):
        dt = s.get("dt")
        out.append(Candidate(
            id=s["id"], name=s.get("name", ""), artists=[a.get("name", "") for a in s.get("ar") or []],
            album=(s.get("al") or {}).get("name", "") or "", duration_s=round(dt / 1000) if dt else None,
            fee=s.get("fee", 0) or 0, alias=list(s.get("alia") or []), rank=i))
    return out


def rank_candidates(target: Target, album: str, cands: List[Candidate]) -> List[Candidate]:
    """用 matcher 过滤并排序：只保留与目标歌手/歌名/版本/时长相符的候选，最优在前。

    返回空列表 = 没有可接受的候选（歌手或歌名对不上、版本不同、或时长相差过大）。
    """
    if not cands:
        return []
    by_id = {str(c.id): c for c in cands}
    files = [LocalFile(nd_song_id=str(c.id), library="main", artist=" / ".join(c.artists), title=c.name,
                       duration=c.duration_s, size=None) for c in cands]
    r = match(LocalIndex(files), target, lenient=False)
    if r.status == "none":
        return []
    chosen_ids = [f.nd_song_id for f in r.candidates] or [r.chosen.nd_song_id]
    picked = [by_id[i] for i in chosen_ids]
    want_album = compact(fold(album)) if album else ""

    def key(c: Candidate):
        album_ok = 0 if (want_album and compact(fold(c.album)) == want_album) else 1
        d = abs(c.duration_s - target.duration_s) if (c.duration_s and target.duration_s) else 1e9
        return (album_ok, d, c.rank)
    return sorted(picked, key=key)
