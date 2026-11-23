from fakes import lb_defaults
import json
import threading
import time
from datetime import datetime

import pytest

import lb_love
import lb_service
import lb_settings
from db import Database
from lb_client import LBError
from lb_service import LbService, NotReady, default_adapter_factory
from settings_store import SettingsStore
from task_manager import RunHandle, TaskBusy, TaskManager
from test_lb_pipeline import FakeAdapter, FakeLB, FakeND, lbtrack, ndsong
from test_lb_love import FakeLB as FakeLoveLB, FakeND as FakeLoveND, hit, song as nd_song


class Env:
    def __init__(self, tmp_path, enabled=True):
        self.db = Database(tmp_path / "t.sqlite"); self.db.migrate()
        self.kv, self.tm = SettingsStore(self.db), TaskManager(self.db)
        self.main, self.disc = tmp_path / "songs", tmp_path / "discovery"
        self.main.mkdir(); self.disc.mkdir()
        self.lb, self.nd = FakeLB(), FakeND()
        self.adapter = FakeAdapter(self.nd, self.disc)
        self.notes = []
        s = lb_defaults()
        s["enabled"] = enabled
        s["listenbrainz"].update(username="me", token="tok")
        s["navidrome"].update(url="http://n", user="u", password="p", main_folder_id=1, discovery_folder_id=2)
        s["dirs"].update(main=str(self.main), discovery=str(self.disc))
        self.save(s)
        self.svc = LbService(db=self.db, kv=self.kv, tm=self.tm, notify=lambda t, b: self.notes.append((t, b)),
                             lb_factory=lambda s: self.lb, nd_factory=lambda s: self.nd,
                             adapter_factory=lambda s, delay: self.adapter, request_delay=lambda: 0, sleep=lambda x: None,
                             now=lambda: datetime(2026, 11, 20, 9, 0, 0))

    def save(self, s):
        self.kv.set(lb_settings.SETTINGS_KEY, s)

    def edit(self, fn):
        s = self.svc.settings() if hasattr(self, "svc") else lb_defaults()
        fn(s); self.save(s)

    def wait(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if self.tm.current() is None:
                time.sleep(0.05)
                if self.tm.current() is None:
                    return
            time.sleep(0.02)
        raise AssertionError("任务没有在时限内结束")

    def last(self):
        r = self.tm.recent_runs()[0]
        r["stats"] = json.loads(r["stats_json"])
        return r


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------- 门禁
@pytest.mark.parametrize("kind", ["sync", "promote", "cleanup"])
def test_master_switch_off_blocks_state_changing_tasks(tmp_path, kind):
    e = Env(tmp_path, enabled=False)
    with pytest.raises(NotReady, match="总开关"):
        e.svc.start(kind)
    assert e.tm.recent_runs() == []


def test_master_switch_off_still_allows_read_only_actions(tmp_path):
    e = Env(tmp_path, enabled=False)
    e.svc.start("reindex"); e.wait()
    assert e.last()["status"] == "done"
    e.svc.lb_factory, e.svc.nd_factory = (lambda s: FakeLoveLB()), (lambda s: FakeLoveND([]))
    e.svc.start("love", dry_run=True); e.wait()
    assert e.last()["type"] == "love_preview" and e.last()["status"] == "done"


def test_real_love_needs_both_switches(tmp_path):
    e = Env(tmp_path)
    with pytest.raises(NotReady, match="红心同步还没有启用"):
        e.svc.start("love")
    assert e.tm.recent_runs() == []


@pytest.mark.parametrize("mutate,msg", [
    (lambda s: s["navidrome"].update(url=""), "Navidrome 地址"),
    (lambda s: s["navidrome"].update(main_folder_id=None), "主库和试听库"),
    (lambda s: s["navidrome"].update(discovery_folder_id=None), "主库和试听库"),
    (lambda s: s["listenbrainz"].update(token=""), "ListenBrainz"),
])
def test_incomplete_configuration_gives_a_clear_message(env, mutate, msg):
    env.edit(mutate)
    with pytest.raises(NotReady, match=msg):
        env.svc.start("sync")
    assert env.tm.recent_runs() == []


def test_unknown_kind_is_rejected(env):
    with pytest.raises(NotReady):
        env.svc.start("format-disk")


# ---------------------------------------------------------------- 运行与互斥
def test_sync_runs_in_background_and_notifies_on_real_changes(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(1), lbtrack(2)])
    rid = env.svc.start("sync", trigger="schedule")
    env.wait()
    r = env.last()
    assert r["id"] == rid and r["status"] == "done" and r["trigger"] == "schedule" and r["stats"]["downloaded"] == 2
    assert env.notes == [("推荐联动：推荐同步", "新下载 2 首")]
    assert len(env.nd.playlists) == 1


