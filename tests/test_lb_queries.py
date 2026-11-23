from fakes import lb_defaults
import json
from datetime import datetime

import pytest

import lb_queries as Q
import lb_repo
import lb_settings
import lb_states as ST
from db import Database

NOW = datetime(2026, 11, 20, 9, 0, 0)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite"); d.migrate()
    return d


def mk(db, mbid="p1", rtype="weekly_jams", n=3, date="2026-11-20T00:00:00+00:00", now=NOW):
    tracks = [{"recording_mbid": f"{mbid}-m{i}", "artist": f"歌手{i}", "title": f"歌{i}", "album": f"专辑{i}", "duration_ms": 200000,
               "artists": [{"name": f"歌手{i}"}]} for i in range(1, n + 1)]
    return lb_repo.create_batch(db, rtype, {"mbid": mbid, "title": "t", "date": date}, {"tracks": tracks}, now)


def ids(db, bid):
    return [i["id"] for i in lb_repo.items_of_batch(db, bid)]


S = lb_defaults()


# ---------------------------------------------------------------- dashboard
def test_dashboard_on_empty_database(db):
    d = Q.dashboard(db, S, None, NOW)
    assert d["recommended_today"] == 0 and d["latest_batches"] == {} and d["last_sync"] is None
    assert d["pending_cleanup"]["files"] == 0 and d["enabled"] is False and d["current_task"] is None


def test_dashboard_aggregates(db):
    b1 = mk(db, "p1", "weekly_jams", 3)
    b2 = mk(db, "p2", "daily", 2, now=datetime(2026, 11, 19, 9, 0, 0))              # 昨天入库的
    i1, i2, i3 = ids(db, b1)
    lb_repo.set_state(db, i1, ST.LOCAL_MAIN)
    lb_repo.set_state(db, i2, ST.DOWNLOADING); lb_repo.set_state(db, i2, ST.LOCAL_DISCOVERY, entered_discovery_at="2026-11-01T00:00:00")
    lb_repo.set_state(db, i3, ST.NOT_FOUND)
    lb_repo.record_download(db, i2, 1, ok=True, now=NOW)
    lb_repo.record_download(db, i3, 1, ok=False, error_kind="not_found", now=NOW)
    lb_repo.record_download(db, ids(db, b2)[0], 1, ok=True, now=datetime(2026, 11, 19, 9, 0, 0))       # 昨天的下载不算今天
    with db.transaction() as c:
        c.execute("INSERT INTO task_runs(type, trigger, started_at, status, stats_json) VALUES('sync','manual','2026-11-20T08:00:00','done','{\"downloaded\":1}')")
        c.execute("INSERT INTO love_sync(mbid, status, synced_at) VALUES('a','submitted','x'),('b','already','x'),('c','failed','x')")
    lb_repo.record_playlist(db, b1, "LB x", "pl1", 3)
    d = Q.dashboard(db, S, {"type": "sync"}, NOW)
    assert d["recommended_today"] == 3 and d["downloaded_today"] == 1
    assert d["latest_batches"]["weekly_jams"]["counts"] == {ST.LOCAL_MAIN: 1, ST.LOCAL_DISCOVERY: 1, ST.NOT_FOUND: 1}
    assert d["latest_batches"]["daily"]["total"] == 2 and "weekly_exploration" not in d["latest_batches"]
    assert d["last_sync"]["stats"] == {"downloaded": 1} and d["last_run"]["type"] == "sync"
    assert d["totals"]["in_discovery"] == 1 and d["totals"]["not_found"] == 1
    assert d["pending_cleanup"] == {"files": 1, "items": 1, "protected": d["pending_cleanup"]["protected"], "retention_days": 7}   # 11-01 入库 → 已过期
    assert d["playlists"] == 1 and d["love"] == {"submitted": 1, "already": 1, "failed": 1} and d["current_task"] == {"type": "sync"}


