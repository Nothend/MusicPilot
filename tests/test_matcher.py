import pytest

import matcher as M
from matcher import LocalFile, LocalIndex, Target, match


def F(i, artist, title, lib="main", dur=None, mbid="", album="", album_artist="", size=1000):
    return LocalFile(nd_song_id=i, library=lib, artist=artist, title=title, duration=dur, mbid=mbid,
                     album=album, album_artist=album_artist, size=size)


def run(files, **t):
    return match(LocalIndex(files), Target(**t))


# ---------------------------------------------------------------- 标准化
def test_fold_and_compact():
    assert M.fold("ＡＢＣ　Ｌｉｖｅ") == "abc live"            # NFKC 全角→半角
    assert M.fold("郭靜 心牆") == "郭静 心墙"                   # 繁→简
    assert M.compact(M.fold("Mr. Brightside!")) == "mrbrightside"   # compact 只处理 fold 之后的文本


def test_version_tags():
    assert M.version_tags("Song (Live)") == {"live"}
    assert M.version_tags("Song - 現場版") == {"live"}
    assert M.version_tags("Alive") == set()                      # 词边界：Alive 不是 Live
    assert M.version_tags("心墙 (Live版)") == {"live"}            # 音乐平台常见写法：汉字紧贴英文，\b 会漏掉
    assert M.version_tags("Silver Lining") == set()              # ver 在单词内部不算 version
    assert M.version_tags("Song (Acoustic Remix)") == {"acoustic", "remix"}
    assert M.version_tags("I Wish You Would (Voice Memo)") == {"voicememo"}
    assert M.version_tags("Song (2011 Remastered)") == {"remaster"}


def test_artist_set_splits_and_keeps_whole():
    assert {"周杰伦", "费玉清"} <= M.artist_set("周杰倫 & 費玉清")
    assert {"a", "b"} <= M.artist_set("A feat. B")
    assert {"a", "b"} <= M.artist_set("A x B")
    assert {"ac", "dc", "acdc"} <= M.artist_set("AC/DC")
    assert "simongarfunkel" in M.artist_set("Simon & Garfunkel")


def test_strip_feat_only_when_bracketed_or_tail():
    assert M.title_key("Song (feat. X)") == M.title_key("Song")
    assert M.title_key("Song feat. X") == M.title_key("Song")
    assert M.title_key("Stay With Me") == "staywithme"          # 标题里的 with 不被误删


# ---------------------------------------------------------------- 名字级匹配
def test_traditional_vs_simplified_real_case():
    r = run([F("1", "郭静", "心墙", dur=228)], artist="郭靜", title="心牆", artists=["郭靜"], duration_s=227)
    assert (r.status, r.level, r.chosen.nd_song_id) == ("main", "normalized", "1")


def test_multi_artist_target_matches_single_artist_local():
    r = run([F("1", "周杰伦", "千里之外")], artist="周杰倫 & 費玉清", title="千里之外",
            artists=["周杰倫", "費玉清"])
    assert r.status == "main"


def test_exact_level():
    r = run([F("1", "Taylor Swift", "Blank Space")], artist="Taylor Swift", title="Blank Space")
    assert r.level == "exact"


def test_case_and_punctuation_are_normalized():
    r = run([F("1", "TAYLOR SWIFT", "Shake It Off!")], artist="taylor swift", title="shake it off")
    assert (r.status, r.level) == ("main", "normalized")


def test_feat_in_local_title_ignored():
    r = run([F("1", "A", "Song (feat. B)")], artist="A", title="Song")
    assert r.status == "main"


@pytest.mark.parametrize("target_title,local_title,expect", [
    ("Song (Live)", "Song", "none"),                 # 版本信息不得被抹掉
    ("Song", "Song (Live)", "none"),
    ("Song - Live", "Song (Live)", "main"),          # 写法不同但同一版本
    ("Song (Remix)", "Song", "none"),
    ("Song (Acoustic)", "Song (Instrumental)", "none"),
    ("Song (2011 Remastered)", "Song", "none"),
    ("I Wish You Would", "I Wish You Would (Voice Memo)", "none"),
    ("Song (Voice Memo)", "Song (Voice Memos)", "main"),
])
def test_version_info_is_respected(target_title, local_title, expect):
    r = run([F("1", "A", local_title)], artist="A", title=target_title)
    assert r.status == expect


def test_fuzzy_accepts_small_typos_but_not_short_titles():
    r = run([F("1", "A", "Everything Has Changed")], artist="A", title="Everything Has Change")
    assert (r.status, r.level) == ("main", "fuzzy")
    assert run([F("1", "A", "Rum")], artist="A", title="Run").status == "none"
    assert run([F("1", "A", "Love Story")], artist="A", title="Love Stories").status == "none"


