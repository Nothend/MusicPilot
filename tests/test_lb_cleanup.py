import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import lb_cleanup
import lb_localfs
import lb_repo
import lb_states as ST
from audio import make_flac
from nd_client import NavidromeError
from test_lb_promote import Env, NOW

OLD = "2026-11-10T00:00:00"          # 比 NOW 早 10 天（保留 7 天 → 已过期）
RECENT = "2026-11-18T00:00:00"       # 2 天前


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def cleanup(env, now=NOW):
    with env.tm.run("cleanup", "manual") as h:
        lb_cleanup.run_cleanup(lb_cleanup.CleanupDeps(env.db, env.settings, env.nd, h, now=lambda: now))
    return env.tm.recent_runs()[0]


def aged(env, entered=OLD, **kw):
    kw.setdefault("star", False)
    item, p, sid = env.add_item(**kw)
    lb_repo.update_fields(env.db, item, entered_discovery_at=entered)
    return item, p, sid


def stats(run):
    return json.loads(run["stats_json"])


def trash_files(env):
    return sorted(p.relative_to(env.disc).as_posix() for p in (env.disc / ".trash").rglob("*") if p.is_file())


# ---------------------------------------------------------------- 基本流程（软删除）
def test_expired_song_goes_to_trash_state_expired_and_leaves_playlist(env):
    item, src, sid = aged(env)
    lb_repo.record_playlist(env.db, 1, "LB Weekly Jams 2026-11-20", "old-pl", 1)
    run = cleanup(env)
    assert run["status"] == "done" and stats(run)["expired"] == 1
    assert not src.exists() and trash_files(env) == [".trash/20261120/src1.flac"]
    row = env.item(item)
    assert (row["state"], row["reason_kind"]) == (ST.EXPIRED, "expired")
    evs = [(r["from_state"], r["to_state"]) for r in env.db.conn.execute("SELECT * FROM item_events WHERE item_id=? ORDER BY id", (item,))]
    assert evs[-1] == (ST.LOCAL_DISCOVERY, ST.EXPIRED)
    assert env.nd.scans == 1 and env.nd.deleted == ["old-pl"] and env.nd.playlists == {}   # 歌单里去掉了
    t = env.db.conn.execute("SELECT * FROM trash_entries").fetchone()
    assert t["item_id"] == item and t["purged_at"] is None and t["trash_path"].endswith("src1.flac")


def test_second_run_is_a_noop(env):
    aged(env)
    cleanup(env)
    scans = env.nd.scans
    run = cleanup(env)
    assert stats(run).get("expired") is None and env.nd.scans == scans


def test_not_yet_expired_is_untouched_and_triggers_no_scan(env):
    item, src, _ = aged(env, entered=RECENT)
    cleanup(env)
    assert src.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY and env.nd.scans == 0


def test_expiry_boundary_is_inclusive_of_exactly_retention_days(env):
    exact = (NOW - timedelta(days=7)).isoformat(timespec="seconds")
    just_under = (NOW - timedelta(days=7) + timedelta(seconds=1)).isoformat(timespec="seconds")
    a, pa, _ = aged(env, entered=exact, title="A")
    b, pb, _ = aged(env, entered=just_under, title="B")
    cleanup(env)
    assert (env.item(a)["state"], env.item(b)["state"]) == (ST.EXPIRED, ST.LOCAL_DISCOVERY) and pb.is_file()


def test_expiry_uses_entry_time_not_file_mtime(env):
    import os
    item, src, _ = aged(env, entered=RECENT)
    os.utime(src, (1, 1))                                     # 文件修改时间很老，但入库时间很近
    cleanup(env)
    assert src.is_file()


def test_unknown_entry_time_is_never_guessed(env):
    item, src, _ = aged(env)
    with env.db.transaction() as c:
        c.execute("UPDATE recommendation_items SET entered_discovery_at=NULL WHERE id=?", (item,))
    cleanup(env)
    assert src.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY


# ---------------------------------------------------------------- 受保护的绝不清理
def test_starred_is_protected_using_navidrome_state_not_stale_flag(env):
    item, src, sid = aged(env, star=True)
    assert env.item(item)["starred"] == 0                       # DB 里的标记是旧的
    cleanup(env)
    assert src.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY and env.item(item)["starred"] == 1


