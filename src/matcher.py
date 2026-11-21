"""本地曲库匹配：判断「一首推荐歌曲」是否已在主库/试听库里。纯逻辑，不碰数据库和网络。

匹配优先级（见 DESIGN-LB.md）：
  1. MBID（recording MBID）            —— 权威，不做时长校验
  2. exact      歌手+歌名 原样相同
  3. normalized 标准化后相同（NFKC / 大小写 / 繁简 / 标点空格 / 去 feat.）
  4. fuzzy      标准化后高度相似（rapidfuzz，短标题不参与）
  5. bracket    仅差「括号里的附加说明」，且双方时长都已知并吻合（≤5 秒）才接受
所有名字级匹配都要求：歌手有交集、版本标记集合相同（Live/Remix/Acoustic/Instrumental/Edit/
Remastered 等不当作同一录音，不会被"标准化"抹掉）。时长作辅助：双方都已知且相差过大则视为不同录音。
主库优先于试听库：主库有任何命中就不再看试听库。

名词：
- 「同一录音的重复文件」（如单曲+专辑里的同一首）：标题相同且时长一致 → 取一个即可，不算歧义；
- MULTIPLE_MATCH：同级候选的标题/时长对不上，无法判断是否同一录音 → 交给人工选择。
"""

import functools
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from opencc import OpenCC
from rapidfuzz import fuzz

DURATION_TOLERANCE_S = 20     # 与目标时长相差超过它，视为不同录音
SAME_RECORDING_S = 5          # 无目标时长时，候选之间相差不超过它，视为同一录音的重复文件
FUZZY_THRESHOLD = 92
FUZZY_MIN_LEN = 5             # 标准化后短于此的标题只接受完全相同，避免误判
ALBUM_DURATION_TOLERANCE_S = 60   # 歌手+歌名+专辑都相同（版本词也相同）时，时长容差放宽到它：同一首歌的不同母带/静音长度
SOFT_DURATION_TOLERANCE_S = 45  # 「软版本词」（Radio Edit 等）会比原版短不少，时长容差放宽到它
CHARVAR_MIN_LEN = 4           # 异体字（单字之差）规则只用于不短于此的标题
CHARVAR_MAX_DUR_S = 2         # 且双方时长必须都已知并几乎相同

_T2S = OpenCC("t2s")
RANK = {"bracket": 0, "charvar": 1, "fuzzy": 2, "variant": 3, "normalized": 4, "exact": 5, "mbid": 6}


# ---------------------------------------------------------------- 标准化
@functools.lru_cache(maxsize=100_000)
def _t2s(s: str) -> str:
    return _T2S.convert(s)


def _strip_latin_accents(s: str) -> str:
    """去掉拉丁字母上的重音符号（Grégoire → Gregoire）：本地库里西文名字常不带重音，而 MusicBrainz 带。
    只处理「紧跟在 ASCII 字母后面」的组合记号；日文假名的浊点/半浊点（が = か + ゙）、韩文等不受影响。"""
    out, prev_ascii_alpha = [], False
    for ch in unicodedata.normalize("NFKD", s):
        if unicodedata.category(ch) == "Mn" and prev_ascii_alpha:
            continue
        out.append(ch)
        prev_ascii_alpha = ch.isascii() and ch.isalpha()
    return unicodedata.normalize("NFKC", "".join(out))


# OpenCC 繁→简之后仍不统一的常见异体字 → 统一写法（只收录确定是同字异形的；其余由 charvar 规则兜底）
_VARIANT_CHARS = str.maketrans({"栢": "柏", "阕": "阙", "峯": "峰", "羣": "群", "牀": "床", "麽": "么"})


@functools.lru_cache(maxsize=100_000)
def fold(s: str) -> str:
    """NFKC（全角→半角）→ 小写 → 去拉丁重音 → 繁体转简体 → 异体字归一。保留标点，供版本词/feat 检测使用。"""
    t = _t2s(_strip_latin_accents(unicodedata.normalize("NFKC", s or "").casefold().strip()))
    return t.translate(_VARIANT_CHARS)


def compact(s: str) -> str:
    """只留字母数字（含汉字）：抹平空格与标点差异。"""
    return "".join(ch for ch in s if ch.isalnum())


