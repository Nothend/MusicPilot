"""仅下载模式（不与 Navidrome 对接）的目录去重：读下载目录里已有音频的标签，建一个与 Navidrome 索引同形的 LocalIndex，
复用 matcher 的全部规则（繁简、版本词、括号、featuring ……），所以「已有」的判断和联动模式一样准。

只读目录；跳过以 . 开头的目录（.staging 等）。读不出标签的文件忽略（不会因此重复下载以外的任何后果）。
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

import mutagen

from lb_localfs import iter_audio_files
from matcher import LocalFile, LocalIndex

logger = logging.getLogger(__name__)
FOLDER_LIBRARY = "main"          # 借用 matcher 里的「主库」槽位：目录里已有的 = 已有


def read_local_file(path: Path) -> Optional[LocalFile]:
    try:
        f = mutagen.File(str(path), easy=True)
        if f is None or f.tags is None:
            return None
        t = f.tags

        def first(k: str) -> str:
            v = t.get(k)
            return str(v[0]).strip() if v else ""
        artists = [str(a).strip() for a in (t.get("artist") or []) if str(a).strip()]
        title = first("title")
        if not title or not artists:
            return None
        length = getattr(getattr(f, "info", None), "length", None)
        return LocalFile(nd_song_id=str(path), library=FOLDER_LIBRARY, artist=", ".join(artists), title=title,
                         album=first("album"), album_artist=first("albumartist"),
                         duration=int(round(length)) if length else None, size=path.stat().st_size)
    except Exception as e:                       # 坏文件/不支持的格式：忽略
        logger.debug(f"读取标签失败 {path}: {e}")
        return None


def build_index(root: str) -> Tuple[LocalIndex, int]:
    files = []
    for p in iter_audio_files(Path(root)):
        lf = read_local_file(p)
        if lf is not None:
            files.append(lf)
    return LocalIndex(files), len(files)
