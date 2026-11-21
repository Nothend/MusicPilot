import json
from datetime import datetime, timedelta

import pytest

import lb_love
import lb_repo
import lb_settings
import lb_states as ST
from db import Database
from lb_client import LBError
from task_manager import TaskManager

NOW = datetime(2026, 11, 20, 9, 0, 0)


class FakeLB:
    def __init__(self):
        self.loved, self.lookups, self.love_calls = [], {}, []
        self.lookup_calls = []
        self.love_errors = {}
        self.lookup_errors = {}
        self.loved_error = None

    def loved_mbids(self):
        if self.loved_error:
            raise self.loved_error
        return list(self.loved)

    def lookup(self, artist, title):
        self.lookup_calls.append((artist, title))
        if (artist, title) in self.lookup_errors:
            raise self.lookup_errors[(artist, title)]
        return self.lookups.get((artist, title))

    def love(self, mbid):
        self.love_calls.append(mbid)
        if mbid in self.love_errors:
            raise self.love_errors[mbid]


class FakeND:
    def __init__(self, songs=()):
        self.songs = list(songs)

    def starred_songs(self, folder_id=None):
        return list(self.songs)


def song(i, artist="歌手", title=None, mbid=""):
    return {"id": f"s{i}", "title": title if title is not None else f"歌{i}", "artist": artist, "album": "", "mbid": mbid, "starred": True,
            "duration": 200, "size": 1, "suffix": "flac", "album_artist": artist, "path_api": ""}


def hit(artist, title, mbid):
    return {"recording_mbid": mbid, "artist": artist, "title": title, "release": "", "artist_mbids": []}


class Env:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "t.sqlite")
        self.db.migrate()
        self.tm = TaskManager(self.db)
        self.lb, self.nd = FakeLB(), FakeND()
        self.settings = lb_settings.defaults()
        self.settings["listenbrainz"].update(username="me", token="tok")
        self.now = NOW

    def run(self, dry=False):
        deps = None
        with self.tm.run("love", "manual") as h:
            deps = lb_love.LoveDeps(self.db, self.settings, self.lb, self.nd, h, dry_run=dry, now=lambda: self.now)
            lb_love.run_love_sync(deps)
        self.plan = deps.plan
        return json.loads(self.tm.recent_runs()[0]["stats_json"])

    def rec(self):
        return {r["mbid"]: r["status"] for r in self.db.conn.execute("SELECT mbid, status FROM love_sync")}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# 必须含字母：纯数字的 MBID 大小写转换是空操作，测不出「统一小写」
M1, M2, M3 = "abcdef01-1111-1111-1111-111111111111", "bcdefa02-2222-2222-2222-222222222222", "cdefab03-3333-3333-3333-333333333333"


# ---------------------------------------------------------------- MBID 来源与可信度
def test_source_priority_item_then_navidrome_then_lookup(env):
    bid = lb_repo.create_batch(env.db, "weekly_jams", {"mbid": "p", "title": "t", "date": "d"},
                               {"tracks": [{"recording_mbid": M1.upper(), "artist": "歌手", "title": "歌1", "album": "",
                                            "duration_ms": 1, "artists": []}]})
    item = lb_repo.items_of_batch(env.db, bid)[0]["id"]
    lb_repo.set_state(env.db, item, ST.LOCAL_DISCOVERY, local_file_id="s1")
    env.nd.songs = [song(1, mbid="ffffffff-ffff-ffff-ffff-ffffffffffff"),      # 有 item 关联：以歌单里的为准，即便 Navidrome 标签也有
                    song(2, mbid=M2),                                           # 只有 Navidrome 标签
                    song(3)]                                                    # 只能查
    env.lb.lookups[("歌手", "歌3")] = hit("歌手", "歌3", M3)
    st = env.run()
    assert env.lb.love_calls == [M1, M2, M3]
    assert (st["via_item"], st["via_navidrome"], st["via_lookup"], st["submitted"]) == (1, 1, 1, 3)
    assert env.lb.lookup_calls == [("歌手", "歌3")]                             # 前两首不需要查


