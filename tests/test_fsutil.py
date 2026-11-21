import errno
import os
import shutil

import pytest

import fsutil


def write(p, data=b"x" * 100):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_same_filesystem_move_is_a_link(tmp_path):
    src = write(tmp_path / "a" / "s.flac", b"hello")
    dst = tmp_path / "b" / "d.flac"
    assert fsutil.safe_move(src, dst) == "link"
    assert not src.exists() and dst.read_bytes() == b"hello"


def test_never_overwrites_existing_destination(tmp_path):
    src = write(tmp_path / "s.flac", b"new")
    dst = write(tmp_path / "d.flac", b"precious")
    with pytest.raises(FileExistsError):
        fsutil.safe_move(src, dst)
    assert dst.read_bytes() == b"precious" and src.read_bytes() == b"new"


def test_missing_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        fsutil.safe_move(tmp_path / "nope", tmp_path / "d")


def exdev(*a, **k):
    raise OSError(errno.EXDEV, "cross-device")


def test_cross_device_falls_back_to_verified_copy(tmp_path, monkeypatch):
    src = write(tmp_path / "x" / "s.flac", b"data" * 50)
    dst = tmp_path / "y" / "d.flac"
    monkeypatch.setattr(os, "link", exdev)
    assert fsutil.safe_move(src, dst) == "copy"
    assert dst.read_bytes() == b"data" * 50 and not src.exists()
    assert [p.name for p in dst.parent.iterdir()] == ["d.flac"]          # 没有残留临时文件


def test_copy_failure_keeps_source_and_cleans_temp(tmp_path, monkeypatch):
    src = write(tmp_path / "s.flac", b"data" * 50)
    dst = tmp_path / "y" / "d.flac"
    monkeypatch.setattr(os, "link", exdev)
    def short_copy(a, b):
        open(b, "wb").write(b"trunc")
    monkeypatch.setattr(shutil, "copyfile", short_copy)
    with pytest.raises(OSError, match="大小不一致"):
        fsutil.safe_move(src, dst)
    assert src.read_bytes() == b"data" * 50 and not dst.exists()
    assert list(dst.parent.iterdir()) == []


def test_copy_path_still_refuses_to_overwrite_if_dst_appears(tmp_path, monkeypatch):
    src = write(tmp_path / "s.flac", b"new" * 10)
    dst = tmp_path / "d.flac"
    monkeypatch.setattr(os, "link", exdev)
    real = shutil.copyfile
    def racing(a, b):
        real(a, b)
        dst.write_bytes(b"someone else")           # 复制期间目标被别人创建
    monkeypatch.setattr(shutil, "copyfile", racing)
    with pytest.raises(FileExistsError):
        fsutil.safe_move(src, dst)
    assert dst.read_bytes() == b"someone else" and src.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["s.flac", "d.flac"] or \
        sorted(p.name for p in tmp_path.iterdir()) == ["d.flac", "s.flac"]


def test_unrelated_oserror_is_not_swallowed(tmp_path, monkeypatch):
    src = write(tmp_path / "s.flac")
    def boom(*a, **k):
        raise OSError(errno.EIO, "disk error")
    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError) as e:
        fsutil.safe_move(src, tmp_path / "d.flac")
    assert e.value.errno == errno.EIO and src.exists()
