"""推荐来源可插拔（P2）：来源设置、音乐平台每日推荐入库/下载、调度与门禁按来源区分。"""
from fakes import lb_defaults
import json
from datetime import datetime, timedelta

import pytest

import lb_adapter
import lb_pipeline
import lb_repo
import lb_settings
import lb_states as ST
from db import Database
from lb_scheduler import LbScheduler
from lb_service import LbService, NotReady
from lb_pipeline import SyncDeps
from settings_store import SettingsStore
from task_manager import TaskManager
from test_lb_adapter import FakeDownloader, GOOD, adapter as make_adapter, info
from test_lb_pipeline import Env, FakeAdapter, FakeLB, FakeND, ndsong, lbtrack, T0
from test_lb_scheduler import Clock, FakeSvc, MON, at, env as sched_env  # noqa: F401  (fixture)


def daily(n=3):
    """get_daily_recommend 返回的形状。"""
    return [{"id": 1000 + i, "name": f"日推{i}", "artists": [f"歌手{i}", "合作者"], "album": f"专辑{i}", "duration_ms": 200000 + i}
            for i in range(1, n + 1)]


class KwAdapter(FakeAdapter):
    """记录每次下载收到的关键字参数（platform_id 等）。"""
    def __init__(self, *a):
        super().__init__(*a)
        self.kws = []

    def download_song(self, artist, title, output_dir, **kw):
        self.kws.append(kw)
        return super().download_song(artist, title, output_dir, **kw)


class NEnv(Env):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.adapter = KwAdapter(self.nd, self.disc_dir)
        self.settings["source"] = "netease"
        self.tracks = daily()
        self.fetches = 0
        self.sleeps = []

    def _fetch(self):
        self.fetches += 1
        return self.tracks

    def sync(self, types=None, trigger="manual", delay=0):
        with self.tm.run("sync", trigger) as h:
            lb_pipeline.run_sync(SyncDeps(self.db, self.settings, None, self.nd, self.adapter, h, request_delay=delay,
                                          sleep=self.sleeps.append, now=lambda: self.now, types=types,
                                          netease_daily=self._fetch))
        return self.tm.recent_runs()[0]


@pytest.fixture
def nenv(tmp_path):
    return NEnv(tmp_path)


def batches(env):
    return env.db.conn.execute("SELECT * FROM recommendation_batches ORDER BY id").fetchall()


# ---------------------------------------------------------------- 设置
def test_old_saved_settings_still_load_with_the_new_keys_defaulted():
    s = lb_settings.load({"enabled": True, "listenbrainz": {"username": "me", "token": "t"}})   # 升级前保存的设置：没有 source
    assert s["source"] == "listenbrainz" and "enabled" not in s
    assert s["strategy"]["netease_daily"] == {"enabled": True, "max_new": 0}
    assert s["schedule"]["netease_daily_time"] == "07:30"
    assert s["playlist_names"]["netease_daily"] == "每日推荐 {date}"


def test_source_can_be_switched_and_junk_is_rejected():
    s = lb_defaults()
    assert lb_settings.apply(s, {"source": "netease"})["source"] == "netease"
    with pytest.raises(ValueError, match="推荐来源"):
        lb_settings.apply(s, {"source": "spotify"})
    with pytest.raises(ValueError):
        lb_settings.apply(s, {"source": ""})


def test_view_lists_only_the_active_sources_lists():
    s = lb_defaults()
    assert [t["key"] for t in lb_settings.view(s, "UTC")["rec_types"]] == ["daily", "weekly_exploration", "weekly_jams"]
    s["source"] = "netease"
    v = lb_settings.view(s, "UTC")
    assert [t["key"] for t in v["rec_types"]] == ["netease_daily"] and v["rec_types"][0]["label"] == "每日推荐"
    assert [x["key"] for x in v["sources"]] == ["listenbrainz", "netease"]


def test_netease_schedule_time_and_strategy_are_validated_and_saved():
    s = lb_settings.apply(lb_defaults(), {"schedule": {"netease_daily_time": "06:45"},
                                                  "strategy": {"netease_daily": {"enabled": False, "max_new": "12"}},
                                                  "playlist_names": {"netease_daily": "日推 {date}"}})
    assert s["schedule"]["netease_daily_time"] == "06:45"
    assert s["strategy"]["netease_daily"] == {"enabled": False, "max_new": 12}
    assert s["playlist_names"]["netease_daily"] == "日推 {date}"
    with pytest.raises(ValueError, match="每日推荐同步时间"):
        lb_settings.apply(lb_defaults(), {"schedule": {"netease_daily_time": "25:00"}})