def test_unstarring_removes_protection(env):
    item, src, sid = aged(env, star=True)
    cleanup(env)
    assert src.is_file()
    env.nd.starred.discard(sid)
    cleanup(env)
    assert not src.exists() and env.item(item)["state"] == ST.EXPIRED


def test_stale_db_star_flag_is_corrected_from_navidrome(env):
    item, src, sid = aged(env, star=False)
    lb_repo.update_fields(env.db, item, starred=1)              # DB 说有红心，但 Navidrome 里已经没有
    cleanup(env)
    assert env.item(item)["state"] == ST.EXPIRED


def test_keep_flag_protects(env):
    item, src, _ = aged(env)
    lb_repo.update_fields(env.db, item, keep=1)
    cleanup(env)
    assert src.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY


def test_promoting_promoted_and_main_items_are_never_touched(env):
    prom, psrc, _ = aged(env, title="P")
    lb_repo.set_state(env.db, prom, ST.PROMOTING)
    done, dsrc, _ = aged(env, title="D")
    lb_repo.set_state(env.db, done, ST.PROMOTING); lb_repo.set_state(env.db, done, ST.PROMOTED)
    main_file = make_flac(env.main / "歌手" / "专辑" / "旧歌.flac", "歌手", "旧歌")
    before = main_file.read_bytes()
    cleanup(env)
    assert psrc.is_file() and dsrc.is_file()
    assert env.item(prom)["state"] == ST.PROMOTING and env.item(done)["state"] == ST.PROMOTED
    assert main_file.read_bytes() == before


def test_shared_file_is_protected_if_any_sibling_is_protected(env):
    a, src, sid = aged(env, batch_mbid="p1")
    b = lb_repo.create_batch(env.db, "daily", {"mbid": "p2", "title": "t", "date": "2026-11-21"},
                             {"tracks": [{"recording_mbid": "mb-郭静-心墙", "artist": "郭静", "title": "心墙", "album": "x",
                                          "duration_ms": 200000, "artists": [{"name": "郭静"}]}]})
    bi = lb_repo.items_of_batch(env.db, b)[0]["id"]
    lb_repo.set_state(env.db, bi, ST.LOCAL_DISCOVERY, local_file_id=sid, local_path=str(src), entered_discovery_at=OLD)
    lb_repo.update_fields(env.db, bi, keep=1)                   # 只有另一个批次的条目被「保留」
    cleanup(env)
    assert src.is_file() and env.item(a)["state"] == ST.LOCAL_DISCOVERY and env.item(bi)["state"] == ST.LOCAL_DISCOVERY


def test_shared_file_expires_together_with_a_single_move(env):
    a, src, sid = aged(env, batch_mbid="p1")
    b = lb_repo.create_batch(env.db, "daily", {"mbid": "p2", "title": "t", "date": "2026-11-21"},
                             {"tracks": [{"recording_mbid": "mb-郭静-心墙", "artist": "郭静", "title": "心墙", "album": "x",
                                          "duration_ms": 200000, "artists": [{"name": "郭静"}]}]})
    bi = lb_repo.items_of_batch(env.db, b)[0]["id"]
    lb_repo.set_state(env.db, bi, ST.LOCAL_DISCOVERY, local_file_id=sid, local_path=str(src), entered_discovery_at=OLD)
    cleanup(env)
    assert env.item(a)["state"] == ST.EXPIRED and env.item(bi)["state"] == ST.EXPIRED
    assert len(trash_files(env)) == 1 and env.db.conn.execute("SELECT COUNT(*) FROM trash_entries").fetchone()[0] == 1


def test_files_unknown_to_the_database_are_left_alone(env):
    stray = make_flac(env.disc / "别人放的" / "x.flac", "路人", "歌")
    staging = make_flac(env.disc / ".staging" / "half.flac", "A", "B")
    aged(env)
    cleanup(env)
    assert stray.is_file() and staging.is_file()


