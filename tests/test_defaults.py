"""全新安装的默认值（音乐平台每日推荐 + 仅下载到目录），以及升级前已保存的设置必须保持原来的行为。"""
import lb_settings
from db import Database
from lb_service import LbService
from settings_store import SettingsStore
from task_manager import TaskManager


def test_fresh_install_defaults_to_platform_daily_recommend_and_folder_download():
    for s in (lb_settings.defaults(), lb_settings.load(None), lb_settings.load({})):
        assert (s["source"], s["mode"], s["channel"]) == ("netease", "folder", "platform")
        assert "enabled" not in s                                    # 没有总开关：选了推荐来源就是要用
        assert s["strategy"]["netease_daily"]["enabled"] is True     # 每天自动获取并下载
        assert s["love"]["enabled"] is False                         # 写 ListenBrainz 的功能仍须明确打开
    v = lb_settings.view(lb_settings.load(None), "UTC")
    assert [t["key"] for t in v["rec_types"]] == ["netease_daily"] and "enabled" not in v


def test_settings_saved_before_the_upgrade_keep_listenbrainz_and_navidrome():
    """升级前保存的设置里没有 source / mode：绝不能被新默认值悄悄改成音乐平台 + 仅下载，否则原有联动会无声停掉。"""
    old = {"enabled": True, "listenbrainz": {"username": "me", "token": "t"},
           "navidrome": {"url": "http://n", "user": "u", "password": "p", "main_folder_id": 1, "discovery_folder_id": 2}}
    s = lb_settings.load(old)
    assert (s["source"], s["mode"]) == ("listenbrainz", "navidrome") and "enabled" not in s
    assert s["navidrome"]["url"] == "http://n" and s["listenbrainz"]["username"] == "me"
    assert s["strategy"]["daily"]["enabled"] and s["discovery"]["auto_cleanup"]        # 原来开着的保持开着


def test_an_install_whose_old_master_switch_was_off_keeps_doing_nothing_automatically():
    """旧版总开关关着 = 同步/转正/清理（会删文件）/红心同步都不自动跑。去掉开关后，这些自动运行项要一并关掉，否则升级后会自己动起来。"""
    s = lb_settings.load({"enabled": False, "listenbrainz": {"username": "me", "token": "t"},
                          "discovery": {"auto_promote": True, "auto_cleanup": True}, "love": {"enabled": True}})
    assert all(t["enabled"] is False for t in s["strategy"].values())
    assert (s["discovery"]["auto_promote"], s["discovery"]["auto_cleanup"], s["love"]["enabled"]) == (False, False, False)
    assert s["listenbrainz"]["username"] == "me"                      # 配置本身不丢
    again = lb_settings.load(lb_settings.apply(s, {"strategy": {"daily": {"enabled": True}}}))   # 之后用户自己勾回来，保存后不会再被关掉
    assert again["strategy"]["daily"]["enabled"] is True and again["strategy"]["weekly_jams"]["enabled"] is False


def test_explicitly_saved_choices_are_respected():
    assert lb_settings.load({"source": "netease", "mode": "folder"})["mode"] == "folder"
    s = lb_settings.load({"source": "listenbrainz", "mode": "navidrome"})
    assert (s["source"], s["mode"]) == ("listenbrainz", "navidrome")
    assert lb_settings.load({"source": "netease", "mode": "navidrome"})["source"] == "netease"


def test_a_save_on_a_fresh_install_persists_both_keys_so_later_loads_never_fall_back_to_legacy():
    saved = lb_settings.apply(lb_settings.load(None), {"love": {"max_per_run": 5}})
    assert saved["source"] == "netease" and saved["mode"] == "folder"
    assert (lb_settings.load(saved)["source"], lb_settings.load(saved)["mode"]) == ("netease", "folder")
    switched = lb_settings.apply(lb_settings.load(None), {"mode": "navidrome", "source": "listenbrainz"})
    assert (lb_settings.load(switched)["source"], lb_settings.load(switched)["mode"]) == ("listenbrainz", "navidrome")


def test_an_enabled_key_in_a_payload_is_ignored_not_an_error():
    s = lb_settings.apply(lb_settings.load(None), {"enabled": False})
    assert "enabled" not in s


def test_the_fresh_default_needs_nothing_to_sync_but_a_download_dir(tmp_path):
    db = Database(tmp_path / "t.sqlite"); db.migrate()
    svc = LbService(db=db, kv=SettingsStore(db), tm=TaskManager(db))
    svc.check_ready("sync", lb_settings.load(None))                   # 不要 ListenBrainz，也不要 Navidrome，也不用先「启用」
    assert svc.dashboard()["setup_issue"] == ""
