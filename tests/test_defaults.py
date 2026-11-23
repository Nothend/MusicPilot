"""全新安装的默认值（音乐平台每日推荐 + 仅下载到目录），以及升级前已保存的设置必须保持原来的 ListenBrainz + Navidrome 联动。"""
import pytest

import lb_settings
from db import Database
from lb_service import LbService, NotReady
from settings_store import SettingsStore
from task_manager import TaskManager


def test_fresh_install_defaults_to_platform_daily_recommend_and_folder_download():
    for s in (lb_settings.defaults(), lb_settings.load(None), lb_settings.load({})):
        assert (s["source"], s["mode"], s["channel"]) == ("netease", "folder", "platform")
        assert s["enabled"] is False                                  # 总开关仍默认关闭：配置完成前不做任何事
    v = lb_settings.view(lb_settings.load(None), "UTC")
    assert [t["key"] for t in v["rec_types"]] == ["netease_daily"]


def test_settings_saved_before_the_upgrade_keep_listenbrainz_and_navidrome():
    """升级前保存的设置里没有 source / mode：绝不能被新默认值悄悄改成音乐平台 + 仅下载，否则原有联动会无声停掉。"""
    old = {"enabled": True, "listenbrainz": {"username": "me", "token": "t"},
           "navidrome": {"url": "http://n", "user": "u", "password": "p", "main_folder_id": 1, "discovery_folder_id": 2}}
    s = lb_settings.load(old)
    assert (s["source"], s["mode"]) == ("listenbrainz", "navidrome") and s["enabled"] is True
    assert s["navidrome"]["url"] == "http://n" and s["listenbrainz"]["username"] == "me"


def test_explicitly_saved_choices_are_respected():
    assert lb_settings.load({"source": "netease", "mode": "folder"})["mode"] == "folder"
    s = lb_settings.load({"source": "listenbrainz", "mode": "navidrome", "enabled": True})
    assert (s["source"], s["mode"]) == ("listenbrainz", "navidrome")
    assert lb_settings.load({"source": "netease", "mode": "navidrome"})["source"] == "netease"


def test_a_save_on_a_fresh_install_persists_both_keys_so_later_loads_never_fall_back_to_legacy():
    saved = lb_settings.apply(lb_settings.load(None), {"enabled": True})
    assert saved["source"] == "netease" and saved["mode"] == "folder"
    assert (lb_settings.load(saved)["source"], lb_settings.load(saved)["mode"]) == ("netease", "folder")
    switched = lb_settings.apply(lb_settings.load(None), {"mode": "navidrome", "source": "listenbrainz"})
    assert (lb_settings.load(switched)["source"], lb_settings.load(switched)["mode"]) == ("listenbrainz", "navidrome")


def test_the_fresh_default_needs_nothing_but_the_master_switch_and_a_download_dir(tmp_path):
    db = Database(tmp_path / "t.sqlite"); db.migrate()
    svc = LbService(db=db, kv=SettingsStore(db), tm=TaskManager(db))
    s = lb_settings.load(None)
    with pytest.raises(NotReady, match="总开关|启用"):
        svc.check_ready("sync", s)                                    # 默认关闭
    s["enabled"] = True
    svc.check_ready("sync", s)                                        # 不要 ListenBrainz，也不要 Navidrome