def test_different_artist_same_title_is_not_a_match():
    assert run([F("1", "Someone Else", "Blank Space")], artist="Taylor Swift", title="Blank Space").status == "none"


def test_missing_target_artist_cannot_match_by_name():
    r = run([F("1", "A", "Song")], artist="", title="Song")
    assert r.status == "none" and "缺少歌手" in r.note


def test_album_artist_counts_as_artist():
    r = run([F("1", "Feat Guy", "Song", album_artist="Main Artist")], artist="Main Artist", title="Song")
    assert r.status == "main"


# ---------------------------------------------------------------- MBID 与库优先级
def test_mbid_beats_names_and_ignores_duration():
    r = run([F("1", "Other", "Other Title", mbid="AAA", dur=999)], artist="A", title="Song", mbid="aaa", duration_s=100)
    assert (r.status, r.level) == ("main", "mbid")


def test_main_preferred_over_discovery():
    files = [F("d", "A", "Song", lib="discovery"), F("m", "A", "Song", lib="main")]
    assert run(files, artist="A", title="Song").chosen.nd_song_id == "m"
    assert run(files, artist="A", title="Song", mbid="x").status == "main"


def test_main_preferred_even_when_discovery_is_a_better_level():
    files = [F("d", "A", "Everything Has Changed", lib="discovery"), F("m", "A", "Everything Has Change", lib="main")]
    r = run(files, artist="A", title="Everything Has Changed")
    assert (r.status, r.chosen.nd_song_id, r.level) == ("main", "m", "fuzzy")


def test_discovery_only():
    r = run([F("d", "A", "Song", lib="discovery")], artist="A", title="Song")
    assert (r.status, r.chosen.nd_song_id) == ("discovery", "d")


def test_mbid_prefers_main_but_falls_back_to_discovery():
    files = [F("d", "x", "y", lib="discovery", mbid="m1")]
    assert run(files, artist="A", title="Song", mbid="M1").status == "discovery"


# ---------------------------------------------------------------- 时长
def test_far_duration_means_different_recording_and_is_reported():
    r = run([F("1", "A", "Song", dur=400)], artist="A", title="Song", duration_s=200)
    assert r.status == "none" and [f.nd_song_id for f in r.near_misses] == ["1"]


def test_near_duration_ok_and_unknown_duration_never_rejects():
    assert run([F("1", "A", "Song", dur=210)], artist="A", title="Song", duration_s=200).status == "main"
    assert run([F("1", "A", "Song", dur=None)], artist="A", title="Song", duration_s=200).status == "main"
    assert run([F("1", "A", "Song", dur=999)], artist="A", title="Song", duration_s=None).status == "main"


# ---------------------------------------------------------------- 多候选
def test_duplicate_files_of_same_recording_are_not_ambiguous():
    files = [F("single", "A", "Song", dur=200, size=5), F("album", "A", "Song", dur=201, size=9)]
    r = run(files, artist="A", title="Song", duration_s=200)
    assert r.status == "main" and r.chosen.nd_song_id == "single" and len(r.candidates) == 2   # 取时长最接近的
    r = run(files, artist="A", title="Song")                                                   # 无目标时长，但互相一致
    assert r.status == "main"


def test_different_length_candidates_without_target_duration_are_multiple():
    files = [F("studio", "A", "Song", dur=200), F("extended", "A", "Song", dur=400)]
    r = run(files, artist="A", title="Song")
    assert r.status == "multiple" and r.chosen is None
    assert {f.nd_song_id for f in r.candidates} == {"studio", "extended"}


def test_target_duration_disambiguates_candidates():
    files = [F("studio", "A", "Song", dur=200), F("extended", "A", "Song", dur=400)]
    r = run(files, artist="A", title="Song", duration_s=205)
    assert (r.status, r.chosen.nd_song_id) == ("main", "studio")
    assert r.near_misses == []                              # near_misses 只在最终没匹配上时用来解释原因


def test_unknown_durations_among_candidates_are_multiple():
    files = [F("a", "A", "Song", dur=None), F("b", "A", "Song", dur=None)]
    assert run(files, artist="A", title="Song").status == "multiple"


def test_best_level_wins_within_a_library():
    files = [F("fuzzy", "A", "Everything Has Change"), F("exact", "A", "Everything Has Changed")]
    r = run(files, artist="A", title="Everything Has Changed")
    assert (r.chosen.nd_song_id, r.level) == ("exact", "exact")


def test_empty_index():
    assert run([], artist="A", title="Song").status == "none"


