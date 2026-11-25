"""中文文本统一成简体：ListenBrainz / MusicBrainz 里的中文歌手、歌名、专辑多是繁体，写进文件标签前要转成简体。

只转纯中文：文本里含日文假名或韩文时原样返回——日文汉字（如「國」「體」的日文写法）转成简体会写错。
"""

import re

from matcher import _t2s

_KANA_HANGUL = re.compile(r"[぀-ヿㇰ-ㇿ가-힯ᄀ-ᇿ]")


def simplify(text: str) -> str:
    if not text or _KANA_HANGUL.search(text):
        return text
    return _t2s(text)
