import json
import sqlite3
from datetime import datetime, timedelta

import pytest

import lb_repo
import lb_states as ST
from db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    d.migrate()
    return d


def track(i, mbid=None, artist="A", title=None):
    return {"recording_mbid": mbid if mbid is not None else f"mbid-{i}", "artist": artist, "title": title or f"T{i}",
            "album": "Al", "duration_ms": 200000, "artists": [{"name": artist, "mbid": "x", "join": ""}]}


def make_batch(db, mbid="pl-1", rtype="weekly_jams", tracks=None, date="2026-11-20T00:00:00+00:00"):
    tracks = tracks if tracks is not None else [track(1), track(2), track(3)]
    return lb_repo.create_batch(db, rtype, {"mbid": mbid, "title": "T", "date": date}, {"tracks": tracks})


def force_state(db, item_id, state):
    with db.transaction() as c:
        c.execute("UPDATE recommendation_items SET state=? WHERE id=?", (state, item_id))


# ---------------------------------------------------------------- 转换表本身
def test_transition_table_is_closed_and_consistent():
    assert set(ST.TRANSITIONS) == set(ST.ALL_STATES)
    for src, dsts in ST.TRANSITIONS.items():
        assert dsts <= set(ST.ALL_STATES) and src not in dsts


def test_terminal_states_have_no_exit():
    for s in (ST.LOCAL_MAIN, ST.PROMOTED, ST.EXPIRED):
        assert ST.TRANSITIONS[s] == frozenset()


def test_key_business_paths_are_legal():
    path = [ST.PENDING, ST.DOWNLOADING, ST.LOCAL_DISCOVERY, ST.PROMOTING, ST.PROMOTED]
    assert all(ST.can_transition(a, b) for a, b in zip(path, path[1:]))
    assert ST.can_transition(ST.LOCAL_DISCOVERY, ST.EXPIRED)
    assert ST.can_transition(ST.DOWNLOADING, ST.PENDING)           # 限流/中断后复位
    assert not ST.can_transition(ST.PROMOTED, ST.EXPIRED)          # 已转正的永不过期
    assert not ST.can_transition(ST.PROMOTING, ST.EXPIRED)         # 转正进行中不能被清理
    assert not ST.can_transition(ST.EXPIRED, ST.LOCAL_DISCOVERY)
    assert not ST.can_transition(ST.PENDING, ST.PROMOTED)


# ---------------------------------------------------------------- set_state：穷举所有状态对
@pytest.mark.parametrize("src", ST.ALL_STATES)
def test_set_state_enforces_table_exhaustively(db, src):
    bid = make_batch(db, tracks=[track(1)])
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    for dst in ST.ALL_STATES:
        force_state(db, item, src)
        events_before = db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0]
        if ST.can_transition(src, dst):
            lb_repo.set_state(db, item, dst, note="n")
            assert lb_repo.get_item(db, item)["state"] == dst
            assert db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == events_before + 1
        else:
            with pytest.raises(ST.InvalidTransition):
                lb_repo.set_state(db, item, dst, reason="should not be saved")
            row = lb_repo.get_item(db, item)
            assert row["state"] == src and row["reason"] == ""        # 非法转换：状态和字段都不变
            assert db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == events_before


def test_set_state_records_event_time_and_fields_atomically(db):
    bid = make_batch(db, tracks=[track(1)])
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    t = datetime(2026, 11, 20, 8, 30, 0)
    lb_repo.set_state(db, item, ST.LOCAL_DISCOVERY, note="下载成功", now=t, local_path="/d/a.flac",
                      entered_discovery_at="2026-11-20T08:30:00")
    row = lb_repo.get_item(db, item)
    assert (row["state"], row["local_path"], row["state_changed_at"]) == (ST.LOCAL_DISCOVERY, "/d/a.flac", "2026-11-20T08:30:00")
    ev = db.conn.execute("SELECT from_state, to_state, at, note FROM item_events WHERE item_id=?", (item,)).fetchone()
    assert tuple(ev) == (ST.PENDING, ST.LOCAL_DISCOVERY, "2026-11-20T08:30:00", "下载成功")


