from datetime import datetime, timedelta

import pytest

import lb_settings
from db import Database
from lb_scheduler import LbScheduler, STATE_KEY
from lb_service import NotReady
from settings_store import SettingsStore
from task_manager import TaskBusy

# 2026-11-23 是周一
MON = datetime(2026, 11, 23, 8, 30, 0)


class FakeSvc:
    def __init__(self):
        self.s = lb_settings.defaults()
        self.s["enabled"] = True
        self.s["discovery"]["auto_promote"] = False       # 时间点类测试里时钟会大幅跳跃，间隔任务的测试再单独打开
        self.calls = []
        self.raises = None

    def settings(self):
        return self.s

    def start(self, kind, *, trigger, types=None, **kw):
        self.calls.append((kind, trigger, types))
        if self.raises:
            raise self.raises
        return 1


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.sqlite"); db.migrate()
    kv = SettingsStore(db)
    svc, clock = FakeSvc(), Clock(MON.replace(hour=0, minute=0))
    sched = LbScheduler(svc, kv, now=clock)
    sched.svc, sched.kv = svc, kv
    return sched, svc, clock, kv


def at(clock, hhmm, day=MON):
    h, m = map(int, hhmm.split(":"))
    clock.t = day.replace(hour=h, minute=m, second=5)


# ---------------------------------------------------------------- 每日 / 每周 / 清理（到点触发）
def test_daily_sync_fires_once_in_its_minute_with_only_daily_type(env):
    sched, svc, clock, _ = env
    at(clock, "08:30")                                             # 默认 daily_time=08:30
    assert sched.tick() == ["daily_sync"]
    assert svc.calls == [("sync", "schedule", ["daily"])]
    for sec in (25, 45):                                           # 同一分钟内 20 秒一次的检查不会重复触发
        clock.t = clock.t.replace(second=sec)
        assert sched.tick() == []
    at(clock, "08:31"); assert sched.tick() == []
    at(clock, "08:30", MON + timedelta(days=1))
    assert sched.tick() == ["daily_sync"]                          # 第二天又会触发


def test_weekly_sync_only_on_its_weekday_and_covers_both_weekly_types(env):
    sched, svc, clock, _ = env
    at(clock, "10:00", MON)                                        # 周一 10:00（默认）
    assert sched.tick() == ["weekly_sync"]
    assert svc.calls == [("sync", "schedule", ["weekly_exploration", "weekly_jams"])]
    at(clock, "10:00", MON + timedelta(days=1)); assert sched.tick() == []         # 周二不触发
    at(clock, "10:00", MON + timedelta(days=7)); assert sched.tick() == ["weekly_sync"]


def test_weekly_skips_disabled_types_and_does_not_fire_if_all_disabled(env):
    sched, svc, clock, _ = env
    svc.s["strategy"]["weekly_exploration"]["enabled"] = False
    at(clock, "10:00"); sched.tick()
    assert svc.calls[-1][2] == ["weekly_jams"]
    svc.calls.clear()
    svc.s["strategy"]["weekly_jams"]["enabled"] = False
    at(clock, "10:00", MON + timedelta(days=7)); assert sched.tick() == [] and svc.calls == []


def test_daily_not_fired_when_daily_type_disabled(env):
    sched, svc, clock, _ = env
    svc.s["strategy"]["daily"]["enabled"] = False
    at(clock, "08:30")
    assert sched.tick() == []


def test_cleanup_fires_at_its_time_only_when_auto_cleanup_is_on(env):
    sched, svc, clock, _ = env
    at(clock, "04:00")
    assert sched.tick() == ["cleanup"] and svc.calls == [("cleanup", "schedule", None)]
    svc.calls.clear(); svc.s["discovery"]["auto_cleanup"] = False
    at(clock, "04:00", MON + timedelta(days=1)); assert sched.tick() == []


def test_master_switch_off_fires_nothing_at_all(env):
    sched, svc, clock, _ = env
    svc.s["enabled"] = False
    for hhmm in ("08:30", "10:00", "04:00"):
        at(clock, hhmm); assert sched.tick() == []
    clock.t += timedelta(days=1)
    assert sched.tick() == [] and svc.calls == []


def test_settings_changes_take_effect_on_the_next_tick(env):
    sched, svc, clock, _ = env
    svc.s["schedule"]["daily_time"] = "21:15"
    at(clock, "08:30"); assert sched.tick() == []
    at(clock, "21:15"); assert sched.tick() == ["daily_sync"]