# ---------------------------------------------------------------- 批次与条目列表
def test_batches_order_and_filter(db):
    mk(db, "p1", "weekly_jams"); mk(db, "p2", "daily"); mk(db, "p3", "weekly_jams")
    assert [b["id"] for b in Q.batches(db)] == [3, 2, 1]
    assert [b["id"] for b in Q.batches(db, "weekly_jams")] == [3, 1]
    assert Q.batches(db, limit=1)[0]["label"] == "每周熟悉" and Q.batches(db)[1]["label"] == "每日熟悉"


def test_items_listing_fields_filter_and_counts(db):
    b = mk(db, n=3)
    a, c, d = ids(db, b)
    lb_repo.set_state(db, a, ST.LOCAL_DISCOVERY, local_path="/disc/x.flac", entered_discovery_at="2026-11-18T00:00:00")
    lb_repo.update_fields(db, a, starred=1, keep=1)
    lb_repo.set_state(db, c, ST.NOT_FOUND, reason_kind="only_mp3", reason="只有 MP3")
    res = Q.items(db, b)
    assert res["counts"] == {ST.LOCAL_DISCOVERY: 1, ST.NOT_FOUND: 1, ST.PENDING: 1}
    first = res["items"][0]
    assert (first["artist"], first["title"], first["album"], first["recommended_at"], first["path"]) == ("歌手1", "歌1", "专辑1", "2026-11-20", "/disc/x.flac")
    assert first["starred"] is True and first["keep"] is True and first["promoted"] is False
    assert [i["title"] for i in Q.items(db, b, ST.NOT_FOUND)["items"]] == ["歌2"]
    assert Q.items(db, b, ST.NOT_FOUND)["counts"] == res["counts"]                     # 计数不随筛选变化
    assert Q.items(db, b)["batch"]["label"] == "每周熟悉"
    with pytest.raises(ValueError):
        Q.items(db, 999)


def test_multiple_match_items_carry_their_candidates(db):
    b = mk(db, n=1)
    item = ids(db, b)[0]
    with db.transaction() as c:
        c.execute("INSERT INTO local_files(nd_song_id, library, artist, title, album, duration, indexed_at) VALUES('m1','main','歌手1','歌1','专辑A',200,'t'),('d1','discovery','歌手1','歌1','专辑B',400,'t')")
    class F:
        def __init__(self, i, l): self.nd_song_id, self.library = i, l
    lb_repo.save_candidates(db, item, [F("m1", "main"), F("d1", "discovery")])
    lb_repo.set_state(db, item, ST.MULTIPLE_MATCH)
    cands = Q.items(db, b)["items"][0]["candidates"]
    assert [(c["nd_song_id"], c["library"], c["album"], c["duration"]) for c in cands] == [("d1", "discovery", "专辑B", 400), ("m1", "main", "专辑A", 200)]


def test_runs_listing(db):
    with db.transaction() as c:
        c.execute("INSERT INTO task_runs(type, trigger, started_at, status, stats_json) VALUES('sync','manual','t1','done','{\"a\":1}'),('cleanup','schedule','t2','failed','{}')")
    r = Q.runs(db)
    assert [x["type"] for x in r] == ["cleanup", "sync"] and r[1]["stats"] == {"a": 1} and "stats_json" not in r[0]


# ---------------------------------------------------------------- 条目操作
def test_keep_and_unkeep_only_apply_to_discovery_items(db):
    b = mk(db, n=2); a, c = ids(db, b)
    lb_repo.set_state(db, a, ST.LOCAL_DISCOVERY)
    assert Q.item_action(db, a, "keep")["keep"] is True
    assert Q.item_action(db, a, "unkeep")["keep"] is False
    with pytest.raises(ValueError, match="试听库"):
        Q.item_action(db, c, "keep")                                              # 还是 PENDING


@pytest.mark.parametrize("state", [ST.NOT_FOUND, ST.DOWNLOAD_FAILED, ST.SKIPPED])
def test_retry_resets_item_and_reactivates_batch(db, state):
    b = mk(db, n=1); item = ids(db, b)[0]
    if state == ST.DOWNLOAD_FAILED:
        lb_repo.set_state(db, item, ST.DOWNLOADING)
    lb_repo.set_state(db, item, state, retry_count=3, next_retry_at="2999-01-01T00:00:00", reason_kind="x", reason="y")
    lb_repo.finish_batch(db, b)
    assert Q.item_action(db, item, "retry", now=NOW)["state"] == ST.PENDING
    row = lb_repo.get_item(db, item)
    assert (row["retry_count"], row["next_retry_at"], row["reason_kind"], row["reason"]) == (0, None, "", "")
    assert [x["id"] for x in lb_repo.active_batches(db)] == [b]                    # 下次同步会重新处理它