@pytest.mark.parametrize("target_title,local_title", [
    # 标题很长时，仅靠相似度会把 Live/Remix/Instrumental 版本当成同一首；版本标记必须挡住它
    ("Everything Has Changed Today For Real And Forever (Live)", "Everything Has Changed Today For Real And Forever"),
    ("Everything Has Changed Today For Real And Forever", "Everything Has Changed Today For Real And Forever (Remix)"),
    ("Everything Has Changed Today For Real And Forever (Instrumental)", "Everything Has Changed Today For Real And Forever (Live)"),
])
def test_version_tags_block_fuzzy_matches_on_long_titles(target_title, local_title):
    assert run([F("1", "A", local_title)], artist="A", title=target_title).status == "none"
    # 对照：没有版本差异时，同样长度的相似标题仍然能 fuzzy 命中
    assert run([F("1", "A", "Everything Has Changed Today For Real And Forevr")], artist="A",
               title="Everything Has Changed Today For Real And Forever").level == "fuzzy"


# ---------------------------------------------------------------- 括号说明（来自真实数据：凤凰传奇 - 奢香夫人）
LONG = "奢香夫人 (展现一代彝族巾帼英雄奢香夫人的异域风情歌曲)"


def test_bracket_description_matches_only_with_duration_evidence():
    r = run([F("1", "凤凰传奇", "奢香夫人", dur=259)], artist="凤凰传奇", title=LONG, duration_s=259)
    assert (r.status, r.level) == ("main", "bracket")
    # 反方向：本地标题带说明、目标不带
    assert run([F("1", "凤凰传奇", LONG, dur=259)], artist="凤凰传奇", title="奢香夫人", duration_s=260).level == "bracket"
    # 没有时长佐证 / 时长对不上：不接受
    assert run([F("1", "凤凰传奇", "奢香夫人", dur=259)], artist="凤凰传奇", title=LONG).status == "none"
    assert run([F("1", "凤凰传奇", "奢香夫人", dur=259)], artist="凤凰传奇", title=LONG, duration_s=270).status == "none"
    assert run([F("1", "凤凰传奇", "奢香夫人", dur=None)], artist="凤凰传奇", title=LONG, duration_s=259).status == "none"


@pytest.mark.parametrize("target_title,local_title", [
    ("Song (Piano Version)", "Song"),       # 不同编曲
    ("Song (国语版)", "Song"),
    ("Song (Cover)", "Song"),
    ("Song (Live)", "Song"),                # 既有的版本词规则仍然优先
    ("Song (Acoustic)", "Song (Piano)"),
])
def test_bracket_level_never_crosses_version_boundaries(target_title, local_title):
    assert run([F("1", "A", local_title, dur=200)], artist="A", title=target_title, duration_s=200).status == "none"


def test_bracket_is_weaker_than_other_levels():
    files = [F("br", "A", "Song", dur=200), F("fz", "A", "Song Is Here Now", dur=200)]
    r = run(files, artist="A", title="Song (a long description)", duration_s=200)
    assert (r.level, r.chosen.nd_song_id) == ("bracket", "br")
    files = [F("br", "A", "Song", dur=200), F("nm", "A", "Song (a long description)", dur=200)]
    assert run(files, artist="A", title="Song (a long description)", duration_s=200).level == "exact"


def test_libraries_parameter_restricts_search_and_priority():
    files = [F("m", "A", "Song", lib="main"), F("d", "A", "Song", lib="discovery")]
    idx = LocalIndex(files)
    t = Target(artist="A", title="Song")
    assert match(idx, t).chosen.nd_song_id == "m"
    assert match(idx, t, libraries=("discovery",)).chosen.nd_song_id == "d"
    assert match(idx, t, libraries=("discovery", "main")).chosen.nd_song_id == "d"
    assert match(LocalIndex([files[0]]), t, libraries=("discovery",)).status == "none"
    assert match(LocalIndex([F("d", "x", "y", lib="discovery", mbid="M")]), Target(artist="A", title="Z", mbid="m"),
                 libraries=("main",)).status == "none"


def test_navidrome_bullet_separator_splits_artists():
    assert {"孙辉", "蓝若兮"} <= M.artist_set("孙辉 • 蓝若兮")
    assert {"ladygaga", "brunomars"} <= M.artist_set("Lady Gaga • Bruno Mars")
    assert run([F("1", "孙辉 • 蓝若兮", "QQ爱")], artist="孙辉", title="QQ爱").status == "main"
    assert M.artist_set("迈克尔·杰克逊") == frozenset({"迈克尔杰克逊"})                   # 中点是名字的一部分，不拆