def test_second_task_is_rejected_immediately_while_one_runs(env):
    gate = threading.Event()
    orig = env.adapter.download_song
    def slow(*a, **k):
        gate.wait(5)
        return orig(*a, **k)
    env.adapter.download_song = slow
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.svc.start("sync")
    with pytest.raises(TaskBusy):
        env.svc.start("cleanup")
    with pytest.raises(TaskBusy):
        env.svc.start("sync")
    assert env.svc.dashboard()["current_task"]["type"] == "sync"
    gate.set(); env.wait()
    env.svc.start("cleanup"); env.wait()                                  # 结束后可以再启动


def test_types_filter_is_passed_to_the_pipeline(env):
    env.lb.add("daily", "pd", [lbtrack(1)]); env.lb.add("weekly_jams", "pj", [lbtrack(2)])
    env.svc.start("sync", types=["daily"]); env.wait()
    assert env.lb.playlist_calls == ["pd"]


# ---------------------------------------------------------------- 通知策略
def test_no_notification_when_nothing_changed(env):
    env.svc.start("sync"); env.wait()                                      # LB 没有歌单
    env.svc.start("cleanup"); env.wait()
    assert env.notes == []


def test_failure_is_notified_and_recorded(env):
    env.nd.up = False
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.svc.start("sync"); env.wait()
    r = env.last()
    assert r["status"] == "failed" and "无法连接" in r["summary"]
    assert len(env.notes) == 1 and "失败" in env.notes[0][1] and "无法连接" in env.notes[0][1]


def _boom(*a, **k):
    raise LBError("ListenBrainz 请求失败（网络错误: SSLError）：/x")


def test_scheduled_sync_network_failure_asks_for_retry_and_stays_quiet(env):
    calls = []
    env.svc.on_failed = lambda kind, types, attempt: calls.append((kind, types, attempt)) or True
    env.lb.created_for = _boom
    env.svc.start("sync", trigger="schedule", types=["daily"]); env.wait()
    assert calls == [("sync", ["daily"], 1)]
    assert env.notes == []                                     # 已安排重试：先不打扰


def test_scheduled_sync_failure_notifies_when_retries_are_exhausted(env):
    env.svc.on_failed = lambda kind, types, attempt: False
    env.lb.created_for = _boom
    env.svc.start("sync", trigger="schedule", attempt=4); env.wait()
    assert len(env.notes) == 1 and "已自动重试 3 次仍失败" in env.notes[0][1]


def test_manual_sync_and_non_network_errors_never_auto_retry(env):
    calls = []
    env.svc.on_failed = lambda *a: calls.append(a) or True
    env.lb.created_for = _boom
    env.svc.start("sync"); env.wait()                           # 手动触发：用户自己看得到，不自动重试
    env.lb.created_for = lambda n: (_ for _ in ()).throw(LBError("Token 无效", 401))
    env.svc.start("sync", trigger="schedule"); env.wait()       # 401 不是网络抖动
    assert calls == [] and len(env.notes) == 2


