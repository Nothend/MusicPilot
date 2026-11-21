from pathlib import Path

import pytest

import lb_adapter
from audio import make_flac
from downloader import DownloadException
from lb_adapter import NeteaseAdapter, validate_audio
from models import DownloadResult, MusicInfo
from netease_search import Candidate, SearchRateLimited
from http_client import APIException


def info(i=1, name="心墙", artists=("郭静",), ftype="flac"):
    return MusicInfo(id=i, name=name, publishTime="", artists=list(artists), album="在树上唱歌", pic_url="",
                     duration=227, track_number=1, download_url="http://x", file_type=ftype, file_size=1, quality="lossless")


class FakeDownloader:
    """模拟 SongDownloader：按脚本返回 get_music_info；download_song 写出一个真实的、带标签的 FLAC。"""
    def __init__(self, infos=None, write_tags=True, fail_download=False):
        self.infos = list(infos or [])
        self.download_dir = None
        self.write_tags = write_tags
        self.fail_download = fail_download
        self.info_calls, self.download_calls = [], 0

    def get_music_info(self, cid, quality, allow_mp3=False):
        self.info_calls.append((cid, quality, allow_mp3))
        r = self.infos.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def _target_path(self, mi):
        return Path(self.download_dir) / f"{'&'.join(mi.artists)} - {mi.name}.{mi.file_type}"

    def download_song(self, mi, quality):
        self.download_calls += 1
        if self.fail_download:
            return DownloadResult(success=False, error_message="下载不完整")
        p = self._target_path(mi)
        if p.exists():        # 与真实 SongDownloader.download_song 一致：同名文件已存在就跳过下载、直接当成功
            return DownloadResult(success=True, file_path=str(p), file_size=p.stat().st_size, music_info=mi)
        make_flac(p, artist="&".join(mi.artists) if self.write_tags else None,
                  title=mi.name if self.write_tags else None)
        return DownloadResult(success=True, file_path=str(p), file_size=p.stat().st_size, music_info=mi)


def cands(*specs):
    return [Candidate(id=i, name=n, artists=list(a), album=al, duration_s=d, rank=k) for k, (i, n, a, al, d) in enumerate(specs)]


GOOD = cands((1, "心墙", ["郭静"], "在树上唱歌", 227))


def adapter(tmp_path, downloader, searches, **kw):
    calls = []
    def search(cookies, kw_, limit):
        calls.append(kw_)
        r = searches.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    a = NeteaseAdapter({"MUSIC_U": "x"}, downloader, "lossless", search=search, sleep=lambda s: None, **kw)
    a.calls = calls
    return a


def run(a, out, **kw):
    args = dict(artist="郭靜", title="心牆", artists=["郭靜"], album="在樹上唱歌", duration_ms=227000)
    args.update(kw)
    return a.download_song(args.pop("artist"), args.pop("title"), out, **args)


# ---------------------------------------------------------------- validate_audio
def test_validate_audio(tmp_path):
    assert validate_audio(make_flac(tmp_path / "ok.flac", "A", "T")) is None
    assert "不存在" in validate_audio(tmp_path / "nope.flac")
    (tmp_path / "empty.flac").write_bytes(b"")
    assert "大小为 0" in validate_audio(tmp_path / "empty.flac")
    (tmp_path / "junk.flac").write_bytes(b"not audio at all" * 10)
    assert "元数据" in validate_audio(tmp_path / "junk.flac")
    assert "Artist 或 Title" in validate_audio(make_flac(tmp_path / "t.flac", "A", None))
    assert "Artist 或 Title" in validate_audio(make_flac(tmp_path / "a.flac", None, "T"))
    assert "Artist 或 Title" in validate_audio(make_flac(tmp_path / "b.flac", "", "T"))


# ---------------------------------------------------------------- 成功路径
def test_success_moves_file_into_output_and_cleans_staging(tmp_path):
    out = tmp_path / "discovery"
    dl = FakeDownloader([info()])
    r = run(adapter(tmp_path, dl, [GOOD]), out)
    assert r.success and r.netease_id == 1 and r.file_type == "flac" and r.quality == "lossless" and not r.reused
    assert Path(r.path) == out / "郭静 - 心墙.flac" and Path(r.path).is_file()
    assert validate_audio(Path(r.path)) is None
    assert list((out / ".staging").iterdir()) == []                      # 暂存区已清空
    assert dl.info_calls == [(1, "lossless", False)]                     # 线上只要无损：allow_mp3 恒为 False


def test_uses_artist_plus_title_query_first(tmp_path):
    a = adapter(tmp_path, FakeDownloader([info()]), [GOOD])
    run(a, tmp_path / "d")
    assert a.calls == ["郭靜 心牆"]


# ---------------------------------------------------------------- 找不到 / 限流
def test_not_found_uses_title_only_fallback_then_gives_up(tmp_path):
    a = adapter(tmp_path, FakeDownloader(), [[], []])
    r = run(a, tmp_path / "d")
    assert (r.success, r.error_kind) == (False, "not_found")
    assert a.calls == ["郭靜 心牆", "心牆"]


def test_fallback_query_can_rescue(tmp_path):
    a = adapter(tmp_path, FakeDownloader([info()]), [[], GOOD])
    assert run(a, tmp_path / "d").success


