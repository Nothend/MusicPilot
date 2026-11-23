"""P3：仅下载到指定目录（不与 Navidrome 对接）+ 下载渠道注册表。"""
from fakes import lb_defaults
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import download_channels
import lb_checks
import lb_pipeline
import lb_queries
import lb_repo
import lb_settings
import lb_states as ST
from audio import make_flac
from db import Database
from lb_adapter import AdapterResult
from lb_pipeline import SyncDeps
from lb_service import LbService, NotReady
from settings_store import SettingsStore
from task_manager import TaskManager
from test_lb_pipeline import FakeAdapter, FakeLB, FakeND, T0, lbtrack as _lbtrack
from test_lb_scheduler import MON, at, env as sched_env  # noqa: F401  (fixture)


def lbtrack(n, **kw):
    """时长与 audio.make_flac 生成的假文件一致（10 秒）：匹配器会用时长区分不同录音。"""
    return _lbtrack(n, dur=10000, **kw)


class NoNavidrome:
    """仅下载模式下任何 Navidrome 调用都是 bug。"""
    def __getattr__(self, name):
        raise AssertionError(f"仅下载模式不应调用 Navidrome.{name}")


class Checker:
    def __init__(self, have=(), boom=False):
        self.have, self.boom, self.closed, self.calls = set(have), boom, False, []

    def exists(self, title, artists, album=""):
        self.calls.append(title)
        if self.boom:
            raise RuntimeError("库连不上")
        return title in self.have

    def close(self):
        self.closed = True


class FEnv:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "t.sqlite"); self.db.migrate()
        self.tm = TaskManager(self.db)
        self.out = tmp_path / "dl"
        self.lb = FakeLB()
        self.fake_nd = FakeND()
        self.adapter = FakeAdapter(self.fake_nd, self.out)
        self.settings = lb_defaults()
        self.settings["mode"] = "folder"
        self.settings["dirs"]["output"] = str(self.out)
        self.now = T0
        self.checker = None

    def sync(self):
        with self.tm.run("sync", "manual") as h:
            lb_pipeline.run_sync(SyncDeps(self.db, self.settings, self.lb, None, self.adapter, h, request_delay=0,
                                          sleep=lambda s: None, now=lambda: self.now, library_checker=self.checker))
        return self.tm.recent_runs()[0]

    def states(self, batch_id=1):
        return {i["title"]: i["state"] for i in lb_repo.items_of_batch(self.db, batch_id)}


@pytest.fixture
def fenv(tmp_path):
    return FEnv(tmp_path)


# ---------------------------------------------------------------- 设置
def test_mode_channel_and_output_dir_defaults_and_validation():
    s = lb_settings.load(None)
    assert (s["mode"], s["channel"], s["dirs"]["output"]) == ("folder", "platform", "/app/downloads")
    ok = lb_settings.apply(s, {"mode": "folder", "channel": "platform", "dirs": {"output": "/music/new"}})
    assert (ok["mode"], ok["dirs"]["output"]) == ("folder", "/music/new")
    for bad in ({"mode": "x"}, {"channel": "x"}, {"dirs": {"output": "relative"}}, {"dirs": {"output": "/a/../b"}}, {"dirs": {"output": "/"}}):
        with pytest.raises(ValueError):
            lb_settings.apply(s, bad)
    v = lb_settings.view(s, "UTC")
    assert [m["key"] for m in v["modes"]] == ["navidrome", "folder"] and [c["key"] for c in v["channels"]] == ["platform"]


# ---------------------------------------------------------------- 流水线
def test_folder_mode_downloads_into_the_folder_without_touching_navidrome(fenv):
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2)])
    run = fenv.sync()
    assert run["status"] == "done" and json.loads(run["stats_json"])["downloaded"] == 2
    assert fenv.states() == {"Song 1": ST.DOWNLOADED, "Song 2": ST.DOWNLOADED}
    assert sorted(p.name for p in fenv.out.glob("*.flac")) == ["A - Song 1.flac", "A - Song 2.flac"]
    it = lb_repo.items_of_batch(fenv.db, 1)[0]
    assert Path(it["local_path"]).parent == fenv.out and it["entered_discovery_at"] is None
    assert fenv.fake_nd.playlists == {} and fenv.fake_nd.scans == 0                    # 没有歌单、没有扫描
    assert fenv.db.conn.execute("SELECT COUNT(*) FROM lb_playlists").fetchone()[0] == 0
    assert fenv.tm.recent_runs()[0]["status"] == "done"
    assert lb_repo.active_batches(fenv.db) == []                                      # 批次处理完即结束


