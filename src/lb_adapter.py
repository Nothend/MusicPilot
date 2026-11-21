"""网易云下载适配器：对推荐联动暴露 `download_song(artist, title, output_dir)`。

内部流程：搜索 → 按「歌手/歌名/版本/时长」选歌（复用 matcher）→ 现有 SongDownloader 取链接、下载、写标签
→ 校验（文件存在、大小>0、mutagen 可读、含 Artist+Title）→ 原子改名到 output_dir。
网易云下载的核心逻辑（取链接/写标签/原子下载）仍由现有 SongDownloader 负责，这里不改动它。

失败分类 error_kind：
  not_found     搜不到歌手+歌名都吻合的候选             → NOT_FOUND
  only_mp3      候选只有 MP3（库只要无损）或仅试听片段   → NOT_FOUND（原因 only_mp3）
  no_copyright  候选存在但无版权/下架/需购买/无链接      → NOT_FOUND
  rate_limited  搜索被限流（不是"不存在"）—— 调用方应暂停，不可记为 NOT_FOUND
  api / download / invalid   接口错误 / 下载失败 / 文件校验失败 → DOWNLOAD_FAILED（可重试）
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

import mutagen

from downloader import DownloadException, SongDownloader
from fsutil import safe_move
from http_client import APIException
from lb_enrich import Enricher, TagContext, run_enrichers
from lb_settings import STAGING_DIRNAME
from matcher import Target
from netease_search import SearchRateLimited, rank_candidates, search_songs

logger = logging.getLogger(__name__)

_UNAVAILABLE_KINDS = ("no_copyright", "vip_only", "need_purchase", "no_url")


@dataclass
class AdapterResult:
    success: bool
    path: Optional[str] = None
    error: str = ""
    error_kind: str = ""
    netease_id: Optional[int] = None
    quality: str = ""
    file_type: str = ""
    reused: bool = False          # 目标文件已存在且校验通过（上次中断前已落盘），直接复用
    enriched: Optional[List[str]] = None   # 实际修改了文件的元数据补全器名（见 lb_enrich.py）
    tried: int = 0                # 尝试过的候选数


def read_tags(path: Path) -> Tuple[str, str, float]:
    """返回 (artist, title, 时长秒)；读不了抛异常。"""
    f = mutagen.File(str(path), easy=True)
    if f is None:
        raise ValueError("无法识别的音频格式")
    def first(key: str) -> str:
        v = (f.tags or {}).get(key) if f.tags is not None else None
        return str(v[0]).strip() if v else ""
    length = float(getattr(f.info, "length", 0) or 0)
    return first("artist"), first("title"), length


def validate_audio(path: Path) -> Optional[str]:
    """校验成功返回 None，否则返回原因。要求：文件存在、大小>0、能读元数据、至少有 Artist+Title。"""
    path = Path(path)
    if not path.is_file():
        return "文件不存在"
    if path.stat().st_size <= 0:
        return "文件大小为 0"
    try:
        artist, title, length = read_tags(path)
    except Exception as e:
        return f"无法读取音频元数据: {e}"
    if not artist or not title:
        return "缺少 Artist 或 Title 标签"
    if length <= 0:
        return "音频时长为 0"
    return None


class NeteaseAdapter:
    def __init__(self, cookies: dict, downloader: SongDownloader, quality: str, *,
                 allow_mp3: bool = False, max_candidates: int = 3, request_delay: float = 0.0,
                 search: Callable = search_songs, sleep: Callable[[float], None] = time.sleep,
                 enrichers: Sequence[Enricher] = ()):
        """allow_mp3 仅用于测试（用非 VIP 账号验证下载链路）；线上恒为 False：库只要无损。"""
        self.cookies = cookies
        self.downloader = downloader
        self.quality = quality
        self.allow_mp3 = allow_mp3
        self.max_candidates = max_candidates
        self.request_delay = request_delay
        self._search = search
        self._sleep = sleep
        # 元数据补全器（默认没有：网易云下载器自己已写好完整标签）。扩展点说明见 lb_enrich.py
        self.enrichers = list(enrichers)

    def download_song(self, artist: str, title: str, output_dir, *, artists: Optional[List[str]] = None,
                      album: str = "", duration_ms: Optional[int] = None) -> AdapterResult:
        out_dir = Path(output_dir)
        target = Target(artist=artist, title=title, artists=list(artists or []),
                        duration_s=(duration_ms / 1000.0) if duration_ms else None)

        # 1) 搜索 + 选歌
        try:
            ranked = self._find(target, album)
        except SearchRateLimited as e:
            return AdapterResult(False, error=str(e), error_kind="rate_limited")
        except APIException as e:
            return AdapterResult(False, error=str(e), error_kind="api")
        if not ranked:
            return AdapterResult(False, error=f"网易云没有「{artist} - {title}」的吻合版本", error_kind="not_found")

        # 2) 依次尝试候选，取到可下载的无损链接为止
        info, only_mp3, unavailable, tried = None, False, "", 0
        for cand in ranked[: self.max_candidates]:
            tried += 1
            if tried > 1:
                self._sleep(self.request_delay)
            try:
                info = self.downloader.get_music_info(cand.id, self.quality, allow_mp3=self.allow_mp3)
            except DownloadException as e:
                if e.kind in _UNAVAILABLE_KINDS:
                    unavailable = str(e)
                    continue
                return AdapterResult(False, error=str(e), error_kind="api", netease_id=cand.id, tried=tried)
            if info is None:
                only_mp3 = True
                continue
            break
        if info is None:
            if only_mp3:
                return AdapterResult(False, error="只有 MP3 或试听片段，库只要无损",
                                     error_kind="only_mp3", tried=tried)
            return AdapterResult(False, error=unavailable or "候选均不可下载", error_kind="no_copyright", tried=tried)

        # 3) 下载到暂存目录（现有下载器：.part 原子写入 + 大小校验 + 写标签）
        staging = out_dir / STAGING_DIRNAME
        try:
            staging.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return AdapterResult(False, error=f"无法创建暂存目录: {e}", error_kind="download", netease_id=info.id, tried=tried)
        self.downloader.download_dir = staging
        staged = self.downloader._target_path(info)
        self._unlink(staged)          # 上次中断可能留下没写完标签的同名文件：一律重下
        res = self.downloader.download_song(info, self.quality)
        if not res.success:
            self._unlink(staged)
            return AdapterResult(False, error=res.error_message or "下载失败", error_kind="download",
                                 netease_id=info.id, tried=tried)
        staged = Path(res.file_path)

        # 4) 元数据补全（可选扩展点；必须在文件进入 Navidrome 之前完成，否则标签变化会让歌曲 id/红心失效）
        enriched: List[str] = []
        if self.enrichers:
            ctx = TagContext(source="netease", artist=artist, title=title, album=album,
                             artists=tuple(artists or ()), duration_s=target.duration_s)
            enriched, fatal = run_enrichers(staged, ctx, self.enrichers)
            if fatal:
                self._unlink(staged)
                return AdapterResult(False, error=fatal, error_kind="invalid", netease_id=info.id, tried=tried)

        # 4b) 校验（补全器弄坏文件也会在这里被拦下）
        why = validate_audio(staged)
        if why:
            self._unlink(staged)
            return AdapterResult(False, error=f"下载后校验失败：{why}", error_kind="invalid", netease_id=info.id, tried=tried)

        # 5) 原子落到 output_dir；目标已存在且合格则复用（幂等：上次已落盘但状态没来得及写库）
        final = out_dir / staged.name
        try:
            if final.exists():
                if validate_audio(final) is None:
                    self._unlink(staged)
                    return AdapterResult(True, str(final), netease_id=info.id, quality=info.quality,
                                         file_type=info.file_type, reused=True, tried=tried, enriched=enriched)
                self._unlink(staged)
                return AdapterResult(False, error=f"目标文件已存在但校验失败，未覆盖：{final.name}",
                                     error_kind="invalid", netease_id=info.id, tried=tried)
            safe_move(staged, final)
        except OSError as e:
            self._unlink(staged)
            return AdapterResult(False, error=f"移入目标目录失败: {e}", error_kind="download", netease_id=info.id, tried=tried)
        return AdapterResult(True, str(final), netease_id=info.id, quality=info.quality,
                             file_type=info.file_type, tried=tried, enriched=enriched)

    # ------------------------------------------------------------ 内部
    def _find(self, target: Target, album: str):
        first = (target.artists[0] if target.artists else target.artist) or ""
        ranked = rank_candidates(target, album, self._search(self.cookies, f"{first} {target.title}".strip(), 20))
        if ranked:
            return ranked
        self._sleep(self.request_delay)   # 兜底：只用歌名搜（歌手名在网易云里写法不同时）
        return rank_candidates(target, album, self._search(self.cookies, target.title, 30))

    @staticmethod
    def _unlink(p: Path) -> None:
        try:
            Path(p).unlink()
        except OSError:
            pass