def test_rate_limited_is_not_reported_as_not_found(tmp_path):
    r = run(adapter(tmp_path, FakeDownloader(), [SearchRateLimited("限流")]), tmp_path / "d")
    assert r.error_kind == "rate_limited"
    r = run(adapter(tmp_path, FakeDownloader(), [[], SearchRateLimited("限流")]), tmp_path / "d")
    assert r.error_kind == "rate_limited"


def test_search_api_error_is_retryable_api_kind(tmp_path):
    r = run(adapter(tmp_path, FakeDownloader(), [APIException("boom")]), tmp_path / "d")
    assert r.error_kind == "api"


# ---------------------------------------------------------------- 候选不可用
def test_only_mp3_everywhere(tmp_path):
    dl = FakeDownloader([None, None])
    two = cands((1, "心墙", ["郭静"], "A", 227), (2, "心墙", ["郭静"], "B", 226))
    r = run(adapter(tmp_path, dl, [two]), tmp_path / "d")
    assert (r.success, r.error_kind, r.tried) == (False, "only_mp3", 2)
    assert dl.download_calls == 0


def test_falls_through_unavailable_candidate_to_next(tmp_path):
    dl = FakeDownloader([DownloadException("无版权", kind="no_copyright"), info(2)])
    two = cands((1, "心墙", ["郭静"], "A", 227), (2, "心墙", ["郭静"], "B", 226))
    r = run(adapter(tmp_path, dl, [two]), tmp_path / "d")
    assert r.success and r.netease_id == 2 and r.tried == 2


def test_all_unavailable_is_no_copyright(tmp_path):
    dl = FakeDownloader([DownloadException("VIP 专享", kind="vip_only")])
    r = run(adapter(tmp_path, dl, [GOOD]), tmp_path / "d")
    assert (r.error_kind, r.success) == ("no_copyright", False)


def test_api_error_while_getting_link_stops_immediately(tmp_path):
    dl = FakeDownloader([DownloadException("接口错误", kind="api"), info(2)])
    two = cands((1, "心墙", ["郭静"], "A", 227), (2, "心墙", ["郭静"], "B", 226))
    r = run(adapter(tmp_path, dl, [two]), tmp_path / "d")
    assert (r.error_kind, r.netease_id, r.tried) == ("api", 1, 1)      # 暂时性错误不换候选，留给重试


def test_only_tries_max_candidates(tmp_path):
    many = cands(*[(i, "心墙", ["郭静"], f"A{i}", 227) for i in range(1, 8)])
    dl = FakeDownloader([None] * 7)
    run(adapter(tmp_path, dl, [many], max_candidates=3), tmp_path / "d")
    assert len(dl.info_calls) == 3


# ---------------------------------------------------------------- 下载与校验失败
def test_download_failure_leaves_nothing(tmp_path):
    out = tmp_path / "d"
    r = run(adapter(tmp_path, FakeDownloader([info()], fail_download=True), [GOOD]), out)
    assert r.error_kind == "download" and "不完整" in r.error
    assert [p for p in out.iterdir() if p.is_file()] == [] and list((out / ".staging").iterdir()) == []


def test_untagged_file_is_rejected_and_removed(tmp_path):
    out = tmp_path / "d"
    r = run(adapter(tmp_path, FakeDownloader([info()], write_tags=False), [GOOD]), out)
    assert (r.success, r.error_kind) == (False, "invalid") and "Artist 或 Title" in r.error
    assert [p for p in out.iterdir() if p.is_file()] == [] and list((out / ".staging").iterdir()) == []


def test_stale_staged_file_from_interrupted_run_is_replaced_not_trusted(tmp_path):
    out = tmp_path / "d"
    stale = make_flac(out / ".staging" / "郭静 - 心墙.flac", artist=None, title=None)   # 上次被杀：没写完标签
    dl = FakeDownloader([info()])
    r = run(adapter(tmp_path, dl, [GOOD]), out)
    assert r.success and dl.download_calls == 1
    assert validate_audio(Path(r.path)) is None


# ---------------------------------------------------------------- 幂等：目标已存在
def test_existing_valid_target_is_reused_and_untouched(tmp_path):
    out = tmp_path / "d"
    existing = make_flac(out / "郭静 - 心墙.flac", "郭静", "心墙", seconds=99)
    before = existing.read_bytes()
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD]), out)
    assert r.success and r.reused and Path(r.path) == existing
    assert existing.read_bytes() == before and list((out / ".staging").iterdir()) == []


def test_existing_invalid_target_is_never_overwritten(tmp_path):
    out = tmp_path / "d"
    bad = out / "郭静 - 心墙.flac"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"corrupt")
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD]), out)
    assert (r.success, r.error_kind) == (False, "invalid")
    assert bad.read_bytes() == b"corrupt"
    assert list((out / ".staging").iterdir()) == []


def test_never_writes_outside_output_dir(tmp_path):
    out = tmp_path / "discovery"
    other = tmp_path / "songs"
    other.mkdir()
    run(adapter(tmp_path, FakeDownloader([info()]), [GOOD]), out)
    assert list(other.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["discovery", "songs"]
