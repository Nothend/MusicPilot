import json
import threading

import pytest

from db import Database
from task_manager import TaskBusy, TaskManager


@pytest.fixture
def tm(tmp_path):
    d = Database(tmp_path / "t.sqlite")
    d.migrate()
    return TaskManager(d, tmp_path / "logs")


def test_run_records_status_stats_and_logs(tm, tmp_path):
    with tm.run("sync", "manual") as h:
        h.log("hello")
        h.add("downloaded", 2)
        h.add("downloaded")
    run = tm.recent_runs()[0]
    assert (run["type"], run["trigger"], run["status"]) == ("sync", "manual", "done")
    assert json.loads(run["stats_json"]) == {"downloaded": 3} and run["ended_at"]
    msgs = [x["msg"] for x in tm.logs(run["id"])]
    assert msgs[0].startswith("任务开始") and "hello" in msgs and msgs[-1].startswith("任务结束")
    assert "hello" in next((tmp_path / "logs").glob("lb-*.log")).read_text(encoding="utf-8")
    assert tm.current() is None


def test_second_task_while_running_is_rejected_not_queued(tm):
    with tm.run("sync", "manual"):
        assert tm.current()["type"] == "sync"
        with pytest.raises(TaskBusy, match="sync"):
            with tm.run("cleanup", "manual"):
                pytest.fail("不应进入")
    assert len(tm.recent_runs()) == 1                    # 被拒绝的不产生运行记录
    with tm.run("cleanup", "manual"):                    # 锁已释放
        pass


def test_busy_from_another_thread(tm):
    started, release = threading.Event(), threading.Event()
    def worker():
        with tm.run("sync", "schedule"):
            started.set()
            release.wait(5)
    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(5)
    with pytest.raises(TaskBusy):
        with tm.run("sync", "manual"):
            pass
    release.set()
    t.join()
    assert [r["status"] for r in tm.recent_runs()] == ["done"]


def test_exception_marks_failed_logs_error_and_releases_lock(tm):
    with pytest.raises(RuntimeError, match="boom"):
        with tm.run("sync", "manual") as h:
            h.add("x")
            raise RuntimeError("boom")
    run = tm.recent_runs()[0]
    assert run["status"] == "failed" and "boom" in run["summary"] and json.loads(run["stats_json"]) == {"x": 1}
    assert any(x["level"] == "ERROR" for x in tm.logs(run["id"]))
    with tm.run("sync", "manual"):
        pass


def test_handle_can_set_paused(tm):
    with tm.run("sync", "manual") as h:
        h.status, h.summary = "paused", "被限流"
    run = tm.recent_runs()[0]
    assert (run["status"], run["summary"]) == ("paused", "被限流")


def test_recover_marks_stale_running_as_interrupted(tm):
    with tm.db.transaction() as c:
        c.execute("INSERT INTO task_runs(type, trigger, started_at, status) VALUES('sync','manual','2026-11-20T08:00:00','running')")
    assert tm.recover() == 1
    run = tm.recent_runs()[0]
    assert run["status"] == "interrupted" and run["ended_at"]
    assert tm.recover() == 0


def test_logs_support_incremental_polling(tm):
    with tm.run("sync", "manual") as h:
        for i in range(5):
            h.log(f"m{i}")
    rid = tm.recent_runs()[0]["id"]
    first = tm.logs(rid, limit=3)
    rest = tm.logs(rid, after_id=first[-1]["id"])
    assert len(first) == 3 and rest[0]["id"] > first[-1]["id"] and len(first) + len(rest) == 7


# ---------------------------------------------------------------- begin/end、run_async、hold
def test_begin_end_pair_and_busy(tm):
    h = tm.begin("sync", "manual")
    with pytest.raises(TaskBusy):
        tm.begin("cleanup", "manual")
    h.add("x", 2)
    tm.end(h)
    assert tm.recent_runs()[0]["status"] == "done" and tm.current() is None
    tm.end(tm.begin("a", "manual"))                          # 锁已释放，可再开始


def test_run_async_returns_immediately_and_is_busy_synchronously(tm):
    gate, done = threading.Event(), threading.Event()
    def work(h):
        h.log("working")
        gate.wait(5)
        h.add("n")
    rid = tm.run_async("sync", "schedule", work, on_done=lambda h, e: done.set())
    assert tm.current()["id"] == rid                            # 已经在跑
    with pytest.raises(TaskBusy):                               # 第二个任务在「调用线程」里就被拒绝，页面能立刻提示
        tm.run_async("cleanup", "manual", lambda h: None)
    gate.set()
    assert done.wait(5)
    run = tm.recent_runs()[0]
    assert run["id"] == rid and run["status"] == "done" and json.loads(run["stats_json"]) == {"n": 1}
    assert tm.current() is None


def test_run_async_exception_is_recorded_and_lock_released_before_on_done(tm):
    seen = {}
    done = threading.Event()
    def boom(h):
        raise RuntimeError("kaput")
    def on_done(h, err):
        seen["err"] = err
        seen["lock_free"] = tm.current() is None
        tm.end(tm.begin("next", "manual"))                       # on_done 时锁已释放，可以马上开始下一个
        done.set()
    tm.run_async("sync", "manual", boom, on_done=on_done)
    assert done.wait(5)
    assert isinstance(seen["err"], RuntimeError) and seen["lock_free"]
    runs = {r["type"]: r for r in tm.recent_runs()}
    assert runs["sync"]["status"] == "failed" and "kaput" in runs["sync"]["summary"]


def test_run_async_survives_even_a_broken_on_done(tm):
    done = threading.Event()
    def on_done(h, e):
        done.set()
        raise RuntimeError("callback bug")
    tm.run_async("sync", "manual", lambda h: None, on_done=on_done)
    assert done.wait(5)
    import time as _t
    for _ in range(50):
        if tm.current() is None:
            break
        _t.sleep(0.05)
    assert tm.current() is None


def test_hold_blocks_until_free_and_shows_label(tm):
    h = tm.begin("sync", "manual")
    got = threading.Event()
    seen = {}
    def waiter():
        with tm.hold("音乐平台下载任务"):
            seen["current"] = tm.current()["type"]
            got.set()
    t = threading.Thread(target=waiter)
    t.start()
    assert not got.wait(0.3)                                    # 推荐任务在跑：下载队列在等，不会插队
    tm.end(h)
    assert got.wait(5) and seen["current"] == "音乐平台下载任务"
    t.join()
    assert tm.current() is None


def test_recommend_task_is_rejected_while_download_queue_holds_the_lock(tm):
    entered, leave = threading.Event(), threading.Event()
    def holder():
        with tm.hold("音乐平台下载任务"):
            entered.set()
            leave.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    assert entered.wait(5)
    with pytest.raises(TaskBusy, match="音乐平台下载任务"):
        tm.begin("sync", "manual")
    leave.set(); t.join()
    tm.end(tm.begin("sync", "manual"))
