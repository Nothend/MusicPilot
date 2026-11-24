"""手动重试要真的重试：同一首歌在更早批次里的「未找到」冷却不能把它挡回去；新启用下载渠道时旧的冷却一并解除。"""
from datetime import timedelta

import lb_queries
import lb_repo
import lb_states as ST
from test_lb_pipeline import T0, env, lbtrack      # noqa: F401
from test_web_lb_api import api                      # noqa: F401


def test_manual_retry_bypasses_the_cooldown_left_by_an_earlier_batch(env):       # noqa: F811
    env.lb.add("weekly_jams", "p1", [lbtrack(1, mbid="same")])
    env.adapter.script = {"Song 1": "not_found"}
    env.sync()
    env.now = T0 + timedelta(days=2)
    env.lb.add("weekly_exploration", "p2", [lbtrack(1, mbid="same")])           # 另一批又推荐了同一首（现实里就是今天这种情况）
    env.sync()
    assert env.adapter.calls == ["Song 1"] and env.items(2)["Song 1"]["reason_kind"] == "cooldown"
    item2 = env.items(2)["Song 1"]["id"]
    lb_queries.item_action(env.db, item2, "retry", now=env.now)                  # 页面上点「重试」
    env.adapter.script = {}
    env.sync()
    assert env.adapter.calls == ["Song 1", "Song 1"] and env.states(2) == {"Song 1": ST.LOCAL_DISCOVERY}


def test_without_retry_the_cooldown_still_holds(env):                             # noqa: F811
    env.lb.add("weekly_jams", "p1", [lbtrack(1, mbid="same")])
    env.adapter.script = {"Song 1": "not_found"}
    env.sync()
    env.now = T0 + timedelta(days=2)
    env.lb.add("weekly_exploration", "p2", [lbtrack(1, mbid="same")])
    env.sync(); env.sync()
    assert env.adapter.calls == ["Song 1"]


def test_enabling_the_cloud_channel_releases_old_cooldowns_once(api):             # noqa: F811
    db = api.app.store.db
    bid = lb_repo.create_batch(db, "weekly_jams", {"mbid": "p", "title": "t", "date": "2026-11-20T12:00:00+00:00"},
                               {"tracks": [lbtrack(1)]})
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    lb_repo.set_state(db, item, ST.NOT_FOUND, next_retry_at="2999-01-01T00:00:00")
    body = {"openlist": {"enabled": True, "url": "http://x:5244", "username": "u", "password": "p", "music_dir": "/音乐"}}
    assert api.post("/api/lb/settings", body).status_code == 200
    assert lb_repo.get_item(db, item)["next_retry_at"] is None                     # 刚启用：冷却解除
    lb_repo.update_fields(db, item, next_retry_at="2999-01-01T00:00:00")
    assert api.post("/api/lb/settings", body).status_code == 200
    assert lb_repo.get_item(db, item)["next_retry_at"] == "2999-01-01T00:00:00"    # 已经启用着再保存：不动
