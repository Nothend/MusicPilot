"""测试自身的安全网 + Bark 通知的接线。

背景：曾因测试读到真实 config.yaml 里的 BARK_API，每跑一次测试就给用户手机推送一堆通知。
这里保证：① 测试访问不到外部网络；② 程序不再读取任何 config.yaml；③ 通知「该发时发、发给谁、发什么」是对的（用替身验证，不联网）。
"""
import logging
import socket
import threading
from pathlib import Path

import pytest
import requests

import lb_settings
import web_server
from test_lb_pipeline import lbtrack
from test_web_lb_api import Api

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- 网络闸门
def test_network_guard_blocks_and_records_external_hosts(_no_external_network):
    violations = _no_external_network
    with pytest.raises(OSError, match="禁止访问外部网络"):
        socket.getaddrinfo("bark.example.com", 443)
    with pytest.raises(OSError):
        socket.create_connection(("203.0.113.7", 443), timeout=1)             # 数字 IP 不经过 DNS，也要拦
    with pytest.raises(requests.RequestException):
        requests.post("https://bark.example.com/key/", timeout=2)
    assert {"bark.example.com", "203.0.113.7"} <= set(violations)
    violations.clear()                                                         # 这是在「测闸门」，清掉才不会让本测试在收尾时失败


def test_network_guard_blocks_a_raw_connect_that_never_resolves_a_name(_no_external_network):
    """直接对数字 IP connect / connect_ex 不会经过 DNS 解析，必须由 connect 这一道单独拦住。"""
    violations = _no_external_network
    sock = socket.socket()
    try:
        with pytest.raises(OSError, match="禁止访问外部网络"):
            sock.connect(("203.0.113.9", 443))
        with pytest.raises(OSError, match="禁止访问外部网络"):
            sock.connect_ex(("203.0.113.10", 443))
    finally:
        sock.close()
    assert {"203.0.113.9", "203.0.113.10"} <= set(violations)
    violations.clear()


def test_network_guard_still_lets_loopback_through(_no_external_network):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    c = socket.create_connection(s.getsockname(), timeout=2)
    c.close(); s.close()
    assert _no_external_network == []


def test_guard_fails_a_test_even_when_the_offending_code_swallows_the_error(tmp_path):
    """Bark 等代码会吞掉自己的异常。用真正的子进程 pytest 验证：吞掉异常的测试，闸门仍会让它在收尾时失败；老老实实的测试不受影响。"""
    import shutil, subprocess, sys
    shutil.copy(REPO / "tests" / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_swallow.py").write_text(
        "import requests\n"
        "def test_swallows():\n"
        "    try:\n"
        "        requests.post('https://bark.example.com/key/', timeout=2)\n"
        "    except Exception:\n"
        "        pass            # 和 BarkNotifier 一样把异常吞掉\n"
        "def test_clean():\n"
        "    assert 1 + 1 == 2\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", str(tmp_path)],
                       capture_output=True, text=True, cwd=tmp_path)
    # 测试函数体本身「通过」了（异常被吞），但闸门在收尾时把它报成 error，整个运行是失败的
    assert r.returncode != 0 and "1 error" in r.stdout and "2 passed" in r.stdout, r.stdout[-600:]
    assert "ERROR test_swallow.py::test_swallows" in r.stdout and "外部网络" in r.stdout


# ---------------------------------------------------------------- 不再读取 config.yaml
def test_stray_config_yaml_is_never_read(tmp_path, monkeypatch):
    """回归：config.yaml 已整体移除。即使 cwd / 数据目录 / 环境变量指向的位置有一份带 BARK_API 的文件，也绝不能生效。"""
    stray = "BARK_API: https://bark.test/SECRETKEY/\nQUALITY_LEVEL: hires\nREQUEST_DELAY: 9\n"
    for d in (REPO, tmp_path, tmp_path / "data"):
        d.mkdir(exist_ok=True)
    (tmp_path / "config.yaml").write_text(stray, encoding="utf-8")
    (tmp_path / "data" / "config.yaml").write_text(stray, encoding="utf-8")
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.chdir(tmp_path)
    api = Api(tmp_path, monkeypatch)
    try:
        s = api.app.store.get()["settings"]
        assert web_server.effective_bark_api(s) == ""
        assert web_server.effective_quality(s, True) == "lossless"
        assert web_server.effective_request_delay(s) == 0.5
        assert api.get("/api/state").json()["settings"]["bark_api"] == ""
    finally:
        api.close()


