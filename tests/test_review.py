"""网盘文件的元数据人工审核：流水线把它放进 REVIEW 状态，页面对比后选择「用推荐的 / 保留原标签 / 放弃」。"""
import json
from pathlib import Path

import pytest
from mutagen.flac import FLAC, Picture

import lb_queries
import lb_repo
import lb_review
import lb_settings
import lb_states as ST
from audio import make_flac
from lb_adapter import AdapterResult
from test_lb_pipeline import Env, env, lbtrack            # noqa: F401
from test_web_lb_api import Api, api                       # noqa: F401

MBID = "11111111-2222-3333-4444-555555555555"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 30


def ad_flac(path: Path) -> Path:
    """一个「被塞了广告」的网盘文件：歌手/歌名对，但注释、歌词、网址、封面都夹带广告。"""
    make_flac(path, "周杰伦", "晴天", seconds=200, extra={"comment": ["加微信 xxx 领福利"], "lyrics": ["广告歌词"],
                                                       "website": ["http://ad.example"], "album": ["原专辑"]})
    f = FLAC(str(path))
    pic = Picture(); pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "ad", JPEG
    f.add_picture(pic); f.save()
    return path


def hold(env, name="周杰伦 - 晴天.flac", cover=True, folder=False):
    """造一条 REVIEW 状态的条目：暂存文件在 <目标目录>/.review/，对照数据按适配器的格式存好。"""
    out = Path(env.settings["dirs"]["output" if folder else "discovery"])
    held = ad_flac(out / ".review" / name)
    cp = ""
    if cover:
        cp = str(held.with_name(held.name + ".cover")); Path(cp).write_bytes(b"\xff\xd8\xff\xdbNEWCOVER")
    bid = lb_repo.create_batch(env.db, "weekly_jams", {"mbid": "x", "title": "t", "date": "2026-11-20T12:00:00+00:00"},
                               {"tracks": [lbtrack(1, "周杰伦", "晴天", mbid=MBID)]}, env.now)
    item = lb_repo.items_of_batch(env.db, bid)[0]["id"]
    lb_repo.set_state(env.db, item, ST.DOWNLOADING)
    data = {"original": lb_review.describe(held),
            "proposed": {"artist": "周杰伦", "artists": ["周杰伦"], "title": "晴天", "album": "叶惠美", "date": "2003-07-31",
                         "lyric": "[00:01.00]真歌词", "recording_mbid": MBID, "release_mbid": "r1", "has_cover": bool(cover)},
            "cover_path": cp}
    lb_repo.set_state(env.db, item, ST.REVIEW, local_path=str(held))
    lb_repo.save_review(env.db, item, str(held), cp, data)
    return item, held, Path(cp) if cp else None


def run(env, item, choice, folder=False):
    if folder:
        env.settings["mode"] = "folder"
    with env.tm.run("review", "manual") as h:
        lb_review.run_review(lb_review.ReviewDeps(env.db, env.settings, h, item, choice, now=lambda: env.now))
    return env.tm.recent_runs()[0]


# ---------------------------------------------------------------- 读：原有标签如实列出（含广告）
def test_describe_lists_every_text_tag_and_picture(tmp_path):
    d = lb_review.describe(ad_flac(tmp_path / "a.flac"))
    tags = dict(d["tags"])
    assert tags["comment"] == "加微信 xxx 领福利" and tags["lyrics"] == "广告歌词" and tags["website"] == "http://ad.example"
    assert len(d["pictures"]) == 1 and d["pictures"][0]["size"] == len(JPEG) and d["length"] > 100


def test_describe_truncates_long_values(tmp_path):
    p = make_flac(tmp_path / "a.flac", "A", "T", extra={"lyrics": ["x" * 5000]})
    v = dict(lb_review.describe(p)["tags"])["lyrics"]
    assert len(v) < 400 and "共 5000 字" in v


# ---------------------------------------------------------------- 选「用推荐的」：清空再写白名单
def test_use_proposed_wipes_ads_and_writes_only_the_whitelist(env):             # noqa: F811
    item, held, cover = hold(env)
    run(env, item, "use_proposed")
    final = Path(env.settings["dirs"]["discovery"]) / held.name
    f = FLAC(str(final))
    assert not held.exists() and not cover.exists() and final.is_file()
    assert set(k.lower() for k in f.keys()) == {"artist", "title", "album", "date", "year", "lyrics", "musicbrainz_trackid", "musicbrainz_albumid"}
    assert f["album"] == ["叶惠美"] and f["lyrics"] == ["[00:01.00]真歌词"] and f["musicbrainz_trackid"] == [MBID]
    assert len(f.pictures) == 1 and f.pictures[0].data == b"\xff\xd8\xff\xdbNEWCOVER"               # 只有推荐的封面，没有原来的广告图
    it = lb_repo.get_item(env.db, item)
    assert it["state"] == ST.LOCAL_DISCOVERY and it["local_path"] == str(final) and it["entered_discovery_at"]
    assert lb_repo.get_review(env.db, item) is None


