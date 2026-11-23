"""推荐联动的 HTTP 接口：真实 HTTP 服务（随机端口）+ 假的外部系统。"""
from fakes import lb_defaults
import json
import threading
import time

import pytest
import requests

import lb_repo
import lb_settings
import lb_states as ST
import web_server
from test_lb_pipeline import FakeAdapter, FakeLB, FakeND, lbtrack


class Api:
    def __init__(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WEB_PASSWORD", raising=False)
        (tmp_path / "dl").mkdir()
        self.app = web_server.App(tmp_path / "data", tmp_path / "dl")
        self.lb, self.nd = FakeLB(), FakeND()
        self.main, self.disc = tmp_path / "songs", tmp_path / "discovery"
        self.main.mkdir(); self.disc.mkdir()
        self.adapter = FakeAdapter(self.nd, self.disc)
        svc = self.app.lb
        svc.lb_factory, svc.nd_factory = (lambda s: self.lb), (lambda s: self.nd)
        svc.adapter_factory = lambda s, d: self.adapter
        svc.request_delay, svc.sleep = (lambda: 0), (lambda x: None)
        self.server = web_server.serve("127.0.0.1", 0, self.app)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.configure()

    def configure(self):
        s = lb_defaults()
        s["listenbrainz"].update(username="me", token="tok")
        s["navidrome"].update(url="http://n", user="u", password="p", main_folder_id=1, discovery_folder_id=2)
        s["dirs"].update(main=str(self.main), discovery=str(self.disc))
        self.app.kv.set(lb_settings.SETTINGS_KEY, s)

    def get(self, path, **kw):
        return requests.get(self.base + path, timeout=10, **kw)

    def post(self, path, body=None, **kw):
        return requests.post(self.base + path, json=body or {}, timeout=10, **kw)

    def wait_idle(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if self.app.tm.current() is None:
                return
            time.sleep(0.03)
        raise AssertionError("任务没有结束")

    def close(self):
        self.server.shutdown(); self.server.server_close()


@pytest.fixture
def api(tmp_path, monkeypatch):
    a = Api(tmp_path, monkeypatch)
    yield a
    a.close()


# ---------------------------------------------------------------- 只读接口
def test_read_endpoints_work_on_an_empty_database(api):
    d = api.get("/api/lb/dashboard").json()
    assert d["setup_issue"] == "" and d["latest_batches"] == {} and d["current_task"] is None
    assert api.get("/api/lb/batches").json() == {"batches": []}
    assert api.get("/api/lb/runs").json() == {"runs": [], "current": None}
    assert api.get("/api/lb/cleanup/preview").json()["due_files"] == 0
    assert api.get("/api/lb/love/preview").json() == {"preview": None}


# ---------------------------------------------------------------- 红心：未解析的歌人工处理
MB = "11111111-2222-3333-4444-555555555555"


def test_unresolved_love_songs_can_be_resolved_and_undone_over_http(api):
    assert api.get("/api/lb/love/unresolved").json() == {"at": None, "items": [], "manual": []}
    api.app.kv.set("lb.love_unresolved", {"at": "2026-11-22T10:00:00",
                                          "items": [{"id": "s1", "artist": "甲", "title": "歌", "reason": "no_result", "cand": ""}]})
    v = api.get("/api/lb/love/unresolved").json()
    assert v["items"][0]["reason_zh"] and v["items"][0]["id"] == "s1"
    assert api.post("/api/lb/love/resolve", {"id": "s1", "mbid": "bad"}).status_code == 400
    assert api.post("/api/lb/love/resolve", {"id": "nope", "mbid": MB}).status_code == 400
    assert api.post("/api/lb/love/resolve", {"id": "s1", "mbid": MB}).json() == {"ok": True, "remaining": 0}
    v = api.get("/api/lb/love/unresolved").json()
    assert v["items"] == [] and v["manual"][0]["mbid"] == MB
    m = v["manual"][0]
    assert api.post("/api/lb/love/undo", {"na": m["norm_artist"], "nt": m["norm_title"]}).json() == {"ok": True}
    assert api.post("/api/lb/love/undo", {"na": "x", "nt": "y"}).status_code == 400


def test_mb_search_is_proxied_and_errors_are_400(api, monkeypatch):
    import mb_client
    monkeypatch.setattr(mb_client, "search_recordings", lambda a, t: [{"mbid": MB, "title": t, "artist": a}])
    assert api.get("/api/lb/love/mb_search?artist=甲&title=歌").json()["results"][0]["mbid"] == MB

    def boom(a, t):
        raise mb_client.MBError("MusicBrainz 限流中，请稍后再试")
    monkeypatch.setattr(mb_client, "search_recordings", boom)
    r = api.get("/api/lb/love/mb_search?artist=甲&title=歌")
    assert r.status_code == 400 and "限流" in r.json()["error"]


def test_todo_counts_everything_waiting_for_the_user(api):
    db = api.app.store.db
    assert api.get("/api/lb/todo").json() == {"multiple": 0, "dup": 0, "love_unresolved": 0, "total": 0,
                                              "multiple_batch": None, "dup_batch": None}
    tr = lambda n: {"recording_mbid": f"m{n}", "artist": "A", "title": f"T{n}", "album": "", "duration_ms": 1, "artists": []}
    bid = lb_repo.create_batch(db, "weekly_jams", {"mbid": "p", "title": "t", "date": "d"}, {"tracks": [tr(1), tr(2)]})
    i1, i2 = [i["id"] for i in lb_repo.items_of_batch(db, bid)]
    lb_repo.set_state(db, i1, ST.MULTIPLE_MATCH)
    with db.transaction() as c:                                          # 一个还在主库里的疑似重复文件 + 一个已不在主库的
        for sid, lib in (("m-main", "main"), ("m-gone", "discovery")):
            c.execute("INSERT INTO local_files(nd_song_id, library, indexed_at) VALUES(?,?,'2026-11-22T00:00:00')", (sid, lib))
    lb_repo.add_dup_suspects(db, i2, [("m-main", "near", 1.0), ("m-gone", "near", 1.0)])
    api.app.kv.set("lb.love_unresolved", {"at": "t", "items": [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]})
    assert api.get("/api/lb/todo").json() == {"multiple": 1, "dup": 1, "love_unresolved": 3, "total": 5,
                                              "multiple_batch": {"id": bid, "type": "weekly_jams"}, "dup_batch": {"id": bid, "type": "weekly_jams"}}
    lb_repo.dismiss_dup_suspects(db, [i2])                               # 选了「两份都留」后不再算待处理
    assert api.get("/api/lb/todo").json()["dup"] == 0


def test_bad_parameters_are_400_not_500(api):
    assert api.get("/api/lb/items").status_code == 400
    assert api.get("/api/lb/items?batch=x").status_code == 400
    assert api.get("/api/lb/items?batch=999").status_code == 400
    assert api.get("/api/lb/run?id=abc").status_code == 400
    assert api.get("/api/lb/run?id=999").status_code == 404


# ---------------------------------------------------------------- 触发任务
def test_action_runs_a_sync_and_the_page_can_follow_its_logs(api):
    api.lb.add("weekly_jams", "pl", [lbtrack(1), lbtrack(2)])
    r = api.post("/api/lb/action", {"kind": "sync"})
    assert r.status_code == 200
    rid = r.json()["run_id"]
    api.wait_idle()
    detail = api.get(f"/api/lb/run?id={rid}").json()
    assert detail["run"]["status"] == "done" and detail["run"]["stats"]["downloaded"] == 2 and detail["running"] is False
    msgs = [x["msg"] for x in detail["logs"]]
    assert msgs[0].startswith("任务开始") and any("下载成功" in m for m in msgs) and msgs[-1].startswith("任务结束")
    later = api.get(f"/api/lb/run?id={rid}&after={detail['logs'][-3]['id']}").json()["logs"]       # 增量拉取日志
    assert len(later) == 2
    d = api.get("/api/lb/dashboard").json()
    assert d["latest_batches"]["weekly_jams"]["counts"] == {ST.LOCAL_DISCOVERY: 2} and d["downloaded_today"] == 2
    assert api.get("/api/lb/runs").json()["runs"][0]["id"] == rid


def test_items_endpoint_lists_the_recommendations(api):
    api.lb.add("weekly_jams", "pl", [lbtrack(1), lbtrack(2)])
    api.post("/api/lb/action", {"kind": "sync"}); api.wait_idle()
    bid = api.get("/api/lb/batches?type=weekly_jams").json()["batches"][0]["id"]
    res = api.get(f"/api/lb/items?batch={bid}").json()
    assert [i["title"] for i in res["items"]] == ["Song 1", "Song 2"] and res["counts"] == {ST.LOCAL_DISCOVERY: 2}
    assert res["items"][0]["path"].endswith("A - Song 1.flac") and res["batch"]["label"] == "每周熟悉"
    assert len(api.get(f"/api/lb/items?batch={bid}&state={ST.NOT_FOUND}").json()["items"]) == 0


def test_missing_configuration_returns_a_readable_400_and_the_dashboard_says_what_is_missing(api):
    s = api.app.kv.get(lb_settings.SETTINGS_KEY)
    s["listenbrainz"].update(username="", token="")
    api.app.kv.set(lb_settings.SETTINGS_KEY, s)
    r = api.post("/api/lb/action", {"kind": "sync"})
    assert r.status_code == 400 and "ListenBrainz" in r.json()["error"]
    assert api.get("/api/lb/runs").json()["runs"] == []
    assert "ListenBrainz" in api.get("/api/lb/dashboard").json()["setup_issue"]       # 页面据此显示「还缺什么」+「去设置」


def test_a_second_action_while_one_runs_gets_409_busy(api):
    gate = threading.Event()
    orig = api.adapter.download_song
    api.adapter.download_song = lambda *a, **k: (gate.wait(5), orig(*a, **k))[1]
    api.lb.add("weekly_jams", "pl", [lbtrack(1)])
    assert api.post("/api/lb/action", {"kind": "sync"}).status_code == 200
    r = api.post("/api/lb/action", {"kind": "cleanup"})
    assert r.status_code == 409 and r.json()["busy"] is True and r.json()["running"]["type"] == "sync"
    assert api.get("/api/lb/dashboard").json()["current_task"]["type"] == "sync"
    assert api.get("/api/lb/runs").json()["current"]["type"] == "sync"
    gate.set(); api.wait_idle()
    assert api.post("/api/lb/action", {"kind": "cleanup"}).status_code == 200
    api.wait_idle()


def test_action_validation(api):
    assert api.post("/api/lb/action", {"kind": "format-disk"}).status_code == 400
    assert api.post("/api/lb/action", {"kind": "sync", "types": ["hacker"]}).status_code == 400
    assert api.post("/api/lb/action", {"kind": "sync", "types": "daily"}).status_code == 400
    assert api.app.tm.recent_runs() == []


def test_action_types_filter_and_reindex_scan(api):
    api.lb.add("daily", "pd", [lbtrack(1)]); api.lb.add("weekly_jams", "pj", [lbtrack(2)])
    api.post("/api/lb/action", {"kind": "sync", "types": ["daily"]}); api.wait_idle()
    assert api.lb.playlist_calls == ["pd"]
    scans = api.nd.scans
    api.post("/api/lb/action", {"kind": "reindex", "scan": True}); api.wait_idle()
    assert api.nd.scans == scans + 1


# ---------------------------------------------------------------- 条目操作
def test_item_actions_over_http(api):
    api.lb.add("weekly_jams", "pl", [lbtrack(1)])
    api.adapter.script = {"Song 1": "not_found"}
    api.post("/api/lb/action", {"kind": "sync"}); api.wait_idle()
    bid = api.get("/api/lb/batches").json()["batches"][0]["id"]
    item = api.get(f"/api/lb/items?batch={bid}").json()["items"][0]
    assert item["state"] == ST.NOT_FOUND
    r = api.post("/api/lb/item", {"id": item["id"], "action": "retry"})
    assert r.status_code == 200 and r.json()["state"] == ST.PENDING
    assert api.post("/api/lb/item", {"id": item["id"], "action": "keep"}).status_code == 400
    assert api.post("/api/lb/item", {"id": "x", "action": "keep"}).status_code == 400
    assert api.post("/api/lb/item", {"id": 9999, "action": "keep"}).status_code == 400
    api.adapter.script = {}
    api.post("/api/lb/action", {"kind": "sync"}); api.wait_idle()
    assert api.get(f"/api/lb/items?batch={bid}").json()["items"][0]["state"] == ST.LOCAL_DISCOVERY      # 重试后下次同步下载成功


# ---------------------------------------------------------------- 安全
def test_post_requires_json_content_type(api):
    r = requests.post(api.base + "/api/lb/action", data='{"kind":"sync"}', headers={"Content-Type": "text/plain"}, timeout=10)
    assert r.status_code == 415 and api.app.tm.recent_runs() == []


def test_basic_auth_protects_every_new_endpoint(tmp_path, monkeypatch):
    a = Api(tmp_path, monkeypatch)
    monkeypatch.setenv("WEB_PASSWORD", "secret")
    try:
        for path in ("/api/lb/dashboard", "/api/lb/runs", "/api/lb/items?batch=1", "/api/lb/cleanup/preview", "/api/lb/love/preview",
                     "/api/lb/love/unresolved", "/api/lb/love/mb_search?title=x", "/api/lb/todo"):
            assert a.get(path).status_code == 401
        for path in ("/api/lb/love/resolve", "/api/lb/love/undo"):
            assert requests.post(a.base + path, json={}, timeout=10).status_code == 401
        assert requests.post(a.base + "/api/lb/action", json={"kind": "sync"}, timeout=10).status_code == 401
        assert requests.get(a.base + "/api/lb/dashboard", auth=("admin", "secret"), timeout=10).status_code == 200
        assert a.app.tm.recent_runs() == []
    finally:
        a.close()


# ---------------------------------------------------------------- 启动善后
def test_startup_recovers_leftovers_from_a_killed_process(api):
    with api.app.store.db.transaction() as c:
        c.execute("INSERT INTO task_runs(type, trigger, started_at, status) VALUES('sync','schedule','2026-11-20T08:00:00','running')")
    bid = lb_repo.create_batch(api.app.store.db, "weekly_jams", {"mbid": "p", "title": "t", "date": "d"},
                               {"tracks": [{"recording_mbid": "m", "artist": "A", "title": "T", "album": "", "duration_ms": 1, "artists": []}]})
    item = lb_repo.items_of_batch(api.app.store.db, bid)[0]["id"]
    lb_repo.set_state(api.app.store.db, item, ST.DOWNLOADING)
    api.app.start()
    try:
        assert api.app.tm.recent_runs()[0]["status"] == "interrupted"
        assert lb_repo.get_item(api.app.store.db, item)["state"] == ST.PENDING
    finally:
        api.app.scheduler.stop()


def test_schedule_endpoint_lists_upcoming_runs_and_follows_the_per_list_switches(api):
    up = api.get("/api/lb/schedule").json()
    assert {u["task"] for u in up["upcoming"]} >= {"每日同步（每日熟悉）", "每周同步（Weekly）", "过期清理", "转正检查"} and up["timezone"]
    s = api.app.kv.get(lb_settings.SETTINGS_KEY)
    for t in s["strategy"].values():
        t["enabled"] = False
    s["discovery"].update(auto_cleanup=False, auto_promote=False)
    api.app.kv.set(lb_settings.SETTINGS_KEY, s)
    assert api.get("/api/lb/schedule").json()["upcoming"] == []


def test_the_scheduler_is_started_and_stopped_with_the_app(api):
    api.app.start()
    try:
        assert any(t.name == "lb-scheduler" for t in threading.enumerate())
    finally:
        api.app.scheduler.stop(); api.app.lb_scheduler.stop()


def test_download_queue_shares_the_lock_with_recommend_tasks(api):
    assert api.app.jobs.task_lock is api.app.tm


def test_manual_download_dedupe_reuses_the_linked_navidrome_connection(api):
    """联动开启时，「音乐库去重」不再让用户填第二份 Navidrome：直接用联动里的那份；关闭联动则沿用页面里单独设置的。"""
    own = {"use_library": True, "library": {"type": "music-tag-web", "navidrome": {"host": "", "user": "", "password": ""},
                                           "mysql": {"host": "db", "port": 3306, "user": "u", "password": "p", "database": "d"}}}
    linked = api.app._library_settings(own)                        # 测试数据是联动 + Navidrome 已配置
    assert linked["library"]["type"] == "navidrome" and linked["use_library"] is True
    assert linked["library"]["navidrome"] == {"host": "http://n", "user": "u", "password": "p"}
    assert linked["library"]["mysql"] == own["library"]["mysql"]    # 其余设置原样保留
    assert own["library"]["type"] == "music-tag-web"                # 不改动原对象

    s = api.app.kv.get(lb_settings.SETTINGS_KEY); s["mode"] = "folder"
    api.app.kv.set(lb_settings.SETTINGS_KEY, s)
    assert api.app._library_settings(own) is own                    # 未联动：用页面里的「音乐库去重」设置

    s["mode"] = "navidrome"; s["navidrome"]["password"] = ""
    api.app.kv.set(lb_settings.SETTINGS_KEY, s)
    assert api.app._library_settings(own) is own                    # 联动但 Navidrome 没配好：同样沿用