def test_app_built_in_tests_has_no_bark_url(tmp_path, monkeypatch):
    api = Api(tmp_path, monkeypatch)
    try:
        assert web_server.effective_bark_api(api.app.store.get()["settings"]) == ""
    finally:
        api.close()


# ---------------------------------------------------------------- Bark 通知接线（替身，不联网）
class FakeRequests:
    """替换 bark 模块里的 requests：记录调用，不联网。"""
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def post(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        if self.fail:
            raise self.fail
        class R:
            def raise_for_status(self): pass
        return R()


def make_api_with_bark(tmp_path, monkeypatch, url="https://bark.test/SECRETKEY/"):
    fake = FakeRequests()
    monkeypatch.setattr("bark.requests", fake)
    api = Api(tmp_path, monkeypatch)
    api.app.store.update(lambda st: st["settings"].update(bark_api=url))      # Bark 地址只来自页面设置
    return api, fake


def test_a_sync_with_real_changes_sends_exactly_one_bark_with_the_summary(tmp_path, monkeypatch):
    api, fake = make_api_with_bark(tmp_path, monkeypatch)
    try:
        api.lb.add("weekly_jams", "pl", [lbtrack(1), lbtrack(2)])
        assert api.post("/api/lb/action", {"kind": "sync"}).status_code == 200
        api.wait_idle(); import time; time.sleep(0.3)
        assert fake.calls == [{"url": "https://bark.test/SECRETKEY/", "params": {"title": "推荐联动：推荐同步", "body": "新下载 2 首"}}]
    finally:
        api.close()


def test_a_run_with_no_changes_sends_no_bark(tmp_path, monkeypatch):
    api, fake = make_api_with_bark(tmp_path, monkeypatch)
    try:
        api.post("/api/lb/action", {"kind": "sync"}); api.wait_idle()               # LB 没有歌单：什么都没发生
        api.post("/api/lb/action", {"kind": "cleanup"}); api.wait_idle()
        import time; time.sleep(0.3)
        assert fake.calls == []
    finally:
        api.close()


def test_a_failed_task_sends_bark_with_the_reason(tmp_path, monkeypatch):
    api, fake = make_api_with_bark(tmp_path, monkeypatch)
    try:
        api.nd.up = False
        api.post("/api/lb/action", {"kind": "sync"}); api.wait_idle()
        import time; time.sleep(0.3)
        assert len(fake.calls) == 1 and fake.calls[0]["params"]["title"] == "推荐联动：推荐同步"
        assert "失败" in fake.calls[0]["params"]["body"] and "无法连接" in fake.calls[0]["params"]["body"]
    finally:
        api.close()


def test_a_failing_love_preview_is_reported_as_a_preview_task(tmp_path, monkeypatch):
    """用户收到过的那条：任务类型要写成「红心同步（预览）」，而不是误导成别的。"""
    api, fake = make_api_with_bark(tmp_path, monkeypatch)
    try:
        class Broken:                                   # 缺方法的假 LB：红心预览会失败
            pass
        api.app.lb.lb_factory = lambda s: Broken()
        api.app.lb.nd_factory = lambda s: type("N", (), {"starred_songs": lambda self, f=None: []})()
        api.post("/api/lb/action", {"kind": "love", "dry_run": True}); api.wait_idle()
        import time; time.sleep(0.3)
        assert fake.calls and fake.calls[0]["params"]["title"] == "推荐联动：红心同步（预览）"
        assert "失败" in fake.calls[0]["params"]["body"]
    finally:
        api.close()


def test_bark_failure_log_never_contains_the_secret_key(tmp_path, monkeypatch, caplog):
    from bark import BarkNotifier
    err = requests.ConnectionError("HTTPSConnectionPool(host='bark.test'): url: /SECRETKEY/?title=x")
    monkeypatch.setattr("bark.requests", FakeRequests(fail=err))
    with caplog.at_level(logging.DEBUG):
        assert BarkNotifier("https://bark.test/SECRETKEY/").send_notification("t", "b") is False
    assert "SECRETKEY" not in caplog.text and "ConnectionError" in caplog.text
