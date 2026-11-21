"""现有「网易云下载任务队列」与推荐任务共用全局锁。"""
import threading
import time

import pytest

from db import Database
from task_manager import TaskBusy, TaskManager
from web_common import load_config_or_empty
from web_jobs import JobManager
from web_store import WebStore


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def setup(tmp_path):
    store = WebStore(tmp_path / "web_state.json")
    tm = TaskManager(store.db)
    jm = JobManager(store, load_config_or_empty(tmp_path), tmp_path / "dl", task_lock=tm)
    (tmp_path / "dl").mkdir()
    ran = []
    def fake_run(job):
        ran.append(job.data["id"])
        jm._finish(job, "done", "ok")
    jm._run = fake_run
    jm.start()
    return jm, tm, ran


def test_download_job_waits_for_a_running_recommend_task_and_then_runs(setup):
    jm, tm, ran = setup
    h = tm.begin("sync", "manual")
    job = jm.submit({"type": "daily"}, "")
    time.sleep(0.4)
    assert ran == [] and jm.get(job["id"])["status"] == "queued"        # 推荐任务在跑：下载任务排队等待
    tm.end(h)
    assert wait_for(lambda: ran == [job["id"]]) and wait_for(lambda: jm.get(job["id"])["status"] == "done")


def test_recommend_task_is_rejected_while_a_download_job_is_running(setup):
    jm, tm, ran = setup
    inside, leave = threading.Event(), threading.Event()
    def slow_run(job):
        inside.set()
        leave.wait(5)
        jm._finish(job, "done", "ok")
    jm._run = slow_run
    jm.submit({"type": "daily"}, "")
    assert inside.wait(5)
    with pytest.raises(TaskBusy, match="网易云下载任务"):
        tm.begin("sync", "manual")
    leave.set()
    assert wait_for(lambda: tm.current() is None)
    tm.end(tm.begin("sync", "manual"))


def test_job_manager_without_task_lock_is_unchanged(tmp_path):
    store = WebStore(tmp_path / "web_state.json")
    jm = JobManager(store, load_config_or_empty(tmp_path), tmp_path / "dl")
    (tmp_path / "dl").mkdir()
    ran = []
    jm._run = lambda job: (ran.append(1), jm._finish(job, "done", "ok"))
    jm.start()
    jm.submit({"type": "daily"}, "")
    assert wait_for(lambda: ran == [1])
