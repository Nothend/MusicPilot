"""试听库与主库疑似重复：检测、保留主库、用试听库替换主库（主库那份进回收站，失败原样放回）。"""
import json
from datetime import datetime

import pytest

import lb_dedup
import lb_repo
import lb_states as ST
from audio import make_flac
from local_index import load_index, sync_index
from test_lb_promote import Env

NOW = datetime(2026, 11, 21, 9, 0, 0)


def setup_pair(tmp_path, star_main=False):
    env = Env(tmp_path)
    item, disc_file, sid = env.add_item("F.I.R.", "月牙湾", album="爱·歌姬", star=False)
    main_file = make_flac(env.main / "F.I.R.飞儿乐团" / "爱·歌姬" / "飞儿乐团 - 月牙湾.flac", "F.I.R.飞儿乐团", "月牙湾",
                          seconds=11, extra={"album": "爱·歌姬"})
    env.nd.rescan()
    main_id = next(s["id"] for s in env.nd.songs.values() if s["_lib"] == "main")
    if star_main:
        env.nd.starred.add(main_id)
    sync_index(env.db, env.nd, 1, 2)
    return env, item, disc_file, main_file, main_id


def run(env, item, choice, main_id=""):
    with env.tm.run("dedup", "manual") as h:
        lb_dedup.run_dedup(lb_dedup.DedupDeps(env.db, env.settings, env.nd, h, item, choice, main_id, now=lambda: NOW))


def test_detect_finds_suspect_and_is_idempotent(tmp_path):
    env, item, _, _, main_id = setup_pair(tmp_path)
    assert lb_dedup.detect(env.db, load_index(env.db), NOW) == 1
    assert lb_dedup.detect(env.db, load_index(env.db), NOW) == 0
    sus = lb_repo.dup_suspects(env.db, item)
    assert [r["main_nd_song_id"] for r in sus] == [main_id]


def test_detect_ignores_unrelated_and_respects_dismiss(tmp_path):
    env, item, *_ = setup_pair(tmp_path)
    env.add_item("别人", "别的歌", star=False)
    sync_index(env.db, env.nd, 1, 2)
    assert lb_dedup.detect(env.db, load_index(env.db), NOW) == 1
    lb_repo.dismiss_dup_suspects(env.db, [item])
    assert lb_dedup.detect(env.db, load_index(env.db), NOW) == 0
    assert lb_repo.dup_suspects(env.db, item) == []


def test_keep_main_trashes_discovery_copy(tmp_path):
    env, item, disc_file, main_file, main_id = setup_pair(tmp_path, star_main=True)
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    run(env, item, "keep_main", main_id)
    it = env.item(item)
    assert it["state"] == ST.LOCAL_MAIN and it["local_file_id"] == main_id and it["starred"] == 1
    assert not disc_file.exists() and main_file.exists()
    trashed = list((env.disc / ".trash").rglob("*.flac"))
    assert len(trashed) == 1
    assert lb_repo.dup_suspects(env.db, item) == []


def test_keep_new_moves_main_copy_to_trash_and_promotes(tmp_path):
    env, item, disc_file, main_file, main_id = setup_pair(tmp_path)
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    run(env, item, "keep_new", main_id)
    it = env.item(item)
    assert it["state"] == ST.PROMOTED
    assert not main_file.exists() and not disc_file.exists()
    assert [p.name for p in (env.disc / ".trash").rglob("飞儿乐团 - 月牙湾.flac")] == ["飞儿乐团 - 月牙湾.flac"]   # 旧主库文件在回收站
    new_files = [p for p in env.main.rglob("*.flac")]
    assert len(new_files) == 1 and "月牙湾" in new_files[0].name
    assert env.nd.stars == []                 # 主库那份没红心 → 不凭空加红心
    pending = [r["trash_path"] for r in lb_repo.trash_pending(env.db)]
    assert len(pending) == 1 and pending[0].startswith(str(env.disc / ".trash"))


def test_keep_new_restores_star_when_main_was_starred(tmp_path):
    env, item, _, _, main_id = setup_pair(tmp_path, star_main=True)
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    env.nd.lose_stars_on_move = True
    run(env, item, "keep_new", main_id)
    assert env.item(item)["state"] == ST.PROMOTED and len(env.nd.stars) == 1