# ---------------------------------------------------------------- 拉丁重音（来自真实数据：Gregoire / Grégoire）
@pytest.mark.parametrize("raw,expected", [
    ("Grégoire", "gregoire"), ("Beyoncé", "beyonce"), ("Señorita", "senorita"), ("JAŸ-Z", "jay-z"),
    ("ROSÉ", "rose"), ("Mötley Crüe", "motley crue"), ("Sigur Rós", "sigur ros"),
])
def test_latin_accents_are_folded(raw, expected):
    assert M.fold(raw) == expected


@pytest.mark.parametrize("raw", ["がっこう", "ぱぴぷ", "한국어", "郭静", "Привет", "αβγ"])
def test_non_latin_scripts_are_untouched_by_accent_folding(raw):
    """日文假名的浊点（が = か + ゙）、韩文、西里尔、希腊字母不能被当成「重音」去掉。"""
    import unicodedata
    assert M.fold(raw) == unicodedata.normalize("NFKC", raw).casefold()


def test_accent_difference_no_longer_blocks_a_match():
    assert run([F("1", "Grégoire", "Toi + moi")], artist="Gregoire", title="Toi + Moi").status == "main"
    assert run([F("1", "Beyoncé", "Halo")], artist="Beyonce", title="Halo").status == "main"
    assert run([F("1", "Beyonce", "Halo")], artist="Beyoncé", title="Halo").status == "main"


def test_accent_folding_does_not_merge_genuinely_different_letters():
    assert run([F("1", "A", "Naïve")], artist="A", title="Navy").status == "none"
    assert run([F("1", "Ö", "x")], artist="Z", title="x").status == "none"


# ---------------------------------------------------------------- 别名 / 软版本词 / 异体字
def test_artist_alias_latin_plus_cjk():
    r = run([F("1", "F.I.R 飞儿乐团", "我们的爱", dur=250)], artist="F.I.R", title="我们的爱", duration_s=250)
    assert r.status == "main"
    r = run([F("1", "Jay Chou 周杰伦", "晴天", dur=269)], artist="周杰伦", title="晴天")
    assert r.status == "main"
    assert run([F("1", "Jay Chou", "晴天")], artist="Chou", title="晴天").status == "none"   # 不做子串匹配


@pytest.mark.parametrize("local,target", [("Halo (Radio Edit)", "Halo"), ("Halo", "Halo (Radio Edit)"),
                                          ("Halo - Single Version", "Halo"), ("Halo [Explicit]", "Halo")])
def test_soft_version_counts_as_same(local, target):
    r = run([F("1", "Beyoncé", local, dur=222)], artist="Beyonce", title=target, duration_s=261)
    assert r.status == "main" and r.level == "variant"


def test_soft_version_still_rejects_hard_version_and_big_gap():
    assert run([F("1", "A", "Halo (Radio Edit)", dur=200)], artist="A", title="Halo (Live)").status == "none"
    assert run([F("1", "A", "Halo (Radio Edit)", dur=100)], artist="A", title="Halo", duration_s=300).status == "none"


def test_soft_prefers_plain_over_variant():
    r = run([F("1", "A", "Halo (Radio Edit)", dur=220), F("2", "A", "Halo", dur=260)], artist="A", title="Halo", duration_s=260)
    assert r.chosen.nd_song_id == "2" and r.level == "exact"


def test_strict_mode_keeps_soft_versions_apart():
    idx = LocalIndex([F("1", "A", "Halo (Radio Edit)", dur=220)])
    assert match(idx, Target(artist="A", title="Halo"), lenient=False).status == "none"


def test_variant_characters():
    assert M.fold("松栢") == M.fold("松柏")
    assert run([F("1", "陈慧娴", "千千阙歌", dur=250)], artist="陈慧娴", title="千千闋歌", duration_s=250).status == "main"
    # 通用兜底：单字之差、标题 ≥4 字、时长 ≤2 秒
    r = run([F("1", "某人", "爱情转移", dur=250)], artist="某人", title="爱情转迻", duration_s=251)
    assert r.status == "main" and r.level == "charvar"
    assert run([F("1", "某人", "爱情转移", dur=250)], artist="某人", title="爱情转迻", duration_s=262).status == "none"
    assert run([F("1", "某人", "爱情转移", dur=250)], artist="某人", title="爱情转迻").status == "none"     # 无时长不接受
    assert run([F("1", "某人", "我爱你", dur=250)], artist="某人", title="我恨你", duration_s=250).status == "none"  # 太短