def test_songs_already_in_the_folder_are_skipped_by_tags_with_the_same_matching_rules(fenv):
    make_flac(fenv.out / "随便什么文件名.flac", artist="A", title="Song 1")
    make_flac(fenv.out / "sub" / "x.flac", artist=["A"], title="Song 2 (Radio Edit)")      # 子目录、软版本词都算已有
    make_flac(fenv.out / ".staging" / "ignored.flac", artist="A", title="Song 3")          # 隐藏目录不参与去重
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2), lbtrack(3)])
    fenv.sync()
    assert fenv.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2": ST.LOCAL_MAIN, "Song 3": ST.DOWNLOADED}
    assert fenv.adapter.calls == ["Song 3"]
    assert lb_repo.items_of_batch(fenv.db, 1)[0]["reason_kind"] == "exists"


def test_several_indistinguishable_files_in_the_folder_still_count_as_already_have(fenv):
    import lb_folder
    from matcher import Target, match
    make_flac(fenv.out / "a" / "x.flac", artist="A", title="Song 1", seconds=10)         # 同歌手同歌名、时长差很大的两个文件
    make_flac(fenv.out / "b" / "x.flac", artist="A", title="Song 1", seconds=200)
    idx, _ = lb_folder.build_index(str(fenv.out))
    assert match(idx, Target(artist="A", title="Song 1", artists=["A"])).status == "multiple"   # 推荐没有时长：区分不了
    fenv.lb.add("weekly_jams", "pl-1", [_lbtrack(1, dur=None)])
    fenv.sync()
    assert fenv.states() == {"Song 1": ST.LOCAL_MAIN} and fenv.adapter.calls == []


def test_the_same_song_twice_in_one_run_is_only_downloaded_once(fenv):
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1, mbid="m1"), lbtrack(1, mbid="m2")])    # 同一首歌两条推荐
    fenv.sync()
    assert fenv.adapter.calls == ["Song 1"]
    states = [i["state"] for i in lb_repo.items_of_batch(fenv.db, 1)]
    assert sorted(states) == [ST.DOWNLOADED, ST.LOCAL_MAIN]


def test_optional_external_library_check_skips_and_failures_fall_back_to_downloading(fenv):
    fenv.checker = Checker(have={"Song 1"})
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2)])
    fenv.sync()
    assert fenv.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2": ST.DOWNLOADED}
    assert lb_repo.items_of_batch(fenv.db, 1)[0]["reason"] == "音乐库中已有"

    fenv.checker = Checker(boom=True)                                                  # 外部库连不上：当作没有，照常下载
    fenv.lb.add("daily", "pl-2", [lbtrack(5)], date="2026-11-21T00:00:00+00:00")
    run = fenv.sync()
    assert fenv.states(2) == {"Song 5": ST.DOWNLOADED} and run["status"] == "done"


def test_failures_and_rate_limit_keep_the_normal_semantics_in_folder_mode(fenv):
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2), lbtrack(3)])
    fenv.adapter.script = {"Song 1": "not_found", "Song 2": AdapterResult(False, error="限流", error_kind="rate_limited")}
    run = fenv.sync()
    assert run["status"] == "paused"
    assert fenv.states() == {"Song 1": ST.NOT_FOUND, "Song 2": ST.PENDING, "Song 3": ST.PENDING}   # 限流 → 暂停，不记成未找到
    fenv.adapter.script = {}
    fenv.sync()
    assert fenv.states() == {"Song 1": ST.NOT_FOUND, "Song 2": ST.DOWNLOADED, "Song 3": ST.DOWNLOADED}


def test_folder_mode_never_deletes_or_changes_existing_files_and_cleans_only_its_staging(fenv):
    keep = make_flac(fenv.out / "old.flac", artist="X", title="Y")
    before = keep.read_bytes()
    stale = fenv.out / ".staging" / "half.part"
    stale.parent.mkdir(parents=True); stale.write_bytes(b"x")
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    fenv.sync()
    assert keep.read_bytes() == before and not stale.exists()


