"""OpenList（AList 分支）API 客户端：登录换 token、搜索、取直链、下载。

只用到这几个接口（token 放在 Authorization 头里，过期会自动重新登录一次）：
  POST /api/auth/login   {username, password}                    → data.token
  POST /api/fs/search    {parent, keywords, scope, page, per_page} → data.content[{parent,name,is_dir,size}]
  POST /api/fs/get       {path}                                  → data.{name,size,raw_url}
  POST /api/fs/list      {path, page, per_page}                  → data.content
搜索依赖 OpenList 里建好的搜索索引（设置 → 索引）；没建索引时接口会报错，这里翻译成可读提示。
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 20
SEARCH_TIMEOUT = 120      # 搜索：Meilisearch 冷缓存时（重启后第一次、索引刚写入后）一次查询可能要一两分钟，不能按普通请求的超时算
CHUNK = 1024 * 256


class OpenListError(Exception):
    """kind：auth 账号/密码错误；index 没有搜索索引；network 连不上；api 其它接口错误。"""

    def __init__(self, message: str, kind: str = "api"):
        super().__init__(message)
        self.kind = kind


class OpenListClient:
    def __init__(self, url: str, username: str, password: str, session: Optional[requests.Session] = None,
                 timeout: int = TIMEOUT):
        self.base = (url or "").strip().rstrip("/")
        self.username, self.password = username, password
        self.timeout = timeout
        self.session = session or requests.Session()
        self._token = ""

    # ------------------------------------------------------------ 基础
    def _login(self) -> None:
        d = self._raw("POST", "/api/auth/login", {"username": self.username, "password": self.password}, auth=False)
        token = (d.get("data") or {}).get("token")
        if not token:
            raise OpenListError("登录失败：没有拿到 token", "auth")
        self._token = token

    def _raw(self, method: str, path: str, body: Optional[dict] = None, auth: bool = True,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        headers = {"Authorization": self._token} if auth and self._token else {}
        t = timeout or self.timeout
        try:
            r = self.session.request(method, self.base + path, json=body, headers=headers, timeout=t)
        except requests.Timeout:
            raise OpenListError(f"OpenList 超过 {t:.0f} 秒没有响应（搜索索引刚重启或还在写入时第一次查询会很慢），稍后会自动重试", "network")
        except requests.RequestException as e:
            raise OpenListError(f"连不上 OpenList（{type(e).__name__}）", "network")
        try:
            d = r.json()
        except ValueError:
            raise OpenListError(f"OpenList 返回了无法解析的内容（HTTP {r.status_code}）", "api")
        d["_http"] = r.status_code
        return d

    def _call(self, path: str, body: dict, timeout: Optional[float] = None) -> Dict[str, Any]:
        if not self._token:
            self._login()
        d = self._raw("POST", path, body, timeout=timeout)
        if d.get("code") == 401 or d["_http"] == 401:        # token 过期：重新登录一次
            self._login()
            d = self._raw("POST", path, body, timeout=timeout)
        code = d.get("code")
        if code != 200:
            msg = str(d.get("message") or f"HTTP {d['_http']}")
            low = msg.lower()
            if "index" in low and ("not" in low or "disabled" in low or "available" in low):
                raise OpenListError("OpenList 没有可用的搜索索引：请先在 OpenList 管理页「设置 → 索引」建好索引", "index")
            if code == 401 or "password" in low or "token" in low:
                raise OpenListError(f"OpenList 授权失败：{msg}", "auth")
            raise OpenListError(f"OpenList 接口错误：{msg}", "api")
        return d.get("data") or {}

    # ------------------------------------------------------------ 接口
    def login(self) -> None:
        """显式登录一次（测试连接用）：账号密码错误会抛 auth。"""
        try:
            self._login()
        except OpenListError as e:
            if e.kind == "api":
                raise OpenListError(f"登录失败：{e}", "auth")
            raise

    def list_dir(self, path: str, per_page: int = 30) -> List[Dict[str, Any]]:
        d = self._call("/api/fs/list", {"path": path, "page": 1, "per_page": per_page, "refresh": False})
        return d.get("content") or []

    def search(self, parent: str, keywords: str, per_page: int = 100) -> List[Dict[str, Any]]:
        """在 parent 目录下搜索，返回文件（不含文件夹）：[{path, name, size}]。"""
        d = self._call("/api/fs/search", {"parent": parent, "keywords": keywords, "scope": 2, "page": 1,
                                         "per_page": per_page, "password": ""}, timeout=SEARCH_TIMEOUT)
        out = []
        for it in d.get("content") or []:
            if it.get("is_dir"):
                continue
            out.append({"path": (it.get("parent") or "").rstrip("/") + "/" + it.get("name", ""),
                        "name": it.get("name", ""), "size": int(it.get("size") or 0)})
        return out

    def get(self, path: str) -> Dict[str, Any]:
        return self._call("/api/fs/get", {"path": path, "password": ""})

    def download(self, path: str, dest: Path) -> int:
        """把网盘文件下载到 dest（先写 .part，大小对得上才改名）。返回字节数。直链失败时退回 OpenList 的 /d 代理。"""
        info = self.get(path)
        want = int(info.get("size") or 0)
        urls = []
        raw = info.get("raw_url") or ""
        if raw:
            urls.append(raw if raw.startswith("http") else self.base + raw)
        from urllib.parse import quote
        urls.append(f"{self.base}/d{quote(path)}")
        last = ""
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".part")
        for u in urls:
            try:
                headers = {"Authorization": self._token} if u.startswith(self.base) else {}
                with self.session.get(u, headers=headers, stream=True, timeout=self.timeout, allow_redirects=True) as r:
                    if r.status_code != 200:
                        last = f"HTTP {r.status_code}"
                        continue
                    n = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(CHUNK):
                            f.write(chunk)
                            n += len(chunk)
                if n <= 0 or (want and n != want):
                    last = f"大小不符（{n} / {want}）"
                    self._rm(tmp)
                    continue
                os.replace(tmp, dest)
                return n
            except (requests.RequestException, OSError) as e:
                last = f"{type(e).__name__}"
                self._rm(tmp)
        raise OpenListError(f"下载失败：{last}", "network")

    @staticmethod
    def _rm(p: Path) -> None:
        try:
            p.unlink()
        except OSError:
            pass