_FEAT_BRACKET = re.compile(r"[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\b[^)\]]*[)\]]")
_FEAT_TAIL = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s.*$")


_BRACKETS = re.compile(r"[(\[][^)\]]*[)\]]")


def strip_brackets(folded_title: str) -> str:
    """去掉全部括号及其内容（MusicBrainz 标题常带长段说明，如「奢香夫人 (……歌曲)」）。"""
    return _BRACKETS.sub(" ", folded_title).strip()


def strip_feat(folded_title: str) -> str:
    t = _FEAT_BRACKET.sub(" ", folded_title)
    return _FEAT_TAIL.sub("", t).strip()


# 「软版本词」：只是同一首歌的发行/剪辑差异（电台版、单曲版、专辑版、脏/净版），按「同一首已有」处理。
# 与 live/remix 等「硬版本词」相反：硬的必须两边一致，软的在 lenient 模式下直接忽略。
_SOFT = re.compile(
    r"\s*(?:[(\[]\s*|-\s+)(?:radio\s+(?:edit|version|mix)|single\s+(?:version|edit|mix)|album\s+(?:version|edit|mix)"
    r"|explicit(?:\s+version)?|clean(?:\s+version)?|(?:mono|stereo|original)\s+(?:version|mix)|bonus\s+track|deluxe(?:\s+(?:version|edition))?|电台版|单曲版|专辑版|原版)\s*[)\]]?")


def strip_soft(folded_title: str) -> str:
    return _SOFT.sub("", folded_title).strip()


def is_soft_edition(title: str) -> bool:
    """标题是否带软版本词（Radio Edit / 单曲版 等）。"""
    f = fold(title)
    return strip_soft(f) != f


# 版本标记：标准化时不抹掉；两边集合不同则视为不同录音
def _w(pattern: str) -> str:
    """英文单词边界只看 ASCII 字母数字：汉字紧贴英文（如「Live版」）时 \b 不成立，会漏掉版本词。"""
    return rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"


_TAGS: List[Tuple[str, "re.Pattern[str]"]] = [(k, re.compile(p)) for k, p in [
    ("live", _w("live") + "|现场|演唱会"),
    ("remix", _w(r"remix(?:ed)?") + "|混音|重混"),
    ("acoustic", _w("acoustic") + "|不插电|原声版"),
    ("instrumental", _w("instrumental") + "|伴奏|纯音乐"),
    ("edit", _w("edit")),
    ("remaster", _w(r"remaster(?:ed)?") + "|重制"),
    ("demo", _w("demo")),
    ("voicememo", r"voice\s*memos?"),
    ("karaoke", r"karaoke|卡拉ok"),
    # 不同语言/编曲的版本：括号说明里出现这些词时，不能当作"仅括号说明不同"
    ("version", _w("version") + "|" + _w(r"ver\.?") + "|国语版|粤语版|日语版|英文版|dj版|钢琴版|女版|男版"),
    ("cover", _w("cover") + "|翻唱"),
]]


def version_tags(title: str) -> FrozenSet[str]:
    t = fold(title)
    return frozenset(k for k, rx in _TAGS if rx.search(t))


_ARTIST_SPLIT = re.compile(r"\s*(?:&|/|,|;|、|\+|＋|•)\s*|\s+(?:feat\.?|ft\.?|featuring|and|x|vs\.?)\s+")


_CJK = "\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af"
# 「F.I.R 飞儿乐团」「Jay Chou 周杰伦」「F.I.R.飞儿乐团」：拉丁名与中日韩名相接（空白可有可无）→ 视为同一歌手的两种写法（别名）
_SCRIPT_GAP = re.compile(rf"(?<=[a-z0-9.!?])\s*(?=[{_CJK}])|(?<=[{_CJK}])\s*(?=[a-z0-9])")


@functools.lru_cache(maxsize=100_000)
def artist_set(name: str) -> FrozenSet[str]:
    """把「A & B」「A/B」「A feat. B」拆成歌手集合（另含整串），元素是 compact 后的名字。
    「拉丁名 + 中日韩名」（F.I.R 飞儿乐团）会再拆成两个别名，使 F.I.R 能命中它。"""
    f = fold(name)
    parts = set()
    for p in _ARTIST_SPLIT.split(f):
        parts.add(compact(p))
        if _SCRIPT_GAP.search(p):
            parts.update(c for c in (compact(x) for x in _SCRIPT_GAP.split(p)) if len(c) >= 2)   # 单字母别名太容易撞名
    parts.add(compact(f))
    parts.discard("")
    return frozenset(parts)