def test_lookup_result_is_verified_across_traditional_simplified(env):
    env.nd.songs = [song(1, "郭静", "心墙")]
    env.lb.lookups[("郭静", "心墙")] = hit("郭靜", "心牆", M1)
    env.run()
    assert env.lb.love_calls == [M1]


@pytest.mark.parametrize("returned", [
    hit("别的歌手", "歌1", M1),                 # 同名歌，别人的（翻唱）
    hit("歌手", "完全不同的歌", M1),
    hit("歌手", "歌1 (Live)", M1),              # 版本不同：不是同一个录音
    None,                                       # 查不到
    {"recording_mbid": "", "artist": "歌手", "title": "歌1"},
])
def test_untrustworthy_lookup_results_are_discarded(env, returned):
    env.nd.songs = [song(1)]
    env.lb.lookups[("歌手", "歌1")] = returned
    st = env.run()
    assert env.lb.love_calls == [] and st["unresolved"] == 1 and env.rec() == {}


def test_bullet_separated_artist_matches_single_artist_lookup(env):
    env.nd.songs = [song(1, "孙辉 • 蓝若兮", "QQ爱")]
    env.lb.lookups[("孙辉 • 蓝若兮", "QQ爱")] = hit("孙辉", "QQ爱", M1)
    env.run()
    assert env.lb.love_calls == [M1]


def test_uppercase_mbids_are_normalized(env):
    assert M1.upper() != M1
    env.nd.songs = [song(1, mbid=M1.upper())]
    env.lb.loved = [M1]                                       # LB 里是小写，Navidrome 标签里是大写：必须认作同一个
    st = env.run()
    assert env.lb.love_calls == [] and st["already"] == 1


def test_submitted_mbids_are_always_lowercase_for_every_source(env):
    env.nd.songs = [song(1, mbid=M1.upper()), song(2)]
    env.lb.lookups[("歌手", "歌2")] = hit("歌手", "歌2", M2.upper())
    env.run()
    assert env.lb.love_calls == [M1, M2] and env.rec() == {M1: "submitted", M2: "submitted"}


# ---------------------------------------------------------------- 缓存
def test_positive_lookup_is_cached_forever(env):
    env.nd.songs = [song(1)]
    env.lb.lookups[("歌手", "歌1")] = hit("歌手", "歌1", M1)
    env.lb.love_errors[M1] = LBError("boom")                  # 让提交失败，这样下次还会走到解析
    env.run()
    env.now += timedelta(days=400)
    env.run()
    assert len(env.lb.lookup_calls) == 1


def test_negative_lookup_is_cached_for_30_days_then_retried(env):
    env.nd.songs = [song(1)]
    env.run()
    env.now += timedelta(days=10)
    env.run()
    assert len(env.lb.lookup_calls) == 1
    env.lb.lookups[("歌手", "歌1")] = hit("歌手", "歌1", M1)
    env.now += timedelta(days=21)                             # 总共 31 天
    env.run()
    assert len(env.lb.lookup_calls) == 2 and env.lb.love_calls == [M1]


def test_rejected_lookup_is_negatively_cached_too(env):
    env.nd.songs = [song(1)]
    env.lb.lookups[("歌手", "歌1")] = hit("别人", "歌1", M1)
    env.run(); env.run()
    assert len(env.lb.lookup_calls) == 1


def test_transient_lookup_error_is_not_cached_and_does_not_stop_the_run(env):
    env.nd.songs = [song(1), song(2, mbid=M2)]
    env.lb.lookup_errors[("歌手", "歌1")] = LBError("503")
    st = env.run()
    assert st["lookup_errors"] == 1 and env.lb.love_calls == [M2]
    env.lb.lookup_errors.clear()
    env.lb.lookups[("歌手", "歌1")] = hit("歌手", "歌1", M1)
    env.run()
    assert M1 in env.lb.love_calls


