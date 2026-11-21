import itertools
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import lb_pipeline
import lb_repo
import lb_settings
import lb_states as ST
from audio import make_flac
from db import Database
from lb_adapter import AdapterResult
from lb_pipeline import SyncDeps, run_sync
from nd_client import NavidromeError
from task_manager import TaskManager

T0 = datetime(2026, 11, 20, 8, 30, 0)


# ---------------------------------------------------------------- 假外部系统
class FakeLB:
    def __init__(self):
        self.headers, self.playlists = [], {}
        self.playlist_calls = []

    def add(self, rtype, mbid, tracks, date="2026-11-20T00:00:00+00:00"):
        self.headers.insert(0, {"mbid": mbid, "title": f"{rtype} {mbid}", "date": date, "type": rtype,
                                "expires_at": "", "source_patch": ""})
        self.playlists[mbid] = {"mbid": mbid, "title": "x", "date": date, "tracks": tracks}

    def created_for(self, n):
        return list(self.headers)

    def playlist(self, mbid):
        self.playlist_calls.append(mbid)
        return self.playlists[mbid]


def lbtrack(n, artist="A", title=None, dur=200000, mbid=None):
    return {"recording_mbid": mbid or f"mb-{n}", "title": title or f"Song {n}", "artist": artist,
            "artists": [{"name": artist, "mbid": "", "join": ""}], "album": "Al", "duration_ms": dur}


def ndsong(sid, artist="A", title="Song", dur=200, starred=False):
    return {"id": sid, "title": title, "artist": artist, "album": "Al", "album_artist": artist, "duration": dur,
            "size": 100, "suffix": "flac", "mbid": "", "starred": starred, "path_api": "x"}


class FakeND:
    def __init__(self):
        self.main, self.indexed_disc, self.disk_disc = [], [], []
        self.scans, self.playlists, self.deleted = 0, {}, []
        self.up = True
        self.index_new_files = True            # False：模拟 Navidrome 没收录新文件
        self._ids = itertools.count(1)

    def ping(self):
        if not self.up:
            raise NavidromeError("无法连接 Navidrome")
        return {"version": "0.64.2"}

    def iter_songs(self, folder_id=None, page=500):
        yield from (self.main if folder_id == 1 else self.indexed_disc)

    def scan_and_wait(self, timeout=600, poll=2.0):
        self.scans += 1
        if self.index_new_files:
            self.indexed_disc = list(self.disk_disc)
        return {"scanning": False, "count": 0, "last_scan": ""}

    def starred_songs(self, folder_id=None):
        return []

    def create_playlist(self, name, ids):
        pid = f"pl{next(self._ids)}"
        self.playlists[pid] = (name, list(ids))
        return pid

    def delete_playlist(self, pid):
        self.deleted.append(pid)
        self.playlists.pop(pid, None)


class FakeAdapter:
    """按歌名编排结果；成功时在 discovery 目录真的落一个文件，并登记到假 Navidrome 的磁盘（扫描后才可见）。"""
    def __init__(self, nd, out_dir):
        self.nd, self.out = nd, Path(out_dir)
        self.script = {}                       # title -> AdapterResult | Exception | list(依次使用)
        self.calls = []

    def download_song(self, artist, title, output_dir, **kw):
        self.calls.append(title)
        r = self.script.get(title, "ok")
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, BaseException):
            raise r
        if r == "ok":
            p = make_flac(Path(output_dir) / f"{artist} - {title}.flac", artist, title)
            sid = f"nd-{title}"
            self.nd.disk_disc.append(ndsong(sid, artist, title, 200))
            return AdapterResult(True, str(p), netease_id=1, quality="lossless", file_type="flac")
        if isinstance(r, str):                  # 错误类型简写
            return AdapterResult(False, error=f"fake {r}", error_kind=r)
        return r