def test_set_state_rejects_unknown_fields_and_missing_items(db):
    bid = make_batch(db, tracks=[track(1)])
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    with pytest.raises(ValueError, match="不允许更新"):
        lb_repo.set_state(db, item, ST.SKIPPED, state="HACK")
    with pytest.raises(KeyError):
        lb_repo.set_state(db, 9999, ST.SKIPPED)
    assert lb_repo.get_item(db, item)["state"] == ST.PENDING


def test_update_fields_does_not_touch_state_or_events(db):
    bid = make_batch(db, tracks=[track(1)])
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    lb_repo.update_fields(db, item, local_file_id="nd1", starred=1)
    row = lb_repo.get_item(db, item)
    assert (row["state"], row["local_file_id"], row["starred"]) == (ST.PENDING, "nd1", 1)
    assert db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] == 0
    with pytest.raises(ValueError):
        lb_repo.update_fields(db, item, state="X")


# ---------------------------------------------------------------- 批次与歌曲实体
def test_create_batch_creates_items_in_order_with_pending_state(db):
    bid = make_batch(db)
    items = lb_repo.items_of_batch(db, bid)
    assert [i["position"] for i in items] == [1, 2, 3] and {i["state"] for i in items} == {ST.PENDING}
    assert items[0]["artist"] == "A" and json.loads(items[0]["artists_json"]) == ["A"] and items[0]["batch_type"] == "weekly_jams"


def test_same_recording_across_batches_shares_one_track_but_keeps_history(db):
    b1 = make_batch(db, "pl-1", tracks=[track(1, mbid="SAME")])
    b2 = make_batch(db, "pl-2", rtype="daily", tracks=[track(1, mbid="same")])    # MBID 大小写不敏感
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    i1, i2 = lb_repo.items_of_batch(db, b1)[0], lb_repo.items_of_batch(db, b2)[0]
    assert i1["track_id"] == i2["track_id"] and i1["id"] != i2["id"]               # 推荐历史：各一条 item


def test_tracks_without_mbid_merge_by_artist_and_title(db):
    make_batch(db, "pl-1", tracks=[track(1, mbid="", artist="X", title="S")])
    make_batch(db, "pl-2", tracks=[track(2, mbid="", artist="X", title="S"), track(3, mbid="", artist="X", title="Other")])
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2


def test_duplicate_playlist_mbid_rolls_back_everything(db):
    make_batch(db, "pl-1")
    tracks_before = db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        make_batch(db, "pl-1", tracks=[track(10), track(11)])
    assert db.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == tracks_before
    assert db.conn.execute("SELECT COUNT(*) FROM recommendation_batches").fetchone()[0] == 1
    assert lb_repo.batch_known(db, "pl-1") and not lb_repo.batch_known(db, "nope")


def test_same_track_twice_in_one_playlist_keeps_both_positions(db):
    bid = make_batch(db, tracks=[track(1, mbid="dup"), track(2), track(1, mbid="dup")])
    assert [i["position"] for i in lb_repo.items_of_batch(db, bid)] == [1, 2, 3]


# ---------------------------------------------------------------- 恢复与查询
def test_recover_interrupted_resets_only_downloading(db):
    bid = make_batch(db)
    a, b, c = [i["id"] for i in lb_repo.items_of_batch(db, bid)]
    lb_repo.set_state(db, a, ST.DOWNLOADING)
    lb_repo.set_state(db, b, ST.DOWNLOADING)
    lb_repo.set_state(db, b, ST.LOCAL_DISCOVERY)
    lb_repo.set_state(db, c, ST.DOWNLOADING)
    assert lb_repo.recover_interrupted(db) == 2
    states = {i["id"]: i["state"] for i in lb_repo.items_of_batch(db, bid)}
    assert states == {a: ST.PENDING, b: ST.LOCAL_DISCOVERY, c: ST.PENDING}
    assert lb_repo.recover_interrupted(db) == 0


def test_download_records_and_attempt_count(db):
    bid = make_batch(db, tracks=[track(1)])
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    assert lb_repo.download_attempts(db, item) == 0
    lb_repo.record_download(db, item, 1, ok=False, error_kind="download", error="x")
    lb_repo.record_download(db, item, 2, ok=True, platform_id=5, path="/p", quality="lossless", file_type="flac")
    assert lb_repo.download_attempts(db, item) == 2