# ---------------------------------------------------------------- 路径安全
def test_refuses_to_delete_a_file_outside_the_discovery_library(env):
    item, src, _ = aged(env)
    main_file = make_flac(env.main / "歌手" / "专辑" / "珍贵.flac", "歌手", "珍贵")
    before = main_file.read_bytes()
    lb_repo.update_fields(env.db, item, local_path=str(main_file))      # 记录的路径被指到了主库
    run = cleanup(env)
    assert main_file.read_bytes() == before and env.item(item)["state"] == ST.LOCAL_DISCOVERY
    assert stats(run)["errors"] == 1 and any("拒绝清理" in x["msg"] for x in env.tm.logs(run["id"]))


@pytest.mark.parametrize("sub", [".trash/20260101", ".staging"])
def test_refuses_files_inside_trash_or_staging(env, sub):
    item, src, _ = aged(env)
    p = make_flac(env.disc / sub / "x.flac", "A", "B")
    lb_repo.update_fields(env.db, item, local_path=str(p))
    cleanup(env)
    assert p.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY


def test_symlink_escape_is_refused(env, tmp_path):
    item, src, _ = aged(env)
    outside = make_flac(tmp_path / "outside" / "x.flac", "A", "B")
    link = env.disc / "link.flac"
    link.symlink_to(outside)
    lb_repo.update_fields(env.db, item, local_path=str(link))
    cleanup(env)
    assert outside.is_file() and env.item(item)["state"] == ST.LOCAL_DISCOVERY


# ---------------------------------------------------------------- 文件找不到 / 路径未知
def test_missing_file_is_marked_expired_without_error(env):
    item, src, _ = aged(env)
    src.unlink()
    run = cleanup(env)
    assert env.item(item)["state"] == ST.EXPIRED and stats(run).get("errors") is None
    assert env.db.conn.execute("SELECT COUNT(*) FROM trash_entries").fetchone()[0] == 0


def test_unknown_path_is_resolved_by_tags(env):
    item, src, _ = aged(env, with_path=False)
    cleanup(env)
    assert not src.exists() and env.item(item)["state"] == ST.EXPIRED and len(trash_files(env)) == 1


def test_same_name_files_in_trash_do_not_overwrite_each_other(env):
    a, pa, _ = aged(env, title="A")
    b, pb, _ = aged(env, title="B")
    pa.rename(env.disc / "same.flac"); lb_repo.update_fields(env.db, a, local_path=str(env.disc / "same.flac"))
    (env.disc / "sub").mkdir()
    pb.rename(env.disc / "sub" / "same.flac"); lb_repo.update_fields(env.db, b, local_path=str(env.disc / "sub" / "same.flac"))
    cleanup(env)
    assert trash_files(env) == [".trash/20261120/same (1).flac", ".trash/20261120/same.flac"]


# ---------------------------------------------------------------- 回收站
def test_trash_days_zero_deletes_immediately_without_trash(env):
    env.settings["discovery"]["trash_days"] = 0
    item, src, _ = aged(env)
    cleanup(env)
    assert not src.exists() and trash_files(env) == [] and env.item(item)["state"] == ST.EXPIRED
    t = env.db.conn.execute("SELECT * FROM trash_entries").fetchone()
    assert t["purged_at"] and t["trash_path"] == ""


def test_purge_deletes_only_expired_trash_entries(env):
    (env.disc / ".trash" / "20261101").mkdir(parents=True)
    old = make_flac(env.disc / ".trash" / "20261101" / "old.flac", "A", "O")
    new = make_flac(env.disc / ".trash" / "20261118" / "new.flac", "A", "N")
    lb_repo.record_trash(env.db, None, "x", str(old), now=NOW - timedelta(days=10))
    lb_repo.record_trash(env.db, None, "y", str(new), now=NOW - timedelta(days=3))
    with env.tm.run("cleanup", "manual") as h:
        n = lb_cleanup.purge_trash(lb_cleanup.CleanupDeps(env.db, env.settings, env.nd, h, now=lambda: NOW), env.disc)
    assert n == 1 and not old.exists() and new.is_file()
    assert not (env.disc / ".trash" / "20261101").exists()               # 空的日期目录被清掉
    assert len(lb_repo.trash_pending(env.db)) == 1