def test_keep_new_puts_main_back_when_promote_fails(tmp_path):
    env, item, disc_file, main_file, main_id = setup_pair(tmp_path)
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    # 目标路径已被别的文件占着 → 转正冲突
    import lb_localfs
    dst = env.main / lb_localfs.main_relpath(lb_localfs.read_file_tags(disc_file), ".flac", ", ")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(b"other")
    with pytest.raises(ValueError, match="原样放回"):
        run(env, item, "keep_new", main_id)
    assert main_file.exists() and disc_file.exists() and dst.read_bytes() == b"other"
    assert env.item(item)["state"] == ST.LOCAL_DISCOVERY
    assert lb_repo.trash_pending(env.db) == []


def test_locate_main_file_refuses_ambiguous(tmp_path):
    env, item, _, main_file, main_id = setup_pair(tmp_path)
    twin = env.main / "别处" / "copy.flac"
    twin.parent.mkdir()
    twin.write_bytes(main_file.read_bytes())
    with pytest.raises(ValueError, match="无法确定"):
        lb_dedup.locate_main_file(env.db, env.main, main_id)


def test_locate_main_file_skips_dot_dirs(tmp_path):
    env, item, _, main_file, main_id = setup_pair(tmp_path)
    hidden = env.main / ".trash" / "copy.flac"
    hidden.parent.mkdir()
    hidden.write_bytes(main_file.read_bytes())
    assert lb_dedup.locate_main_file(env.db, env.main, main_id) == main_file


def test_validate_errors(tmp_path):
    env, item, _, _, main_id = setup_pair(tmp_path)
    with pytest.raises(ValueError, match="没有待处理"):
        lb_dedup.validate(env.db, item, "keep_new")
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    with pytest.raises(ValueError, match="未知"):
        lb_dedup.validate(env.db, item, "nuke")
    with pytest.raises(ValueError, match="不在疑似重复"):
        lb_dedup.validate(env.db, item, "keep_new", "bogus")
    assert lb_dedup.validate(env.db, item, "keep_new") == main_id


def test_items_view_and_dismiss_via_queries(tmp_path):
    import lb_queries
    env, item, *_ = setup_pair(tmp_path)
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    batch = env.item(item)["batch_id"]
    row = next(r for r in lb_queries.items(env.db, batch)["items"] if r["id"] == item)
    assert len(row["dups"]) == 1 and row["dups"][0]["main"]["suffix"] == "flac" and row["dups"][0]["new"]["suffix"] == "flac"
    lb_queries.item_action(env.db, item, "dup_dismiss")
    row = next(r for r in lb_queries.items(env.db, batch)["items"] if r["id"] == item)
    assert row["dups"] == []
    with pytest.raises(ValueError):
        lb_queries.item_action(env.db, item, "dup_dismiss")


def test_sync_summary_mentions_dup_suspects():
    from lb_service import LbService
    class H:                       # 最小 RunHandle 替身
        status, summary = "done", ""
        stats = {"dup_suspects": 2}
    assert "疑似与主库重复" in LbService.summarize("推荐同步", H(), None)


def test_service_starts_dedup_task_and_validates_first(tmp_path):
    import time
    import lb_settings
    from lb_service import LbService
    from settings_store import SettingsStore
    env, item, disc_file, main_file, main_id = setup_pair(tmp_path)
    s = dict(env.settings)
    kv = SettingsStore(env.db)
    kv.set(lb_settings.SETTINGS_KEY, s)
    svc = LbService(db=env.db, kv=kv, tm=env.tm, nd_factory=lambda x: env.nd, now=lambda: NOW)
    with pytest.raises(ValueError, match="没有待处理"):                  # 还没检测出疑似重复 → 同步拒绝
        svc.start("dedup", params={"item_id": item, "choice": "keep_main"})
    lb_dedup.detect(env.db, load_index(env.db), NOW)
    svc.start("dedup", params={"item_id": item, "choice": "keep_main"})
    for _ in range(200):
        if env.tm.current() is None:
            break
        time.sleep(0.05)
    assert env.item(item)["state"] == ST.LOCAL_MAIN and not disc_file.exists()
