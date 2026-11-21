import json
import os
import sqlite3
import stat
import threading

import pytest

import db as dbmod
from db import Database, DatabaseVersionError
from web_store import WebStore


def make_store(tmp_path):
    return WebStore(tmp_path / "web_state.json")


# ---------------------------------------------------------------- Database
def test_fresh_db_migrates_and_is_private(tmp_path):
    d = Database(tmp_path / "x.sqlite")
    assert d.migrate() == len(dbmod.MIGRATIONS)
    tables = {r[0] for r in d.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "job_history"} <= tables
    assert stat.S_IMODE(os.stat(tmp_path / "x.sqlite").st_mode) == 0o600
    d.conn.execute("PRAGMA wal_checkpoint")  # 触发 wal 文件，权限应沿用主库
    for suffix in ("-wal", "-shm"):
        p = tmp_path / ("x.sqlite" + suffix)
        if p.exists():
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_migrate_is_idempotent(tmp_path):
    d = Database(tmp_path / "x.sqlite")
    v = d.migrate()
    assert d.migrate() == v


def test_newer_db_version_refuses(tmp_path):
    d = Database(tmp_path / "x.sqlite")
    d.migrate()
    d.conn.execute(f"PRAGMA user_version={len(dbmod.MIGRATIONS) + 5}")
    with pytest.raises(DatabaseVersionError):
        d.migrate()


def test_transaction_rolls_back_on_error(tmp_path):
    d = Database(tmp_path / "x.sqlite")
    d.migrate()
    with pytest.raises(RuntimeError):
        with d.transaction() as c:
            c.execute("INSERT INTO settings VALUES('k','1','t')")
            raise RuntimeError("boom")
    assert d.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_failed_migration_rolls_back_version(tmp_path, monkeypatch):
    def bad(conn):
        conn.execute("CREATE TABLE t2(a)")
        raise RuntimeError("bad migration")
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS + [bad])
    d = Database(tmp_path / "x.sqlite")
    with pytest.raises(RuntimeError):
        d.migrate()
    assert d.version == len(dbmod.MIGRATIONS) - 1
    assert not d.conn.execute("SELECT 1 FROM sqlite_master WHERE name='t2'").fetchone()


# ---------------------------------------------------------------- WebStore
def test_defaults_and_persistence_across_reopen(tmp_path):
    s = make_store(tmp_path)
    st = s.get()
    assert st["account"] is None and st["settings"]["download_source"] == "self"
    s.update(lambda x: x.__setitem__("account", {"uid": 1, "cookies": {"MUSIC_U": "a"}}))
    s.update(lambda x: x["settings"].update(bark_api="http://bark/", quality="lossless"))
    s.update(lambda x: x["schedules"].append({"id": "s1", "times": ["08:00"]}))
    s.db.close()
    s2 = make_store(tmp_path)
    st = s2.get()
    assert st["account"]["uid"] == 1
    assert st["settings"]["bark_api"] == "http://bark/" and st["settings"]["use_library"] is True
    assert st["schedules"] == [{"id": "s1", "times": ["08:00"]}]


def test_get_returns_deep_copy(tmp_path):
    s = make_store(tmp_path)
    snap = s.get()
    snap["settings"]["bark_api"] = "changed"
    assert s.get()["settings"]["bark_api"] is None


def test_history_order_diff_and_trim(tmp_path):
    s = make_store(tmp_path)
    items = [{"id": f"j{i}", "status": "done", "n": i} for i in range(1, 6)]
    s.update(lambda x: x.__setitem__("history", items))
    # 只改一条：只应写一行（INSERT..ON CONFLICT 计 1 次变更）
    before = s.db.conn.total_changes
    items2 = [dict(i) for i in items]
    items2[2]["status"] = "failed"
    s.update(lambda x: x.__setitem__("history", items2))
    assert s.db.conn.total_changes - before == 1
    # 追加一条并裁掉最旧两条
    items3 = items2[2:] + [{"id": "j6", "status": "done", "n": 6}]
    s.update(lambda x: x.__setitem__("history", items3))
    s.db.close()
    got = make_store(tmp_path).get()["history"]
    assert [h["id"] for h in got] == ["j3", "j4", "j5", "j6"]
    assert got[0]["status"] == "failed"