def test_purge_refuses_entries_pointing_outside_trash(env):
    victim = make_flac(env.main / "歌手" / "专辑" / "珍贵.flac", "歌手", "珍贵")
    before = victim.read_bytes()
    lb_repo.record_trash(env.db, None, "x", str(victim), now=NOW - timedelta(days=100))     # 被篡改的记录
    with env.tm.run("cleanup", "manual") as h:
        n = lb_cleanup.purge_trash(lb_cleanup.CleanupDeps(env.db, env.settings, env.nd, h, now=lambda: NOW), env.disc)
    assert n == 0 and victim.read_bytes() == before and len(lb_repo.trash_pending(env.db)) == 1


def test_purge_with_file_already_gone_just_marks_purged(env):
    lb_repo.record_trash(env.db, None, "x", str(env.disc / ".trash" / "gone.flac"), now=NOW - timedelta(days=30))
    with env.tm.run("cleanup", "manual") as h:
        lb_cleanup.purge_trash(lb_cleanup.CleanupDeps(env.db, env.settings, env.nd, h, now=lambda: NOW), env.disc)
    assert lb_repo.trash_pending(env.db) == []


def test_full_cycle_trash_then_purge_after_trash_days(env):
    item, src, _ = aged(env)
    cleanup(env)
    assert len(trash_files(env)) == 1
    cleanup(env, now=NOW + timedelta(days=6))
    assert len(trash_files(env)) == 1                                     # 还没到 7 天
    cleanup(env, now=NOW + timedelta(days=8))
    assert trash_files(env) == [] and lb_repo.trash_pending(env.db) == []


# ---------------------------------------------------------------- 歌单清理
def set_fetched(env, batch_id, ts):
    with env.db.transaction() as c:
        c.execute("UPDATE recommendation_batches SET fetched_at=? WHERE id=?", (ts, batch_id))


def test_old_playlists_are_deleted_recent_kept_only_ours(env):
    old_b = lb_repo.create_batch(env.db, "weekly_jams", {"mbid": "o", "title": "t", "date": "x"}, {"tracks": []})
    new_b = lb_repo.create_batch(env.db, "weekly_jams", {"mbid": "n", "title": "t", "date": "x"}, {"tracks": []})
    set_fetched(env, old_b, "2026-10-01T00:00:00"); set_fetched(env, new_b, "2026-11-15T00:00:00")
    lb_repo.record_playlist(env.db, old_b, "LB old", "pl-old", 5)
    lb_repo.record_playlist(env.db, new_b, "LB new", "pl-new", 5)
    run = cleanup(env)
    assert env.nd.deleted == ["pl-old"] and stats(run)["playlists_deleted"] == 1
    assert [r["nd_playlist_id"] for r in env.db.conn.execute("SELECT nd_playlist_id FROM lb_playlists WHERE deleted_at IS NULL")] == ["pl-new"]


def test_playlist_delete_failure_is_tolerated_and_not_retried_forever(env):
    b = lb_repo.create_batch(env.db, "weekly_jams", {"mbid": "o", "title": "t", "date": "x"}, {"tracks": []})
    set_fetched(env, b, "2026-10-01T00:00:00")
    lb_repo.record_playlist(env.db, b, "LB old", "pl-gone", 5)
    def boom(pid):
        raise NavidromeError("not found")
    env.nd.delete_playlist = boom
    run = cleanup(env)
    assert run["status"] == "done" and lb_repo.live_playlists_older_than(env.db, "2999") == []


# ---------------------------------------------------------------- 预览
def test_preview_counts_without_side_effects(env):
    due, p1, _ = aged(env, title="Due")
    aged(env, entered=RECENT, title="Fresh")
    aged(env, title="Starred", star=True)
    k, _, _ = aged(env, title="Keep"); lb_repo.update_fields(env.db, k, keep=1)
    pv = lb_cleanup.preview(env.db, env.settings, nd=env.nd, now=NOW)
    assert pv["due_files"] == 1 and pv["due_items"] == 1 and pv["retention_days"] == 7
    assert pv["protected"]["protected_starred"] == 1 and pv["protected"]["protected_keep"] == 1
    assert pv["sample"][0]["title"] == "Due"
    assert p1.is_file() and env.item(due)["state"] == ST.LOCAL_DISCOVERY and env.nd.scans == 0


