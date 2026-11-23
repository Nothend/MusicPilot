from fakes import lb_defaults
import errno
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

import lb_localfs
import lb_promote
import lb_repo
import lb_settings
import lb_states as ST
from audio import make_flac
from db import Database
from nd_client import NavidromeError
from task_manager import TaskManager

NOW = datetime(2026, 11, 20, 9, 0, 0)


class DiskND:
    """假 Navidrome：扫描时按磁盘上的两个目录重建索引。歌曲 id 由标签派生（与真实 Navidrome 实测一致），
    所以移动文件不改 id、红心保留；可开关模拟「红心丢失」「id 变化」「尚未收录」。"""
    def __init__(self, main, disc):
        self.dirs = {"main": Path(main), "discovery": Path(disc)}
        self.songs, self.starred, self.lib_of = {}, set(), {}
        self.lose_stars_on_move = False
        self.new_id_on_move = False
        self.skip_indexing = False
        self.fail_scan = 0
        self.scans, self.stars, self.playlists, self.deleted = 0, [], {}, []
        self.rescan()

    def _sid(self, t, lib_suffix=""):
        return "id-" + hashlib.md5((t.artist + "|" + t.title + lib_suffix).encode()).hexdigest()[:8]

    def rescan(self):
        new = {}
        for lib, root in self.dirs.items():
            for p in lb_localfs.iter_audio_files(root):
                t = lb_localfs.read_file_tags(p)
                base = self._sid(t)
                sid = base
                moved = base in self.lib_of and self.lib_of[base] != lib
                if moved and self.new_id_on_move:
                    sid = base + "-new"
                if moved and self.lose_stars_on_move:
                    self.starred.discard(base)
                new[sid] = {"id": sid, "title": t.title, "artist": t.artist, "album": t.album, "album_artist": t.artist,
                            "duration": 200, "size": p.stat().st_size, "suffix": p.suffix[1:], "mbid": "",
                            "path_api": "x", "_lib": lib}
                self.lib_of[sid] = lib
                self.lib_of[base] = lib
        if self.skip_indexing:
            prev = self.songs
            new = {k: v for k, v in new.items()
                   if v["_lib"] != "main" or (k in prev and prev[k]["_lib"] == "main")}   # 主库里的新文件还没被收录
        self.songs = new

    def ping(self):
        return {"version": "t"}

    def scan_and_wait(self, timeout=600, poll=2.0):
        if self.fail_scan:
            self.fail_scan -= 1
            raise NavidromeError("scan failed")
        self.scans += 1
        self.rescan()
        return {}

    def iter_songs(self, folder_id=None, page=500):
        lib = "main" if folder_id == 1 else "discovery"
        for s in self.songs.values():
            if s["_lib"] == lib:
                yield {**s, "starred": s["id"] in self.starred}

    def starred_songs(self, folder_id=None):
        lib = "main" if folder_id == 1 else "discovery"
        return [{"id": s["id"]} for s in self.songs.values() if s["_lib"] == lib and s["id"] in self.starred]

    def star(self, sid):
        self.stars.append(sid)
        self.starred.add(sid)

    def create_playlist(self, name, ids):
        pid = f"pl{len(self.playlists) + 1}"
        self.playlists[pid] = (name, list(ids))
        return pid

    def delete_playlist(self, pid):
        self.deleted.append(pid)
        self.playlists.pop(pid, None)