def test_history_reorder_renumbers(tmp_path):
    s = make_store(tmp_path)
    a, b, c = ({"id": i} for i in "abc")
    s.update(lambda x: x.__setitem__("history", [a, b, c]))
    s.update(lambda x: x.__setitem__("history", [c, a, b]))
    s.db.close()
    assert [h["id"] for h in make_store(tmp_path).get()["history"]] == ["c", "a", "b"]


def test_history_skips_items_without_id(tmp_path):
    s = make_store(tmp_path)
    s.update(lambda x: x.__setitem__("history", [{"id": "ok"}, {"no": "id"}]))
    s.db.close()
    assert [h["id"] for h in make_store(tmp_path).get()["history"]] == ["ok"]


def test_update_exception_does_not_persist(tmp_path):
    s = make_store(tmp_path)
    def boom(x):
        x["settings"]["bark_api"] = "half"
        raise RuntimeError("x")
    with pytest.raises(RuntimeError):
        s.update(boom)
    s.db.close()
    assert make_store(tmp_path).get()["settings"]["bark_api"] is None


def test_concurrent_updates(tmp_path):
    s = make_store(tmp_path)
    s.update(lambda x: x["settings"].__setitem__("request_delay", 0))
    def work():
        for _ in range(25):
            s.update(lambda x: x["settings"].__setitem__("request_delay", x["settings"]["request_delay"] + 1))
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert s.get()["settings"]["request_delay"] == 200
    s.db.close()
    assert make_store(tmp_path).get()["settings"]["request_delay"] == 200


# ---------------------------------------------------------------- 旧 JSON 导入
LEGACY = {
    "account": {"uid": 7, "nickname": "n", "cookies": {"MUSIC_U": "x"}},
    "download_account": {"uid": 8, "cookies": {"MUSIC_U": "y"}},
    "settings": {"bark_api": "http://b/", "download_source": "borrowed", "future_key": 1},
    "schedules": [{"id": "s1", "times": ["09:30"]}],
    "history": [{"id": "j1", "status": "done"}, {"id": "j2", "status": "failed"}],
    "unknown_top_level": 1,
}


def test_legacy_import(tmp_path):
    legacy = tmp_path / "web_state.json"
    legacy.write_text(json.dumps(LEGACY), encoding="utf-8")
    s = WebStore(legacy)
    st = s.get()
    assert st["account"]["uid"] == 7 and st["download_account"]["uid"] == 8
    assert st["settings"]["download_source"] == "borrowed" and st["settings"]["use_library"] is True
    assert st["settings"]["future_key"] == 1
    assert [h["id"] for h in st["history"]] == ["j1", "j2"]
    assert not legacy.exists() and (tmp_path / "web_state.json.bak").exists()
    # 再次启动不重复导入，内容不变
    s.db.close()
    s2 = WebStore(legacy)
    assert s2.get() == st


def test_legacy_not_reimported_after_bak_restored(tmp_path):
    legacy = tmp_path / "web_state.json"
    legacy.write_text(json.dumps(LEGACY), encoding="utf-8")
    WebStore(legacy).db.close()
    s = WebStore(legacy)
    s.update(lambda x: x["settings"].__setitem__("bark_api", "NEW"))
    s.db.close()
    # 用户把 .bak 放回原名：库里已有状态，不得覆盖
    (tmp_path / "web_state.json.bak").rename(legacy)
    s2 = WebStore(legacy)
    assert s2.get()["settings"]["bark_api"] == "NEW"
    assert legacy.exists()


def test_corrupt_legacy_left_untouched(tmp_path):
    legacy = tmp_path / "web_state.json"
    legacy.write_text("{not json", encoding="utf-8")
    s = WebStore(legacy)
    assert s.get()["account"] is None
    assert legacy.read_text(encoding="utf-8") == "{not json"
    assert not (tmp_path / "web_state.json.bak").exists()


def test_legacy_non_object_left_untouched(tmp_path):
    legacy = tmp_path / "web_state.json"
    legacy.write_text("[1,2]", encoding="utf-8")
    WebStore(legacy)
    assert legacy.exists()


def test_legacy_bak_name_collision(tmp_path):
    legacy = tmp_path / "web_state.json"
    (tmp_path / "web_state.json.bak").write_text("old", encoding="utf-8")
    legacy.write_text(json.dumps(LEGACY), encoding="utf-8")
    WebStore(legacy)
    assert (tmp_path / "web_state.json.bak").read_text(encoding="utf-8") == "old"  # 不覆盖已有备份
    assert not legacy.exists()
    assert len(list(tmp_path.glob("web_state.json.*.bak"))) == 1