# ---------------------------------------------------------------- 流水线：音乐平台每日推荐
def test_daily_recommend_becomes_a_batch_downloaded_by_platform_id_and_published(nenv):
    run = nenv.sync()
    assert run["status"] == "done" and json.loads(run["stats_json"])["recommended"] == 3
    (b,) = batches(nenv)
    assert (b["type"], b["lb_playlist_mbid"], b["lb_date"], b["total"]) == ("netease_daily", "netease:daily:2026-11-20", "2026-11-20", 3)
    it = nenv.items()
    assert it["日推1"]["artist"] == "歌手1, 合作者" and it["日推1"]["platform_song_id"] == "1001"
    assert json.loads(it["日推1"]["artists_json"]) == ["歌手1", "合作者"] and it["日推1"]["duration_ms"] == 200001
    assert set(nenv.states().values()) == {ST.LOCAL_DISCOVERY}
    assert [k["platform_id"] for k in nenv.adapter.kws] == [1001, 1002, 1003]          # 按平台歌曲 id 下载（不搜索）
    (name, ids), = nenv.nd.playlists.values()
    assert name == "每日推荐 2026-11-20" and len(ids) == 3


def test_no_listenbrainz_client_is_needed_and_the_same_day_is_not_fetched_twice(nenv):
    nenv.sync()
    run2 = nenv.sync()
    assert len(batches(nenv)) == 1 and len(nenv.adapter.kws) == 3                       # 第二次不新建批次、不重复下载
    assert json.loads(run2["stats_json"]).get("recommended", 0) == 0
    assert nenv.fetches == 1                                                          # 已处理过的那天连平台接口都不再请求


def test_before_6am_still_counts_as_the_previous_day(nenv):
    nenv.now = datetime(2026, 11, 21, 3, 0, 0)       # 平台 6 点才刷新：3 点取到的仍是 20 号的推荐
    nenv.sync()
    assert batches(nenv)[0]["lb_playlist_mbid"] == "netease:daily:2026-11-20"
    nenv.now = datetime(2026, 11, 21, 7, 30, 0)      # 7:30 的定时同步取到的是 21 号的新推荐，不会被误判成「已处理」
    nenv.tracks = daily(2)
    nenv.sync()
    assert [b["lb_playlist_mbid"] for b in batches(nenv)] == ["netease:daily:2026-11-20", "netease:daily:2026-11-21"]


def test_disabled_list_or_empty_recommend_creates_nothing(nenv):
    nenv.settings["strategy"]["netease_daily"]["enabled"] = False
    nenv.sync()
    assert batches(nenv) == [] and nenv.fetches == 0
    nenv.settings["strategy"]["netease_daily"]["enabled"] = True
    nenv.tracks = []
    nenv.sync()
    assert batches(nenv) == []


def test_missing_platform_account_fails_the_run_with_a_clear_error(nenv):
    with pytest.raises(ValueError, match="音乐平台账号"):
        with nenv.tm.run("sync", "manual") as h:
            lb_pipeline.run_sync(SyncDeps(nenv.db, nenv.settings, None, nenv.nd, nenv.adapter, h, request_delay=0,
                                          sleep=lambda s: None, now=lambda: nenv.now))


def test_local_library_dedupe_still_applies_to_platform_recommendations(nenv):
    nenv.nd.main = [ndsong("m1", "歌手1, 合作者", "日推1")]
    nenv.sync()
    assert nenv.states()["日推1"] == ST.LOCAL_MAIN and len(nenv.adapter.kws) == 2