# ---------------------------------------------------------------- 装配
class Env:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "t.sqlite")
        self.db.migrate()
        self.tm = TaskManager(self.db)
        self.main_dir, self.disc_dir = tmp_path / "songs", tmp_path / "discovery"
        self.main_dir.mkdir()
        self.disc_dir.mkdir()
        self.lb, self.nd = FakeLB(), FakeND()
        self.adapter = FakeAdapter(self.nd, self.disc_dir)
        self.settings = lb_settings.defaults()
        self.settings["navidrome"].update(url="http://n", user="u", password="p", main_folder_id=1, discovery_folder_id=2)
        self.settings["dirs"].update(main=str(self.main_dir), discovery=str(self.disc_dir))
        self.now = T0

    def sync(self, types=None, trigger="manual"):
        with self.tm.run("sync", trigger) as h:
            lb_pipeline.run_sync(SyncDeps(self.db, self.settings, self.lb, self.nd, self.adapter, h,
                                          request_delay=0, sleep=lambda s: None, now=lambda: self.now, types=types))
        return self.tm.recent_runs()[0]

    def items(self, batch_id=1):
        return {i["title"]: i for i in lb_repo.items_of_batch(self.db, batch_id)}

    def states(self, batch_id=1):
        return {t: i["state"] for t, i in self.items(batch_id).items()}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def stats(run):
    return json.loads(run["stats_json"])


# ---------------------------------------------------------------- 主流程
def test_full_happy_path_covers_every_outcome(env):
    env.nd.main = [ndsong("m1", "A", "Song 1")]
    env.nd.indexed_disc = [ndsong("d2", "A", "Song 2")]
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2), lbtrack(3), lbtrack(4), lbtrack(5)])
    env.adapter.script = {"Song 4": "not_found", "Song 5": "download"}
    run = env.sync()
    assert run["status"] == "done"
    assert env.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2": ST.LOCAL_DISCOVERY, "Song 3": ST.LOCAL_DISCOVERY,
                            "Song 4": ST.NOT_FOUND, "Song 5": ST.DOWNLOAD_FAILED}
    it = env.items()
    assert it["Song 1"]["local_file_id"] == "m1" and it["Song 2"]["local_file_id"] == "d2"
    assert it["Song 3"]["local_file_id"] == "nd-Song 3"                       # 扫描后认领到了 id
    assert Path(it["Song 3"]["local_path"]).is_file() and it["Song 3"]["entered_discovery_at"] == "2026-11-20T08:30:00"
    assert it["Song 4"]["reason_kind"] == "not_found" and it["Song 4"]["next_retry_at"]      # 进入冷却
    assert it["Song 5"]["retry_count"] == 1 and it["Song 5"]["next_retry_at"] == "2026-11-20T09:00:00"
    assert env.adapter.calls == ["Song 3", "Song 4", "Song 5"]                # 本地已有的不下载
    assert env.nd.scans == 1
    # 歌单：按顺序放入「有文件」的歌（主库 + 试听库 + 新下载），名称带日期
    (name, ids), = env.nd.playlists.values()
    assert name == "LB Weekly Jams 2026-11-20" and ids == ["m1", "d2", "nd-Song 3"]
    s = stats(run)
    assert (s["recommended"], s["local_main"], s["local_discovery"], s["downloaded"], s["not_found"], s["failed"]) == (5, 1, 1, 1, 1, 1)
    assert [r["item_id"] for r in env.db.conn.execute("SELECT item_id FROM download_records")] == [3, 4, 5]


def test_every_state_change_is_logged_as_event(env):
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    env.sync()
    evs = [tuple(r) for r in env.db.conn.execute("SELECT from_state, to_state FROM item_events ORDER BY id")]
    assert evs == [(ST.PENDING, ST.DOWNLOADING), (ST.DOWNLOADING, ST.LOCAL_DISCOVERY)]


def test_never_writes_to_main_library_dir(env):
    (env.main_dir / "keep.flac").write_bytes(b"x")
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1), lbtrack(2)])
    env.sync()
    assert [p.name for p in env.main_dir.iterdir()] == ["keep.flac"]


def test_batch_marked_done_when_nothing_left_to_do(env):
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    env.sync()
    assert lb_repo.active_batches(env.db) == []