def test_adapter_not_ready_fails_the_task_with_a_readable_reason(env):
    def bad(s, delay):
        raise NotReady("推荐联动只下载无损，需要VIP")
    env.svc.adapter_factory = bad
    env.svc.start("sync"); env.wait()
    r = env.last()
    assert r["status"] == "failed" and "VIP" in r["summary"] and "VIP" in env.notes[0][1]


def test_paused_run_is_notified(env):
    from lb_adapter import AdapterResult
    env.adapter.script = {"Song 1": AdapterResult(False, error="限流", error_kind="rate_limited")}
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.svc.start("sync"); env.wait()
    assert env.last()["status"] == "paused" and "已暂停" in env.notes[0][1]


def test_notification_failure_never_breaks_the_task(env):
    def boom(t, b):
        raise RuntimeError("bark down")
    env.svc.notify = boom
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.svc.start("sync"); env.wait()
    assert env.last()["status"] == "done"


@pytest.mark.parametrize("stats,status,err,expected", [
    ({"downloaded": 3, "not_found": 2}, "done", None, "新下载 3 首，未找到 2 首"),
    ({"promoted": 1}, "done", None, "转正 1 首"),
    ({"expired": 4}, "done", None, "过期清理 4 首"),
    ({"submitted": 9}, "done", None, "提交红心 9 首"),
    ({"not_found": 5}, "done", None, ""),                      # 只有「未找到」不值得打扰
    ({}, "done", None, ""),
    ({"downloaded": 1, "errors": 2}, "done", None, "新下载 1 首，出错/失败 2 首"),
])
def test_summarize(stats, status, err, expected):
    class H:  # 最小替身
        pass
    h = H(); h.stats, h.status, h.summary = stats, status, ""
    assert LbService.summarize("x", h, err) == expected


def test_summarize_error_and_paused():
    class H: pass
    h = H(); h.stats, h.status, h.summary = {}, "paused", "被限流"
    assert LbService.summarize("x", h, None) == "已暂停：被限流"
    assert "失败：RuntimeError: boom" == LbService.summarize("x", h, RuntimeError("boom"))


# ---------------------------------------------------------------- 索引、预览、红心
def test_reindex_with_scan_calls_navidrome_scan_and_fills_index(env):
    env.nd.main = [ndsong("m1", "A", "S")]
    env.svc.start("reindex", scan=True); env.wait()
    assert env.nd.scans == 1 and env.last()["stats"] == {"main": 1, "discovery": 0}
    assert env.db.conn.execute("SELECT COUNT(*) FROM local_files").fetchone()[0] == 1
    env.svc.start("reindex", scan=False); env.wait()
    assert env.nd.scans == 1                                                # 仅同步索引：不触发扫描


def test_love_preview_is_stored_for_the_page_and_never_writes(env):
    lb, nd = FakeLoveLB(), FakeLoveND([nd_song(1, mbid="abcdef01-1111-1111-1111-111111111111")])
    env.svc.lb_factory, env.svc.nd_factory = (lambda s: lb), (lambda s: nd)
    assert env.svc.love_preview() is None
    env.svc.start("love", dry_run=True); env.wait()
    pv = env.svc.love_preview()
    assert lb.love_calls == [] and [p["mbid"] for p in pv["plan"]] == ["abcdef01-1111-1111-1111-111111111111"]
    assert pv["stats"]["would_submit"] == 1


def test_real_love_submits_when_enabled(env):
    env.edit(lambda s: s["love"].update(enabled=True))
    lb, nd = FakeLoveLB(), FakeLoveND([nd_song(1, mbid="abcdef01-1111-1111-1111-111111111111")])
    env.svc.lb_factory, env.svc.nd_factory = (lambda s: lb), (lambda s: nd)
    env.svc.start("love"); env.wait()
    assert lb.love_calls == ["abcdef01-1111-1111-1111-111111111111"] and env.notes[0][1] == "提交红心 1 首"