# ---------------------------------------------------------------- 不重复提交
def test_already_loved_on_listenbrainz_is_skipped_and_recorded(env):
    env.nd.songs = [song(1, mbid=M1)]
    env.lb.loved = [M1]
    st = env.run()
    assert env.lb.love_calls == [] and st["already"] == 1 and env.rec() == {M1: "already"}


def test_submitted_is_not_resubmitted_even_if_listenbrainz_list_is_stale(env):
    env.nd.songs = [song(1, mbid=M1)]
    env.run()
    assert env.lb.love_calls == [M1] and env.rec() == {M1: "submitted"}
    env.run()                                                  # LB 的 loved 列表里仍没有（延迟）
    assert env.lb.love_calls == [M1]


def test_two_starred_songs_with_the_same_recording_are_submitted_once(env):
    env.nd.songs = [song(1, mbid=M1), song(2, "别名", "别题", mbid=M1)]
    env.run()
    assert env.lb.love_calls == [M1]


def test_unstarring_never_unloves(env):
    env.nd.songs = [song(1, mbid=M1)]
    env.run()
    env.nd.songs = []                                          # 在 Navidrome 取消了红心
    env.run()
    assert env.lb.love_calls == [M1] and env.rec() == {M1: "submitted"}      # 没有任何撤销动作，记录保留
    assert not hasattr(env.lb, "unlove")


# ---------------------------------------------------------------- 上限与预览
def test_per_run_cap_defers_the_rest_and_next_run_continues(env):
    env.settings["love"]["max_per_run"] = 2
    env.nd.songs = [song(i, mbid=f"{i:08d}-0000-0000-0000-000000000000") for i in range(1, 6)]
    st = env.run()
    assert len(env.lb.love_calls) == 2 and st["deferred"] == 3
    env.run(); env.run()
    assert len(env.lb.love_calls) == 5 and len(set(env.lb.love_calls)) == 5


def test_dry_run_submits_and_records_nothing_but_reports_the_plan(env):
    env.settings["love"]["max_per_run"] = 2
    env.nd.songs = [song(1, mbid=M1), song(2, mbid=M2), song(3, mbid=M3)]
    st = env.run(dry=True)
    assert env.lb.love_calls == [] and env.rec() == {} and st["would_submit"] == 2 and st["deferred"] == 1
    assert [p["mbid"] for p in env.plan] == [M1, M2] and {p["source"] for p in env.plan} == {"navidrome"}


def test_dry_run_does_not_record_already_loved(env):
    env.nd.songs = [song(1, mbid=M1)]
    env.lb.loved = [M1]
    env.run(dry=True)
    assert env.rec() == {}


# ---------------------------------------------------------------- 失败处理
def test_single_failure_is_recorded_and_retried_next_run(env):
    env.nd.songs = [song(1, mbid=M1), song(2, mbid=M2)]
    env.lb.love_errors[M1] = LBError("ListenBrainz 请求失败（HTTP 503）", 503)
    st = env.run()
    assert st["failed"] == 1 and st["submitted"] == 1 and env.rec() == {M1: "failed", M2: "submitted"}
    env.lb.love_errors.clear()
    env.run()
    assert env.rec() == {M1: "submitted", M2: "submitted"} and env.lb.love_calls.count(M2) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_invalid_token_aborts_the_run(env, status):
    env.nd.songs = [song(1, mbid=M1), song(2, mbid=M2)]
    env.lb.love_errors[M1] = LBError("Invalid token", status)
    with pytest.raises(ValueError, match="Token"):
        env.run()
    assert env.lb.love_calls == [M1] and env.rec() == {}       # 立刻停止，不再尝试第二首
    assert env.tm.recent_runs()[0]["status"] == "failed"


def test_invalid_token_during_lookup_also_aborts(env):
    env.nd.songs = [song(1)]
    env.lb.lookup_errors[("歌手", "歌1")] = LBError("bad", 401)
    with pytest.raises(ValueError, match="Token"):
        env.run()