class Env:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "t.sqlite")
        self.db.migrate()
        self.tm = TaskManager(self.db)
        self.main, self.disc = tmp_path / "songs", tmp_path / "discovery"
        self.main.mkdir(); self.disc.mkdir()
        self.nd = DiskND(self.main, self.disc)
        self.settings = lb_defaults()
        self.settings["navidrome"].update(url="http://n", user="u", password="p", main_folder_id=1, discovery_folder_id=2)
        self.settings["dirs"].update(main=str(self.main), discovery=str(self.disc))
        self._n = 0

    def add_item(self, artist="郭静", title="心墙", album="在树上唱歌", state=ST.LOCAL_DISCOVERY, star=True,
                 with_path=True, batch_mbid=None, track=3, year="2006"):
        self._n += 1
        p = make_flac(self.disc / f"src{self._n}.flac", artist, title,
                      extra={"album": album, "tracknumber": str(track), "date": year})
        self.nd.rescan()
        sid = next(s["id"] for s in self.nd.songs.values() if s["title"] == title and s["artist"] == artist)
        if star:
            self.nd.starred.add(sid)
        bid = lb_repo.create_batch(self.db, "weekly_jams", {"mbid": batch_mbid or f"pl-{self._n}", "title": "t", "date": "2026-11-20"},
                                   {"tracks": [{"recording_mbid": f"mb-{artist}-{title}", "artist": artist, "title": title,
                                                "album": album, "duration_ms": 200000, "artists": [{"name": artist}]}]})
        item = lb_repo.items_of_batch(self.db, bid)[0]["id"]
        lb_repo.set_state(self.db, item, ST.LOCAL_DISCOVERY, local_file_id=sid, entered_discovery_at="2026-11-18T00:00:00",
                          local_path=str(p) if with_path else None)
        if state != ST.LOCAL_DISCOVERY:
            lb_repo.set_state(self.db, item, state)
        return item, p, sid

    def promote(self):
        with self.tm.run("promote", "manual") as h:
            lb_promote.run_promote(lb_promote.PromoteDeps(self.db, self.settings, self.nd, h, now=lambda: NOW))
        return self.tm.recent_runs()[0]

    def item(self, i):
        return lb_repo.get_item(self.db, i)

    def promos(self, i):
        return [r["status"] for r in self.db.conn.execute("SELECT status FROM promotion_records WHERE item_id=? ORDER BY id", (i,))]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


DST = "郭静/在树上唱歌 (2006)/03 - 郭静 - 心墙.flac"


# ---------------------------------------------------------------- 主流程
def test_starred_song_is_moved_to_main_with_naming_rules_and_marked_promoted(env):
    item, src, sid = env.add_item()
    run = env.promote()
    dst = env.main / DST
    assert run["status"] == "done" and dst.is_file() and not src.exists()
    row = env.item(item)
    assert (row["state"], row["local_file_id"], row["starred"], row["local_path"]) == (ST.PROMOTED, sid, 1, str(dst))
    assert lb_localfs.read_file_tags(dst).title == "心墙"                       # 内容完好
    assert env.promos(item) == ["moved"] and env.nd.stars == []                  # 红心保留，无需补星
    evs = [(r["from_state"], r["to_state"]) for r in env.db.conn.execute("SELECT * FROM item_events WHERE item_id=? ORDER BY id", (item,))]
    assert evs[-2:] == [(ST.LOCAL_DISCOVERY, ST.PROMOTING), (ST.PROMOTING, ST.PROMOTED)]
    assert env.nd.scans == 1


def test_unstarred_songs_are_left_alone(env):
    item, src, _ = env.add_item(star=False)
    env.promote()
    assert src.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY and list(env.main.iterdir()) == []
    assert env.nd.scans == 0                                                      # 没有要转正的就不触发扫描


def test_starred_flag_follows_navidrome_both_ways(env):
    a, _, sa = env.add_item(title="A", star=True)
    b, _, sb = env.add_item(title="B", star=False)
    env.settings["dirs"]["main"] = str(env.main)
    lb_promote.sync_starred_flags(env.db, env.nd, 2)
    assert (env.item(a)["starred"], env.item(b)["starred"]) == (1, 0)
    env.nd.starred.discard(sa)
    lb_promote.sync_starred_flags(env.db, env.nd, 2)
    assert env.item(a)["starred"] == 0                                            # 取消红心 = 取消保护


def test_promoted_items_never_touch_other_files_in_main(env):
    pre = make_flac(env.main / "已有歌手" / "已有专辑" / "旧歌.flac", "已有歌手", "旧歌")
    before = pre.read_bytes()
    env.add_item()
    env.promote()
    assert pre.read_bytes() == before
    assert sorted(p.relative_to(env.main).as_posix() for p in env.main.rglob("*.flac")) == sorted(["已有歌手/已有专辑/旧歌.flac", DST])


