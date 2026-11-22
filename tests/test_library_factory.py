"""音乐库检查器改为直接吃页面设置（不再经过 config.yaml 的 Config 对象）。"""
import pytest

import web_common
from library import MusicTagWebChecker, NavidromeChecker, make_library_checker


def lib(**over):
    d = web_common.default_library()
    d.update(over)
    return d


def test_none_and_missing_settings_mean_no_library_check():
    assert make_library_checker(None) is None
    assert make_library_checker(lib()) is None
    assert web_common.build_library_checker({}) is None                     # 从未保存过：不使用


def test_navidrome_checker_is_built_from_page_settings():
    c = make_library_checker(lib(type="navidrome", navidrome={"host": "nd.local:4533", "user": "u", "password": "p"}))
    assert isinstance(c, NavidromeChecker) and c.base_url == "http://nd.local:4533" and c.username == "u"


def test_https_host_is_not_downgraded():
    c = NavidromeChecker("https://nd.example.com/", "u", "p")
    assert c.base_url == "https://nd.example.com"


def test_use_library_switch_off_wins():
    s = {"use_library": False, "library": lib(type="navidrome", navidrome={"host": "h", "user": "u", "password": "p"})}
    assert web_common.build_library_checker(s) is None


def test_music_tag_web_needs_complete_mysql_settings():
    with pytest.raises(ValueError, match="缺少"):
        MusicTagWebChecker({"host": "h"})


def test_test_library_refuses_none_type():
    with pytest.raises(ValueError, match="无需测试"):
        web_common.test_library(lib())
