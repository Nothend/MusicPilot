"""测试用的假 HTTP 会话：按调用顺序返回预设响应，并记录请求。"""
import json


class FakeResp:
    def __init__(self, status=200, body=None, headers=None, raw=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.text = raw if raw is not None else (json.dumps(body) if body is not None else "")

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def request(self, method, url, **kw):
        return self._next(method, url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


def _post(self, url, **kw):
    return self._next("POST", url, **kw)


FakeSession.post = _post
