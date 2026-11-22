"""测试配置。

1) src/ 是扁平导入，需把它放进 sys.path。
2) **网络闸门**（autouse）：测试期间任何访问非本机地址的 DNS 解析 / 连接都会被记录并让该测试失败。
   背景：曾有测试创建了真实的 App，App 读到了仓库里真实 config.yaml 的 BARK_API，结果每跑一次测试就给用户手机推送
   一堆通知（config.yaml 现已整体移除，所有设置只在页面/数据库里，不再有「读到真实配置」的途径）。
   Bark 等代码会吞掉自己的异常，所以光靠「抛异常」不够，闸门会在测试结束时再统一断言一次。
"""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


@pytest.fixture(autouse=True)
def _no_external_network(monkeypatch):
    violations = []

    def check(host):
        if host is not None and str(host) not in _LOOPBACK:
            violations.append(str(host))
            raise OSError(f"测试禁止访问外部网络：{host}")

    def getaddrinfo(host, *a, **k):
        check(host)
        return _REAL_GETADDRINFO(host, *a, **k)

    def connect(self, address):
        check(address[0] if isinstance(address, tuple) else address)
        return _REAL_CONNECT(self, address)

    def connect_ex(self, address):
        check(address[0] if isinstance(address, tuple) else address)
        return _REAL_CONNECT_EX(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield violations
    assert not violations, f"测试试图访问外部网络（可能向真实服务发了请求）：{sorted(set(violations))}"
