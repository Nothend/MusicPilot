"""PRD 验收闭环（跨模块）：推荐 → 入库 → 下载到试听库 → 歌单 → 红心 → 转正 → 过期清理 → 再次推荐 → 回收站到期。

用会读磁盘的假 Navidrome（歌曲 id 由标签派生）串起真实的流水线/转正/清理代码，检查模块之间的衔接。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import lb_cleanup
import lb_pipeline
import lb_promote
import lb_repo
import lb_states as ST
from audio import make_flac
from lb_adapter import AdapterResult
from test_lb_pipeline import FakeLB, lbtrack
from test_lb_promote import Env

T0 = datetime(2026, 11, 20, 8, 30, 0)


class DiskAdapter:
    def __init__(self):
        self.calls = []

    def download_song(self, artist, title, output_dir, *, artists=None, album="", duration_ms=None):
        self.calls.append(title)
        n = len(self.calls)
        p = make_flac(Path(output_dir) / f"{artist} - {title}.flac", artist, title,
                      extra={"album": "专辑", "tracknumber": str(n), "date": "2026-01-01"})
        return AdapterResult(True, str(p), netease_id=n, quality="lossless", file_type="flac")


def test_full_lifecycle(tmp_path):
    env = Env(tmp_path)
    lb, adapter = FakeLB(), DiskAdapter()
    clock = {"now": T0}

    def sync():
        with env.tm.run("sync", "schedule") as h:
            lb_pipeline.run_sync(lb_pipeline.SyncDeps(env.db, env.settings, lb, env.nd, adapter, h, request_delay=0,
                                                      sleep=lambda s: None, now=lambda: clock["now"]))

    def promote():
        with env.tm.run("promote", "schedule") as h:
            lb_promote.run_promote(lb_promote.PromoteDeps(env.db, env.settings, env.nd, h, now=lambda: clock["now"]))

    def cleanup():
        with env.tm.run("cleanup", "schedule") as h:
            lb_cleanup.run_cleanup(lb_cleanup.CleanupDeps(env.db, env.settings, env.nd, h, now=lambda: clock["now"]))

    def state(batch, title):
        return {i["title"]: i["state"] for i in lb_repo.items_of_batch(env.db, batch)}[title]

    def playlist_names():
        return sorted(n for n, _ in env.nd.playlists.values())

    # 1) 推荐 → 下载到试听库 → 建歌单
    lb.add("weekly_jams", "p1", [lbtrack(1, "歌手", "A"), lbtrack(2, "歌手", "B"), lbtrack(3, "歌手", "C")])
    sync()
    assert [state(1, t) for t in "ABC"] == [ST.LOCAL_DISCOVERY] * 3
    assert sorted(p.name for p in env.disc.glob("*.flac")) == ["歌手 - A.flac", "歌手 - B.flac", "歌手 - C.flac"]
    assert playlist_names() == ["LB Weekly Jams 2026-11-20"]
    (_, ids), = env.nd.playlists.values()
    assert len(ids) == 3 and env.main.exists() and list(env.main.rglob("*.flac")) == []      # 主库纹丝不动

    # 2) 用户在 Navidrome 给 B 点红心 → 自动转正进主库，红心保留，歌单仍含 3 首
    id_b = next(s["id"] for s in env.nd.songs.values() if s["title"] == "B")
    env.nd.starred.add(id_b)
    clock["now"] = T0 + timedelta(hours=2)
    promote()
    assert state(1, "B") == ST.PROMOTED and state(1, "A") == ST.LOCAL_DISCOVERY
    assert [p.relative_to(env.main).as_posix() for p in env.main.rglob("*.flac")] == ["歌手/专辑 (2026)/02 - 歌手 - B.flac"]
    assert id_b in env.nd.starred and env.nd.stars == []                                      # 红心保留，无需补星
    (_, ids), = env.nd.playlists.values()
    assert len(ids) == 3 and id_b in ids

    # 3) 8 天后过期清理：A、C 进回收站并从歌单去掉；B 已在主库，不受影响
    clock["now"] = T0 + timedelta(days=8)
    cleanup()
    assert state(1, "A") == ST.EXPIRED and state(1, "C") == ST.EXPIRED and state(1, "B") == ST.PROMOTED
    assert list(env.disc.glob("*.flac")) == []
    assert len([p for p in (env.disc / ".trash").rglob("*.flac")]) == 2
    assert len(list(env.main.rglob("*.flac"))) == 1
    (_, ids), = env.nd.playlists.values()
    assert ids == [id_b]

    # 4) 之后 A 又被推荐（B 也再次被推荐）：A 重新下载；B 命中主库不重复下载
    lb.add("daily", "p2", [lbtrack(1, "歌手", "A"), lbtrack(2, "歌手", "B")], date="2026-11-28T00:00:00+00:00")
    adapter.calls.clear()
    sync()
    assert adapter.calls == ["A"]
    assert state(2, "A") == ST.LOCAL_DISCOVERY and state(2, "B") == ST.LOCAL_MAIN
    assert [i["state"] for i in lb_repo.items_of_batch(env.db, 1) if i["title"] == "A"] == [ST.EXPIRED]      # 推荐历史保留
    assert env.db.conn.execute("SELECT COUNT(DISTINCT track_id) FROM recommendation_items").fetchone()[0] == 3
    assert len(list(env.main.rglob("*.flac"))) == 1                                            # 主库依旧只有 B

    # 5) 第 16 天：第一次进回收站的 A、C 已满 7 天被彻底删除；第二次下载的 A（第 8 天入库）刚好到期，进回收站
    clock["now"] = T0 + timedelta(days=16)
    cleanup()
    assert state(2, "A") == ST.EXPIRED
    assert [p.name for p in (env.disc / ".trash").rglob("*.flac")] == ["歌手 - A.flac"] and len(lb_repo.trash_pending(env.db)) == 1
    assert (env.disc / ".trash" / (T0 + timedelta(days=8)).strftime("%Y%m%d")).exists() is False      # 旧日期目录已清掉
    # 第 31 天：这份回收站文件也满 7 天；第一份歌单（入库于第 0 天）超过 30 天被删，第二份（第 8 天）保留
    clock["now"] = T0 + timedelta(days=31)
    cleanup()
    assert list((env.disc / ".trash").rglob("*.flac")) == [] and lb_repo.trash_pending(env.db) == []
    assert playlist_names() == ["LB Daily Jams 2026-11-28"]
    (_, ids), = env.nd.playlists.values()
    assert ids == [id_b]                                                                     # 只剩仍在库里的 B

    # 6) 全程：每个条目的状态历史都留痕，且没有任何条目停在中间态
    states = {r[0] for r in env.db.conn.execute("SELECT DISTINCT state FROM recommendation_items")}
    assert not states & {ST.DOWNLOADING, ST.PROMOTING, ST.PENDING}
    assert env.db.conn.execute("SELECT COUNT(*) FROM item_events").fetchone()[0] >= 9
