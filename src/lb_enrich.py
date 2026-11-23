"""元数据补全（刮削）扩展点：给将来的其它下载渠道预留。

背景：音乐平台下载器自己已经写好了完整标签（歌手/歌名/专辑/年份/歌词/封面），所以目前没有任何补全器在用。
以后若接入的下载渠道产出的文件缺标签，就实现一个 `Enricher`，传给对应渠道的适配器即可，无需改流水线。

时机（重要）：补全只发生在**下载后、校验前**，对象是暂存目录里的文件。不要在文件进入 Navidrome 之后再改标签：
Navidrome 的歌曲 id 由标签派生，入库并被标红心之后再改标签会让 id 变化、红心丢失。

约定：
- 只能就地修改传入的暂存文件（不要改名、不要写到别处）；
- `required=False`（默认）的补全器失败只记警告，下载照常继续；`required=True` 失败则本次下载判为失败，暂存文件会被清理；
- 补全之后适配器仍会做统一校验（文件存在、大小>0、可读元数据、含 Artist+Title），所以补全器弄坏文件会被拦下；
- 补全器应当幂等：重复执行不会产生不同结果。

下载渠道本身的接口见 `DownloadChannel`：任何渠道只要实现 `download_song(...) -> AdapterResult` 就能被流水线使用。
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TagContext:
    """补全器可参考的「这首歌应该是什么」：来自 ListenBrainz 推荐（比文件本身的标签更可信）。"""
    source: str                                   # 下载渠道名，如 "platform"
    artist: str
    title: str
    album: str = ""
    artists: Tuple[str, ...] = ()
    duration_s: Optional[float] = None
    recording_mbid: str = ""


@dataclass
class EnrichResult:
    ok: bool
    changed: bool = False
    note: str = ""


class Enricher(ABC):
    name: str = "enricher"
    required: bool = False

    def applies(self, ctx: TagContext, path: Path) -> bool:
        """默认对所有文件生效；可按渠道/格式/是否缺标签等条件跳过。"""
        return True

    @abstractmethod
    def enrich(self, path: Path, ctx: TagContext) -> EnrichResult:
        """就地补全 path 的元数据。可以抛异常（会被捕获并按失败处理）。"""


def run_enrichers(path: Path, ctx: TagContext, enrichers: Sequence[Enricher],
                  log: Optional[Callable[[str, str], None]] = None) -> Tuple[List[str], Optional[str]]:
    """依次运行补全器。返回 (确实修改了文件的补全器名列表, 致命错误或 None)。

    致命错误 = 某个 required 补全器失败，或补全器把文件弄没了；出现时立即停止后续补全器。
    """
    emit = log or (lambda level, msg: logger.log(getattr(logging, level, logging.INFO), msg))
    applied: List[str] = []
    for e in enrichers:
        try:
            if not e.applies(ctx, path):
                continue
            res = e.enrich(path, ctx)
        except Exception as ex:                      # 补全器的任何异常都按「失败」处理，不拖垮任务
            res = EnrichResult(False, note=f"{type(ex).__name__}: {ex}")
        if not Path(path).is_file():
            return applied, f"补全器 {e.name} 之后文件不见了"
        if res.ok:
            if res.changed:
                applied.append(e.name)
                emit("INFO", f"元数据已补全（{e.name}）" + (f"：{res.note}" if res.note else ""))
            continue
        if e.required:
            return applied, f"必需的元数据补全器 {e.name} 失败：{res.note}"
        emit("WARNING", f"元数据补全失败（{e.name}，已忽略）：{res.note}")
    return applied, None


@runtime_checkable
class DownloadChannel(Protocol):
    """下载渠道：流水线只依赖这个形状。音乐平台适配器即其一；将来新增渠道实现同样的方法即可。"""

    def download_song(self, artist: str, title: str, output_dir, *, artists=None, album: str = "",
                      duration_ms: Optional[int] = None):
        """成功/失败都返回 lb_adapter.AdapterResult（error_kind 取值见 lb_adapter 模块文档）。"""