def test_cannot_read_existing_loves_means_nothing_is_submitted(env):
    env.nd.songs = [song(1, mbid=M1)]
    env.lb.loved_error = LBError("503")
    with pytest.raises(ValueError, match="已有红心"):
        env.run()
    assert env.lb.love_calls == []                             # 不知道 LB 上有什么时，绝不盲目提交


def test_requires_credentials(env):
    env.settings["listenbrainz"]["token"] = ""
    with pytest.raises(ValueError, match="用户名和 Token"):
        env.run()


def test_songs_without_artist_or_title_are_skipped(env):
    env.nd.songs = [song(1, "", "x"), song(2, "a", "")]
    st = env.run()
    assert st["unresolved"] == 2 and env.lb.love_calls == [] and env.lb.lookup_calls == []


def test_settings_love_group_defaults_and_validation():
    d = lb_settings.defaults()
    assert d["love"] == {"enabled": False, "max_per_run": 100}                 # 默认关闭
    s = lb_settings.apply(d, {"love": {"enabled": "true", "max_per_run": "50"}})
    assert s["love"] == {"enabled": True, "max_per_run": 50}
    for bad in (0, 1001, "x"):
        with pytest.raises(ValueError):
            lb_settings.apply(d, {"love": {"max_per_run": bad}})


def test_existing_loves_returned_in_uppercase_are_still_recognized(env):
    env.nd.songs = [song(1, mbid=M1)]
    env.lb.loved = [M1.upper()]
    st = env.run()
    assert env.lb.love_calls == [] and st["already"] == 1


# ---------------------------------------------------------------- 每轮名字查询预算（名字查询很慢，不能霸占全局锁）
def run_with_budget(env, budget, dry=False):
    with env.tm.run("love", "manual") as h:
        deps = lb_love.LoveDeps(env.db, env.settings, env.lb, env.nd, h, dry_run=dry, now=lambda: env.now, lookup_budget=budget)
        lb_love.run_love_sync(deps)
    return json.loads(env.tm.recent_runs()[0]["stats_json"])


def test_lookup_budget_defers_the_rest_and_cached_results_are_free(env):
    env.nd.songs = [song(i) for i in range(1, 6)]
    for i in range(1, 6):
        env.lb.lookups[("歌手", f"歌{i}")] = hit("歌手", f"歌{i}", f"{i:08d}-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    st = run_with_budget(env, 2)
    assert len(env.lb.lookup_calls) == 2 and st["lookup_deferred"] == 3 and st["submitted"] == 2
    st = run_with_budget(env, 2)                                           # 前两首已缓存且已提交：不再查，只查新的 2 首
    assert len(env.lb.lookup_calls) == 4 and st["lookup_deferred"] == 1 and st["submitted"] == 2
    st = run_with_budget(env, 2)
    assert len(env.lb.lookup_calls) == 5 and st.get("lookup_deferred") is None and len(set(env.lb.love_calls)) == 5


def test_budget_does_not_limit_songs_that_need_no_lookup(env):
    env.nd.songs = [song(i, mbid=f"{i:08d}-bbbb-bbbb-bbbb-bbbbbbbbbbbb") for i in range(1, 6)]
    st = run_with_budget(env, 0)
    assert env.lb.lookup_calls == [] and st["submitted"] == 5 and st.get("lookup_deferred") is None


def test_deferred_songs_are_not_counted_as_unresolved_or_cached_as_missing(env):
    env.nd.songs = [song(1), song(2)]
    st = run_with_budget(env, 0)
    assert st["lookup_deferred"] == 2 and st.get("unresolved") is None
    assert env.db.conn.execute("SELECT COUNT(*) FROM mbid_cache").fetchone()[0] == 0     # 没查过就不能写「查不到」


def test_default_budget_is_bounded():
    assert 1 <= lb_love.LOOKUPS_PER_RUN <= 200