def test_use_proposed_without_a_cover_leaves_no_picture(env):                    # noqa: F811
    item, held, _ = hold(env, cover=False)
    run(env, item, "use_proposed")
    assert FLAC(str(Path(env.settings["dirs"]["discovery"]) / held.name)).pictures == []            # 宁可没有封面，也不留原来的


# ---------------------------------------------------------------- 选「保留原标签」
def test_keep_original_leaves_existing_tags_and_only_fills_missing(env):         # noqa: F811
    item, held, cover = hold(env)
    f = FLAC(str(held)); del f["album"]; f.save()                                  # 原文件没有专辑
    run(env, item, "keep_original")
    g = FLAC(str(Path(env.settings["dirs"]["discovery"]) / held.name))
    assert g["comment"] == ["加微信 xxx 领福利"] and len(g.pictures) == 1 and g.pictures[0].data == JPEG   # 你看过了：原样保留
    assert g["album"] == ["叶惠美"] and g["musicbrainz_trackid"] == [MBID]                             # 只补缺的
    assert lb_repo.get_item(env.db, item)["state"] == ST.LOCAL_DISCOVERY and not cover.exists()


# ---------------------------------------------------------------- 选「放弃」
def test_reject_deletes_files_and_puts_the_item_in_cooldown(env):                # noqa: F811
    item, held, cover = hold(env)
    run(env, item, "reject")
    it = lb_repo.get_item(env.db, item)
    assert not held.exists() and not cover.exists() and not list(Path(env.settings["dirs"]["discovery"]).glob("*.flac"))
    assert it["state"] == ST.NOT_FOUND and it["reason_kind"] == "rejected" and it["next_retry_at"]
    assert lb_repo.get_review(env.db, item) is None


# ---------------------------------------------------------------- 下载目录模式
def test_folder_mode_ends_in_downloaded_in_the_output_dir(env):                  # noqa: F811
    env.settings["dirs"]["output"] = str(Path(env.settings["dirs"]["discovery"]).parent / "out")
    item, held, _ = hold(env, folder=True)
    env.settings["mode"] = "folder"
    run(env, item, "use_proposed")
    it = lb_repo.get_item(env.db, item)
    assert it["state"] == ST.DOWNLOADED and Path(it["local_path"]).parent == Path(env.settings["dirs"]["output"]) and Path(it["local_path"]).is_file()


# ---------------------------------------------------------------- 校验与保护
def test_validate_rejects_wrong_state_choice_and_missing_file(env):              # noqa: F811
    item, held, _ = hold(env)
    with pytest.raises(ValueError):
        lb_review.validate(env.db, item, "nope")
    held.unlink()
    with pytest.raises(ValueError, match="暂存文件"):
        lb_review.validate(env.db, item, "use_proposed")
    lb_review.validate(env.db, item, "reject")                                     # 文件没了也能放弃
    lb_repo.set_state(env.db, item, ST.NOT_FOUND)
    with pytest.raises(ValueError, match="待审核"):
        lb_review.validate(env.db, item, "reject")


def test_existing_target_is_never_overwritten_and_the_item_stays_in_review(env):  # noqa: F811
    item, held, cover = hold(env)
    clash = Path(env.settings["dirs"]["discovery"]) / held.name
    clash.write_bytes(b"someone else's file")
    with pytest.raises(ValueError, match="同名"):
        run(env, item, "keep_original")
    assert clash.read_bytes() == b"someone else's file" and held.exists()
    assert lb_repo.get_item(env.db, item)["state"] == ST.REVIEW and lb_repo.get_review(env.db, item)


# ---------------------------------------------------------------- 流水线：下载后进入 REVIEW，不会被重复下载
def test_pipeline_parks_review_results_and_does_not_redownload(env):             # noqa: F811
    held = ad_flac(Path(env.settings["dirs"]["discovery"]) / ".review" / "A - Song 1.flac")
    review = {"original": lb_review.describe(held), "proposed": {"artist": "A", "title": "Song 1"}, "cover_path": ""}
    env.adapter.script = {"Song 1": AdapterResult(True, str(held), quality="lossless", file_type="flac", review=review)}
    env.lb.add("weekly_jams", "pl", [lbtrack(1)])
    run1 = env.sync()
    it = lb_repo.items_of_batch(env.db, 1)[0]
    assert it["state"] == ST.REVIEW and it["local_path"] == str(held) and lb_repo.get_review(env.db, it["id"])
    assert json.loads(run1["stats_json"]).get("to_review") == 1
    calls = len(env.adapter.calls)
    env.sync()
    assert len(env.adapter.calls) == calls and lb_repo.items_of_batch(env.db, 1)[0]["state"] == ST.REVIEW


