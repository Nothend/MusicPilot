import pytest

import lb_settings as S


def test_defaults_safe_and_complete():
    d = S.defaults()
    assert d["enabled"] is False                       # 配置完成前什么都不做
    assert d["strategy"]["daily"]["max_new"] == 0      # Daily 不限
    assert d["strategy"]["weekly_jams"]["max_new"] == 30
    assert d["discovery"]["retention_days"] == 7 and d["discovery"]["trash_days"] == 7
    assert d["schedule"]["daily_time"] == "08:30"


def test_load_merges_partial_and_drops_unknown():
    s = S.load({"dirs": {"main": "/x"}, "strategy": {"daily": {"max_new": 5}}, "bogus": 1})
    assert s["dirs"] == {"main": "/x", "discovery": "/music/discovery"}
    assert s["strategy"]["daily"] == {"enabled": True, "max_new": 5}
    assert "bogus" not in s
    assert S.load(None) == S.defaults() and S.load("garbage") == S.defaults()


def test_secrets_blank_keeps_saved():
    cur = S.defaults()
    cur["listenbrainz"]["token"] = "TOK"
    cur["navidrome"]["password"] = "PW"
    s = S.apply(cur, {"listenbrainz": {"username": " me ", "token": ""}, "navidrome": {"password": ""}})
    assert s["listenbrainz"] == {"username": "me", "token": "TOK"}
    assert s["navidrome"]["password"] == "PW"
    s = S.apply(s, {"listenbrainz": {"token": "NEW"}})
    assert s["listenbrainz"]["token"] == "NEW"


def test_view_never_leaks_secrets():
    cur = S.defaults()
    cur["listenbrainz"].update(username="u", token="SECRET-TOKEN")
    cur["navidrome"].update(url="http://n", user="a", password="SECRET-PW")
    v = S.view(cur, "Asia/Shanghai")
    text = str(v)
    assert "SECRET-TOKEN" not in text and "SECRET-PW" not in text
    assert v["listenbrainz"]["has_token"] is True and v["navidrome"]["has_password"] is True
    assert v["timezone"] == "Asia/Shanghai"


def test_apply_does_not_mutate_input():
    cur = S.defaults()
    S.apply(cur, {"dirs": {"main": "/a/b"}})
    assert cur == S.defaults()


@pytest.mark.parametrize("payload,msg", [
    ({"navidrome": {"url": "ftp://x"}}, "http"),
    ({"dirs": {"main": "relative/path"}}, "绝对路径"),
    ({"dirs": {"main": "/music/../etc"}}, ".."),
    ({"dirs": {"main": "/"}}, "根目录"),
    ({"dirs": {"main": "/music", "discovery": "/music/discovery"}}, "包含"),
    ({"dirs": {"main": "/music/a", "discovery": "/music/a"}}, "包含"),
    ({"navidrome": {"main_folder_id": 2, "discovery_folder_id": 2}}, "同一个"),
    ({"strategy": {"daily": {"max_new": -1}}}, "范围"),
    ({"strategy": {"daily": {"max_new": "x"}}}, "整数"),
    ({"discovery": {"retention_days": 0}}, "范围"),
    ({"schedule": {"daily_time": "25:00"}}, "HH:MM"),
    ({"schedule": {"weekly_weekday": 7}}, "范围"),
    ({"schedule": {"promote_interval_min": 1}}, "范围"),
    ({"retry": {"download_max_retries": 99}}, "范围"),
])
def test_validation_errors(payload, msg):
    with pytest.raises(ValueError, match=msg):
        S.apply(S.defaults(), payload)


def test_valid_full_update_and_normalization():
    s = S.apply(S.defaults(), {
        "enabled": "true",
        "navidrome": {"url": "http://10.0.0.6:4533/", "user": "u", "main_folder_id": "1", "discovery_folder_id": 2},
        "dirs": {"main": "/music//songs/", "discovery": "/music/discovery"},
        "strategy": {"weekly_jams": {"enabled": False, "max_new": "10"}},
        "discovery": {"auto_cleanup": "false", "retention_days": "14"},
        "schedule": {"daily_time": "07:05", "weekly_weekday": "2"},
    })
    assert s["enabled"] is True
    assert s["navidrome"]["url"] == "http://10.0.0.6:4533"
    assert (s["navidrome"]["main_folder_id"], s["navidrome"]["discovery_folder_id"]) == (1, 2)
    assert s["dirs"]["main"] == "/music/songs"
    assert s["strategy"]["weekly_jams"] == {"enabled": False, "max_new": 10}
    assert s["discovery"]["auto_cleanup"] is False and s["discovery"]["retention_days"] == 14
    assert s["schedule"]["daily_time"] == "07:05" and s["schedule"]["weekly_weekday"] == 2


def test_failed_apply_leaves_nothing_half_done():
    cur = S.defaults()
    with pytest.raises(ValueError):
        S.apply(cur, {"enabled": True, "dirs": {"main": "bad"}})
    assert cur["enabled"] is False


# ---------------------------------------------------------------- 命名：多歌手连接符
def test_naming_default_and_update():
    assert S.defaults()["naming"] == {"artist_separator": ", "}
    s = S.apply(S.defaults(), {"naming": {"artist_separator": " & "}})
    assert s["naming"]["artist_separator"] == " & "                       # 首尾空格有意义，不被 strip
    assert S.view(s, "UTC")["naming"]["artist_separator"] == " & "
    assert S.apply(s, {"naming": {"artist_separator": " / "}})["naming"]["artist_separator"] == " / "   # 斜杠允许（落盘时换全角）


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 11, "a\nb", "a\x00b"])
def test_naming_rejects_bad_separators(bad):
    with pytest.raises(ValueError, match="连接符"):
        S.apply(S.defaults(), {"naming": {"artist_separator": bad}})


# ---- 歌单名模板（用户 2026-11-21 要求：在设置里可改）
def test_playlist_name_defaults_keep_old_names():
    s = S.defaults()
    assert s["playlist_names"]["daily"] == "LB Daily Jams {date}"


def test_playlist_name_can_be_customised_and_validated():
    s = S.apply(S.defaults(), {"playlist_names": {"daily": "  每日熟悉 {date} "}})
    assert s["playlist_names"]["daily"] == "每日熟悉 {date}" and s["playlist_names"]["weekly_jams"].startswith("LB Weekly Jams")
    for bad in ("", "   ", "x{name}", "a}b", "x" * 61, "a\nb"):
        with pytest.raises(ValueError):
            S.apply(S.defaults(), {"playlist_names": {"daily": bad}})


def test_view_exposes_chinese_label_english_name_and_description():
    t = {x["key"]: x for x in S.view(S.defaults(), "UTC")["rec_types"]}
    assert t["weekly_exploration"]["label"] == "每周探新" and t["weekly_exploration"]["en"] == "Weekly Exploration"
    assert all(len(x["label"]) <= 6 and x["desc"] for x in t.values())