# ---------------------------------------------------------------- 拉取歌单的规则
def test_only_newest_playlist_per_type_and_processed_once(env):
    env.lb.add("weekly_jams", "old", [lbtrack(1)], date="2026-09-07T00:00:00+00:00")
    env.lb.add("weekly_jams", "new", [lbtrack(2)], date="2026-09-14T00:00:00+00:00")
    env.sync()
    assert env.lb.playlist_calls == ["new"]
    assert env.db.conn.execute("SELECT lb_playlist_mbid FROM recommendation_batches").fetchall()[0][0] == "new"
    env.sync()                                        # 再跑一次：同一份歌单不再处理
    assert env.lb.playlist_calls == ["new"]
    assert env.db.conn.execute("SELECT COUNT(*) FROM recommendation_batches").fetchone()[0] == 1


def test_no_playlist_for_a_type_is_normal_not_an_error(env):
    run = env.sync()                                  # LB 什么都没有（如目前没有 Daily Jams）
    assert run["status"] == "done"
    msgs = [x["msg"] for x in env.tm.logs(run["id"])]
    assert any("Daily Jams" in m and "暂无" in m for m in msgs)


def test_disabled_type_and_types_filter(env):
    env.settings["strategy"]["weekly_exploration"]["enabled"] = False
    env.lb.add("weekly_exploration", "pe", [lbtrack(1)])
    env.lb.add("weekly_jams", "pj", [lbtrack(2)])
    env.lb.add("daily", "pd", [lbtrack(3)])
    env.sync(types=["daily"])
    assert env.lb.playlist_calls == ["pd"]
    env.sync()                                        # 全部类型：exploration 被禁用，daily 已处理
    assert sorted(env.lb.playlist_calls) == ["pd", "pj"]


def test_empty_playlist_is_skipped(env):
    env.lb.add("weekly_jams", "pl", [])
    env.sync()
    assert env.db.conn.execute("SELECT COUNT(*) FROM recommendation_batches").fetchone()[0] == 0


# ---------------------------------------------------------------- 最大新下载数
def test_cap_counts_only_successful_downloads_and_ignores_local_hits(env):
    env.settings["strategy"]["weekly_jams"]["max_new"] = 1
    env.nd.main = [ndsong("m1", "A", "Song 1"), ndsong("m2", "A", "Song 2")]            # 两首本地已有，不占名额
    env.lb.add("weekly_jams", "pl", [lbtrack(i) for i in range(1, 6)])
    env.adapter.script = {"Song 3": "not_found"}                                         # 失败的也不占名额
    env.sync()
    assert env.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2": ST.LOCAL_MAIN, "Song 3": ST.NOT_FOUND,
                            "Song 4": ST.LOCAL_DISCOVERY, "Song 5": ST.SKIPPED}
    assert env.items()["Song 5"]["reason_kind"] == "over_limit"
    assert env.adapter.calls == ["Song 3", "Song 4"]


def test_cap_zero_means_unlimited(env):
    env.settings["strategy"]["daily"]["max_new"] = 0
    env.lb.add("daily", "pd", [lbtrack(i) for i in range(1, 8)])
    env.sync()
    assert set(env.states().values()) == {ST.LOCAL_DISCOVERY} and len(env.adapter.calls) == 7


# ---------------------------------------------------------------- 限流、中断、恢复
def test_rate_limit_pauses_run_without_marking_not_found_then_resumes_without_refetching(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(i) for i in range(1, 6)])
    env.adapter.script = {"Song 3": [AdapterResult(False, error="限流", error_kind="rate_limited"), "ok"]}
    run = env.sync()
    assert run["status"] == "paused"
    assert env.states() == {"Song 1": ST.LOCAL_DISCOVERY, "Song 2": ST.LOCAL_DISCOVERY, "Song 3": ST.PENDING,
                            "Song 4": ST.PENDING, "Song 5": ST.PENDING}        # 被限流的不是 NOT_FOUND
    assert len(lb_repo.active_batches(env.db)) == 1
    env.adapter.calls.clear()
    run2 = env.sync()
    assert run2["status"] == "done"
    assert set(env.states().values()) == {ST.LOCAL_DISCOVERY}
    assert env.adapter.calls == ["Song 3", "Song 4", "Song 5"]                 # 1、2 不重复处理
    assert env.lb.playlist_calls == ["pl"]                                      # 不重新拉取歌单