def test_only_the_active_sources_batches_are_processed(nenv):
    """切换来源前遗留的「进行中」批次：切到另一个来源时不处理，切回来继续。"""
    nenv.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    nenv.settings["source"] = "listenbrainz"
    nenv.settings["strategy"]["netease_daily"]["enabled"] = False
    nenv.adapter.script = {"Song 1": "download"}                       # LB 这首下载失败 → 批次保持进行中，等待重试
    def lb_sync():
        with nenv.tm.run("sync", "manual") as h:
            lb_pipeline.run_sync(SyncDeps(nenv.db, nenv.settings, nenv.lb, nenv.nd, nenv.adapter, h, request_delay=0,
                                          sleep=lambda s: None, now=lambda: nenv.now))
    lb_sync()
    lb_batch = batches(nenv)[0]
    assert lb_batch["status"] == "active" and nenv.states(lb_batch["id"]) == {"Song 1": ST.DOWNLOAD_FAILED}

    nenv.settings["source"] = "netease"; nenv.settings["strategy"]["netease_daily"]["enabled"] = True
    nenv.now += timedelta(hours=2)                                     # 重试时间早已到
    nenv.adapter.script = {}; nenv.adapter.calls.clear(); nenv.adapter.kws.clear()
    nenv.sync()
    assert "Song 1" not in nenv.adapter.calls                          # 音乐平台来源下不碰 LB 的遗留批次
    assert nenv.states(lb_batch["id"]) == {"Song 1": ST.DOWNLOAD_FAILED}
    assert [k["platform_id"] for k in nenv.adapter.kws] == [1001, 1002, 1003]

    nenv.settings["source"] = "listenbrainz"                           # 切回来：继续重试
    lb_sync()
    assert nenv.states(lb_batch["id"]) == {"Song 1": ST.LOCAL_DISCOVERY}


def test_listenbrainz_tracks_have_no_platform_id_and_keep_the_search_pacing(tmp_path):
    env = Env(tmp_path)
    env.adapter = KwAdapter(env.nd, env.disc_dir)
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    sleeps = []
    with env.tm.run("sync", "manual") as h:
        lb_pipeline.run_sync(SyncDeps(env.db, env.settings, env.lb, env.nd, env.adapter, h, request_delay=0.5,
                                      sleep=sleeps.append, now=lambda: env.now))
    assert env.adapter.kws == [{"artists": ["A"], "album": "Al", "duration_ms": 200000, "recording_mbid": "mb-1"}]   # 不带 platform_id；带 LB 的录音 MBID（网盘渠道补标签用）
    assert max(sleeps) == lb_pipeline.SEARCH_MIN_INTERVAL                                  # 搜索类仍留足 2 秒


def test_platform_id_downloads_only_wait_the_users_request_delay(nenv):
    nenv.sync(delay=0.5)
    assert 0.5 in nenv.sleeps and lb_pipeline.SEARCH_MIN_INTERVAL not in nenv.sleeps


def test_same_song_seen_again_keeps_its_first_platform_id(nenv):
    nenv.sync()
    nenv.now = datetime(2026, 11, 21, 8, 0, 0)
    nenv.tracks = [dict(daily(1)[0], id=9999)]        # 同歌手同歌名、平台 id 变了：沿用已记录的歌曲实体
    nenv.sync()
    rows = nenv.db.conn.execute("SELECT platform_song_id FROM tracks WHERE title='日推1'").fetchall()
    assert [r[0] for r in rows] == ["1001"]


# ---------------------------------------------------------------- 适配器：已知平台 id 不搜索
def test_adapter_with_platform_id_skips_search_entirely(tmp_path):
    dl = FakeDownloader([info(1001, "日推1", ("歌手1",))])
    a = make_adapter(tmp_path, dl, [])                                    # 搜索脚本为空：被调用就会 IndexError
    res = a.download_song("歌手1", "日推1", tmp_path, artists=["歌手1"], platform_id=1001)
    assert res.success and a.calls == [] and dl.info_calls == [(1001, "lossless", False)]


def test_adapter_with_platform_id_reports_unavailable_without_falling_back_to_search(tmp_path):
    from downloader import DownloadException
    dl = FakeDownloader([DownloadException("无版权", kind="no_copyright")])
    a = make_adapter(tmp_path, dl, [])
    res = a.download_song("歌手1", "日推1", tmp_path, platform_id=1001)
    assert not res.success and res.error_kind == "no_copyright" and a.calls == []