def test_cleanup_preview_works_with_and_without_navidrome(env):
    pv = env.svc.cleanup_preview()
    assert pv["due_files"] == 0 and "stale_stars" not in pv
    env.edit(lambda s: s["navidrome"].update(url=""))
    pv = env.svc.cleanup_preview()
    assert pv["due_files"] == 0 and pv["stale_stars"] is True


def test_cleanup_preview_falls_back_when_navidrome_is_unreachable(env):
    def broken(s):
        raise RuntimeError("down")
    env.svc.nd_factory = broken
    assert env.svc.cleanup_preview()["stale_stars"] is True


# ---------------------------------------------------------------- 音乐平台账号选择（与现有下载任务一致）
class FakeWN:
    registry = {}

    def __init__(self, cookies):
        self.cookies = cookies
        self.api = f"api-for-{cookies.get('MUSIC_U')}"

    def account_info(self):
        return FakeWN.registry.get(self.cookies.get("MUSIC_U"))


class FakeDownloader:
    def __init__(self, cookies, api):
        self.cookies, self.api = cookies, api


@pytest.fixture
def platform(monkeypatch):
    import downloader, web_platform
    FakeWN.registry = {}
    monkeypatch.setattr(web_platform, "WebPlatform", FakeWN)
    monkeypatch.setattr(downloader, "SongDownloader", FakeDownloader)
    return FakeWN.registry


def state(account=None, borrowed=None, source="self", quality=None):
    return {"account": account, "download_account": borrowed,
            "settings": {"download_source": source, "quality": quality}}


class Store:
    def __init__(self, st): self.st = st
    def get(self): return self.st


def acct(token):
    return {"cookies": {"MUSIC_U": token}, "nickname": token}


def test_vip_login_account_is_used_directly(platform):
    platform["me"] = {"is_vip": True}
    a = default_adapter_factory(Store(state(acct("me"))), {}, 1.5)
    assert a.cookies == {"MUSIC_U": "me"} and a.quality == "lossless" and a.allow_mp3 is False and a.request_delay == 1.5


def test_vip_login_ignores_borrowed_account_entirely(platform):
    platform["me"] = {"is_vip": True}                       # 借用账号根本不在注册表里：若被校验就会失败
    a = default_adapter_factory(Store(state(acct("me"), acct("friend"), "borrowed")), {}, 0)
    assert a.cookies == {"MUSIC_U": "me"}


def test_non_vip_uses_borrowed_vip_account(platform):
    platform["me"] = {"is_vip": False}; platform["friend"] = {"is_vip": True}
    a = default_adapter_factory(Store(state(acct("me"), acct("friend"), "borrowed")), {}, 0)
    assert a.cookies == {"MUSIC_U": "friend"} and a.downloader.api == "api-for-friend" and a.quality == "lossless"


@pytest.mark.parametrize("st,reg,msg", [
    (state(None), {}, "扫码登录"),
    (state(acct("me")), {"me": None}, "登录已失效"),
    (state(acct("me")), {"me": {"is_vip": False}}, "VIP"),                                         # 非 VIP 且没选借用
    (state(acct("me"), None, "borrowed"), {"me": {"is_vip": False}}, "VIP"),                        # 选了借用但不存在
    (state(acct("me"), acct("friend"), "borrowed"), {"me": {"is_vip": False}, "friend": None}, "借用账号登录已失效"),
    (state(acct("me"), acct("friend"), "borrowed"), {"me": {"is_vip": False}, "friend": {"is_vip": False}}, "已不是VIP"),
])
def test_account_problems_give_clear_errors_and_never_fall_back_to_non_vip(platform, st, reg, msg):
    platform.update(reg)
    with pytest.raises(NotReady, match=msg):
        default_adapter_factory(Store(st), {}, 0)


def test_configured_quality_is_respected_for_vip(platform):
    platform["me"] = {"is_vip": True}
    a = default_adapter_factory(Store(state(acct("me"), quality="hires")), {}, 0)
    assert a.quality == "hires"