# ---------------------------------------------------------------- 冲突：绝不覆盖
def test_name_conflict_skips_records_and_never_overwrites(env):
    item, src, _ = env.add_item()
    existing = make_flac(env.main / DST, "郭静", "心墙", extra={"album": "别的版本"})
    before = existing.read_bytes()
    env.promote()
    assert existing.read_bytes() == before and src.is_file()
    row = env.item(item)
    assert (row["state"], row["reason_kind"]) == (ST.LOCAL_DISCOVERY, lb_promote.CONFLICT) and "同名" in row["reason"]
    assert env.promos(item) == ["conflict"]
    env.promote()                                                                  # 自动转正不再反复尝试同一个冲突
    assert env.promos(item) == ["conflict"]


def test_conflict_item_is_still_protected_from_cleanup_by_star_flag(env):
    item, _, _ = env.add_item()
    make_flac(env.main / DST, "郭静", "心墙")
    env.promote()
    assert env.item(item)["starred"] == 1


# ---------------------------------------------------------------- 红心与 id
def test_lost_star_after_move_is_restored(env):
    env.nd.lose_stars_on_move = True
    item, _, sid = env.add_item()
    env.promote()
    assert env.nd.stars == [sid] and sid in env.nd.starred
    assert env.item(item)["state"] == ST.PROMOTED and "restarred" in env.promos(item)


def test_changed_id_is_claimed_and_playlist_republished(env):
    env.nd.new_id_on_move = True
    item, _, sid = env.add_item()
    lb_repo.record_playlist(env.db, 1, "LB Weekly Jams 2026-11-20", "old-pl", 1)
    env.promote()
    row = env.item(item)
    assert row["state"] == ST.PROMOTED and row["local_file_id"] == sid + "-new"
    assert env.nd.deleted == ["old-pl"]                                           # 旧歌单（含失效 id）被替换
    (name, ids), = env.nd.playlists.values()
    assert ids == [sid + "-new"] and name == "LB Weekly Jams 2026-11-20"
    assert sid + "-new" in env.nd.stars                                          # 新 id 上没有红心 → 补星


# ---------------------------------------------------------------- 中断与恢复
def test_crash_after_move_is_finished_by_next_run_without_moving_again(env):
    item, src, sid = env.add_item()
    env.nd.fail_scan = 1
    with pytest.raises(NavidromeError):
        env.promote()
    assert (env.main / DST).is_file() and not src.exists()
    row = env.item(item)
    assert row["state"] == ST.PROMOTING and row["local_path"] == str(env.main / DST)
    assert env.tm.recent_runs()[0]["status"] == "failed"
    env.promote()
    assert env.item(item)["state"] == ST.PROMOTED and env.promos(item) == ["moved"]


def test_not_yet_indexed_stays_promoting_then_completes(env):
    item, _, _ = env.add_item()
    env.nd.skip_indexing = True
    run = env.promote()
    assert env.item(item)["state"] == ST.PROMOTING and json.loads(run["stats_json"]).get("unresolved") == 1
    env.nd.skip_indexing = False
    env.promote()
    assert env.item(item)["state"] == ST.PROMOTED


def test_promoting_with_file_missing_everywhere_is_rolled_back_not_stuck(env):
    item, src, _ = env.add_item()
    lb_repo.set_state(env.db, item, ST.PROMOTING, local_path=str(env.main / DST))
    run = env.promote()
    assert env.item(item)["state"] == ST.LOCAL_DISCOVERY and env.item(item)["reason_kind"] == lb_promote.ERROR


# ---------------------------------------------------------------- 找不到文件 / 路径未知
def test_unknown_path_is_resolved_by_tags(env):
    item, src, _ = env.add_item(with_path=False)
    env.promote()
    assert (env.main / DST).is_file() and not src.exists() and env.item(item)["state"] == ST.PROMOTED


def test_missing_source_file_is_recorded_and_retryable(env):
    item, src, _ = env.add_item()
    src.unlink()
    env.promote()
    row = env.item(item)
    assert (row["state"], row["reason_kind"]) == (ST.LOCAL_DISCOVERY, lb_promote.ERROR)
    assert env.promos(item) == ["error"] and list(env.main.iterdir()) == []
    make_flac(src, "郭静", "心墙", extra={"album": "在树上唱歌", "tracknumber": "3", "date": "2006"})   # 文件回来了
    env.nd.rescan()
    env.promote()
    assert env.item(item)["state"] == ST.PROMOTED                                  # error 类失败下次会自动重试