def test_crash_mid_run_resumes_from_database_state(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(i) for i in range(1, 5)])
    env.adapter.script = {"Song 3": KeyboardInterrupt()}                        # 模拟进程被杀（BaseException）
    with pytest.raises(KeyboardInterrupt):
        env.sync()
    assert env.states() == {"Song 1": ST.LOCAL_DISCOVERY, "Song 2": ST.LOCAL_DISCOVERY,
                            "Song 3": ST.DOWNLOADING, "Song 4": ST.PENDING}    # 每首处理完立刻落库
    assert env.tm.recent_runs()[0]["status"] == "failed"
    env.tm.recover()
    env.adapter.script = {}
    env.adapter.calls.clear()
    run = env.sync()
    assert run["status"] == "done" and set(env.states().values()) == {ST.LOCAL_DISCOVERY}
    assert env.adapter.calls == ["Song 3", "Song 4"]
    assert env.lb.playlist_calls == ["pl"]


def test_single_item_exception_does_not_stop_the_run(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(i) for i in range(1, 4)])
    env.adapter.script = {"Song 2": RuntimeError("boom")}
    run = env.sync()
    assert run["status"] == "done" and stats(run)["errors"] == 1
    st = env.states()
    assert st["Song 1"] == ST.LOCAL_DISCOVERY and st["Song 3"] == ST.LOCAL_DISCOVERY
    assert st["Song 2"] == ST.DOWNLOAD_FAILED and env.items()["Song 2"]["reason_kind"] == "exception"


def test_staging_leftovers_are_cleaned_at_start(env):
    stale = env.disc_dir / ".staging" / "half.flac.part"
    stale.parent.mkdir()
    stale.write_bytes(b"x")
    (env.disc_dir / "real.flac").write_bytes(b"keep")
    env.sync()
    assert not stale.exists() and (env.disc_dir / "real.flac").read_bytes() == b"keep"


def test_navidrome_down_aborts_before_any_work(env):
    env.nd.up = False
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    with pytest.raises(NavidromeError):
        env.sync()
    assert env.adapter.calls == [] and env.db.conn.execute("SELECT COUNT(*) FROM recommendation_batches").fetchone()[0] == 0
    assert env.tm.recent_runs()[0]["status"] == "failed"


def test_requires_navidrome_libraries_configured(env):
    env.settings["navidrome"]["discovery_folder_id"] = None
    with pytest.raises(ValueError, match="主库和试听库"):
        env.sync()