def test_preview_works_without_navidrome(env):
    aged(env)
    assert lb_cleanup.preview(env.db, env.settings, now=NOW)["due_files"] == 1


# ---------------------------------------------------------------- 异常与恢复
def test_crash_between_move_and_state_update_is_repaired_next_run(env, monkeypatch):
    item, src, _ = aged(env)
    real = lb_repo.set_state
    calls = {"n": 0}
    def flaky(db, i, to, **kw):
        if to == ST.EXPIRED and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("db hiccup")
        return real(db, i, to, **kw)
    monkeypatch.setattr(lb_repo, "set_state", flaky)
    run = cleanup(env)
    assert stats(run)["errors"] == 1 and env.item(item)["state"] == ST.LOCAL_DISCOVERY
    assert not src.exists() and len(trash_files(env)) == 1                # 文件已在回收站
    cleanup(env)
    assert env.item(item)["state"] == ST.EXPIRED                          # 下次发现文件不在了，补上状态
    assert env.db.conn.execute("SELECT COUNT(*) FROM trash_entries").fetchone()[0] == 1


def test_one_failing_group_does_not_stop_the_others(env, monkeypatch):
    a, pa, _ = aged(env, title="A")
    b, pb, _ = aged(env, title="B")
    real = lb_cleanup.safe_move
    def flaky(src, dst):
        if Path(src) == pa:
            raise OSError("io error")
        return real(src, dst)
    monkeypatch.setattr(lb_cleanup, "safe_move", flaky)
    run = cleanup(env)
    assert env.item(a)["state"] == ST.LOCAL_DISCOVERY and pa.is_file() and env.item(b)["state"] == ST.EXPIRED
    assert stats(run)["errors"] == 1


def test_requires_navidrome_libraries(env):
    env.settings["navidrome"]["discovery_folder_id"] = None
    with pytest.raises(ValueError, match="主库和试听库"):
        cleanup(env)


def test_expired_song_can_be_recommended_and_downloaded_again(env):
    """闭环：清理后文件不在试听库了，再次被推荐会重新下载（流水线对 EXPIRED 的处理见 test_lb_pipeline）。"""
    item, src, sid = aged(env)
    cleanup(env)
    env.nd.rescan()
    assert [s for s in env.nd.songs.values() if s["_lib"] == "discovery"] == []


@pytest.mark.parametrize("sibling_state", [ST.PROMOTING, ST.PROMOTED, ST.LOCAL_MAIN])
def test_file_shared_with_a_promoted_or_main_item_is_never_cleaned(env, sibling_state):
    """防御：正常流程里转正会把共用同一文件的兄弟条目一并处理；万一出现不一致，清理也绝不能动这个文件。"""
    a, src, sid = aged(env, batch_mbid="p1")
    b = lb_repo.create_batch(env.db, "daily", {"mbid": "p2", "title": "t", "date": "2026-11-21"},
                             {"tracks": [{"recording_mbid": "mb-郭静-心墙", "artist": "郭静", "title": "心墙", "album": "x",
                                          "duration_ms": 200000, "artists": [{"name": "郭静"}]}]})
    bi = lb_repo.items_of_batch(env.db, b)[0]["id"]
    lb_repo.set_state(env.db, bi, ST.LOCAL_DISCOVERY, local_file_id=sid, local_path=str(src), entered_discovery_at=OLD)
    if sibling_state == ST.LOCAL_MAIN:
        lb_repo.set_state(env.db, bi, ST.LOCAL_MAIN)
    else:
        lb_repo.set_state(env.db, bi, ST.PROMOTING)
        if sibling_state == ST.PROMOTED:
            lb_repo.set_state(env.db, bi, ST.PROMOTED)
    cleanup(env)
    assert src.is_file() and env.item(a)["state"] == ST.LOCAL_DISCOVERY and env.item(bi)["state"] == sibling_state
