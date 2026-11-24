"""推荐列表只认「当前周期」：每日=今天、每周=本周（周一起）；过期歌单不入库、不下载，页面列表为空并说明原因。"""
from datetime import datetime, timedelta

import lb_period
import lb_queries
import lb_states as ST
from test_lb_pipeline import T0, env, lbtrack      # noqa: F401  T0 = 2026-11-20（周五）8:30；env 是装配好的假环境

FRI = datetime(2026, 11, 20, 8, 30)


def msgs(env, run):
    return [x["msg"] for x in env.tm.logs(run["id"])]


# ---------------------------------------------------------------- 周期边界
def test_period_start_boundaries():
    assert lb_period.period_start("daily", FRI) == "2026-11-20"
    assert lb_period.period_start("weekly_jams", FRI) == "2026-11-16"                      # 周五 → 本周一
    assert lb_period.period_start("weekly_exploration", datetime(2026, 11, 22, 23, 59)) == "2026-11-16"   # 周日仍是本周
    assert lb_period.period_start("weekly_jams", datetime(2026, 11, 23, 0, 0)) == "2026-11-23"            # 下周一换周
    assert lb_period.period_start("netease_daily", datetime(2026, 11, 20, 5, 59)) == "2026-11-19"         # 平台 6 点前仍是前一天
    assert lb_period.period_start("netease_daily", datetime(2026, 11, 20, 6, 0)) == "2026-11-20"
    assert lb_period.is_current("weekly_jams", "2026-11-16", FRI) and not lb_period.is_current("weekly_jams", "2026-11-15", FRI)
    assert not lb_period.is_current("daily", "", FRI)


# ---------------------------------------------------------------- 获取
def test_stale_weekly_is_not_fetched_and_the_reason_is_recorded(env):          # noqa: F811
    env.lb.add("weekly_jams", "old", [lbtrack(1)], date="2026-09-14T12:00:00+00:00")    # 今天的情况：LB 最新还是 9 月那份
    run = env.sync()
    assert run["status"] == "done"
    assert env.lb.playlist_calls == [] and env.db.conn.execute("SELECT COUNT(*) FROM recommendation_batches").fetchone()[0] == 0
    assert any("每周熟悉" in m and "还没有生成" in m and "2026-09-14" in m for m in msgs(env, run))
    assert env.kv.get(lb_period.FETCH_STATUS_KEY)["weekly_jams"]["state"] == "stale"
    lists = {x["type"]: x for x in lb_queries.current_lists(env.db, env.settings, env.kv, env.now)}
    w = lists["weekly_jams"]
    assert w["batch_id"] is None and "2026-09-14" in w["reason"] and "本周" in w["reason"] and "听歌记录" in w["reason"]


def test_last_weeks_playlist_is_rejected_but_this_mondays_is_taken(env):        # noqa: F811
    env.lb.add("weekly_jams", "last", [lbtrack(1)], date="2026-11-15T12:00:00+00:00")      # 上周日：不算本周
    env.lb.add("weekly_exploration", "mon", [lbtrack(2)], date="2026-11-16T12:00:00+00:00")  # 本周一：算
    env.sync()
    assert env.lb.playlist_calls == ["mon"]


def test_yesterdays_daily_is_rejected_and_the_title_date_decides(env):          # noqa: F811
    env.lb.add("daily", "y", [lbtrack(1)], date="2026-11-19T12:00:00+00:00")
    env.sync()
    assert env.lb.playlist_calls == []
    # 北京时间 00:01 生成的「今天」歌单：UTC 创建时间还是前一天，以标题里的日期为准
    env.lb.headers.insert(0, {"mbid": "t", "title": "Daily Jams for u, 2026-11-20 Fri", "date": "2026-11-19T16:01:00+00:00",
                              "type": "daily", "expires_at": "", "source_patch": ""})
    env.lb.playlists["t"] = {"mbid": "t", "title": "x", "date": "", "tracks": [lbtrack(2)]}
    run = env.sync()
    assert env.lb.playlist_calls == ["t"] and any("获取到新歌单 2026-11-20" in m for m in msgs(env, run))


def test_no_playlist_at_all_records_none(env):                                  # noqa: F811
    env.sync()
    assert env.kv.get(lb_period.FETCH_STATUS_KEY)["daily"]["state"] == "none"
    assert "还没有任何" in lb_queries.current_lists(env.db, env.settings, env.kv, env.now)[0]["reason"]


# ---------------------------------------------------------------- 不再处理过期批次
def test_expired_active_batch_is_not_downloaded_automatically(env):             # noqa: F811
    env.lb.add("weekly_jams", "w", [lbtrack(1)])
    env.sync()
    env.adapter.calls.clear()
    with env.db.transaction() as c:                      # 让它回到「待下载」且批次仍进行中
        c.execute("UPDATE recommendation_items SET state=?", (ST.PENDING,))
        c.execute("UPDATE recommendation_batches SET status='active'")
    env.now = T0 + timedelta(days=4)                      # 下周二
    env.sync()
    assert env.adapter.calls == [] and env.states() == {"Song 1": ST.PENDING}


def test_history_lists_old_batches_with_current_flag(env):                      # noqa: F811
    env.lb.add("weekly_jams", "w", [lbtrack(1)])
    env.sync()
    b = lb_queries.batches(env.db, "weekly_jams", now=T0 + timedelta(days=4))
    assert len(b) == 1 and b[0]["current"] is False and b[0]["counts"] == {ST.LOCAL_DISCOVERY: 1}
    assert lb_queries.batches(env.db, "weekly_jams", now=T0)[0]["current"] is True
    assert lb_queries.current_lists(env.db, env.settings, env.kv, T0 + timedelta(days=4))[2]["batch_id"] is None   # 下周二：本周还没有
    assert lb_queries.current_lists(env.db, env.settings, env.kv, T0)[2]["batch_id"] == b[0]["id"]