def test_last_item_for_track_finds_latest_other_item_in_states(db):
    b1 = make_batch(db, "p1", tracks=[track(1, mbid="m")])
    b2 = make_batch(db, "p2", tracks=[track(1, mbid="m")])
    i1, i2 = lb_repo.items_of_batch(db, b1)[0], lb_repo.items_of_batch(db, b2)[0]
    assert lb_repo.last_item_for_track(db, i2["track_id"], i2["id"], (ST.NOT_FOUND,)) is None
    lb_repo.set_state(db, i1["id"], ST.NOT_FOUND)
    got = lb_repo.last_item_for_track(db, i2["track_id"], i2["id"], (ST.NOT_FOUND,))
    assert got["id"] == i1["id"]
    assert lb_repo.last_item_for_track(db, i1["track_id"], i1["id"], (ST.NOT_FOUND,)) is None   # 不含自己


def test_active_batches_and_finish(db):
    b1, b2 = make_batch(db, "p1"), make_batch(db, "p2")
    lb_repo.finish_batch(db, b1)
    assert [b["id"] for b in lb_repo.active_batches(db)] == [b2]


def test_playlist_records(db):
    bid = make_batch(db)
    lb_repo.record_playlist(db, bid, "LB x", "nd-1", 3)
    rows = lb_repo.live_playlists_of_batch(db, bid)
    assert [r["nd_playlist_id"] for r in rows] == ["nd-1"]
    lb_repo.mark_playlist_deleted(db, rows[0]["id"])
    assert lb_repo.live_playlists_of_batch(db, bid) == []


def test_v9_fixes_batch_date_and_registers_playlist_rename():
    import sqlite3
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for m in dbmod.MIGRATIONS[:8]:
        m(conn)
    conn.execute("INSERT INTO recommendation_batches(id,type,lb_playlist_mbid,lb_title,lb_date,fetched_at,status,total) "
                 "VALUES(1,'daily','m1','Daily Jams for u, 2026-11-23 Mon','2026-11-22T16:01:10+00:00','t','active',1)")
    conn.execute("INSERT INTO recommendation_batches(id,type,lb_playlist_mbid,lb_title,lb_date,fetched_at,status,total) "
                 "VALUES(2,'weekly_jams','m2','Weekly Jams for u, week of 2026-09-14 Mon','2026-09-14T00:12:01+00:00','t','active',1)")
    for bid, name in ((1, "LB Daily Jams 2026-11-22"), (2, "LB Weekly Jams 2026-09-14")):
        conn.execute("INSERT INTO lb_playlists(batch_id,name,nd_playlist_id,song_count,created_at) VALUES(?,?,?,1,'t')",
                     (bid, name, f"p{bid}"))
    dbmod._v9(conn)
    dates = {r["id"]: r["lb_date"] for r in conn.execute("SELECT id, lb_date FROM recommendation_batches")}
    assert dates == {1: "2026-11-23", 2: "2026-09-14"}
    ren = {r["batch_id"]: r["rename_to"] for r in conn.execute("SELECT batch_id, rename_to FROM lb_playlists")}
    assert ren == {1: "LB Daily Jams 2026-11-23", 2: None}     # 日期没变的不动


def test_apply_renames_calls_navidrome_and_clears_flag(tmp_path):
    import db as dbmod
    import lb_playlist
    import lb_repo
    d = dbmod.Database(tmp_path / "x.sqlite"); d.migrate()
    with d.transaction() as c:
        c.execute("INSERT INTO recommendation_batches(id,type,lb_playlist_mbid,lb_title,lb_date,fetched_at,status,total) "
                  "VALUES(1,'daily','m1','t','2026-11-23','t','active',1)")
        c.execute("INSERT INTO lb_playlists(batch_id,name,nd_playlist_id,song_count,created_at,rename_to) "
                  "VALUES(1,'LB Daily Jams 2026-11-22','p1',1,'t','LB Daily Jams 2026-11-23')")
    calls = []

    class ND:
        def rename_playlist(self, pid, name):
            calls.append((pid, name))
    assert lb_playlist.apply_renames(d, ND(), lambda *a, **k: None) == 1
    assert calls == [("p1", "LB Daily Jams 2026-11-23")]
    assert lb_repo.pending_renames(d) == []
    assert lb_repo.live_playlists_of_batch(d, 1)[0]["name"] == "LB Daily Jams 2026-11-23"