def artist_key(name: str) -> str:
    """缓存/去重用的歌手键（整串 compact）。"""
    return compact(fold(name))


def title_key(title: str) -> str:
    return compact(strip_feat(fold(title)))


# ---------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class LocalFile:
    nd_song_id: str
    library: str                 # main | discovery
    artist: str = ""
    title: str = ""
    album: str = ""
    album_artist: str = ""
    duration: Optional[int] = None
    mbid: str = ""
    starred: bool = False
    size: Optional[int] = None


@dataclass
class Target:
    """要查找的歌：来自 ListenBrainz 歌单。"""
    artist: str
    title: str
    artists: List[str] = field(default_factory=list)   # 歌单里的歌手拆分（artist_credit_name），可空
    mbid: str = ""
    duration_s: Optional[float] = None
    album: str = ""


@dataclass
class MatchResult:
    status: str                                   # none | main | discovery | multiple
    level: str = ""                               # mbid | exact | normalized | fuzzy
    chosen: Optional[LocalFile] = None
    candidates: List[LocalFile] = field(default_factory=list)   # multiple 时供人工选择；其余为同一录音的全部文件
    near_misses: List[LocalFile] = field(default_factory=list)  # 歌手+歌名吻合但时长相差过大（仅供日志）
    note: str = ""


# ---------------------------------------------------------------- 索引
class LocalIndex:
    def __init__(self, files: List[LocalFile]):
        self.files = list(files)
        self.by_mbid: Dict[str, List[LocalFile]] = {}
        self.by_artist: Dict[str, List[LocalFile]] = {}
        for f in self.files:
            if f.mbid:
                self.by_mbid.setdefault(f.mbid.lower(), []).append(f)
            for a in artist_set(f.artist) | artist_set(f.album_artist):
                self.by_artist.setdefault(a, []).append(f)

    def candidates_for(self, names: FrozenSet[str], library: str) -> List[LocalFile]:
        seen, out = set(), []
        for a in names:
            for f in self.by_artist.get(a, ()):
                if f.library == library and f.nd_song_id not in seen:
                    seen.add(f.nd_song_id)
                    out.append(f)
        return out


# ---------------------------------------------------------------- 匹配
def _target_artists(t: Target) -> FrozenSet[str]:
    names = set(artist_set(t.artist))
    for a in t.artists:
        names |= artist_set(a)
    return frozenset(names)


def _bracket_key(title: str) -> str:
    return compact(strip_brackets(strip_feat(fold(title))))


def _is_cjk(ch: str) -> bool:
    return "\u3400" <= ch <= "\u9fff"


def _one_char_variant(a: str, b: str) -> bool:
    """两个 compact 标题等长、恰有一个字不同，且这个字在两边都是汉字（疑似异体字/错别字）。"""
    if len(a) != len(b) or len(a) < CHARVAR_MIN_LEN:
        return False
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diff) == 1 and _is_cjk(diff[0][0]) and _is_cjk(diff[0][1])


def _title_level(t: Target, f: LocalFile, lenient: bool = True) -> Optional[str]:
    """lenient=True（查本地库）：Radio Edit/单曲版/专辑版/Explicit 等软版本词不算不同录音。
    lenient=False（挑网易云搜索结果）：软版本词也必须一致，免得用电台剪辑版顶替原版。"""
    if t.artist.strip() == f.artist.strip() and t.title.strip() == f.title.strip():
        return "exact"
    a, b = title_key(t.title), title_key(f.title)
    if lenient:
        ts, fs = strip_soft(fold(t.title)), strip_soft(fold(f.title))
        soft = ts != fold(t.title) or fs != fold(f.title)
    else:
        ts, fs, soft = fold(t.title), fold(f.title), False
    if lenient and soft:
        if version_tags(ts) != version_tags(fs):
            return None
        sa, sb = compact(strip_feat(ts)), compact(strip_feat(fs))
        return "variant" if sa and sa == sb else None
    if version_tags(t.title) != version_tags(f.title):
        return None
    if not a or not b:
        return None
    if a == b:
        return "normalized"
    if min(len(a), len(b)) >= FUZZY_MIN_LEN and fuzz.ratio(a, b) >= FUZZY_THRESHOLD:
        return "fuzzy"
    d = _dur_diff(t, f)
    # 异体字：标题只差一个汉字，必须有时长佐证（≤2 秒）
    if d is not None and d <= CHARVAR_MAX_DUR_S and _one_char_variant(a, b):
        return "charvar"
    # 仅差括号说明：必须有时长佐证（无佐证时宁可不匹配，也不把不同录音当同一首）
    if d is not None and d <= SAME_RECORDING_S:
        ba, bb = _bracket_key(t.title), _bracket_key(f.title)
        if ba and ba == bb and len(ba) >= 2:
            return "bracket"
    return None


