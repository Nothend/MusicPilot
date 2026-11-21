import os
from pathlib import Path

import pytest

import lb_localfs as L
from audio import make_flac
from lb_localfs import FileTags, find_file_by_tags, inside, main_relpath, read_file_tags, safe_component


def tags(**kw):
    d = dict(title="歌", artists=["歌手"], album="专辑")
    d.update(kw)
    return FileTags(**d)


# ---------------------------------------------------------------- 命名规则（用例取自用户真实库）
@pytest.mark.parametrize("t,expected", [
    (tags(title="白沙之恋", artists=["戴荃"], album="白沙之恋", year="2026", track=3),
     "戴荃/白沙之恋 (2026)/03 - 戴荃 - 白沙之恋.flac"),
    (tags(title="百年孤寂", artists=["王菲"], album="只爱陌生人"),
     "王菲/只爱陌生人/王菲 - 百年孤寂.flac"),                                           # 没有曲目号 → 不带前缀
    (tags(title="Song", artists=["P!nk", "Nate Ruess"], album="Al", track=11),
     "P!nk, Nate Ruess/Al/11 - P!nk, Nate Ruess - Song.flac"),                          # 多歌手默认「, 」
    (tags(title="千里之外", artists=["周杰伦", "费玉清"], album_artist="周杰伦", album="依然范特西", year="2006", track=1),
     "周杰伦/依然范特西 (2006)/01 - 周杰伦, 费玉清 - 千里之外.flac"),                  # 目录用专辑歌手，文件名用歌手
    (tags(), "歌手/专辑/歌手 - 歌.flac"),
    (FileTags(), "Unknown Artist/Unknown Album/Unknown Artist - Unknown Title.flac"),
    (tags(track=123), "歌手/专辑/123 - 歌手 - 歌.flac"),
])
def test_main_relpath_matches_real_library_conventions(t, expected):
    assert main_relpath(t, ".flac").as_posix() == expected


def test_multi_disc_gets_disc_folder_but_single_disc_does_not():
    assert main_relpath(tags(track=2, disc=2, total_discs=2), ".flac").as_posix() == "歌手/专辑/Disc 2/02 - 歌手 - 歌.flac"
    assert main_relpath(tags(track=2, disc=1, total_discs=1), ".flac").as_posix() == "歌手/专辑/02 - 歌手 - 歌.flac"
    assert main_relpath(tags(track=2, disc=0, total_discs=3), ".flac").parts[2] == "Disc 1"


def test_extension_is_lowercased():
    assert main_relpath(tags(), ".FLAC").suffix == ".flac" and main_relpath(tags(), ".mp3").suffix == ".mp3"


# ---------------------------------------------------------------- 名字清洗
@pytest.mark.parametrize("raw,expected", [
    ("Beyoncé / JAŸ-Z", "Beyoncé ／ JAŸ-Z"),             # 与真实库一致：非法字符换成全角
    ('a:b*c?d"e<f>g|h\\i', "a：b＊c？d＂e＜f＞g｜h＼i"),
    ("T.R.Y.", "T.R.Y"),                                  # 末尾的点会让群晖生成 TailCharacterConflict 目录
    ("trailing space  ", "trailing space"),
    ("  many   spaces ", "many spaces"),
    ("", "_"), ("...", "_"), (".", "_"), ("..", "_"),
    (".hidden", "hidden"),                                 # 以点开头的目录会被 Navidrome 忽略，必须去掉
    ("tab\tand\nnewline", "tab and newline"),
    ("x" * 400, "x" * 150),
])
def test_safe_component(raw, expected):
    assert safe_component(raw) == expected


def test_malicious_tags_cannot_escape_the_main_directory(tmp_path):
    main = tmp_path / "songs"
    main.mkdir()
    for evil in ["../../etc", "/etc/passwd", "..", "a/../../b", "..\\..\\x"]:
        rel = main_relpath(tags(artists=[evil], album=evil, title=evil), ".flac")
        assert ".." not in rel.parts and not rel.is_absolute()
        assert inside(main / rel, main)


def test_inside_resolves_symlinks_and_dotdot(tmp_path):
    root, other = tmp_path / "songs", tmp_path / "other"
    root.mkdir(); other.mkdir()
    (root / "link").symlink_to(other)
    assert inside(root / "a" / "b.flac", root)
    assert not inside(root / "link" / "x.flac", root)
    assert not inside(root / ".." / "other" / "x", root)
    assert not inside(root.parent, root)