# ---------------------------------------------------------------- 共享同一文件
def test_two_batches_sharing_one_file_are_promoted_together_with_one_move(env):
    a, src, sid = env.add_item(batch_mbid="p1")
    b = lb_repo.create_batch(env.db, "daily", {"mbid": "p2", "title": "t", "date": "2026-11-21"},
                             {"tracks": [{"recording_mbid": "mb-郭静-心墙", "artist": "郭静", "title": "心墙", "album": "x",
                                          "duration_ms": 200000, "artists": [{"name": "郭静"}]}]})
    bi = lb_repo.items_of_batch(env.db, b)[0]["id"]
    lb_repo.set_state(env.db, bi, ST.LOCAL_DISCOVERY, local_file_id=sid, local_path=str(src))
    env.promote()
    assert env.item(a)["state"] == ST.PROMOTED and env.item(bi)["state"] == ST.PROMOTED
    assert env.promos(a) == ["moved"] and len(list(env.main.rglob("*.flac"))) == 1


def test_sibling_that_did_not_join_the_group_becomes_local_main(env):
    a, src, sid = env.add_item(batch_mbid="p1")
    b = lb_repo.create_batch(env.db, "daily", {"mbid": "p2", "title": "t", "date": "2026-11-21"},
                             {"tracks": [{"recording_mbid": "mb-郭静-心墙", "artist": "郭静", "title": "心墙", "album": "x",
                                          "duration_ms": 200000, "artists": [{"name": "郭静"}]}]})
    bi = lb_repo.items_of_batch(env.db, b)[0]["id"]
    lb_repo.set_state(env.db, bi, ST.LOCAL_DISCOVERY, local_file_id=sid, local_path=str(src))
    lb_repo.update_fields(env.db, bi, reason_kind=lb_promote.CONFLICT)             # 被排除在本组之外
    env.promote()
    assert env.item(a)["state"] == ST.PROMOTED and env.item(bi)["state"] == ST.LOCAL_MAIN
    assert env.item(bi)["local_path"] == str(env.main / DST)


# ---------------------------------------------------------------- 安全
def test_hostile_tags_cannot_write_outside_main(env, tmp_path):
    item, src, _ = env.add_item(artist="../../escape", title="x", album="../../../evil")
    env.promote()
    written = [p for p in tmp_path.rglob("*.flac") if env.main in p.parents]
    assert len(written) == 1 and lb_localfs.inside(written[0], env.main)
    assert not (tmp_path / "escape").exists() and not (tmp_path.parent / "evil").exists()


def test_cross_device_move_falls_back_to_copy_and_still_promotes(env, monkeypatch):
    def exdev(*a, **k):
        raise OSError(errno.EXDEV, "x")
    monkeypatch.setattr(os, "link", exdev)
    item, src, _ = env.add_item()
    env.promote()
    assert (env.main / DST).is_file() and not src.exists() and env.item(item)["state"] == ST.PROMOTED
    assert [p.name for p in (env.main / DST).parent.iterdir()] == ["03 - 郭静 - 心墙.flac"]       # 没有残留临时文件


def test_move_failure_reverts_state_and_keeps_source(env, monkeypatch):
    def boom(src, dst):
        raise OSError(errno.EIO, "disk error")
    monkeypatch.setattr(lb_promote, "safe_move", boom)
    item, src, _ = env.add_item()
    env.promote()
    row = env.item(item)
    assert src.is_file() and (row["state"], row["reason_kind"]) == (ST.LOCAL_DISCOVERY, lb_promote.ERROR)
    assert env.promos(item) == ["error"]


def test_requires_navidrome_libraries(env):
    env.settings["navidrome"]["main_folder_id"] = None
    with pytest.raises(ValueError, match="主库和试听库"):
        env.promote()


def test_playlist_republished_for_promoted_batch(env):
    item, _, sid = env.add_item()
    env.promote()
    (name, ids), = env.nd.playlists.values()
    assert ids == [sid] and name.startswith("LB Weekly Jams")
