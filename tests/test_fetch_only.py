"""推荐为主、下载为辅：默认只获取推荐歌单并与本地比对，不下载；要下载的在推荐歌单里手动点（可整批或单首）。"""
import json
import time
from datetime import datetime

import pytest

import lb_pipeline
import lb_repo
import lb_settings
import lb_states as ST
from db import Database
from fakes import lb_defaults
from lb_adapter import AdapterResult
from lb_pipeline import SyncDeps
from lb_service import LbService
from settings_store import SettingsStore
from task_manager import TaskManager
from test_lb_pipeline import Env, FakeAdapter, FakeLB, FakeND, T0, lbtrack, ndsong


def run_sync(env, **kw):
    with env.tm.run("sync", "manual") as h:
        lb_pipeline.run_sync(SyncDeps(env.db, env.settings, env.lb, env.nd, env.adapter, h, request_delay=0,
                                      sleep=lambda s: None, now=lambda: env.now, **kw))
    return env.tm.recent_runs()[0]


# ---------------------------------------------------------------- 设置默认值
def test_fresh_install_does_not_download_automatically_but_old_installs_keep_doing_so():
    assert lb_settings.load(None)["auto_download"] is False
    assert lb_settings.load({"listenbrainz": {"username": "me"}})["auto_download"] is True       # 升级前保存过设置：旧版同步总是顺带下载
    assert lb_settings.load({"auto_download": False, "source": "netease", "mode": "folder"})["auto_download"] is False
    assert lb_settings.apply(lb_settings.load(None), {"auto_download": "true"})["auto_download"] is True


# ---------------------------------------------------------------- 流水线
def test_fetch_only_records_the_recommendation_and_local_matches_but_never_downloads(tmp_path):
    env = Env(tmp_path)
    env.nd.main = [ndsong("m1", "A", "Song 1")]
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2), lbtrack(3)])
    run = run_sync(env, download=False)
    assert run["status"] == "done" and env.adapter.calls == [] and env.nd.scans == 0
    assert env.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2": ST.PENDING, "Song 3": ST.PENDING}   # 本地有的照常识别，其余待下载
    st = json.loads(run["stats_json"])
    assert st["recommended"] == 3 and st["local_main"] == 1 and st["to_download"] == 2
    assert lb_repo.active_batches(env.db)                                                          # 还有待下载的：批次保持进行中
    (name, ids), = env.nd.playlists.values()
    assert ids == ["m1"]                                                                           # 歌单只放已有文件的


def test_downloading_later_picks_up_the_pending_items(tmp_path):
    env = Env(tmp_path)
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2)])
    run_sync(env, download=False)
    run_sync(env, download=True)
    assert env.states() == {"Song 1": ST.LOCAL_DISCOVERY, "Song 2": ST.LOCAL_DISCOVERY} and env.adapter.calls == ["Song 1", "Song 2"]
    assert not lb_repo.active_batches(env.db)


def test_only_the_chosen_item_is_downloaded(tmp_path):
    env = Env(tmp_path)
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2), lbtrack(3)])
    run_sync(env, download=False)
    chosen = [i for i in lb_repo.items_of_batch(env.db, 1) if i["title"] == "Song 2"][0]["id"]
    run_sync(env, download=True, only_items=[chosen])
    assert env.states() == {"Song 1": ST.PENDING, "Song 2": ST.LOCAL_DISCOVERY, "Song 3": ST.PENDING}
    assert env.adapter.calls == ["Song 2"]


def test_fetch_only_leaves_failed_items_alone_and_does_not_restart_their_retry(tmp_path):
    env = Env(tmp_path)
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    env.adapter.script = {"Song 1": "download"}
    run_sync(env)
    assert env.states() == {"Song 1": ST.DOWNLOAD_FAILED}
    env.now = datetime(2026, 11, 21, 8, 30)                           # 重试时间早已到
    run_sync(env, download=False)
    assert env.states() == {"Song 1": ST.DOWNLOAD_FAILED}              # 只获取推荐时不碰它
    env.adapter.script = {}
    run_sync(env, download=True)
    assert env.states() == {"Song 1": ST.LOCAL_DISCOVERY}


