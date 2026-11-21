import pytest
import requests

from fakes import FakeResp, FakeSession
from http_client import APIException
from matcher import Target
from netease_search import Candidate, SearchRateLimited, rank_candidates, search_songs


def song(i, name, artists, album, dt, fee=1, alia=None):
    return {"id": i, "name": name, "ar": [{"name": a} for a in artists], "al": {"name": album}, "dt": dt * 1000,
            "fee": fee, "alia": alia or []}


def body(*songs):
    return FakeResp(200, {"code": 200, "result": {"songs": list(songs), "songCount": len(songs)}})


def test_parse_results():
    sess = FakeSession([body(song(1, "心墙", ["郭静"], "在树上唱歌", 226, alia=["别名"]))])
    c = search_songs({"MUSIC_U": "x"}, "郭靜 心牆", session=sess)[0]
    assert (c.id, c.name, c.artists, c.album, c.duration_s, c.alias, c.rank) == (1, "心墙", ["郭静"], "在树上唱歌", 226, ["别名"], 0)
    assert sess.calls[0]["data"]["s"] == "郭靜 心牆" and sess.calls[0]["data"]["type"] == 1


def test_empty_result_is_empty_list_not_error():
    assert search_songs({}, "zzz", session=FakeSession([FakeResp(200, {"code": 200, "result": {}})])) == []


def test_rate_limit_is_retried_with_backoff_then_succeeds():
    sleeps = []
    sess = FakeSession([FakeResp(200, {"code": 405, "message": "操作频繁，请稍候再试"}),
                        FakeResp(200, {"code": 405}), body(song(1, "a", ["b"], "c", 100))])
    got = search_songs({}, "q", session=sess, sleep=sleeps.append, backoff=10)
    assert len(got) == 1 and sleeps == [10, 20]


def test_rate_limit_exhausted_raises_distinct_error():
    sess = FakeSession([FakeResp(200, {"code": 405})] * 3)
    with pytest.raises(SearchRateLimited):
        search_songs({}, "q", session=sess, sleep=lambda s: None)
    assert issubclass(SearchRateLimited, APIException)       # 兼容只捕获 APIException 的旧调用方


def test_other_failures_raise_api_exception_without_retry():
    with pytest.raises(APIException, match="code=500"):
        search_songs({}, "q", session=FakeSession([FakeResp(200, {"code": 500, "message": "x"})]))
    with pytest.raises(APIException, match="请求失败"):
        search_songs({}, "q", session=FakeSession([requests.ConnectionError("x")]))
    with pytest.raises(APIException, match="JSON"):
        search_songs({}, "q", session=FakeSession([FakeResp(200, None, raw="<html>")]))


# ---------------------------------------------------------------- 选歌（来自真实搜索：郭靜 心牆）
def real_cands():
    return [
        Candidate(27671269, "心墙", ["郭静"], "致纯静", 227, rank=0),
        Candidate(233866, "心墙", ["郭静"], "在树上唱歌", 226, rank=1),
        Candidate(2032434515, "心墙 (Live版)", ["郭静"], "声生不息·宝岛季 第2期", 261, rank=2),
        Candidate(1413872119, "心墙", ["任嘉伦"], "心墙", 283, rank=3),
        Candidate(233888, "爱情讯息", ["郭静"], "下一个天亮", 280, rank=4),
    ]


def target(**kw):
    d = dict(artist="郭靜", title="心牆", artists=["郭靜"], duration_s=227)
    d.update(kw)
    return Target(**d)


def test_ranking_filters_live_other_artist_and_other_songs():
    ranked = rank_candidates(target(), "", real_cands())
    assert {c.id for c in ranked} == {27671269, 233866}


def test_album_breaks_ties_between_same_recording_on_different_albums():
    ranked = rank_candidates(target(), "在樹上唱歌", real_cands())      # LB 给的是繁体专辑名
    assert [c.id for c in ranked] == [233866, 27671269]


def test_without_album_closest_duration_then_relevance():
    ranked = rank_candidates(target(duration_s=227), "", real_cands())
    assert ranked[0].id == 27671269                                  # 227s 比 226s 更接近


def test_no_acceptable_candidate_gives_empty_list():
    assert rank_candidates(target(title="完全不同的歌"), "", real_cands()) == []
    assert rank_candidates(target(), "", []) == []
    assert rank_candidates(target(duration_s=500), "", real_cands()) == []       # 时长差太多 → 视为不同版本


def test_target_wanting_live_picks_live_version():
    ranked = rank_candidates(target(title="心牆 (Live)", duration_s=260), "", real_cands())
    assert [c.id for c in ranked] == [2032434515]


def test_unknown_duration_still_resolves_to_ordered_candidates():
    ranked = rank_candidates(target(duration_s=None), "在树上唱歌", real_cands())
    assert ranked[0].id == 233866 and {c.id for c in ranked} == {27671269, 233866}
