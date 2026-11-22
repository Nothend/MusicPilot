"""红心同步：解析不出 MBID 的歌可人工处理（指定 MBID / 忽略 / 撤销），以及 MusicBrainz 搜索客户端。"""
from datetime import timedelta

import pytest
import requests

import lb_manual
import lb_repo
import mb_client
from matcher import artist_key, title_key
from settings_store import SettingsStore
from test_lb_love import Env, hit, song

MB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.kv = SettingsStore(e.db)
    return e


def snapshot(e):
    e.kv.set(lb_manual.KEY, {"at": "t", "items": e.unresolved})


def test_unresolved_items_carry_reason_and_candidate(env):
    env.nd.songs = [song(1, "甲", "没查到"), song(2, "乙", "查歪了")]
    env.lb.lookups[("乙", "查歪了")] = hit("别人", "另一首", "aaaaaaaa-0000-0000-0000-000000000000")
    env.run(dry=True)
    by = {u["id"]: u for u in env.unresolved}
    assert by["s1"]["reason"] == "no_result" and by["s1"]["cand"] == ""
    assert by["s2"]["reason"] == "mismatch" and "另一首" in by["s2"]["cand"]
    # 负缓存命中（不再查 LB）时原因依然能给出
    env.lb.lookup_calls.clear(); env.run(dry=True)
    assert env.lb.lookup_calls == [] and {u["reason"] for u in env.unresolved} == {"no_result", "mismatch"}


def test_manual_mbid_is_submitted_with_manual_source_and_never_looked_up(env):
    env.nd.songs = [song(1, "甲", "没查到")]
    env.run(dry=True); snapshot(env)
    lb_manual.resolve(env.db, env.kv, "s1", mbid=MB.upper(), now=env.now)       # 大写也接受，统一小写
    env.lb.lookup_calls.clear()
    st = env.run()
    assert env.lb.love_calls == [MB] and st["via_manual"] == 1 and "unresolved" not in st
    assert env.lb.lookup_calls == []


def test_ignored_song_is_neither_submitted_nor_listed(env):
    env.nd.songs = [song(1, "甲", "不想同步"), song(2, "乙", "正常", mbid=MB)]
    env.run(dry=True); snapshot(env)
    lb_manual.resolve(env.db, env.kv, "s1", ignore=True, now=env.now)
    st = env.run()
    assert env.lb.love_calls == [MB] and st["ignored"] == 1 and "unresolved" not in st and env.unresolved == []


def test_manual_rows_survive_automatic_cache_writes_and_expiry(env):
    env.nd.songs = [song(1, "甲", "没查到")]
    env.run(dry=True); snapshot(env)
    lb_manual.resolve(env.db, env.kv, "s1", mbid=MB, now=env.now)
    na, nt = artist_key("甲"), title_key("没查到")
    lb_repo.cache_put_mbid(env.db, na, nt, "", False, env.now, reason="no_result")        # 自动结果想覆盖：不允许
    assert lb_repo.cache_get_mbid(env.db, na, nt)["mbid"] == MB
    env.now += timedelta(days=365)                                                          # 负缓存早过期了，手动的仍有效
    env.run(); assert env.lb.love_calls == [MB]


def test_resolve_validates_and_uses_server_side_names(env):
    env.nd.songs = [song(1, "甲", "没查到")]
    env.run(dry=True); snapshot(env)
    for bad in ("", "not-a-mbid", MB[:-1], MB + "0"):
        with pytest.raises(ValueError, match="MBID 格式"):
            lb_manual.resolve(env.db, env.kv, "s1", mbid=bad)
    with pytest.raises(ValueError, match="不在未解析清单"):
        lb_manual.resolve(env.db, env.kv, "ghost", mbid=MB)
    assert lb_manual.view(env.db, env.kv)["manual"] == []                                   # 校验失败什么都没写


def test_resolve_removes_item_and_undo_brings_it_back(env):
    env.nd.songs = [song(1, "甲", "没查到")]
    env.run(dry=True); snapshot(env)
    assert lb_manual.resolve(env.db, env.kv, "s1", mbid=MB)["remaining"] == 0
    v = lb_manual.view(env.db, env.kv)
    assert v["items"] == [] and v["manual"][0]["mbid"] == MB and v["manual"][0]["artist"] == "甲"
    lb_manual.undo(env.db, v["manual"][0]["norm_artist"], v["manual"][0]["norm_title"])
    assert lb_manual.view(env.db, env.kv)["manual"] == []
    env.run(dry=True)                                                                       # 撤销后恢复自动解析（查不到→又在清单里）
    assert [u["id"] for u in env.unresolved] == ["s1"]
    with pytest.raises(ValueError):
        lb_manual.undo(env.db, "x", "y")


def test_undo_never_touches_automatic_cache_rows(env):
    env.nd.songs = [song(1, "甲", "没查到")]
    env.run(dry=True)
    with pytest.raises(ValueError):
        lb_manual.undo(env.db, artist_key("甲"), title_key("没查到"))
    assert lb_repo.cache_get_mbid(env.db, artist_key("甲"), title_key("没查到")) is not None


def test_set_manual_needs_exactly_one_of_mbid_or_ignore(env):
    with pytest.raises(ValueError):
        lb_repo.cache_set_manual(env.db, "a", "b", artist="a", title="b")
    with pytest.raises(ValueError):
        lb_repo.cache_set_manual(env.db, "a", "b", artist="a", title="b", mbid=MB, ignored=True)


# ---------------------------------------------------------------- MusicBrainz 搜索
class Resp:
    def __init__(self, code, data=None, bad=False):
        self.status_code, self._d, self._bad = code, data, bad

    def json(self):
        if self._bad:
            raise ValueError("x")
        return self._d


class Sess:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


def search(resp, artist="歌手", title="歌名"):
    s = Sess(resp)
    return mb_client.search_recordings(artist, title, session=s, sleep=lambda x: None), s


def test_mb_search_parses_results_and_sends_identifying_user_agent():
    out, s = search(Resp(200, {"recordings": [
        {"id": MB, "title": "T", "score": 100, "length": 1000, "artist-credit": [{"name": "A", "joinphrase": " & "}, {"name": "B"}],
         "releases": [{"title": "R"}]},
        {"id": "bad-id", "title": "x"}]}))
    assert out == [{"mbid": MB, "title": "T", "artist": "A & B", "release": "R", "length_ms": 1000, "score": 100}]
    url, kw = s.calls[0]
    assert url.startswith("https://musicbrainz.org/") and "MusicPilot" in kw["headers"]["User-Agent"]


def test_mb_search_escapes_lucene_syntax():
    _, s = search(Resp(200, {"recordings": []}), artist='A" OR artist:"x', title="a:b (c)")
    q = s.calls[0][1]["params"]["query"]
    assert '\\"' in q and "\\:" in q and "\\(" in q and 'OR artist:"x' not in q.replace('\\"', "")


@pytest.mark.parametrize("resp,msg", [(Resp(503), "限流"), (Resp(500), "HTTP 500"), (Resp(200, bad=True), "无法解析"),
                                      (requests.ConnectionError("x"), "连接 MusicBrainz 失败")])
def test_mb_search_errors_are_readable(resp, msg):
    with pytest.raises(mb_client.MBError, match=msg):
        search(resp)


def test_mb_search_needs_a_title():
    with pytest.raises(mb_client.MBError):
        search(Resp(200, {}), title=" ")