# ---------------------------------------------------------------- 调度
def test_scheduler_fires_the_platform_daily_sync_only_for_that_source(sched_env):
    sched, svc, clock, _ = sched_env
    svc.s["source"] = "netease"
    at(clock, "07:30")
    assert sched.tick() == ["netease_daily_sync"] and svc.calls == [("sync", "schedule", ["netease_daily"])]
    svc.calls.clear()
    at(clock, "08:30")                                                    # LB 的每日同步时间：来源是音乐平台时不触发
    assert sched.tick() == [] and svc.calls == []
    at(clock, "10:00")                                                    # 周一 10:00 = LB 每周同步时间：同样不触发
    assert sched.tick() == []


def test_scheduler_does_not_fire_platform_sync_for_listenbrainz_source_or_when_disabled(sched_env):
    sched, svc, clock, _ = sched_env
    at(clock, "07:30")
    assert sched.tick() == []                                             # 默认来源 = ListenBrainz
    svc.s["source"] = "netease"; svc.s["strategy"]["netease_daily"]["enabled"] = False
    at(clock, "07:30", day=MON + timedelta(days=1))
    assert sched.tick() == []


def test_upcoming_reflects_the_active_source(sched_env):
    sched, svc, clock, _ = sched_env
    svc.s["source"] = "netease"
    assert [u["task"] for u in sched.upcoming(MON.replace(hour=0))] == ["过期清理", "每日推荐同步"]    # 按时间先后：04:00 清理在 07:30 之前


# ---------------------------------------------------------------- 服务门禁
def _svc_settings(source, lb_creds):
    s = lb_defaults()
    s["source"] = source
    s["navidrome"].update(url="http://n", user="u", password="p", main_folder_id=1, discovery_folder_id=2)
    if lb_creds:
        s["listenbrainz"].update(username="me", token="t")
    return s


def test_platform_source_sync_does_not_need_listenbrainz_credentials(tmp_path):
    db = Database(tmp_path / "t.sqlite"); db.migrate()
    svc = LbService(db=db, kv=SettingsStore(db), tm=TaskManager(db))
    svc.check_ready("sync", _svc_settings("netease", False))                 # 不抛异常
    with pytest.raises(NotReady, match="ListenBrainz 用户名和 Token"):
        svc.check_ready("sync", _svc_settings("listenbrainz", False))
    with pytest.raises(NotReady, match="ListenBrainz 用户名和 Token"):       # 红心同步无论来源都要写 LB
        s = _svc_settings("netease", False); s["love"]["enabled"] = True
        svc.check_ready("love", s)


def test_service_runs_a_platform_sync_end_to_end_without_a_listenbrainz_client(tmp_path):
    import time
    db = Database(tmp_path / "t.sqlite"); db.migrate()
    kv, tm = SettingsStore(db), TaskManager(db)
    main, disc = tmp_path / "songs", tmp_path / "discovery"; main.mkdir(); disc.mkdir()
    nd = FakeND()
    s = _svc_settings("netease", False); s["dirs"].update(main=str(main), discovery=str(disc))
    kv.set(lb_settings.SETTINGS_KEY, s)

    def no_lb(_s):
        raise AssertionError("来源是音乐平台时不应创建 ListenBrainz 客户端")
    svc = LbService(db=db, kv=kv, tm=tm, lb_factory=no_lb, nd_factory=lambda x: nd,
                    adapter_factory=lambda x, d: FakeAdapter(nd, disc), netease_factory=lambda: (lambda: daily(2)),
                    request_delay=lambda: 0, sleep=lambda x: None, now=lambda: datetime(2026, 11, 20, 9, 0, 0))
    svc.start("sync")
    end = time.time() + 10
    while tm.current() is not None and time.time() < end:
        time.sleep(0.05)
    run = tm.recent_runs()[0]
    assert run["status"] == "done" and json.loads(run["stats_json"])["downloaded"] == 2


def test_dashboard_only_shows_the_active_sources_latest_batches(nenv):
    import lb_queries
    nenv.sync()
    d = lb_queries.dashboard(nenv.db, nenv.settings, None, nenv.now)
    assert d["source"] == "netease" and [x["type"] for x in d["current"]] == ["netease_daily"] and d["current"][0]["batch_id"]
    nenv.settings["source"] = "listenbrainz"
    assert all(x["batch_id"] is None for x in lb_queries.dashboard(nenv.db, nenv.settings, None, nenv.now)["current"])   # 切回 LB：没有 LB 的批次