# ---------------------------------------------------------------- 间隔任务（转正 / 红心）
def test_promote_waits_a_full_interval_after_start_then_repeats(env):
    sched, svc, clock, _ = env
    svc.s["discovery"]["auto_promote"] = True
    start = clock.t
    clock.t = start + timedelta(minutes=29); assert sched.tick() == []         # 启动后先等满一个间隔，不立刻跑
    clock.t = start + timedelta(minutes=30); assert sched.tick() == ["promote"]
    clock.t = start + timedelta(minutes=45); assert sched.tick() == []
    clock.t = start + timedelta(minutes=60); assert sched.tick() == ["promote"]


def test_promote_respects_auto_promote_switch_and_custom_interval(env):
    sched, svc, clock, _ = env
    start = clock.t
    svc.s["discovery"]["auto_promote"] = False
    clock.t = start + timedelta(hours=5); assert "promote" not in sched.tick()
    svc.s["discovery"]["auto_promote"] = True; svc.s["schedule"]["promote_interval_min"] = 5
    assert "promote" in sched.tick()


def test_love_is_off_by_default_and_runs_on_its_own_interval(env):
    sched, svc, clock, _ = env
    start = clock.t
    clock.t = start + timedelta(hours=3); assert "love" not in sched.tick()    # 默认关闭：绝不会自己去写 ListenBrainz
    svc.s["love"]["enabled"] = True
    assert "love" in sched.tick()
    clock.t += timedelta(minutes=59); assert "love" not in sched.tick()
    clock.t += timedelta(minutes=1); assert "love" in sched.tick()


# ---------------------------------------------------------------- 忙碌 / 未就绪 / 异常
def test_busy_is_skipped_not_queued_and_not_retried_in_the_same_minute(env):
    sched, svc, clock, _ = env
    svc.raises = TaskBusy({"type": "sync", "started_at": "x"})
    at(clock, "08:30")
    assert sched.tick() == []                                       # 没有启动
    svc.raises = None
    clock.t = clock.t.replace(second=45)
    assert sched.tick() == []                                       # 同一分钟不会补触发：错过就错过
    assert len(svc.calls) == 1


def test_busy_interval_task_waits_another_interval(env):
    sched, svc, clock, _ = env
    svc.s["discovery"]["auto_promote"] = True
    start = clock.t
    svc.raises = TaskBusy({})
    clock.t = start + timedelta(minutes=30); assert sched.tick() == []
    svc.raises = None
    clock.t = start + timedelta(minutes=45); assert sched.tick() == []
    clock.t = start + timedelta(minutes=60); assert sched.tick() == ["promote"]


def test_not_ready_and_unexpected_errors_do_not_break_the_loop(env):
    sched, svc, clock, _ = env
    svc.raises = NotReady("缺少配置")
    at(clock, "08:30"); assert sched.tick() == []
    svc.raises = RuntimeError("boom")
    at(clock, "10:00"); assert sched.tick() == []
    svc.raises = None
    at(clock, "04:00"); assert sched.tick() == ["cleanup"]


def test_two_tasks_due_in_the_same_minute_both_fire(env):
    sched, svc, clock, _ = env
    svc.s["schedule"]["cleanup_time"] = "08:30"
    at(clock, "08:30")
    assert sched.tick() == ["daily_sync", "cleanup"]


# ---------------------------------------------------------------- 重启
def test_restart_in_the_same_minute_does_not_fire_twice(env):
    sched, svc, clock, kv = env
    at(clock, "08:30"); sched.tick()
    assert kv.get(STATE_KEY) == {"daily_sync": "2026-11-23 08:30"}
    again = LbScheduler(svc, kv, now=clock)                         # 模拟进程重启
    assert again.tick() == [] and len(svc.calls) == 1


# ---------------------------------------------------------------- 展示
def test_upcoming_lists_next_runs_in_order(env):
    sched, svc, clock, _ = env
    clock.t = MON.replace(hour=9, minute=0)
    up = {u["task"]: u["next"] for u in sched.upcoming()}
    assert up["每日同步（每日熟悉）"] == "2026-11-24 08:30"
    assert up["每周同步（Weekly）"] == "2026-11-23 10:00"                   # 现在是周一 09:00，今天 10:00 还没到
    assert up["过期清理"] == "2026-11-24 04:00"                              # 今天 04:00 已过 → 明天
    assert "转正检查" not in up and "红心同步" not in up                      # 对应开关没开就不显示
    svc.s["discovery"]["auto_promote"] = True
    up = {u["task"]: u["next"] for u in sched.upcoming()}
    assert up["转正检查"] == "2026-11-23 09:00"                              # 间隔已满（启动于 00:00）→ 下一个检查周期就会跑，显示为「现在」
    assert [u["next"] for u in sched.upcoming()] == sorted(u["next"] for u in sched.upcoming())


def test_upcoming_is_empty_when_disabled(env):
    sched, svc, clock, _ = env
    svc.s["enabled"] = False
    assert sched.upcoming() == []