# ---------------------------------------------------------------- 读标签
def test_read_file_tags_from_real_flac(tmp_path):
    p = make_flac(tmp_path / "a.flac", ["郭静", "某人"], "心墙",
                  extra={"album": "在树上唱歌", "albumartist": "郭静", "date": "2026-03-01",
                         "tracknumber": "3/12", "discnumber": "1/2"})
    t = read_file_tags(p)
    assert (t.title, t.artists, t.artist, t.album_artist, t.album, t.year, t.track, t.disc, t.total_discs) == \
        ("心墙", ["郭静", "某人"], "郭静, 某人", "郭静", "在树上唱歌", "2026", 3, 1, 2)


def test_read_file_tags_tolerates_missing_fields(tmp_path):
    t = read_file_tags(make_flac(tmp_path / "a.flac", "A", "T"))
    assert (t.album, t.year, t.track, t.total_discs) == ("", "", 0, 0)


def test_read_file_tags_rejects_junk(tmp_path):
    (tmp_path / "x.flac").write_bytes(b"junk" * 20)
    with pytest.raises(Exception):
        read_file_tags(tmp_path / "x.flac")


# ---------------------------------------------------------------- 按标签在目录里找文件
def test_find_file_by_tags_matches_across_trad_simplified_and_skips_dot_dirs(tmp_path):
    make_flac(tmp_path / "x" / "one.flac", "Other", "Another Song")
    want = make_flac(tmp_path / "y" / "two.flac", "郭静", "心墙")
    make_flac(tmp_path / ".staging" / "three.flac", "郭静", "心墙")               # 暂存区的不算
    make_flac(tmp_path / ".trash" / "four.flac", "郭静", "心墙")
    assert find_file_by_tags(tmp_path, "郭靜", "心牆", ["郭靜"]) == want


def test_find_file_by_tags_none_or_ambiguous_returns_none(tmp_path):
    make_flac(tmp_path / "a" / "1.flac", "A", "Song")
    assert find_file_by_tags(tmp_path, "A", "Nope") is None
    make_flac(tmp_path / "b" / "2.flac", "A", "Song")                              # 两份同名且无法区分
    assert find_file_by_tags(tmp_path, "A", "Song") is None
    assert find_file_by_tags(tmp_path / "missing", "A", "Song") is None


def test_find_file_by_tags_respects_version_tags(tmp_path):
    make_flac(tmp_path / "a.flac", "A", "Song (Live)")
    assert find_file_by_tags(tmp_path, "A", "Song") is None


def test_iter_audio_files_ignores_non_audio_and_dot_dirs(tmp_path):
    make_flac(tmp_path / "a" / "ok.flac")
    (tmp_path / "a" / "lyrics.txt").write_text("x")
    make_flac(tmp_path / ".hidden" / "no.flac")
    assert [p.name for p in L.iter_audio_files(tmp_path)] == ["ok.flac"]


# ---------------------------------------------------------------- 多歌手连接符（可配置，默认「, 」）
def test_default_artist_separator_is_comma_space():
    t = tags(title="Song", artists=["P!nk", "Nate Ruess"], album="Al", track=11)
    assert main_relpath(t, ".flac").as_posix() == "P!nk, Nate Ruess/Al/11 - P!nk, Nate Ruess - Song.flac"


@pytest.mark.parametrize("sep,expected", [
    (", ", "A, B/Al/A, B - S.flac"),
    (" & ", "A & B/Al/A & B - S.flac"),
    ("; ", "A; B/Al/A; B - S.flac"),
    (" / ", "A ／ B/Al/A ／ B - S.flac"),          # 填斜杠 = 复现 MoviePilot（真实库 25/25 一致的写法）
    ("&", "A&B/Al/A&B - S.flac"),
])
def test_artist_separator_is_configurable(sep, expected):
    assert main_relpath(tags(title="S", artists=["A", "B"], album="Al"), ".flac", sep).as_posix() == expected


def test_separator_cannot_create_path_levels():
    p = main_relpath(tags(title="S", artists=["A", "B"], album="Al"), ".flac", "/../")
    assert len(p.parts) == 3 and ".." not in p.parts            # 即使有人填了带斜杠的连接符，也只是名字里多了全角字符


def test_single_artist_and_album_artist_are_unaffected_by_separator():
    t = tags(title="S", artists=["A"], album="Al", album_artist="周杰伦")
    assert main_relpath(t, ".flac", " & ").as_posix() == "周杰伦/Al/A - S.flac"
