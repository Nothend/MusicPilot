import pytest
import requests

from fakes import FakeResp, FakeSession
from lb_client import LBError, ListenBrainzClient, classify

MB = "https://musicbrainz.org/doc/jspf#"


def client(responses, **kw):
    sess = FakeSession(responses)
    sleeps = []
    c = ListenBrainzClient("SECRET", "me", session=sess, sleep=sleeps.append, **kw)
    return c, sess, sleeps


PLAYLIST = {"playlist": {
    "title": "Weekly Jams for me, week of 2026-09-14 Mon", "date": "2026-09-14T00:12:01+00:00",
    "track": [{
        "title": "心牆", "creator": "郭靜", "album": "在樹上唱歌", "duration": 227000,
        "identifier": ["https://musicbrainz.org/recording/1c37cb82-2af9-481c-a214-276a59b0ff9e"],
        "extension": {MB + "track": {"additional_metadata": {"artists": [
            {"artist_credit_name": "郭靜", "artist_mbid": "eb7f", "join_phrase": ""}]}}},
    }, {"title": "无专辑", "creator": "X", "identifier": "https://musicbrainz.org/recording/abc"}]}}


def test_validate_token_sends_auth_and_hides_nothing_in_url():
    c, sess, _ = client([FakeResp(200, {"valid": True, "user_name": "me"})])
    assert c.validate_token() == {"valid": True, "user_name": "me"}
    call = sess.calls[0]
    assert call["headers"]["Authorization"] == "Token SECRET" and "SECRET" not in call["url"]


def test_created_for_classifies_by_patch_then_title():
    body = {"playlists": [
        {"playlist": {"identifier": "https://listenbrainz.org/playlist/aaa", "title": "Weekly Jams for me",
                      "date": "2026-09-14T00:00:00+00:00", "extension": {MB + "playlist": {"additional_metadata": {
                          "algorithm_metadata": {"source_patch": "weekly-jams"}, "expires_at": "2026-09-28"}}}}},
        {"playlist": {"identifier": "https://listenbrainz.org/playlist/bbb", "title": "Daily Jams for me on 2026-11-20",
                      "date": "2026-11-20T00:00:00+00:00", "extension": {}}},
        {"playlist": {"identifier": "https://listenbrainz.org/playlist/ccc", "title": "Top Discoveries of 2025 for me",
                      "date": "2026-02-22T00:00:00+00:00"}},
    ]}
    c, _, _ = client([FakeResp(200, body)])
    got = c.created_for()
    assert [(g["mbid"], g["type"]) for g in got] == [("aaa", "weekly_jams"), ("bbb", "daily"), ("ccc", "other")]
    assert got[0]["expires_at"] == "2026-09-28"


def test_classify():
    assert classify("x", "weekly-exploration") == "weekly_exploration"
    assert classify("Daily Jams for me", "") == "daily"
    assert classify("random", "") == "other"


def test_playlist_parsing_handles_missing_fields():
    c, _, _ = client([FakeResp(200, PLAYLIST)])
    pl = c.playlist("pid")
    t0, t1 = pl["tracks"]
    assert t0["recording_mbid"] == "1c37cb82-2af9-481c-a214-276a59b0ff9e"
    assert (t0["artist"], t0["album"], t0["duration_ms"]) == ("郭靜", "在樹上唱歌", 227000)
    assert t0["artists"][0]["mbid"] == "eb7f"
    assert t1["recording_mbid"] == "abc" and t1["album"] == "" and t1["duration_ms"] is None and t1["artists"] == []


def test_lookup_hit_and_miss():
    c, sess, _ = client([FakeResp(200, {"recording_mbid": "r1", "artist_credit_name": "郭靜", "recording_name": "心牆",
                                        "release_name": "R", "artist_mbids": ["a"]}), FakeResp(200, {})])
    assert c.lookup("郭静", "心墙")["recording_mbid"] == "r1"
    assert sess.calls[0]["params"] == {"artist_name": "郭静", "recording_name": "心墙"}
    assert c.lookup("nobody", "nothing") is None


def test_empty_body_200_is_retried():
    c, _, sleeps = client([FakeResp(200, None, raw=""), FakeResp(200, {"valid": True, "user_name": "me"})])
    assert c.validate_token()["valid"] is True
    assert sleeps  # 退避过


def test_429_waits_for_reset_in_then_succeeds():
    c, _, sleeps = client([FakeResp(429, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "4"}),
                           FakeResp(200, {"valid": True})])
    c.validate_token()
    assert sleeps == [4.0]


def test_5xx_exhausts_and_message_has_no_secret():
    c, _, _ = client([FakeResp(503, {})] * 5)
    with pytest.raises(LBError) as e:
        c.validate_token()
    assert "SECRET" not in str(e.value)


def test_network_error_retried_then_raised():
    c, sess, sleeps = client([requests.ConnectionError("x")] * 5)
    with pytest.raises(LBError, match="网络错误"):
        c.validate_token()
    assert sleeps == [2, 4, 8, 16]            # 实测 SSLError 抖动能持续十几秒：退避总共约 30 秒，而不是原来的 6 秒


def test_ssl_error_is_retried_and_can_recover():
    c, _, sleeps = client([requests.exceptions.SSLError("eof")] * 3 + [FakeResp(200, {"valid": True, "user_name": "me"})])
    assert c.validate_token()["valid"] is True and len(sleeps) == 3


def test_4xx_raises_immediately_with_server_message():
    c, sess, _ = client([FakeResp(401, {"error": "Invalid token"})])
    with pytest.raises(LBError, match="Invalid token") as e:
        c.validate_token()
    assert e.value.status == 401 and len(sess.calls) == 1


def test_loved_mbids_paginates():
    c, sess, _ = client([
        FakeResp(200, {"feedback": [{"recording_mbid": "a"}, {"recording_mbid": "b"}], "total_count": 3}),
        FakeResp(200, {"feedback": [{"recording_mbid": "c"}], "total_count": 3})])
    assert c.loved_mbids(page_size=2) == ["a", "b", "c"]
    assert [x["params"]["offset"] for x in sess.calls] == [0, 2]


def test_love_posts_score_one():
    c, sess, _ = client([FakeResp(200, {"status": "ok"})])
    c.love("rid")
    assert sess.calls[0]["method"] == "POST" and sess.calls[0]["json"] == {"recording_mbid": "rid", "score": 1}


def test_batch_date_follows_title_not_utc_creation_time():
    # 真实案例：LB 页面标题 2026-11-23，创建于 UTC 11-22 16:01（北京时间 11-23 00:01）
    from lb_client import batch_date
    assert batch_date("Daily Jams for u, 2026-11-23 Mon", "2026-11-22T16:01:10.101451+00:00") == "2026-11-23"
    assert batch_date("Weekly Jams for u, week of 2026-09-14 Mon", "2026-09-14T00:12:01+00:00") == "2026-09-14"
    assert batch_date("无日期标题", "2026-09-14T00:12:01+00:00") in ("2026-09-14", "2026-09-13", "2026-09-15")
    assert batch_date("", "") == ""