def test_fetch_only_in_folder_mode_and_for_platform_recommendations(tmp_path):
    env = Env(tmp_path)
    env.settings.update(source="netease", mode="folder")
    env.settings["dirs"]["output"] = str(tmp_path / "dl")
    tracks = [{"id": 1, "name": "日推", "artists": ["歌手"], "album": "专辑", "duration_ms": 1}]
    with env.tm.run("sync", "manual") as h:
        lb_pipeline.run_sync(SyncDeps(env.db, env.settings, None, None, env.adapter, h, request_delay=0, sleep=lambda s: None,
                                      now=lambda: env.now, netease_daily=lambda: tracks, download=False))
    assert env.adapter.calls == [] and not list((tmp_path / "dl").glob("*.flac"))
    assert {i["title"]: i["state"] for i in lb_repo.items_of_batch(env.db, 1)} == {"日推": ST.PENDING}


# ---------------------------------------------------------------- 服务：按设置 / 明确指定
class SEnv:
    def __init__(self, tmp_path, auto_download):
        self.db = Database(tmp_path / "t.sqlite"); self.db.migrate()
        self.kv, self.tm = SettingsStore(self.db), TaskManager(self.db)
        main, disc = tmp_path / "songs", tmp_path / "discovery"; main.mkdir(); disc.mkdir()
        self.nd, self.lb = FakeND(), FakeLB()
        self.adapter = FakeAdapter(self.nd, disc)
        s = lb_defaults()
        s["auto_download"] = auto_download
        s["listenbrainz"].update(username="me", token="t")
        s["navidrome"].update(url="http://n", user="u", password="p", main_folder_id=1, discovery_folder_id=2)
        s["dirs"].update(main=str(main), discovery=str(disc))
        self.kv.set(lb_settings.SETTINGS_KEY, s)
        self.svc = LbService(db=self.db, kv=self.kv, tm=self.tm, lb_factory=lambda x: self.lb, nd_factory=lambda x: self.nd,
                             adapter_factory=lambda x, d: self.adapter, request_delay=lambda: 0, sleep=lambda x: None,
                             now=lambda: T0)
        self.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2)])

    def go(self, **kw):
        self.svc.start("sync", **kw)
        end = time.time() + 10
        while self.tm.current() is not None and time.time() < end:
            time.sleep(0.05)
        return self.tm.recent_runs()[0]


def test_the_sync_button_follows_the_auto_download_setting_and_can_be_overridden(tmp_path):
    e = SEnv(tmp_path, auto_download=False)
    e.go()                                                            # 按设置：不下载
    assert e.adapter.calls == []
    e.go(params={"download": True})                                    # 明确要求下载
    assert e.adapter.calls == ["Song 1", "Song 2"]


def test_with_auto_download_on_the_default_sync_downloads_and_can_be_forced_off(tmp_path):
    e = SEnv(tmp_path, auto_download=True)
    e.go(params={"download": False})
    assert e.adapter.calls == []
    e.go()
    assert e.adapter.calls == ["Song 1", "Song 2"]


def test_single_item_download_through_the_service(tmp_path):
    e = SEnv(tmp_path, auto_download=False)
    e.go()
    second = [i for i in lb_repo.items_of_batch(e.db, 1) if i["title"] == "Song 2"][0]["id"]
    e.go(params={"download": True, "item_ids": [second]})
    assert e.adapter.calls == ["Song 2"]


def test_the_api_passes_download_and_item_id_through(monkeypatch):
    import web_server
    seen = {}
    class App:
        lb = type("L", (), {"start": lambda self, kind, **kw: seen.update(kw) or 7})()
    r = web_server.App.lb_action(App(), {"kind": "sync", "download": True, "item_id": "5"})
    assert r == {"run_id": 7} and seen["params"] == {"download": True, "item_ids": [5]}
    web_server.App.lb_action(App(), {"kind": "sync"})
    assert seen["params"] == {"download": None}                        # 省略 = 按设置
    with pytest.raises(ValueError, match="条目编号"):
        web_server.App.lb_action(App(), {"kind": "sync", "item_id": "x"})