def test_platform_source_with_folder_mode_downloads_by_platform_id(fenv):
    fenv.settings["source"] = "netease"
    kws = []
    orig = fenv.adapter.download_song
    fenv.adapter.download_song = lambda a, t, o, **kw: (kws.append(kw), orig(a, t, o, **kw))[1]
    tracks = [{"id": 7, "name": "日推", "artists": ["歌手"], "album": "专辑", "duration_ms": 1}]
    with fenv.tm.run("sync", "manual") as h:
        lb_pipeline.run_sync(SyncDeps(fenv.db, fenv.settings, None, None, fenv.adapter, h, request_delay=0,
                                      sleep=lambda s: None, now=lambda: fenv.now, netease_daily=lambda: tracks))
    assert kws[0]["platform_id"] == 7 and (fenv.out / "歌手 - 日推.flac").is_file()


def test_switching_back_to_navidrome_mode_leaves_downloaded_items_inert(fenv):
    fenv.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    fenv.sync()
    assert ST.TRANSITIONS[ST.DOWNLOADED] == frozenset()                               # 终态：清理/转正都不会碰它
    with fenv.db.lock:
        n = fenv.db.conn.execute("SELECT COUNT(*) FROM recommendation_items WHERE state=?", (ST.LOCAL_DISCOVERY,)).fetchone()[0]
    assert n == 0


# ---------------------------------------------------------------- 服务门禁
def _svc(tmp_path, **kw):
    db = Database(tmp_path / "s.sqlite"); db.migrate()
    return db, LbService(db=db, kv=SettingsStore(db), tm=TaskManager(db), **kw)


def _folder_settings(tmp_path):
    s = lb_defaults()
    s.update(enabled=True, mode="folder")
    s["dirs"]["output"] = str(tmp_path / "dl")
    s["listenbrainz"].update(username="me", token="t")
    return s                                                                          # 注意：没有填 Navidrome 任何信息


def test_folder_mode_sync_needs_no_navidrome_but_other_tasks_are_refused(tmp_path):
    _, svc = _svc(tmp_path)
    s = _folder_settings(tmp_path)
    svc.check_ready("sync", s)                                                         # 不抛异常
    for kind in ("promote", "cleanup", "reindex", "dedup", "love"):
        with pytest.raises(NotReady, match="仅下载到指定目录"):
            svc.check_ready(kind, s, dry_run=True)
    s["dirs"]["output"] = ""
    with pytest.raises(NotReady, match="下载目录"):
        svc.check_ready("sync", s)
    s2 = lb_defaults(); s2.update(enabled=True)                               # 联动模式仍要求 Navidrome
    s2["listenbrainz"].update(username="me", token="t")
    with pytest.raises(NotReady, match="Navidrome"):
        svc.check_ready("sync", s2)


def test_service_runs_a_folder_sync_end_to_end_and_closes_the_checker(tmp_path):
    db, _ = _svc(tmp_path)
    kv, tm = SettingsStore(db), TaskManager(db)
    s = _folder_settings(tmp_path)
    kv.set(lb_settings.SETTINGS_KEY, s)
    lb, fnd, checker = FakeLB(), FakeND(), Checker()
    lb.add("weekly_jams", "pl-1", [lbtrack(1)])

    def no_nd(_s):
        raise AssertionError("仅下载模式不应创建 Navidrome 客户端")
    svc = LbService(db=db, kv=kv, tm=tm, lb_factory=lambda x: lb, nd_factory=no_nd,
                    adapter_factory=lambda x, d: FakeAdapter(fnd, tmp_path / "dl"), library_checker_factory=lambda: checker,
                    request_delay=lambda: 0, sleep=lambda x: None, now=lambda: T0)
    svc.start("sync")
    end = time.time() + 10
    while tm.current() is not None and time.time() < end:
        time.sleep(0.05)
    run = tm.recent_runs()[0]
    assert run["status"] == "done" and json.loads(run["stats_json"])["downloaded"] == 1
    assert checker.closed and checker.calls == ["Song 1"]


