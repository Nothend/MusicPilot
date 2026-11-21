"""试听库/主库的本地文件辅助：读标签、按 MoviePilot 的命名规则拼主库路径、在试听库里按标签找文件。

命名规则（与用户现有库一致，来自 MoviePilot 的重命名模板，并按真实库核对过）：
    {album_artist or artist or 'Unknown Artist'}/{album or 'Unknown Album'}[ (年份)][/Disc N（仅多碟）]/
        [曲目号 - ]{artist or album_artist or 'Unknown Artist'} - {title}.ext
- 多歌手的连接符可配置（设置 naming.artist_separator），默认「, 」——音乐库社区里最通行的写法，
  也和用户库里较早期的目录一致（`P!nk, Nate Ruess`）。MoviePilot 现在的写法是「 / 」再换成全角
  （`王朝1982 ／ 龚琳娜`），把连接符设成「 / 」即可精确复现（已用真实库 25 个样本核对 25/25 一致）；
- 曲目号补零到两位（`03 - 戴荃 - 白沙之恋.flac`），没有曲目号就不带前缀；
- 名字里的非法字符换成**全角**字符（真实库里是 `Beyoncé ／ JAŸ-Z`），去掉末尾的点和空格
  （群晖遇到以点结尾的目录名会生成 `_DiskStation_..._TailCharacterConflict` 冲突目录）。
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import mutagen

from matcher import LocalFile, LocalIndex, Target, match

logger = logging.getLogger(__name__)

AUDIO_EXTS = (".flac", ".mp3", ".m4a")
_FULLWIDTH = {"/": "／", "\\": "＼", ":": "：", "*": "＊", "?": "？", '"': "＂", "<": "＜", ">": "＞", "|": "｜"}
_MAX_COMPONENT = 150


@dataclass
class FileTags:
    title: str = ""
    artists: List[str] = field(default_factory=list)
    album_artist: str = ""
    album: str = ""
    year: str = ""
    track: int = 0
    disc: int = 0
    total_discs: int = 0

    @property
    def artist(self) -> str:
        """通用展示/匹配用的连接（匹配器会再按分隔符拆开，所以连接符不影响匹配）。"""
        return ", ".join(self.artists)

    def artists_joined(self, sep: str) -> str:
        return sep.join(self.artists)


def _first(tags, key: str) -> str:
    v = tags.get(key) if tags is not None else None
    return str(v[0]).strip() if v else ""


def _num(s: str) -> int:
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else 0


def read_file_tags(path: Path) -> FileTags:
    f = mutagen.File(str(path), easy=True)
    if f is None:
        raise ValueError("无法识别的音频格式")
    t = f.tags
    artists = [str(a).strip() for a in (t.get("artist") if t is not None else None) or [] if str(a).strip()]
    m = re.search(r"\d{4}", _first(t, "date") or _first(t, "year"))
    disc_raw, total = _first(t, "discnumber"), _first(t, "totaldiscs") or _first(t, "disctotal")
    total_discs = _num(total) or (_num(disc_raw.split("/")[1]) if "/" in disc_raw else 0)
    return FileTags(title=_first(t, "title"), artists=artists, album_artist=_first(t, "albumartist"),
                    album=_first(t, "album"), year=m.group(0) if m else "", track=_num(_first(t, "tracknumber")),
                    disc=_num(disc_raw), total_discs=total_discs)


def safe_component(name: str) -> str:
    """路径的一个层级：非法字符换全角、空白压成单个空格、去掉其余控制字符，
    去掉首尾空白与首尾的点（前导点会让 Navidrome 忽略该目录，末尾点会让群晖产生冲突目录），限长。
    永远不会产生 '..'、空名或绝对路径。"""
    s = "".join(_FULLWIDTH.get(c, c) for c in name or "")
    s = re.sub(r"\s+", " ", s)                 # 先把 \t \n 等空白统一成空格
    s = re.sub(r"[\x00-\x1f\x7f]", "", s)      # 再去掉剩下的控制字符
    s = s.strip(". ")
    s = s[:_MAX_COMPONENT].rstrip(". ")
    return s or "_"


DEFAULT_ARTIST_SEP = ", "


def main_relpath(tags: FileTags, ext: str, artist_sep: str = DEFAULT_ARTIST_SEP) -> Path:
    """按命名规则得到相对主库根目录的路径。artist_sep 是多歌手的连接符（见模块说明）。"""
    artists = tags.artists_joined(artist_sep)
    artist_dir = tags.album_artist or artists or "Unknown Artist"
    album_dir = (tags.album or "Unknown Album") + (f" ({tags.year})" if tags.year else "")
    parts = [safe_component(artist_dir), safe_component(album_dir)]
    if tags.total_discs > 1:
        parts.append(f"Disc {tags.disc or 1}")
    name = (f"{tags.track:02d} - " if tags.track > 0 else "") + \
        f"{artists or tags.album_artist or 'Unknown Artist'} - {tags.title or 'Unknown Title'}"
    parts.append(safe_component(name) + ext.lower())
    return Path(*parts)


def inside(child: Path, root: Path) -> bool:
    """child 是否确实位于 root 之内（解析符号链接与 .. 之后）。所有写主库的操作前都要过这一关。"""
    try:
        Path(child).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def iter_audio_files(root: Path):
    """遍历目录下的音频文件，跳过以 . 开头的目录（.staging / .trash，Navidrome 也忽略它们）。"""
    root = Path(root)
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = sorted(cur.iterdir())
        except OSError:
            continue
        for p in entries:
            if p.name.startswith("."):
                continue
            if p.is_dir():
                stack.append(p)
            elif p.suffix.lower() in AUDIO_EXTS and p.is_file():
                yield p


def find_file_by_tags(root: Path, artist: str, title: str, artists: Optional[List[str]] = None,
                      duration_s: Optional[float] = None) -> Optional[Path]:
    """在目录里按标签找「就是这首歌」的唯一文件（复用 matcher 的规则）；找不到或不唯一返回 None。

    用于试听库里不是我们自己下载的文件（没有记录真实路径）——Navidrome API 的 path 不可信，只能反查磁盘。
    """
    files, by_id = [], {}
    for p in iter_audio_files(root):
        try:
            t = read_file_tags(p)
        except Exception:
            continue
        sid = str(p)
        by_id[sid] = p
        files.append(LocalFile(nd_song_id=sid, library="discovery", artist=t.artist, title=t.title,
                               album=t.album, album_artist=t.album_artist, size=p.stat().st_size))
    r = match(LocalIndex(files), Target(artist=artist, title=title, artists=list(artists or []), duration_s=None),
              libraries=("discovery",))
    return by_id[r.chosen.nd_song_id] if r.status == "discovery" and r.chosen else None
