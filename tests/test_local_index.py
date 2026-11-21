import pytest

from db import Database
from local_index import load_index, sync_index
from matcher import Target, match


def song(i, artist="A", title=None, **kw):
    d = {"id": i, "title": title or f"T{i}", "artist": artist, "album": "Al", "album_artist": artist,
         "duration": 200, "size": 100, "suffix": "flac", "mbid": "", "starred": False, "path_api": "x/y.flac"}
    d.update(kw)
    return d


class FakeND:
    def __init__(self, libs, fail_on=None):
        self.libs, self.fail_on = libs, fail_on

    def iter_songs(self, folder_id=None):
        if folder_id == self.fail_on:
            raise RuntimeError("navidrome down")
        yield from self.libs[folder_id]


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    d.migrate()
    return d


def count(db, lib=None):
    q, a = ("SELECT COUNT(*) FROM local_files WHERE library=?", (lib,)) if lib else ("SELECT COUNT(*) FROM local_files", ())
    return db.conn.execute(q, a).fetchone()[0]


def test_first_sync_populates_both_libraries(db):
    st = sync_index(db, FakeND({1: [song("a"), song("b")], 2: [song("c")]}), 1, 2)
    assert (count(db, "main"), count(db, "discovery")) == (2, 1)
    assert st["main"] == {"total": 2, "added": 2, "removed": 0} and st["discovery"]["added"] == 1


def test_resync_updates_adds_and_removes(db):
    sync_index(db, FakeND({1: [song("a"), song("b")], 2: [song("c")]}), 1, 2)
    st = sync_index(db, FakeND({1: [song("a", title="Renamed"), song("n")], 2: []}), 1, 2)
    assert st["main"] == {"total": 2, "added": 1, "removed": 1}      # b 被移除，n 新增
    assert st["discovery"] == {"total": 0, "added": 0, "removed": 1}  # 试听库清空是合法的
    assert db.conn.execute("SELECT title FROM local_files WHERE nd_song_id='a'").fetchone()[0] == "Renamed"
    assert count(db) == 2


def test_song_moved_between_libraries_keeps_row_not_deleted(db):
    sync_index(db, FakeND({1: [], 2: [song("x", starred=True)]}), 1, 2)
    st = sync_index(db, FakeND({1: [song("x", starred=True)], 2: []}), 1, 2)   # 转正：id 不变，库变了
    assert st["discovery"]["removed"] == 0
    row = db.conn.execute("SELECT library, starred FROM local_files WHERE nd_song_id='x'").fetchone()
    assert (row[0], row[1]) == ("main", 1)


def test_fetch_failure_leaves_old_index_untouched(db):
    sync_index(db, FakeND({1: [song("a")], 2: [song("c")]}), 1, 2)
    with pytest.raises(RuntimeError):
        sync_index(db, FakeND({1: [song("zzz")], 2: []}, fail_on=2), 1, 2)
    ids = {r[0] for r in db.conn.execute("SELECT nd_song_id FROM local_files")}
    assert ids == {"a", "c"}


def test_main_sudden_drop_is_not_deleted(db):
    big = [song(f"s{i}") for i in range(200)]
    sync_index(db, FakeND({1: big, 2: []}), 1, 2)
    st = sync_index(db, FakeND({1: big[:10], 2: []}), 1, 2)          # 200 → 10：疑似异常
    assert st["skipped_delete"] == ["main"] and count(db, "main") == 200


def test_small_main_library_may_shrink(db):
    sync_index(db, FakeND({1: [song("a"), song("b")], 2: []}), 1, 2)
    st = sync_index(db, FakeND({1: [song("a")], 2: []}), 1, 2)
    assert st["skipped_delete"] == [] and count(db, "main") == 1


def test_requires_both_folders(db):
    with pytest.raises(ValueError, match="主库和试听库"):
        sync_index(db, FakeND({}), None, 2)


def test_load_index_feeds_matcher(db):
    sync_index(db, FakeND({1: [song("m1", artist="郭静", title="心墙", duration=228)],
                           2: [song("d1", artist="Taylor Swift", title="Blank Space")]}), 1, 2)
    idx = load_index(db)
    r = match(idx, Target(artist="郭靜", title="心牆", duration_s=227))
    assert (r.status, r.chosen.nd_song_id) == ("main", "m1")
    assert match(idx, Target(artist="Taylor Swift", title="Blank Space")).status == "discovery"