def test_channel_registry_makes_the_selected_channel_and_rejects_unknown(monkeypatch):
    made = []
    monkeypatch.setitem(download_channels.FACTORIES, "platform", lambda store, s, delay: made.append((store, delay)) or "ADAPTER")
    assert download_channels.make("platform", "STORE", lb_defaults(), 0.7) == "ADAPTER" and made == [("STORE", 0.7)]
    with pytest.raises(ValueError, match="未知的下载渠道"):
        download_channels.make("nope", None, {}, 0)
    assert set(download_channels.FACTORIES) == set(lb_settings.CHANNELS)             # 注册表与设置里的可选项保持一致


def test_service_builds_its_adapter_from_the_selected_channel(tmp_path, monkeypatch):
    seen = []
    for name in ("platform", "alt"):                                                  # 注册第二个渠道，证明确实按所选渠道创建
        monkeypatch.setitem(download_channels.FACTORIES, name, lambda store, s, delay, n=name: seen.append(n) or f"A-{n}")
    monkeypatch.setitem(lb_settings.CHANNELS, "alt", {"label": "备用", "desc": "x"})
    _, svc = _svc(tmp_path, store="STORE")
    s = lb_defaults()
    assert svc.adapter_factory(s, 0.5) == "A-platform"
    s["channel"] = lb_settings.apply(s, {"channel": "alt"})["channel"]
    assert svc.adapter_factory(s, 0.5) == "A-alt" and seen == ["platform", "alt"]


def test_folder_index_counts_only_tagged_audio_and_skips_hidden_dirs(tmp_path):
    import lb_folder
    make_flac(tmp_path / "a.flac", artist="A", title="T")
    make_flac(tmp_path / "untagged.flac", artist=None, title=None)
    make_flac(tmp_path / "no_title.flac", artist="A", title=None)                    # 有标签但缺歌名：不入索引
    make_flac(tmp_path / ".staging" / "h.flac", artist="B", title="U")
    (tmp_path / "note.txt").write_text("x")
    idx, n = lb_folder.build_index(str(tmp_path))
    assert n == 1 and [f.title for f in idx.files] == ["T"]


# ---------------------------------------------------------------- 调度 / 页面数据 / 目录检查
def test_scheduler_in_folder_mode_only_syncs(sched_env):
    sched, svc, clock, _ = sched_env
    svc.s["mode"] = "folder"
    svc.s["discovery"]["auto_promote"] = True; svc.s["discovery"]["auto_cleanup"] = True; svc.s["love"]["enabled"] = True
    at(clock, "04:00")
    assert sched.tick() == []                                                          # 清理时间到了也不触发
    at(clock, "08:30")
    assert sched.tick() == ["daily_sync"]
    clock.t += timedelta(hours=3)                                                      # 间隔任务（转正 / 红心）久等也不触发
    assert "promote" not in sched.tick() and "love" not in sched.tick()
    assert [u["task"] for u in sched.upcoming(MON.replace(hour=0))] == ["每日同步（每日熟悉）", "每周同步（Weekly）"]


def test_dashboard_reports_mode_and_channel(fenv):
    d = lb_queries.dashboard(fenv.db, fenv.settings, None, fenv.now)
    assert (d["mode"], d["channel"]) == ("folder", "platform")


def test_output_dir_check_creates_and_probes_the_folder_and_reports_failures(tmp_path):
    make_flac(tmp_path / "dl" / "a.flac", artist="A", title="T")
    s = lb_defaults(); s["dirs"]["output"] = str(tmp_path / "dl")
    r = lb_checks.check_output_dir(s)
    assert r["ok"] and "1 个音频文件" in r["checks"][1]["msg"] and not list((tmp_path / "dl" / ".staging").iterdir())
    blocker = tmp_path / "file"; blocker.write_text("x")
    s["dirs"]["output"] = str(blocker / "sub")                                          # 父级是文件：创建失败
    bad = lb_checks.check_output_dir(s)
    assert not bad["ok"] and bad["message"] == "下载目录检查未通过"


def test_output_dir_check_fails_when_the_folder_cannot_be_written(tmp_path, monkeypatch):
    s = lb_defaults(); s["dirs"]["output"] = str(tmp_path / "dl")
    def boom(self, data):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(Path, "write_bytes", boom)
    r = lb_checks.check_output_dir(s)
    assert not r["ok"] and "不可写" in r["checks"][0]["msg"]
