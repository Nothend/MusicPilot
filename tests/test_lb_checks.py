import os
from pathlib import Path

import pytest

import lb_checks
import lb_settings as S


class FakeLB:
    def __init__(self, valid=True, user="me", lists=None):
        self.valid, self.user, self.lists = valid, user, lists or []

    def validate_token(self):
        return {"valid": self.valid, "user_name": self.user}

    def created_for(self, n):
        return self.lists


class FakeND:
    def __init__(self, folders):
        self.folders = folders

    def ping(self):
        return {"version": "0.64.2"}

    def music_folders(self):
        return self.folders


def settings(**kw):
    s = S.defaults()
    s["listenbrainz"].update(username="me", token="t")
    s["navidrome"].update(url="http://n", user="u", password="p")
    for k, v in kw.items():
        s[k].update(v)
    return s


def test_lb_requires_credentials():
    with pytest.raises(ValueError, match="用户名和 Token"):
        lb_checks.test_listenbrainz(S.defaults())


def test_lb_invalid_token_and_user_mismatch():
    with pytest.raises(ValueError, match="无效"):
        lb_checks.test_listenbrainz(settings(), FakeLB(valid=False))
    with pytest.raises(ValueError, match="不一致"):
        lb_checks.test_listenbrainz(settings(), FakeLB(user="someone"))


def test_lb_success_reports_latest_per_type_and_missing_daily():
    lists = [{"type": "weekly_jams", "title": "WJ new", "date": "2026-09-14T00:00:00"},
             {"type": "weekly_jams", "title": "WJ old", "date": "2026-08-24T00:00:00"},
             {"type": "other", "title": "x", "date": "2026-01-01T00:00:00"}]
    r = lb_checks.test_listenbrainz(settings(), FakeLB(lists=lists))
    assert r["latest"]["weekly_jams"]["title"] == "WJ new"
    assert "每周熟悉 2026-09-14" in r["message"] and "每日熟悉 暂无" in r["message"]


def test_navidrome_warns_about_missing_second_library_and_bad_ids():
    r = lb_checks.test_navidrome(settings(navidrome={"discovery_folder_id": 9}), FakeND([{"id": 1, "name": "M"}]))
    assert any("试听库" in w for w in r["warnings"])
    r = lb_checks.test_navidrome(settings(), FakeND([{"id": 1, "name": "M"}, {"id": 2, "name": "D"}]))
    assert r["warnings"] == [] and r["folders"][1]["id"] == 2


def test_navidrome_requires_credentials():
    with pytest.raises(ValueError):
        lb_checks.test_navidrome(S.defaults())


def test_check_dirs_never_writes_to_main_and_probes_discovery(tmp_path):
    main, disc = tmp_path / "songs", tmp_path / "discovery"
    main.mkdir()
    (main / "keep.flac").write_bytes(b"x")
    before = sorted(os.listdir(main))
    r = lb_checks.check_dirs(settings(dirs={"main": str(main), "discovery": str(disc)}))
    assert r["ok"] is True
    assert sorted(os.listdir(main)) == before                       # 主库没有任何新增
    assert (disc / ".staging").is_dir() and (disc / ".trash").is_dir()
    assert list((disc / ".staging").iterdir()) == []                 # 探针文件已清理
    assert any(c["name"] == "挂载关系" and ("同一挂载点" in c["msg"] or "独立的挂载" in c["msg"]) for c in r["checks"])


def test_check_dirs_missing_main_fails(tmp_path):
    r = lb_checks.check_dirs(settings(dirs={"main": str(tmp_path / "nope"), "discovery": str(tmp_path / "d")}))
    assert r["ok"] is False
    assert not (tmp_path / "nope").exists()                          # 不替用户创建主库目录


# ---------------------------------------------------------------- 挂载关系（Docker 里 st_dev 相同也可能是不同挂载）
MOUNTINFO = r"""
22 1 8:1 / / rw,relatime - ext4 /dev/sda1 rw
40 22 0:50 / /music/songs rw,relatime - ext4 /dev/sdb1 rw
41 22 0:50 /sub /music/discovery rw,relatime - ext4 /dev/sdb1 rw
42 22 8:1 /data /app/data rw,relatime - ext4 /dev/sda1 rw
43 22 0:60 / /mnt/with\040space rw,relatime - ext4 /dev/sdc1 rw
"""


def test_mount_point_picks_the_longest_matching_prefix(tmp_path):
    f = lb_checks._mount_point
    assert f(Path("/music/songs/a/b"), MOUNTINFO) == "/music/songs"
    assert f(Path("/music/discovery"), MOUNTINFO) == "/music/discovery"
    assert f(Path("/music/songs-other"), MOUNTINFO) == "/"                     # 前缀必须按目录边界匹配
    assert f(Path("/app/data/x"), MOUNTINFO) == "/app/data"
    assert f(Path("/mnt/with space/x"), MOUNTINFO) == "/mnt/with space"        # mountinfo 里的空格是 \040


def test_mount_point_unreadable_returns_none(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("no /proc")))
    assert lb_checks._mount_point(Path("/x")) is None


def _dirs(tmp_path, main_mp, disc_mp):
    main, disc = tmp_path / "songs", tmp_path / "discovery"
    main.mkdir()
    info = f"1 0 0:1 / / rw - x x x\n2 1 0:2 / {main_mp} rw - x x x\n3 1 0:3 / {disc_mp} rw - x x x\n"
    return lb_checks.check_dirs(settings(dirs={"main": str(main), "discovery": str(disc)}), mountinfo=info)


def mount_msg(r):
    return next(c["msg"] for c in r["checks"] if c["name"] == "挂载关系")


def test_separate_mounts_are_reported_honestly_even_if_st_dev_matches(tmp_path):
    r = _dirs(tmp_path, str(tmp_path / "songs"), str(tmp_path / "discovery"))       # 两个独立挂载
    assert os.stat(tmp_path / "songs").st_dev == os.stat(tmp_path / "discovery").st_dev      # st_dev 确实相同……
    assert "独立的挂载" in mount_msg(r) and "复制" in mount_msg(r) and "同一挂载点，转正可直接" not in mount_msg(r)
    assert r["ok"] is True


def test_same_mount_is_reported_as_direct_rename(tmp_path):
    r = _dirs(tmp_path, str(tmp_path), str(tmp_path))
    assert mount_msg(r) == "同一挂载点，转正可直接改名移动"


def test_unreadable_mount_info_falls_back_without_overclaiming(tmp_path):
    main, disc = tmp_path / "songs", tmp_path / "discovery"
    main.mkdir()
    r = lb_checks.check_dirs(settings(dirs={"main": str(main), "discovery": str(disc)}), mountinfo="garbage\n")
    assert "无法读取挂载信息" in mount_msg(r) and "不行自动改为复制" in mount_msg(r)
