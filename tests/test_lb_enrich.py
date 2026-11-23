from pathlib import Path

import pytest
from mutagen.flac import FLAC

from audio import make_flac
from lb_adapter import PlatformAdapter, validate_audio
from lb_enrich import DownloadChannel, EnrichResult, Enricher, TagContext, run_enrichers
from test_lb_adapter import FakeDownloader, GOOD, adapter, info, run

CTX = TagContext(source="x", artist="A", title="T")


class Fill(Enricher):
    """示例补全器：缺 album 时补上 LB 给的专辑。"""
    name = "fill-album"

    def __init__(self, album="Filled"):
        self.album = album

    def applies(self, ctx, path):
        return ctx.source != "skip-me"

    def enrich(self, path, ctx):
        f = FLAC(str(path))
        if f.get("album"):
            return EnrichResult(True, False, "已有专辑")
        f["album"] = self.album
        f.save()
        return EnrichResult(True, True, f"album={self.album}")


class Boom(Enricher):
    name = "boom"
    def __init__(self, required=False):
        self.required = required
    def enrich(self, path, ctx):
        raise RuntimeError("scraper offline")


class Fails(Enricher):
    name = "fails"
    def __init__(self, required=False):
        self.required = required
    def enrich(self, path, ctx):
        return EnrichResult(False, note="no match")


class Strip(Enricher):
    name = "strip"
    def enrich(self, path, ctx):
        f = FLAC(str(path)); f.delete(); f.save()
        return EnrichResult(True, True)


class Delete(Enricher):
    name = "delete"
    def enrich(self, path, ctx):
        Path(path).unlink()
        return EnrichResult(True, True)


class Rec(Enricher):
    def __init__(self, name):
        self.name = name
    def enrich(self, path, ctx):
        order.append(self.name)
        return EnrichResult(True, False)


order = []


# ---------------------------------------------------------------- run_enrichers
def test_applies_changes_and_is_idempotent(tmp_path):
    p = make_flac(tmp_path / "a.flac", "A", "T")
    assert run_enrichers(p, CTX, [Fill()]) == (["fill-album"], None)
    assert FLAC(str(p))["album"] == ["Filled"]
    assert run_enrichers(p, CTX, [Fill("Other")]) == ([], None)              # 幂等：已有专辑，不再改
    assert FLAC(str(p))["album"] == ["Filled"]


def test_applies_false_skips(tmp_path):
    p = make_flac(tmp_path / "a.flac")
    assert run_enrichers(p, TagContext(source="skip-me", artist="A", title="T"), [Fill()]) == ([], None)
    assert "album" not in FLAC(str(p))


def test_runs_in_given_order(tmp_path):
    order.clear()
    run_enrichers(make_flac(tmp_path / "a.flac"), CTX, [Rec("first"), Rec("second"), Rec("third")])
    assert order == ["first", "second", "third"]


def test_optional_failures_are_logged_and_ignored(tmp_path):
    logs = []
    p = make_flac(tmp_path / "a.flac")
    applied, fatal = run_enrichers(p, CTX, [Boom(), Fails(), Fill()], log=lambda lv, m: logs.append((lv, m)))
    assert fatal is None and applied == ["fill-album"]                       # 前面失败不影响后面的补全器
    assert [lv for lv, _ in logs].count("WARNING") == 2 and any("scraper offline" in m for _, m in logs)


@pytest.mark.parametrize("bad", [Boom(required=True), Fails(required=True)])
def test_required_failure_is_fatal_and_stops_the_chain(tmp_path, bad):
    order.clear()
    applied, fatal = run_enrichers(make_flac(tmp_path / "a.flac"), CTX, [bad, Rec("never")])
    assert fatal and bad.name in fatal and order == []


def test_enricher_that_deletes_the_file_is_fatal(tmp_path):
    applied, fatal = run_enrichers(make_flac(tmp_path / "a.flac"), CTX, [Delete()])
    assert "文件不见了" in fatal


def test_no_enrichers_is_a_noop(tmp_path):
    assert run_enrichers(make_flac(tmp_path / "a.flac"), CTX, []) == ([], None)


# ---------------------------------------------------------------- 接入适配器
def test_default_adapter_has_no_enrichers_and_behaves_as_before(tmp_path):
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD]), tmp_path / "d")
    assert r.success and r.enriched == []


def test_enricher_runs_on_staged_file_before_it_reaches_output(tmp_path):
    out = tmp_path / "d"
    seen = {}

    class Spy(Enricher):
        name = "spy"
        def enrich(self, path, ctx):
            seen["path"], seen["ctx"] = Path(path), ctx
            seen["final_exists"] = (out / "郭静 - 心墙.flac").exists()
            return EnrichResult(True, False)
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD], enrichers=[Spy()]), out)
    assert r.success
    assert seen["path"].parent == out / ".staging" and seen["final_exists"] is False     # 此时还没进入试听库
    assert (seen["ctx"].source, seen["ctx"].artist, seen["ctx"].album, seen["ctx"].duration_s) == ("platform", "郭靜", "在樹上唱歌", 227.0)


def test_enriched_tags_end_up_in_the_final_file(tmp_path):
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD], enrichers=[Fill("补全专辑")]), tmp_path / "d")
    assert r.success and r.enriched == ["fill-album"]
    assert FLAC(r.path)["album"] == ["补全专辑"]


def test_required_enricher_failure_fails_download_and_cleans_up(tmp_path):
    out = tmp_path / "d"
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD], enrichers=[Fails(required=True)]), out)
    assert (r.success, r.error_kind) == (False, "invalid") and "fails" in r.error
    assert [p for p in out.iterdir() if p.is_file()] == [] and list((out / ".staging").iterdir()) == []


def test_optional_enricher_failure_does_not_fail_download(tmp_path):
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD], enrichers=[Boom()]), tmp_path / "d")
    assert r.success and r.enriched == []


def test_validation_still_catches_an_enricher_that_breaks_the_file(tmp_path):
    out = tmp_path / "d"
    r = run(adapter(tmp_path, FakeDownloader([info()]), [GOOD], enrichers=[Strip()]), out)
    assert (r.success, r.error_kind) == (False, "invalid") and "Artist 或 Title" in r.error
    assert [p for p in out.iterdir() if p.is_file()] == []


# ---------------------------------------------------------------- 渠道接口
def test_platform_adapter_satisfies_download_channel_protocol(tmp_path):
    assert isinstance(adapter(tmp_path, FakeDownloader(), []), DownloadChannel)


def test_any_object_with_download_song_is_a_channel():
    class Other:
        def download_song(self, artist, title, output_dir, *, artists=None, album="", duration_ms=None):
            return None
    assert isinstance(Other(), DownloadChannel) and not isinstance(object(), DownloadChannel)