@pytest.mark.parametrize("t", ["Halo (Mono Version)", "Halo [Bonus Track]", "Halo (Deluxe Edition)", "Halo - Original Mix", "Halo (原版)"])
def test_more_soft_phrases(t):
    assert M.is_soft_edition(t) and not M.is_soft_edition("Halo (Live)") and not M.is_soft_edition("Halo (Remix)")
    assert run([F("1", "A", t, dur=200)], artist="A", title="Halo", duration_s=200).level == "variant"


# ---------------------------------------------------------------- 2026-11-21 真实漏判：歌手别名无空格 / 同专辑时长差
def test_artist_alias_split_without_space():
    assert "fir" in M.artist_set("F.I.R.飞儿乐团")
    assert {"gem", "邓紫棋"} <= M.artist_set("G.E.M.邓紫棋")
    assert "e" not in M.artist_set("E神")          # 单字母别名不收，免得撞名


def test_fir_no_space_alias_matches_main():
    r = run([F("m", "F.I.R.飞儿乐团", "月牙湾", dur=308, album="爱·歌姬")],
            artist="F.I.R.", title="月牙灣", artists=["F.I.R."], duration_s=308, album="愛‧歌姬")
    assert r.status == "main"


def test_same_album_widens_duration_tolerance_to_60s():
    files = [F("m", "毛不易", "像我这样的人", dur=239, album="平凡的一天")]
    ok = run(files, artist="毛不易", title="像我这样的人", duration_s=207, album="平凡的一天")     # 差 32 秒
    assert ok.status == "main"
    far = run(files, artist="毛不易", title="像我这样的人", duration_s=170, album="平凡的一天")    # 差 69 秒
    assert far.status == "none" and far.near_misses
    other_album = run(files, artist="毛不易", title="像我这样的人", duration_s=207, album="别的专辑")
    assert other_album.status == "none"              # 专辑不同：仍按 20 秒容差
    no_album = run(files, artist="毛不易", title="像我这样的人", duration_s=207)
    assert no_album.status == "none"


def test_same_album_does_not_merge_live_version():
    files = [F("m", "毛不易", "像我这样的人 (Live)", dur=239, album="平凡的一天")]
    assert run(files, artist="毛不易", title="像我这样的人", duration_s=207, album="平凡的一天").status == "none"


# ---------------------------------------------------------------- 仅差 Live 标记
def lv(files, **t):
    return M.live_variants(LocalIndex(files), Target(**t))


def test_live_in_library_but_not_in_target_close_duration_is_same():
    r = lv([F("a", "张韶涵", "阿刁 (Live)", dur=322)], artist="張韶涵", title="阿刁", duration_s=320)
    assert (r.status, r.chosen.nd_song_id, r.kind) == ("main", "a", "live")


def test_live_difference_with_far_duration_asks_user_both_directions():
    r = lv([F("a", "张韶涵", "阿刁 (Live)", dur=322)], artist="張韶涵", title="阿刁", duration_s=315)   # 差 7 秒
    assert (r.status, r.kind, [f.nd_song_id for f in r.candidates]) == ("multiple", "live", ["a"])
    r = lv([F("a", "A", "Song", dur=200)], artist="A", title="Song - Live", duration_s=260)             # 反方向：推荐有 Live，库里没有
    assert r.status == "multiple" and r.kind == "live"


def test_live_difference_with_unknown_duration_asks_user():
    assert lv([F("a", "A", "Song (Live)", dur=None)], artist="A", title="Song", duration_s=200).status == "multiple"


def test_live_variants_ignores_other_differences():
    f = [F("a", "A", "Song (Live)", dur=200)]
    assert lv(f, artist="A", title="Song (Remix)", duration_s=200).status == "none"            # 还差 Remix
    assert lv(f, artist="B", title="Song", duration_s=200).status == "none"                     # 歌手不同（赵雷那首）
    assert lv(f, artist="A", title="Other", duration_s=200).status == "none"                    # 标题不同
    assert lv([F("a", "A", "Song (Live)")], artist="A", title="Song (Live)", duration_s=1).status == "none"   # 都是 Live：本函数不管
    assert lv([F("a", "A", "Song (Instrumental)")], artist="A", title="Song", duration_s=200).status == "none"


def test_live_cleaner_handles_various_spellings():
    for t in ("阿刁 (Live)", "阿刁 - 现场版", "阿刁 [live版]", "阿刁 演唱会版"):
        assert lv([F("a", "A", t, dur=100)], artist="A", title="阿刁", duration_s=100).status == "main", t


def test_plain_match_still_treats_live_as_different():
    assert run([F("a", "A", "Song (Live)", dur=200)], artist="A", title="Song", duration_s=200).status == "none"
