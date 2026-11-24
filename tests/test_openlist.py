"""OpenList 网盘渠道：客户端（假会话）、文件名解析、搜索/选歌/下载/补全标签、与音乐平台的回退组合、设置与连接测试。"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.flac import FLAC

import lb_checks
import lb_settings
import openlist_channel as oc
from audio import make_flac
from fakes import lb_defaults
from lb_adapter import AdapterResult, PlatformAdapter
from openlist_client import OpenListClient, OpenListError


# ---------------------------------------------------------------- 假 OpenList（会话级）
class Resp:
    def __init__(self, data=None, status=200, body=b"", chunks=None):
        self._d, self.status_code, self._chunks = data, status, chunks if chunks is not None else [body]

    def json(self):
        if self._d is None:
            raise ValueError("not json")
        return self._d

    def iter_content(self, n):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    """按 /api/... 路径应答；files = {网盘路径: bytes}。"""

    def __init__(self, files=None, index=True):
        self.files, self.index, self.calls, self.logins = files or {}, index, [], 0
        self.expire_next = False
        self.direct = {}

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path
        self.calls.append((path, json, dict(headers or {})))
        if path == "/api/auth/login":
            self.logins += 1
            if json["password"] != "pw":
                return Resp({"code": 400, "message": "password incorrect"})
            return Resp({"code": 200, "data": {"token": f"tok{self.logins}"}})
        if headers.get("Authorization") != f"tok{self.logins}" or self.expire_next:
            self.expire_next = False
            return Resp({"code": 401, "message": "token is expired"}, 401)
        if path == "/api/fs/search":
            if not self.index:
                return Resp({"code": 500, "message": "search not available: index is not enabled"})
            kws = json["keywords"].split()
            hits = [p for p in self.files if all(k.lower() in p.lower() for k in kws)]
            return Resp({"code": 200, "data": {"content": [
                {"parent": p.rsplit("/", 1)[0], "name": p.rsplit("/", 1)[1], "is_dir": False, "size": len(self.files[p])} for p in hits]}})
        if path == "/api/fs/get":
            p = json["path"]
            if p not in self.files:
                return Resp({"code": 500, "message": "object not found"})
            return Resp({"code": 200, "data": {"name": p.rsplit("/", 1)[1], "size": len(self.files[p]), "raw_url": f"http://cdn{p}"}})
        if path == "/api/fs/list":
            return Resp({"code": 200, "data": {"content": [{"name": "周杰伦", "is_dir": True}]}})
        return Resp({"code": 404, "message": "x"}, 404)

    def get(self, url, headers=None, stream=True, timeout=None, allow_redirects=True):
        p = url.split("http://cdn", 1)[1] if url.startswith("http://cdn") else None
        if p is None or p not in self.files:
            return Resp(status=404)
        data = self.direct.get(p, self.files[p])
        return Resp(status=200, chunks=[data[:len(data) // 2], data[len(data) // 2:]])


def flac_bytes(tmp_path, artist=None, title=None, seconds=200, extra=None):
    p = make_flac(tmp_path / "src.flac", artist, title, seconds=seconds, extra=extra)
    return p.read_bytes()


@pytest.fixture
def client_for():
    def mk(files=None, index=True, pw="pw"):
        s = FakeSession(files, index)
        return OpenListClient("http://ol:5244/", "u", pw, session=s), s
    return mk


# ---------------------------------------------------------------- 文件名解析
def test_parse_names_handles_common_layouts():
    assert ("周杰伦", "晴天") in oc.parse_names("/音乐/周杰伦/叶惠美/周杰伦 - 晴天.flac")
    assert ("晴天", "周杰伦") in oc.parse_names("/音乐/x/周杰伦 - 晴天.flac")               # 顺序不定：两种都给
    assert ("周杰伦", "晴天") in oc.parse_names("/音乐/周杰伦/叶惠美/03.晴天.flac")          # 只有歌名：歌手取上级目录
    assert ("周杰伦", "晴天") in oc.parse_names("/音乐/周杰伦 - 叶惠美 - 晴天.flac")          # 歌手 - 专辑 - 歌名
    assert oc.parse_names("/音乐/周杰伦/01 - 晴天.flac")[0][1] in ("晴天", "周杰伦")


# ---------------------------------------------------------------- 客户端
def test_client_logs_in_lazily_and_searches_files_only(client_for):
    c, s = client_for({"/音乐/A/A - S.flac": b"x" * 10})
    assert s.logins == 0
    r = c.search("/音乐", "A S")
    assert s.logins == 1 and r == [{"path": "/音乐/A/A - S.flac", "name": "A - S.flac", "size": 10}]
    assert s.calls[-1][2] == {"Authorization": "tok1"}


def test_client_relogs_in_once_when_the_token_expires(client_for):
    c, s = client_for({"/音乐/a.flac": b"1"})
    c.search("/音乐", "a")
    s.expire_next = True
    assert c.search("/音乐", "a") and s.logins == 2


def test_client_error_kinds(client_for):
    c, s = client_for(pw="pw")
    c.password = "bad"
    with pytest.raises(OpenListError) as e:
        c.login()
    assert e.value.kind == "auth"
    c2, s2 = client_for(index=False)
    with pytest.raises(OpenListError) as e:
        c2.search("/音乐", "a")
    assert e.value.kind == "index" and "索引" in str(e.value)
    import requests
    class Boom:
        def request(self, *a, **k): raise requests.ConnectionError("x")
    with pytest.raises(OpenListError) as e:
        OpenListClient("http://ol", "u", "pw", session=Boom()).login()
    assert e.value.kind == "network"


def test_client_download_checks_size_and_leaves_no_part_file(client_for, tmp_path):
    c, s = client_for({"/音乐/a.flac": b"abcdefgh"})
    dest = tmp_path / "o" / "a.flac"
    dest.parent.mkdir()
    assert c.download("/音乐/a.flac", dest) == 8 and dest.read_bytes() == b"abcdefgh"
    s.direct["/音乐/a.flac"] = b"abc"                                           # 直链给了残缺内容
    dest2 = tmp_path / "o" / "b.flac"
    with pytest.raises(OpenListError):
        c.download("/音乐/a.flac", dest2)
    assert not dest2.exists() and not list(dest.parent.glob("*.part"))


# ---------------------------------------------------------------- 渠道
def adapter(client_for, tmp_path, files, **kw):
    c, s = client_for(files)
    return oc.OpenListAdapter(c, "/音乐", sleep=lambda x: None, **kw), s


def test_downloaded_file_is_held_for_review_untouched_with_a_proposal(client_for, tmp_path):
    data = flac_bytes(tmp_path)                                                  # 没有任何标签的 flac
    a, s = adapter(client_for, tmp_path, {"/音乐/周杰伦/叶惠美/周杰伦 - 晴天.flac": data, "/音乐/周杰伦/周杰伦 - 晴天.mp3": b"mp3"})
    out = tmp_path / "out"
    r = a.download_song("周杰伦", "晴天", out, artists=["周杰伦"], album="叶惠美", duration_ms=200000, recording_mbid=MBID)
    assert r.success and r.quality == "lossless" and r.file_type == "flac" and r.review
    held = Path(r.path)
    assert held.parent == out / ".review" and held.read_bytes() == data         # 文件原样放进 .review/，没有入库，没有被改动
    assert not list(out.glob("*.flac")) and not list((out / ".staging").glob("*"))
    assert r.review["original"]["tags"] == [] and r.review["original"]["pictures"] == []
    p = r.review["proposed"]
    assert (p["artist"], p["title"], p["album"], p["recording_mbid"]) == ("周杰伦", "晴天", "叶惠美", MBID)


def test_original_tags_are_listed_for_review_and_the_file_is_not_modified(client_for, tmp_path):
    data = flac_bytes(tmp_path, "文件里的歌手", "晴天", extra={"album": ["原专辑"]})
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦/周杰伦 - 晴天.flac": data})
    r = a.download_song("周杰伦", "晴天", tmp_path / "out", album="别的专辑")
    tags = dict(r.review["original"]["tags"])
    assert r.success and tags["artist"] == "文件里的歌手" and tags["album"] == "原专辑"        # 原有标签如实列出，等你对比
    assert FLAC(r.path)["artist"] == ["文件里的歌手"]                                        # 文件本身没被改动
    assert r.review["proposed"]["artist"] == "周杰伦" and r.review["proposed"]["album"] == "别的专辑"


def test_platform_metadata_goes_into_the_proposal_only_when_hinted(client_for, tmp_path):
    info = SimpleNamespace(album="叶惠美", publishTime="2003-07-31", lyric="[00:01]hi", tlyric="", track_number=3, pic_url="http://c/1.jpg")
    seen = []
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)},
                   meta_lookup=lambda cid: seen.append(cid) or info, fetch_cover=lambda u: b"\xff\xd8jpegdata")
    r = a.download_song("周杰伦", "晴天", tmp_path / "o1", candidate_hint=77)
    p = r.review["proposed"]
    assert seen == [77] and p["date"] == "2003-07-31" and p["lyric"] == "[00:01]hi" and p["has_cover"]
    assert Path(r.review["cover_path"]).read_bytes() == b"\xff\xd8jpegdata"
    a2, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)}, meta_lookup=lambda c: seen.append(c) or info)
    r2 = a2.download_song("周杰伦", "晴天", tmp_path / "o2")                      # 没有提示（平台根本没有这首）：不查平台，留空
    assert seen == [77] and not r2.review["proposed"]["lyric"] and not r2.review["proposed"]["has_cover"] and not r2.review["cover_path"]


def test_meta_lookup_failure_is_ignored(client_for, tmp_path):
    def boom(cid): raise RuntimeError("x")
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)}, meta_lookup=boom)
    assert a.download_song("周杰伦", "晴天", tmp_path / "o", candidate_hint=1).success


def test_failure_kinds(client_for, tmp_path):
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.mp3": b"m", "/音乐/别人 - 晴天.flac": flac_bytes(tmp_path)})
    assert a.download_song("周杰伦", "晴天", tmp_path / "o").error_kind == "only_mp3"       # 只有有损版本
    assert a.download_song("周杰伦", "不存在的歌", tmp_path / "o").error_kind == "not_found"
    b, _ = adapter(client_for, tmp_path, {})
    c, s = client_for({}, index=False)
    assert oc.OpenListAdapter(c, "/音乐", sleep=lambda x: None).download_song("a", "b", tmp_path / "o").error_kind == "api"   # 索引问题可重试，不记成未找到


def test_wrong_duration_version_is_skipped_for_the_next_candidate(client_for, tmp_path):
    short = flac_bytes(tmp_path, seconds=30)
    full = flac_bytes(tmp_path, seconds=200)
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦/周杰伦 - 晴天.flac": short, "/音乐/合集/周杰伦 - 晴天.flac": full})
    r = a.download_song("周杰伦", "晴天", tmp_path / "o", duration_ms=200000)
    assert r.success and r.tried == 2
    b, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": short})
    r2 = b.download_song("周杰伦", "晴天", tmp_path / "o2", duration_ms=200000)
    assert not r2.success and r2.error_kind == "not_found" and not list((tmp_path / "o2").glob("*.flac"))


def test_already_approved_file_is_reused_not_downloaded_again(client_for, tmp_path):
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path, "周杰伦", "晴天")})
    r1 = a.download_song("周杰伦", "晴天", tmp_path / "o")
    final = tmp_path / "o" / Path(r1.path).name
    Path(r1.path).rename(final)                                                  # 模拟：用户审核通过，文件已入库
    r2 = a.download_song("周杰伦", "晴天", tmp_path / "o")
    assert r2.success and r2.reused and r2.path == str(final) and not r2.review


# ---------------------------------------------------------------- 回退组合
class Stub:
    def __init__(self, res): self.res, self.calls = res, []
    def download_song(self, artist, title, out, **kw):
        self.calls.append(kw); return self.res


def test_fallback_only_for_not_found_only_mp3_no_copyright():
    ok = AdapterResult(True, "/x.flac")
    p = Stub(ok); s = Stub(ok)
    assert oc.FallbackAdapter(p, s).download_song("a", "b", "/o").success and not s.calls
    for kind in ("api", "download", "invalid", "rate_limited"):
        p, s = Stub(AdapterResult(False, error_kind=kind)), Stub(ok)
        assert oc.FallbackAdapter(p, s).download_song("a", "b", "/o").error_kind == kind and not s.calls
    for kind in oc.FALLBACK_KINDS:
        p, s = Stub(AdapterResult(False, error_kind=kind, candidate_id=9)), Stub(ok)
        assert oc.FallbackAdapter(p, s).download_song("a", "b", "/o", platform_id=5).success
        assert s.calls == [{"candidate_hint": 9, "platform_id": 5}]


def test_fallback_error_semantics():
    plat = AdapterResult(False, error="平台没有", error_kind="not_found")
    r = oc.FallbackAdapter(Stub(plat), Stub(AdapterResult(False, error="网盘没有", error_kind="not_found"))).download_song("a", "b", "/o")
    assert r.error_kind == "not_found" and "平台没有" in r.error and "网盘没有" in r.error      # 两边都没有 → 未找到
    r = oc.FallbackAdapter(Stub(plat), Stub(AdapterResult(False, error="连不上", error_kind="api"))).download_song("a", "b", "/o")
    assert r.error_kind == "api"                                                              # 网盘临时故障 → 可重试，不进冷却


def test_platform_adapter_reports_candidate_id_when_it_has_the_song_but_cannot_download():
    class DL:
        def get_music_info(self, *a, **k): return None                                         # 只有 MP3
    p = PlatformAdapter({}, DL(), "lossless", search=lambda *a, **k: [], sleep=lambda x: None)
    p._find = lambda t, al: [SimpleNamespace(id=4242)]
    r = p.download_song("A", "T", "/tmp/x")
    assert r.error_kind == "only_mp3" and r.candidate_id == 4242


# ---------------------------------------------------------------- 设置与连接测试
def test_settings_validate_and_hide_the_password():
    s = lb_settings.apply(lb_defaults(), {"openlist": {"url": "http://nas:5244/", "username": "u", "password": "pw", "music_dir": "音乐/"}})
    assert s["openlist"] == {"enabled": False, "url": "http://nas:5244", "username": "u", "password": "pw", "music_dir": "/音乐"}
    s2 = lb_settings.apply(s, {"openlist": {"enabled": True, "password": ""}})              # 密码留空 = 不修改
    assert s2["openlist"]["password"] == "pw" and s2["openlist"]["enabled"]
    v = lb_settings.view(s2, "UTC")["openlist"]
    assert v["has_password"] and "password" not in v
    with pytest.raises(ValueError):
        lb_settings.apply(lb_defaults(), {"openlist": {"enabled": True}})                   # 没填地址/目录不能启用
    with pytest.raises(ValueError):
        lb_settings.apply(lb_defaults(), {"openlist": {"url": "ftp://x"}})
    with pytest.raises(ValueError):
        lb_settings.apply(lb_defaults(), {"openlist": {"music_dir": "/音乐/../etc"}})


def test_wrap_only_when_enabled_and_complete():
    prim = SimpleNamespace(downloader=None, quality="lossless", request_delay=0)
    s = lb_defaults()
    assert oc.wrap_with_openlist(prim, s) is prim
    s["openlist"].update(enabled=True, url="http://x", username="u", password="p", music_dir="/音乐")
    w = oc.wrap_with_openlist(prim, s)
    assert isinstance(w, oc.FallbackAdapter) and w.primary is prim and w.secondary.music_dir == "/音乐"


def test_connection_test_reports_missing_index(client_for):
    s = lb_defaults(); s["openlist"].update(url="http://x", username="u", password="pw", music_dir="/音乐")
    c, _ = client_for({"/音乐/a.flac": b"1"})
    assert "搜索索引可用" in lb_checks.test_openlist(s, c)["message"]
    c2, _ = client_for(index=False)
    r = lb_checks.test_openlist(s, c2)
    assert r["warnings"] and "索引" in r["warnings"][0]
    c3, _ = client_for(pw="pw"); c3.password = "bad"
    with pytest.raises(ValueError):
        lb_checks.test_openlist(s, c3)
    s["openlist"]["music_dir"] = ""
    with pytest.raises(ValueError):
        lb_checks.test_openlist(s)


# ---------------------------------------------------------------- 外部元数据来源（MusicBrainz / 封面库 / 歌词库）
import meta_sources
import mb_client

MBID = "11111111-2222-3333-4444-555555555555"
R1, R2 = "aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 20


class R:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._d, self.content = status, data, content

    def json(self):
        return self._d


class NetSession:
    """MusicBrainz / CAA / LRCLIB 的假应答；calls 记录被访问的地址。"""

    def __init__(self, releases=None, covers=None, lyric=None, search=None, mb_status=200):
        self.releases = releases if releases is not None else []
        self.covers, self.lyric, self.search, self.mb_status, self.calls = covers or {}, lyric, search or [], mb_status, []

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append(url)
        if "musicbrainz.org/ws/2/recording/" in url:
            return R(self.mb_status, {"releases": self.releases})
        if "coverartarchive.org" in url:
            rid = url.split("/release/")[1].split("/")[0]
            return R(200, content=self.covers[rid]) if rid in self.covers else R(404)
        if url.endswith("/api/get"):
            return R(200, self.lyric) if self.lyric else R(404)
        if url.endswith("/api/search"):
            return R(200, self.search)
        raise AssertionError(url)


@pytest.fixture(autouse=True)
def _mb_throttle_reset():
    mb_client._last[0] = -1e9
    yield


def ext(session):
    return meta_sources.ExternalMeta(session, sleep=lambda x: None, clock=lambda: 1e6)


def test_releases_prefer_matching_album_then_official_earliest():
    s = NetSession(releases=[{"id": R2, "title": "精选集", "date": "2010-01-01", "status": "Official"},
                             {"id": R1, "title": "叶惠美", "date": "2003-07-31", "status": "Official"},
                             {"id": MBID, "title": "Bootleg", "date": "1999", "status": "Bootleg"}])
    assert [r["id"] for r in ext(s).releases(MBID, "叶惠美")][0] == R1
    assert [r["id"] for r in ext(s).releases(MBID, "")][:2] == [R1, R2]                 # 没有专辑名：正式发行里最早的在前（盗版排后）
    assert ext(NetSession(mb_status=503)).releases(MBID) == []


def test_lookup_collects_date_cover_and_lyrics_and_falls_through_missing_covers():
    s = NetSession(releases=[{"id": R1, "title": "叶惠美", "date": "2003-07-31", "status": "Official"},
                             {"id": R2, "title": "叶惠美 (再版)", "date": "2004", "status": "Official"}],
                   covers={R2: JPEG}, lyric={"syncedLyrics": "[00:01.00]晴天", "plainLyrics": "晴天", "duration": 269})
    m = ext(s).lookup(recording_mbid=MBID, artist="周杰伦", title="晴天", album="叶惠美", duration_s=269)
    assert (m.album, m.publishTime, m.release_mbid, m.cover, m.lyric) == ("叶惠美", "2003-07-31", R1, JPEG, "[00:01.00]晴天")   # 第一个发行没封面，用第二个的


def test_lyrics_prefer_matching_duration_and_ignore_other_versions():
    near = {"syncedLyrics": "", "plainLyrics": "对的版本", "duration": 270}
    far = {"syncedLyrics": "[00:01.00]别的", "duration": 400}
    e = ext(NetSession(lyric=far, search=[far, near]))
    assert e.lyrics("a", "b", "", 269) == "对的版本"
    assert ext(NetSession(lyric=None, search=[far])).lyrics("a", "b", "", 269) == ""
    assert ext(NetSession(lyric={"plainLyrics": "无时长", "duration": None})).lyrics("a", "b", "", 269) == "无时长"


def test_each_source_failing_never_breaks_the_lookup():
    class Boom(NetSession):
        def get(self, url, **k):
            if "musicbrainz" in url:
                raise requests_error()
            return super().get(url, **k)
    import requests
    requests_error = requests.ConnectionError
    m = ext(Boom(lyric={"plainLyrics": "词", "duration": 200})).lookup(recording_mbid=MBID, artist="a", title="b", duration_s=200)
    assert m.lyric == "词" and m.publishTime == "" and m.cover is None
    m2 = ext(NetSession()).lookup(recording_mbid="", artist="a", title="b")             # 没有 MBID：不访问 MusicBrainz
    assert m2.publishTime == ""


def test_proposal_collects_everything_from_external_sources_when_the_platform_has_nothing(client_for, tmp_path):
    s = NetSession(releases=[{"id": R1, "title": "叶惠美", "date": "2003-07-31", "status": "Official"}], covers={R1: JPEG},
                   lyric={"syncedLyrics": "[00:01.00]晴天", "duration": 200})
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)}, external=ext(s))
    r = a.download_song("周杰伦", "晴天", tmp_path / "o", duration_ms=200000, recording_mbid=MBID)      # 没有 candidate_hint：平台根本没有
    p = r.review["proposed"]
    assert (p["date"], p["album"], p["lyric"], p["release_mbid"], p["recording_mbid"]) == ("2003-07-31", "叶惠美", "[00:01.00]晴天", R1, MBID)
    assert p["has_cover"] and Path(r.review["cover_path"]).read_bytes() == JPEG


def test_external_is_skipped_when_the_platform_already_gave_everything(client_for, tmp_path):
    info = SimpleNamespace(album="叶惠美", publishTime="2003", lyric="词", tlyric="", track_number=1, pic_url="http://c/1.jpg")
    s = NetSession()
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)}, external=ext(s),
                   meta_lookup=lambda c: info, fetch_cover=lambda u: JPEG)
    assert a.download_song("周杰伦", "晴天", tmp_path / "o", candidate_hint=5, recording_mbid=MBID).success and s.calls == []


def test_platform_values_win_and_external_only_fills_gaps(client_for, tmp_path):
    info = SimpleNamespace(album="平台专辑", publishTime="2003-07-31", lyric="", tlyric="", track_number=0, pic_url="")
    s = NetSession(releases=[{"id": R1, "title": "MB专辑", "date": "1999", "status": "Official"}], covers={R1: JPEG},
                   lyric={"plainLyrics": "外部歌词", "duration": 200})
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)}, external=ext(s), meta_lookup=lambda c: info)
    p = a.download_song("周杰伦", "晴天", tmp_path / "o", candidate_hint=5, duration_ms=200000, recording_mbid=MBID).review["proposed"]
    assert p["date"] == "2003-07-31" and p["lyric"] == "外部歌词" and p["has_cover"]


def test_external_crash_is_ignored_and_mbid_is_still_proposed(client_for, tmp_path):
    class Crash:
        def lookup(self, **k): raise RuntimeError("boom")
    a, _ = adapter(client_for, tmp_path, {"/音乐/周杰伦 - 晴天.flac": flac_bytes(tmp_path)}, external=Crash())
    r = a.download_song("周杰伦", "晴天", tmp_path / "o", recording_mbid=MBID)
    assert r.success and r.review["proposed"]["recording_mbid"] == MBID


def test_wrap_uses_external_sources():
    s = lb_defaults(); s["openlist"].update(enabled=True, url="http://x", username="u", password="p", music_dir="/音乐")
    w = oc.wrap_with_openlist(SimpleNamespace(downloader=None, quality="lossless", request_delay=0), s)
    assert isinstance(w.secondary.external, meta_sources.ExternalMeta)