def test_retry_clears_promotion_conflict_so_auto_promote_tries_again(db):
    b = mk(db, n=1); item = ids(db, b)[0]
    lb_repo.set_state(db, item, ST.LOCAL_DISCOVERY, reason_kind="promote_conflict", reason="主库已有同名文件")
    r = Q.item_action(db, item, "retry")
    assert r["state"] == ST.LOCAL_DISCOVERY and r["reason_kind"] == ""


@pytest.mark.parametrize("state", [ST.PENDING, ST.LOCAL_MAIN, ST.PROMOTED, ST.EXPIRED])
def test_retry_is_refused_when_pointless(db, state):
    b = mk(db, n=1); item = ids(db, b)[0]
    if state == ST.PROMOTED:
        lb_repo.set_state(db, item, ST.LOCAL_DISCOVERY); lb_repo.set_state(db, item, ST.PROMOTING); lb_repo.set_state(db, item, ST.PROMOTED)
    elif state == ST.EXPIRED:
        lb_repo.set_state(db, item, ST.LOCAL_DISCOVERY); lb_repo.set_state(db, item, ST.EXPIRED)
    elif state != ST.PENDING:
        lb_repo.set_state(db, item, state)
    with pytest.raises(ValueError, match="无需重试"):
        Q.item_action(db, item, "retry")


def _multiple(db):
    b = mk(db, n=1); item = ids(db, b)[0]
    with db.transaction() as c:
        c.execute("INSERT INTO local_files(nd_song_id, library, artist, title, indexed_at) VALUES('m1','main','歌手1','歌1','t'),('d1','discovery','歌手1','歌1','t')")
    class F:
        def __init__(self, i, l): self.nd_song_id, self.library = i, l
    lb_repo.save_candidates(db, item, [F("m1", "main"), F("d1", "discovery")])
    lb_repo.set_state(db, item, ST.MULTIPLE_MATCH, reason_kind="multiple", reason="x")
    return item


def test_choose_main_candidate(db):
    item = _multiple(db)
    assert Q.item_action(db, item, "choose", "m1")["state"] == ST.LOCAL_MAIN
    row = lb_repo.get_item(db, item)
    assert row["local_file_id"] == "m1" and row["reason_kind"] == "" and Q.candidates(db, item) == []


def test_choose_discovery_candidate_sets_entry_time_and_inherits_known_path(db):
    other = mk(db, "p0", n=1); oi = ids(db, other)[0]
    lb_repo.set_state(db, oi, ST.LOCAL_DISCOVERY, local_file_id="d1", local_path="/disc/known.flac", entered_discovery_at="2026-11-01T00:00:00")
    item = _multiple(db)
    assert Q.item_action(db, item, "choose", "d1", now=NOW)["state"] == ST.LOCAL_DISCOVERY
    row = lb_repo.get_item(db, item)
    assert (row["local_file_id"], row["local_path"], row["entered_discovery_at"]) == ("d1", "/disc/known.flac", "2026-11-01T00:00:00")


def test_choose_validation(db):
    item = _multiple(db)
    with pytest.raises(ValueError, match="候选"):
        Q.item_action(db, item, "choose", "not-a-candidate")
    assert lb_repo.get_item(db, item)["state"] == ST.MULTIPLE_MATCH
    b = mk(db, "p9", n=1)
    with pytest.raises(ValueError, match="多个候选"):
        Q.item_action(db, ids(db, b)[0], "choose", "m1")


def test_unknown_action_and_item(db):
    b = mk(db, n=1)
    with pytest.raises(ValueError, match="未知"):
        Q.item_action(db, ids(db, b)[0], "explode")
    with pytest.raises(ValueError, match="不存在"):
        Q.item_action(db, 9999, "keep")