def same_album(a: str, b: str) -> bool:
    """专辑名标准化后相同（都非空）。"""
    ca, cb = compact(fold(a)), compact(fold(b))
    return bool(ca) and ca == cb


def _dur_diff(t: Target, f: LocalFile) -> Optional[float]:
    if t.duration_s is None or f.duration is None:
        return None
    return abs(f.duration - t.duration_s)


def _order_key(t: Target, f: LocalFile):
    d = _dur_diff(t, f)
    return (d if d is not None else 1e9, -(f.size or 0), f.nd_song_id)


def _resolve(t: Target, level: str, cands: List[LocalFile], lib: str) -> MatchResult:
    """同级候选 → 唯一结果或 multiple。"""
    cands = sorted(cands, key=lambda f: _order_key(t, f))
    if len(cands) == 1:
        return MatchResult(lib, level, cands[0], cands)
    if level == "mbid":                       # 同一 MBID = 同一录音
        return MatchResult(lib, level, cands[0], cands, note="同一 MBID 的重复文件")
    if t.duration_s is not None and all(f.duration is not None for f in cands):
        return MatchResult(lib, level, cands[0], cands, note="时长均与目标吻合，视为同一录音的重复文件")
    durs = [f.duration for f in cands if f.duration is not None]
    same_title = len({title_key(f.title) for f in cands}) == 1
    if same_title and len(durs) == len(cands) and max(durs) - min(durs) <= SAME_RECORDING_S:
        return MatchResult(lib, level, cands[0], cands, note="标题与时长一致，视为同一录音的重复文件")
    return MatchResult("multiple", level, None, cands, note=f"{lib} 中有 {len(cands)} 个无法区分的候选")


def match(index: LocalIndex, t: Target, libraries: Tuple[str, ...] = ("main", "discovery"),
          lenient: bool = True) -> MatchResult:
    """libraries 限定要查找的库（顺序即优先级）；默认先主库后试听库。lenient 见 _title_level。"""
    near: List[LocalFile] = []

    # 1) MBID：权威。主库优先
    if t.mbid:
        hits = index.by_mbid.get(t.mbid.lower(), [])
        for lib in libraries:
            sel = [f for f in hits if f.library == lib]
            if sel:
                return _resolve(t, "mbid", sel, lib)

    # 2) 名字级：先主库，主库没有再看试听库
    names = _target_artists(t)
    if not names:
        return MatchResult("none", note="目标缺少歌手，无法按名字匹配")
    for lib in libraries:
        scored: List[Tuple[str, LocalFile]] = []
        for f in index.candidates_for(names, lib):
            level = _title_level(t, f, lenient)
            if level is None:
                continue
            d = _dur_diff(t, f)
            limit = SOFT_DURATION_TOLERANCE_S if level == "variant" else DURATION_TOLERANCE_S
            if level in ("exact", "normalized") and same_album(t.album, f.album):
                limit = max(limit, ALBUM_DURATION_TOLERANCE_S)
            if d is not None and d > limit:
                near.append(f)          # 歌名吻合但时长差太多：多半是另一个版本
                continue
            scored.append((level, f))
        if scored:
            best = max(RANK[l] for l, _ in scored)
            level = next(l for l, _ in scored if RANK[l] == best)
            return _resolve(t, level, [f for l, f in scored if RANK[l] == best], lib)
    return MatchResult("none", near_misses=near,
                       note="歌名吻合但时长相差过大，视为不同录音" if near else "")
