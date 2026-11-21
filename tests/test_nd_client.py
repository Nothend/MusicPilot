import hashlib

import pytest
import requests

from fakes import FakeResp, FakeSession
from nd_client import NavidromeClient, NavidromeError


def ok(**payload):
    return FakeResp(200, {"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}})


def client(responses):
    sess = FakeSession(responses)
    return NavidromeClient("http://nd:4533/", "u", "PASSWORD", session=sess, sleep=lambda s: None), sess


def song(i, **kw):
    return {"id": f"s{i}", "title": f"t{i}", "artist": "a", "album": "b", "duration": 100, "suffix": "flac",
            "musicBrainzId": "", **kw}


def test_auth_token_is_md5_of_password_and_salt_and_password_not_sent():
    c, sess = client([ok(serverVersion="0.64.2", type="navidrome", openSubsonic=True)])
    assert c.ping()["version"] == "0.64.2"
    p = sess.calls[0]["params"]
    assert p["t"] == hashlib.md5(("PASSWORD" + p["s"]).encode()).hexdigest()
    assert "PASSWORD" not in str(sess.calls[0]) and sess.calls[0]["url"] == "http://nd:4533/rest/ping"


def test_salt_changes_every_call():
    c, sess = client([ok(), ok()])
    c.ping(); c.ping()
    assert sess.calls[0]["params"]["s"] != sess.calls[1]["params"]["s"]


def test_error_response_maps_to_exception_with_code():
    c, _ = client([FakeResp(200, {"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})])
    with pytest.raises(NavidromeError, match="Wrong username") as e:
        c.ping()
    assert e.value.code == 40


def test_connection_and_format_errors_do_not_leak_params():
    c, _ = client([requests.ConnectionError("boom")])
    with pytest.raises(NavidromeError, match="无法连接") as e:
        c.ping()
    assert "PASSWORD" not in str(e.value)
    c, _ = client([FakeResp(200, None, raw="<html>")])
    with pytest.raises(NavidromeError, match="格式异常"):
        c.ping()
    c, _ = client([FakeResp(502, {})])
    with pytest.raises(NavidromeError, match="502"):
        c.ping()


def test_music_folders():
    c, _ = client([ok(musicFolders={"musicFolder": [{"id": 1, "name": "Music Library"}, {"id": 2, "name": "Discovery"}]})])
    assert c.music_folders() == [{"id": 1, "name": "Music Library"}, {"id": 2, "name": "Discovery"}]


def test_iter_songs_pages_until_short_page_and_filters_folder():
    pages = [ok(searchResult3={"song": [song(i) for i in range(3)]}),
             ok(searchResult3={"song": [song(3)]})]
    c, sess = client(pages)
    got = list(c.iter_songs(folder_id=2, page=3))
    assert [g["id"] for g in got] == ["s0", "s1", "s2", "s3"]
    assert [x["params"]["songOffset"] for x in sess.calls] == [0, 3]
    assert all(x["params"]["musicFolderId"] == 2 and x["params"]["query"] == "" for x in sess.calls)


def test_empty_library_returns_nothing():
    c, _ = client([ok(searchResult3={})])
    assert list(c.iter_songs()) == []


def test_song_mapping_marks_path_as_display_only():
    c, _ = client([ok(searchResult3={"song": [song(1, path="x/y.flac", musicBrainzId="mb", starred="2026-01-01")]})])
    s = list(c.iter_songs())[0]
    assert s["mbid"] == "mb" and s["starred"] is True and s["path_api"] == "x/y.flac" and "path" not in s


def test_scan_and_wait_polls_until_idle():
    c, sess = client([ok(), ok(scanStatus={"scanning": True, "count": 1}), ok(scanStatus={"scanning": True}),
                      ok(scanStatus={"scanning": False, "count": 9, "lastScan": "t"})])
    st = c.scan_and_wait(timeout=5, poll=0.01)
    assert st == {"scanning": False, "count": 9, "last_scan": "t"}


def test_scan_timeout():
    c, _ = client([ok()] + [ok(scanStatus={"scanning": True})] * 50)
    with pytest.raises(NavidromeError, match="超时"):
        c.scan_and_wait(timeout=0, poll=0.01)


def test_star_playlist_calls():
    c, sess = client([ok(), ok(), ok(playlist={"id": "p1"}), ok(), ok(starred2={"song": [song(1)]})])
    c.star("s1"); c.unstar("s1")
    assert c.create_playlist("LB Daily", ["a", "b"]) == "p1"
    assert sess.calls[2]["params"]["songId"] == ["a", "b"] and sess.calls[2]["params"]["name"] == "LB Daily"
    c.delete_playlist("p1")
    assert sess.calls[3]["url"].endswith("/deletePlaylist") and sess.calls[3]["params"]["id"] == "p1"
    assert c.starred_songs()[0]["id"] == "s1"