# ---------------------------------------------------------------- 页面接口
def test_review_detail_and_todo_and_http_flow(api):                              # noqa: F811
    db = api.app.store.db
    bid = lb_repo.create_batch(db, "weekly_jams", {"mbid": "p", "title": "t", "date": "2026-11-20T12:00:00+00:00"},
                               {"tracks": [lbtrack(1, "周杰伦", "晴天", mbid=MBID)]})
    item = lb_repo.items_of_batch(db, bid)[0]["id"]
    held = ad_flac(api.disc / ".review" / "周杰伦 - 晴天.flac")
    lb_repo.set_state(db, item, ST.DOWNLOADING); lb_repo.set_state(db, item, ST.REVIEW, local_path=str(held))
    lb_repo.save_review(db, item, str(held), "", {"original": lb_review.describe(held),
                        "proposed": {"artist": "周杰伦", "artists": ["周杰伦"], "title": "晴天", "album": "叶惠美"}, "cover_path": ""})
    t = api.get("/api/lb/todo").json()
    assert t["review"] == 1 and t["total"] >= 1 and t["review_batch"] == {"id": bid, "type": "weekly_jams"}
    d = api.get(f"/api/lb/review?item={item}").json()
    assert d["item"]["title"] == "晴天" and d["exists"] and dict(d["original"]["tags"])["comment"].startswith("加微信")
    assert api.get("/api/lb/review?item=99999").status_code == 404
    assert api.post("/api/lb/action", {"kind": "review", "item_id": item, "choice": "bogus"}).status_code == 400
    assert api.post("/api/lb/action", {"kind": "review", "item_id": item, "choice": "use_proposed"}).status_code == 200
    api.wait_idle()
    assert lb_repo.get_item(db, item)["state"] == ST.LOCAL_DISCOVERY
    assert api.get("/api/lb/todo").json()["review"] == 0
    assert api.post("/api/lb/action", {"kind": "review", "item_id": item, "choice": "reject"}).status_code == 400   # 已处理过的不能再审核


# ---------------------------------------------------------------- 中文统一成简体（ListenBrainz / MusicBrainz 里的中文多是繁体）
def test_simplify_converts_chinese_but_leaves_japanese_and_korean_alone():
    from zh import simplify
    assert simplify("羅大佑") == "罗大佑" and simplify("亞細亞的孤兒") == "亚细亚的孤儿" and simplify("Adele - 21") == "Adele - 21"
    assert simplify("") == "" and simplify(None) is None
    assert simplify("宇多田ヒカル 初恋 國") == "宇多田ヒカル 初恋 國"            # 含假名：日文汉字不能当繁体转
    assert simplify("방탄소년단 體") == "방탄소년단 體"


def test_proposal_is_simplified_in_the_adapter_and_the_staged_file_name(tmp_path):
    from openlist_channel import OpenListAdapter
    from test_openlist import FakeSession, flac_bytes
    from openlist_client import OpenListClient
    s = FakeSession({"/音乐/罗大佑/罗大佑 - 亚细亚的孤儿.flac": flac_bytes(tmp_path)})
    a = OpenListAdapter(OpenListClient("http://ol", "u", "pw", session=s), "/音乐", sleep=lambda x: None)
    r = a.download_song("羅大佑", "亞細亞的孤兒", tmp_path / "o", artists=["羅大佑"], album="之乎者也", recording_mbid=MBID)
    p = r.review["proposed"]
    assert (p["artist"], p["artists"], p["title"], p["album"]) == ("罗大佑", ["罗大佑"], "亚细亚的孤儿", "之乎者也")
    assert Path(r.path).name == "罗大佑 - 亚细亚的孤儿.flac"


def test_use_proposed_writes_simplified_even_for_an_older_traditional_proposal(env):   # noqa: F811
    item, held, _ = hold(env)
    data = json.loads(lb_repo.get_review(env.db, item)["data_json"])
    data["proposed"].update(artist="羅大佑", artists=["羅大佑"], title="亞細亞的孤兒", album="之乎者也", lyric="[00:01.00]愛情")
    lb_repo.save_review(env.db, item, str(held), "", data)
    assert lb_queries.review_detail(env.db, item)["proposed"]["title"] == "亚细亚的孤儿"          # 对话框里看到的
    run(env, item, "use_proposed")
    f = FLAC(str(Path(env.settings["dirs"]["discovery"]) / held.name))
    assert f["artist"] == ["罗大佑"] and f["title"] == ["亚细亚的孤儿"] and f["album"] == ["之乎者也"] and f["lyrics"] == ["[00:01.00]爱情"]


def test_keep_original_fills_missing_tags_with_simplified_names(env):                        # noqa: F811
    item, held, _ = hold(env)
    f = FLAC(str(held)); del f["album"]; f.save()
    data = json.loads(lb_repo.get_review(env.db, item)["data_json"])
    data["proposed"].update(album="之乎者也專輯")
    lb_repo.save_review(env.db, item, str(held), "", data)
    run(env, item, "keep_original")
    assert FLAC(str(Path(env.settings["dirs"]["discovery"]) / held.name))["album"] == ["之乎者也专辑"]