# ---------------------------------------------------------------- 失败重试
def test_failed_download_retries_after_backoff_then_succeeds(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.adapter.script = {"Song 1": ["download", "ok"]}
    env.sync()
    assert env.states() == {"Song 1": ST.DOWNLOAD_FAILED} and len(lb_repo.active_batches(env.db)) == 1   # 还能重试，批次保持活跃
    env.sync()                                                                                           # 还没到重试时间
    assert env.adapter.calls == ["Song 1"]
    env.now = T0 + timedelta(minutes=31)
    env.sync()
    assert env.states() == {"Song 1": ST.LOCAL_DISCOVERY} and env.adapter.calls == ["Song 1", "Song 1"]
    assert env.db.conn.execute("SELECT COUNT(*) FROM download_records WHERE item_id=1").fetchone()[0] == 2
    assert lb_repo.active_batches(env.db) == []


def test_retries_are_capped(env):
    env.settings["retry"]["download_max_retries"] = 2
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.adapter.script = {"Song 1": "download"}
    for k in range(6):
        env.now = T0 + timedelta(hours=3 * k)
        env.sync()
    assert len(env.adapter.calls) == 3                                        # 首次 + 2 次重试
    assert env.states() == {"Song 1": ST.DOWNLOAD_FAILED}
    assert lb_repo.active_batches(env.db) == []                               # 用尽重试后批次收尾


# ---------------------------------------------------------------- 跨批次
def test_not_found_cooldown_blocks_repeat_search_until_it_expires(env):
    env.lb.add("weekly_jams", "p1", [lbtrack(1, mbid="same")])
    env.adapter.script = {"Song 1": "not_found"}
    env.sync()
    env.lb.add("weekly_exploration", "p2", [lbtrack(1, mbid="same")])           # 另一份歌单又推荐了同一首
    env.now = T0 + timedelta(days=2)
    env.sync()
    assert env.adapter.calls == ["Song 1"]                                       # 冷却期内不再搜索
    assert env.states(2) == {"Song 1": ST.NOT_FOUND} and env.items(2)["Song 1"]["reason_kind"] == "cooldown"
    env.lb.add("daily", "p3", [lbtrack(1, mbid="same")])
    env.now = T0 + timedelta(days=8)                                             # 冷却 7 天已过
    env.adapter.script = {}
    env.sync()
    assert env.adapter.calls == ["Song 1", "Song 1"] and env.states(3) == {"Song 1": ST.LOCAL_DISCOVERY}


def test_same_song_in_two_batches_is_downloaded_only_once(env):
    env.lb.add("weekly_jams", "p1", [lbtrack(1, mbid="same")])
    env.lb.add("weekly_exploration", "p2", [lbtrack(1, mbid="same")])
    env.sync()
    assert env.adapter.calls == ["Song 1"]
    a, b = env.items(1)["Song 1"], env.items(2)["Song 1"]
    assert b["state"] == ST.LOCAL_DISCOVERY and b["local_path"] == a["local_path"] and b["local_file_id"] == "nd-Song 1"


def test_expired_song_recommended_again_is_downloaded_again(env):
    env.lb.add("weekly_jams", "p1", [lbtrack(1, mbid="same")])
    env.sync()
    lb_repo.set_state(env.db, 1, ST.EXPIRED)                                     # 过期清理后文件已不在（索引里也没有）
    env.nd.indexed_disc, env.nd.disk_disc = [], []
    env.lb.add("daily", "p2", [lbtrack(1, mbid="same")])
    env.sync()
    assert env.adapter.calls == ["Song 1", "Song 1"] and env.states(2) == {"Song 1": ST.LOCAL_DISCOVERY}


# ---------------------------------------------------------------- 匹配的特殊结果
def test_multiple_match_is_recorded_for_manual_choice_and_not_downloaded(env):
    env.nd.main = [ndsong("a", "A", "Song 1", dur=200), ndsong("b", "A", "Song 1", dur=400)]
    env.lb.add("weekly_jams", "pl", [lbtrack(1, dur=None)])
    env.sync()
    assert env.states() == {"Song 1": ST.MULTIPLE_MATCH} and env.adapter.calls == []
    cands = {r["nd_song_id"] for r in env.db.conn.execute("SELECT nd_song_id FROM match_candidates")}
    assert cands == {"a", "b"}


def test_inherits_known_path_and_entry_time_for_already_adopted_discovery_file(env):
    env.lb.add("weekly_jams", "p1", [lbtrack(1, mbid="m")])
    env.sync()
    first = env.items(1)["Song 1"]
    env.lb.add("daily", "p2", [lbtrack(1, mbid="m")])
    env.now = T0 + timedelta(days=3)
    env.sync()
    second = env.items(2)["Song 1"]
    assert second["state"] == ST.LOCAL_DISCOVERY and second["entered_discovery_at"] == first["entered_discovery_at"]
    assert second["local_path"] == first["local_path"]                            # 过期计时从首次入库算起，不被重置


def test_starred_flag_is_picked_up_from_index(env):
    env.nd.main = [ndsong("m1", "A", "Song 1", starred=True)]
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.sync()
    assert env.items()["Song 1"]["starred"] == 1


# ---------------------------------------------------------------- 扫描认领与歌单
def test_unresolved_new_file_is_claimed_on_next_run_and_playlist_republished(env):
    env.nd.index_new_files = False                                                # Navidrome 这次没收录
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    run = env.sync()
    assert stats(run)["unresolved"] == 1 and env.items()["Song 1"]["local_file_id"] is None
    assert env.nd.playlists == {}                                                  # 没有 id 就不放进歌单
    env.nd.index_new_files = True
    env.sync()
    assert env.items()["Song 1"]["local_file_id"] == "nd-Song 1"
    (name, ids), = env.nd.playlists.values()
    assert ids == ["nd-Song 1"]


def test_playlist_is_replaced_not_duplicated_when_republished(env):
    env.nd.index_new_files = False
    env.lb.add("weekly_jams", "pl", [lbtrack(1), lbtrack(2)])
    env.nd.main = [ndsong("m1", "A", "Song 1")]
    env.sync()
    first = list(env.nd.playlists)
    env.nd.index_new_files = True
    env.sync()
    assert len(env.nd.playlists) == 1 and env.nd.deleted == first                   # 旧的删掉，只剩新的
    (_, ids), = env.nd.playlists.values()
    assert ids == ["m1", "nd-Song 2"]
    assert len(lb_repo.live_playlists_of_batch(env.db, 1)) == 1


def test_no_playable_songs_creates_no_playlist(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    env.adapter.script = {"Song 1": "not_found"}
    env.sync()
    assert env.nd.playlists == {}


def test_playlist_failure_does_not_fail_the_task(env):
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    def boom(name, ids):
        raise NavidromeError("创建失败")
    env.nd.create_playlist = boom
    run = env.sync()
    assert run["status"] == "done" and env.states() == {"Song 1": ST.LOCAL_DISCOVERY}
    assert any(x["level"] == "ERROR" and "创建歌单失败" in x["msg"] for x in env.tm.logs(run["id"]))


def test_exhausted_item_is_not_retried_even_while_batch_stays_active(env):
    """批次里还有别的待处理项（被限流）时批次保持活跃；已用尽重试的失败项也不能被无限重试。"""
    env.settings["retry"]["download_max_retries"] = 1
    env.lb.add("weekly_jams", "pl", [lbtrack(1), lbtrack(2)])
    rl = AdapterResult(False, error="限流", error_kind="rate_limited")
    env.adapter.script = {"Song 1": "download", "Song 2": [rl, rl, rl, "ok"]}
    for k in range(4):
        env.now = T0 + timedelta(hours=3 * k)
        env.sync()
    assert env.adapter.calls.count("Song 1") == 2                         # 首次 + 1 次重试，之后不再试
    assert env.states() == {"Song 1": ST.DOWNLOAD_FAILED, "Song 2": ST.LOCAL_DISCOVERY}
    assert lb_repo.active_batches(env.db) == []


def test_library_has_radio_edit_gets_replace_hint(env):
    env.nd.main = [ndsong("m1", "A", "Song 1 (Radio Edit)", dur=170), ndsong("m2", "A", "Song 2", dur=200)]
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1, dur=200000), lbtrack(2, title="Song 2 (Radio Edit)", dur=200000)])
    env.sync()
    it = env.items()
    assert env.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2 (Radio Edit)": ST.LOCAL_MAIN} or env.states() == {"Song 1": ST.LOCAL_MAIN, "Song 2": ST.LOCAL_MAIN}
    s1 = it["Song 1"]
    assert s1["reason_kind"] == "variant" and "Radio Edit" in s1["reason"] and "原版" in s1["reason"]
    other = [v for k, v in it.items() if k != "Song 1"][0]
    assert other["reason_kind"] in ("", None)          # 库里是原版、推荐的是电台版：不提示
    assert env.adapter.calls == []                      # 都不下载


def test_library_has_radio_edit_gets_replace_hint(env):
    env.nd.main = [ndsong("m1", "A", "Song 1 (Radio Edit)", dur=170), ndsong("m2", "A", "Song 2", dur=200)]
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1, dur=200000), lbtrack(2, title="Song 2 (Radio Edit)", dur=200000)])
    env.sync()
    rows = list(env.items().values())
    assert [r["state"] for r in rows] == [ST.LOCAL_MAIN, ST.LOCAL_MAIN] and env.adapter.calls == []   # 都不下载
    hinted = [r for r in rows if r["reason_kind"] == "variant"]
    assert len(hinted) == 1 and "Radio Edit" in hinted[0]["reason"] and "原版" in hinted[0]["reason"]  # 只有「库里是剪辑版」才提示


def test_hard_version_in_library_still_downloads_original(env):
    env.nd.main = [ndsong("m1", "A", "Song 1 (Live)", dur=200)]
    env.lb.add("weekly_jams", "pl-1", [lbtrack(1)])
    env.sync()
    assert env.adapter.calls == ["Song 1"]            # Live ≠ 原版：原版照常下载
