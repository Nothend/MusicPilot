"""Web 控制台入口：标准库 ThreadingHTTPServer，JSON API + 单页静态页面。

启动：cd src && python web_server.py        （端口 WEB_PORT，默认 8080）
可选访问保护：设置 WEB_PASSWORD 后启用 HTTP Basic 认证（用户名 WEB_USER，默认 admin）。
"""

import base64
import hmac
import io
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import segno

import lb_checks
import lb_manual
import mb_client
import lb_queries
import lb_repo
import lb_settings
from http_client import APIException
from logger import setup_logger
from models import QualityLevel
from web_common import (base_download_dir, download_account_of, effective_bark_api, effective_library,
                        effective_quality, effective_request_delay, fetch_tracks, host_download_dir,
                        list_subdirs, merge_library, normalize_source,
                        parse_account_paste, resolve_subdir, test_library)
from web_jobs import JobManager
from web_platform import (QR_EXPIRED, QR_OK, WebPlatform, qr_check, qr_create, qr_login_url)
from web_scheduler import Scheduler
from lb_scheduler import LbScheduler
from lb_service import LbService
from settings_store import SettingsStore
from task_manager import TaskBusy, TaskManager
from web_store import WebStore, default_data_dir

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web_static"
MAX_BODY = 256 * 1024
QR_TTL = 300  # 秒：服务端保留未完成的扫码会话的时长


class App:
    """应用上下文：持有 store/jobs/scheduler/配置，供请求处理器调用。"""

    def __init__(self, data_dir: Optional[Path] = None, base_dir: Optional[Path] = None):
        data_dir = data_dir or default_data_dir()
        self.store = WebStore(data_dir / "web_state.json")
        self.kv = SettingsStore(self.store.db)  # 推荐功能（lb.*）的设置，与 Web 状态同库
        self.base_dir = base_dir or base_download_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.scheduler: Optional[Scheduler] = None
        # 推荐联动：全局任务锁（与现有下载队列共用）、运行记录/日志写 SQLite，文件日志在数据目录 logs/
        self.tm = TaskManager(self.store.db, log_dir=data_dir / "logs")
        self.jobs = JobManager(self.store, self.base_dir,
                               on_finish=lambda j: self.scheduler and self.scheduler.record_result(j),
                               task_lock=self.tm)
        self.lb = LbService(db=self.store.db, kv=self.kv, tm=self.tm, store=self.store,
                            notify=self._bark_notify,
                            request_delay=lambda: effective_request_delay(self.store.get()["settings"]))
        self.jobs.library_settings = self._library_settings
        self.scheduler = Scheduler(self.store, self.jobs)
        self.lb_scheduler = LbScheduler(self.lb, self.kv)      # 推荐联动的定时器（总开关关闭时什么都不触发）
        self._qr: Dict[str, float] = {}
        self._qr_purpose: Dict[str, str] = {}   # 二维码 key -> "login"(默认) | "borrow"(添加借用账号)
        self._qr_lock = threading.Lock()
        self._lb_lock = threading.Lock()
        self._acct_cache = (0.0, None)  # (时间戳, 账号资料)，避免页面频繁刷新打音乐平台接口

    def _library_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """与 Navidrome 联动开启时，手动下载的「音乐库去重」直接用联动里填的那份 Navidrome 连接，不再让用户填两遍。"""
        s = lb_settings.load(self.kv.get(lb_settings.SETTINGS_KEY))
        n = s["navidrome"]
        if s["mode"] != "navidrome" or not (n["url"] and n["user"] and n["password"]):
            return settings
        lib = effective_library(settings)
        return {**settings, "use_library": True,
                "library": {"type": "navidrome", "navidrome": {"host": n["url"], "user": n["user"], "password": n["password"]},
                            "mysql": lib["mysql"]}}

    def _bark_notify(self, title: str, body: str) -> bool:
        from bark import BarkNotifier
        return BarkNotifier(effective_bark_api(self.store.get()["settings"])).send_notification(title, body)

    def start(self) -> None:
        # 上次进程被杀时遗留的状态：运行记录标为中断、「下载中」的歌复位为待处理
        interrupted = self.tm.recover()
        reset = lb_repo.recover_interrupted(self.store.db)
        if interrupted or reset:
            logger.warning(f"启动善后：{interrupted} 个遗留任务标记为中断，{reset} 首「下载中」的歌复位为待处理")
        self.jobs.start()
        self.scheduler.start()
        self.lb_scheduler.start()

    # ------------------------------------------------------------ 账号
    def wn(self) -> WebPlatform:
        account = self.store.get().get("account")
        if not account or not account.get("cookies"):
            raise PermissionError("尚未登录，请先扫码登录")
        return WebPlatform(account["cookies"])

    def account_view(self, refresh: bool = False) -> Dict[str, Any]:
        account = self.store.get().get("account")
        if not account or not account.get("cookies"):
            return {"logged_in": False}
        ts, cached = self._acct_cache
        if refresh or cached is None or time.time() - ts > 60:
            try:
                info = WebPlatform(account["cookies"]).account_info()
            except Exception as e:
                logger.warning(f"刷新账号信息失败，沿用缓存: {e}")
                info = cached  # 网络抖动不应把用户踢成未登录
                if info is None:
                    info = {k: account.get(k) for k in ("uid", "nickname", "avatar", "vip_type")}
                    info["is_vip"] = bool(account.get("vip_type"))
            else:
                if info is None:
                    self.store.update(lambda s: s["account"].__setitem__("invalid", True) if s.get("account") else None)
                    self._acct_cache = (time.time(), None)
                    return {"logged_in": True, "invalid": True, "nickname": account.get("nickname", "")}
            self._acct_cache = (time.time(), info)
        else:
            info = cached
        settings = self.store.get()["settings"]
        return {
            "logged_in": True, "invalid": False,
            "uid": info["uid"], "nickname": info["nickname"], "avatar": info["avatar"],
            "is_vip": info["is_vip"],
            "quality": effective_quality(settings, info["is_vip"]),
            "mode": "VIP" if info["is_vip"] else "MP3",
        }

    def qr_new(self, purpose: str = "login") -> Dict[str, Any]:
        key = qr_create()
        with self._qr_lock:
            now = time.time()
            self._qr = {k: t for k, t in self._qr.items() if now - t < QR_TTL}
            self._qr_purpose = {k: v for k, v in self._qr_purpose.items() if k in self._qr}
            self._qr[key] = now
            self._qr_purpose[key] = "borrow" if purpose == "borrow" else "login"
        # 输出固定尺寸的 PNG（base64）而非 SVG：segno 的 SVG 不带 viewBox，被 CSS 缩放时图形不会
        # 跟着缩放、右下角会被裁掉，导致 App 扫不出。静默区取标准的 4 格，纠错等级与参考项目一致（L）。
        buf = io.BytesIO()
        segno.make(qr_login_url(key), error="l").save(buf, kind="png", scale=8, border=4)
        return {"key": key, "png": base64.b64encode(buf.getvalue()).decode("ascii")}

    def qr_poll(self, key: str) -> Dict[str, Any]:
        with self._qr_lock:
            known = key in self._qr
        if not known:
            return {"code": QR_EXPIRED, "message": "二维码已过期，请刷新"}
        result = qr_check(key)
        with self._qr_lock:
            purpose = self._qr_purpose.get(key, "login")
        if result["code"] == QR_OK:
            cookies = result["cookies"]
            info = WebPlatform(cookies).account_info()
            if info is None:
                return {"code": 500, "message": "登录成功但无法获取账号信息，请重试"}
            if purpose == "borrow":
                # 借用账号必须是VIP：不是就拒绝，不保存
                if not info["is_vip"]:
                    with self._qr_lock:
                        self._qr.pop(key, None)
                        self._qr_purpose.pop(key, None)
                    return {"code": 500, "message": f"账号「{info['nickname']}」不是VIP，无法作为借用账号"}
                self._save_download_account(cookies, info)
                message = "借用账号已添加"
            else:
                account = {"cookies": cookies, "invalid": False,
                           **{k: info[k] for k in ("uid", "nickname", "avatar", "vip_type")}}
                self.store.update(lambda s: s.__setitem__("account", account))
                self._acct_cache = (time.time(), info)
                message = "登录成功"
            with self._qr_lock:
                self._qr.pop(key, None)
                self._qr_purpose.pop(key, None)
            return {"code": QR_OK, "message": message}
        if result["code"] == QR_EXPIRED:
            with self._qr_lock:
                self._qr.pop(key, None)
                self._qr_purpose.pop(key, None)
        return {"code": result["code"], "message": result["message"]}

    def logout(self) -> None:
        self.store.update(lambda s: s.__setitem__("account", None))
        self._acct_cache = (0.0, None)

    # ------------------------------------------------------------ 借用账号（仅用于下载）
    def _save_download_account(self, cookies: Dict[str, str], info: Dict[str, Any]) -> None:
        def apply(state):
            state["download_account"] = {"cookies": cookies, "invalid": False,
                                         **{k: info[k] for k in ("uid", "nickname", "avatar", "vip_type")}}
            state["settings"]["download_source"] = "borrowed"   # 刚添加就是为了用它
        self.store.update(apply)

    def borrow_import(self, text: str) -> Dict[str, Any]:
        """粘贴导入借用账号。不信任粘贴内容自带的昵称/VIP，一律用 cookie 向音乐平台重新校验；非 VIP 拒绝。"""
        cookies = parse_account_paste(text)
        try:
            info = WebPlatform(cookies).account_info()
        except APIException as e:
            raise ValueError(f"校验借用账号时连接音乐平台失败，请稍后重试：{e}")
        if info is None:
            raise ValueError("该登录凭证已失效或无效，请重新获取")
        if not info["is_vip"]:
            raise ValueError(f"账号「{info['nickname']}」不是VIP，无法作为借用账号")
        self._save_download_account(cookies, info)
        return self.settings_view()

    def borrow_remove(self) -> Dict[str, Any]:
        def apply(state):
            state["download_account"] = None
            state["settings"]["download_source"] = "self"   # 没有借用账号了，回到用自己的
        self.store.update(apply)
        return self.settings_view()

    def download_account_view(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """借用账号概况（绝不返回 cookie）。"""
        acc = state.get("download_account")
        if not acc or not acc.get("cookies"):
            return {"exists": False}
        return {"exists": True, "nickname": acc.get("nickname", ""), "avatar": acc.get("avatar", ""),
                "uid": acc.get("uid"), "invalid": bool(acc.get("invalid"))}

    # ------------------------------------------------------------ 歌曲预览 / 歌单列表
    def preview(self, source: Dict[str, Any]) -> Dict[str, Any]:
        source = normalize_source(source)
        wn = self.wn()
        info = self.account_view()
        if info.get("invalid"):
            raise PermissionError("登录已失效，请重新扫码")
        fetched = fetch_tracks(wn, source, info.get("uid"))
        return {"name": fetched["name"], "missing": fetched["missing"], "tracks": [
            {"id": t["id"], "name": t["name"], "artists": t.get("artists") or [], "album": t.get("album", "")}
            for t in fetched["tracks"]]}

    def playlists(self) -> Dict[str, Any]:
        wn = self.wn()
        info = self.account_view()
        if info.get("invalid"):
            raise PermissionError("登录已失效，请重新扫码")
        items = wn.api.get_user_playlist(info["uid"], wn.cookies)["playlists"]

        def is_date(name: str) -> bool:
            return isinstance(name, str) and len(name) == 8 and name.isdigit()
        # 日期命名的歌单排前面（新的在前），其余保持音乐平台原顺序
        dated = sorted((p for p in items if is_date(p["name"])), key=lambda p: p["name"], reverse=True)
        others = [p for p in items if not is_date(p["name"])]
        return {"playlists": [{"id": p["id"], "name": p["name"], "count": p["track_count"],
                               "dated": is_date(p["name"])} for p in dated + others]}

    # ------------------------------------------------------------ 设置
    def settings_view(self) -> Dict[str, Any]:
        state = self.store.get()
        s = state["settings"]
        bark = effective_bark_api(s)
        lib = effective_library(s)
        use_lib = bool(s.get("use_library", True))
        return {
            "bark_api": bark,
            "download_source": "borrowed" if (s.get("download_source") == "borrowed"
                                              and (state.get("download_account") or {}).get("cookies")) else "self",
            "download_account": self.download_account_view(state),
            "use_library": use_lib,
            "library": {  # 密码绝不回显，只告知是否已设置
                # type 即页面唯一的开关：旧数据里 use_library=false 的，按"不使用"展示（与 build_library_checker 实际行为一致）
                "type": lib["type"] if use_lib else "none",
                "navidrome": {"host": lib["navidrome"]["host"], "user": lib["navidrome"]["user"],
                              "has_password": bool(lib["navidrome"]["password"])},
                "mysql": {"host": lib["mysql"]["host"], "port": lib["mysql"]["port"],
                          "user": lib["mysql"]["user"], "database": lib["mysql"]["database"],
                          "has_password": bool(lib["mysql"]["password"])},
            },
            "quality": s.get("quality") or "",
            "request_delay": effective_request_delay(s),
            "qualities": [q.value for q in QualityLevel],
        }

    def library_test(self, payload: Dict[str, Any]) -> str:
        """用页面当前填写（密码留空则用已保存的）测试连接，不保存。"""
        s = self.store.get()["settings"]
        lib = merge_library(effective_library(s), payload)
        return test_library(lib)

    def settings_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        def apply(state):
            s = state["settings"]
            if "library" in payload and isinstance(payload["library"], dict):
                s["library"] = merge_library(effective_library(s), payload["library"])
                # 页面上只保留"音乐库类型"一个控件（不使用=关闭），保存时把旧的 use_library 总开关复位，
                # 避免旧数据里残留的 false 让选了类型却不生效
                s["use_library"] = True
            if "request_delay" in payload:
                v = payload["request_delay"]
                if v is None or v == "":
                    s["request_delay"] = None
                else:
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        raise ValueError("请求间隔必须是数字")
                    if not 0 <= v <= 60:
                        raise ValueError("请求间隔范围应为 0-60 秒")
                    s["request_delay"] = v
            if "bark_api" in payload:
                value = payload["bark_api"]
                if value is None:
                    s["bark_api"] = None
                else:
                    value = str(value).strip()
                    if value and not value.lower().startswith(("http://", "https://")):
                        raise ValueError("Bark 地址须以 http:// 或 https:// 开头")
                    s["bark_api"] = value
            if "use_library" in payload:
                s["use_library"] = bool(payload["use_library"])
            if "download_source" in payload:
                src = payload["download_source"]
                if src not in ("self", "borrowed"):
                    raise ValueError("无效的下载账号选项")
                if src == "borrowed" and not (state.get("download_account") or {}).get("cookies"):
                    raise ValueError("还没有借用账号，请先扫码添加或粘贴导入")
                s["download_source"] = src
            if "quality" in payload:
                q = payload["quality"] or None
                if q is not None and q not in {x.value for x in QualityLevel}:
                    raise ValueError("无效的音质等级")
                s["quality"] = q
        self.store.update(apply)
        return self.settings_view()

    # ------------------------------------------------------------ 推荐功能（ListenBrainz）设置
    def lb_current(self) -> Dict[str, Any]:
        return lb_settings.load(self.kv.get(lb_settings.SETTINGS_KEY))

    def lb_settings_view(self) -> Dict[str, Any]:
        tz = os.environ.get("TZ") or time.tzname[0]
        return lb_settings.view(self.lb_current(), tz)

    def lb_settings_update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lb_lock:  # 读-改-写串行，避免两次保存互相覆盖
            new = lb_settings.apply(self.lb_current(), payload)
            self.kv.set(lb_settings.SETTINGS_KEY, new)
        return self.lb_settings_view()

    def lb_test(self, target: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """用页面当前填写（密码/Token 留空则沿用已保存的）测试，不保存。"""
        s = lb_settings.apply(self.lb_current(), payload or {})
        if target == "listenbrainz":
            return lb_checks.test_listenbrainz(s)
        if target == "navidrome":
            return lb_checks.test_navidrome(s)
        if target == "dirs":
            return lb_checks.check_dirs(s)
        if target == "output":
            return lb_checks.check_output_dir(s)
        raise ValueError("未知的测试项")

    # ------------------------------------------------------------ 推荐联动：任务与查询
    def lb_action(self, body: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(body.get("kind", ""))
        types = body.get("types")
        if types is not None:
            if not isinstance(types, list) or not all(t in lb_settings.REC_TYPES for t in types):
                raise ValueError("推荐类型不合法")
        params = None
        if kind == "dedup":
            try:
                params = {"item_id": int(body.get("item_id")), "choice": str(body.get("choice", "")),
                          "main_id": str(body.get("main_id") or "")}
            except (TypeError, ValueError):
                raise ValueError("缺少条目编号")
        run_id = self.lb.start(kind, trigger="manual", types=types, scan=bool(body.get("scan")),
                               dry_run=bool(body.get("dry_run")), params=params)
        return {"run_id": run_id}

    def lb_run_detail(self, run_id: int, after: int = 0) -> Dict[str, Any]:
        with self.store.db.lock:
            row = self.store.db.conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ApiError(404, "任务记录不存在")
        run = lb_queries._run_row(row)
        return {"run": run, "logs": self.tm.logs(run_id, after_id=after, limit=1000),
                "running": bool(self.tm.current() and self.tm.current().get("id") == run_id)}

    def test_bark(self) -> bool:
        from bark import BarkNotifier
        api = effective_bark_api(self.store.get()["settings"])
        if not api:
            raise ValueError("尚未设置 Bark 地址")
        return BarkNotifier(api).send_notification("云音乐下载", "Bark 通知测试成功")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message



class Handler(BaseHTTPRequestHandler):
    app: App = None  # 由 serve() 注入
    protocol_version = "HTTP/1.1"
    server_version = "AutoMusicWeb"

    def log_message(self, fmt, *args):  # 静默默认的逐请求 stderr 日志（轮询很频繁）
        logger.debug("%s %s", self.address_string(), fmt % args)

    # ---- 认证 / 输出
    def _authorized(self) -> bool:
        password = os.environ.get("WEB_PASSWORD", "")
        if not password:
            return True
        user = os.environ.get("WEB_USER", "admin")
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                given = base64.b64decode(header[6:]).decode("utf-8")
            except Exception:
                return False
            return hmac.compare_digest(given.encode("utf-8"), f"{user}:{password}".encode("utf-8"))
        return False

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(413, "请求体过大")
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    # ---- 分发
    def _dispatch(self, method: str) -> None:
        if not self._authorized():
            return self._send(401, "需要登录".encode("utf-8"), "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="AutoMusicDown", charset="UTF-8"'})
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                html = (STATIC_DIR / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._json({"error": "Not Found"}, 404)
            if method == "POST":
                # 防 CSRF：跨站表单无法设置 JSON Content-Type
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    raise ApiError(415, "Content-Type 必须为 application/json")
            self._json(self._route(method, path, query))
        except ApiError as e:
            self._json({"error": e.message}, e.status)
        except PermissionError as e:
            self._json({"error": str(e), "need_login": True}, 401)
        except TaskBusy as e:       # 已有任务在跑：不排队，页面提示「任务正在运行」
            self._json({"error": str(e), "busy": True, "running": e.running}, 409)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except APIException as e:  # 音乐平台接口返回错误（如 ID 不存在/风控），属上游问题
            self._json({"error": str(e)}, 502)
        except Exception as e:
            logger.error(f"请求处理异常 {method} {path}: {e}", exc_info=True)
            self._json({"error": f"服务器错误: {e}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


    # ---- 路由
    def _route(self, method: str, path: str, query: Dict[str, Any]) -> Any:
        app = self.app
        q = lambda k: (query.get(k) or [""])[0]
        if method == "GET":
            if path == "/api/state":
                return {"account": app.account_view(refresh=q("refresh") == "1"),
                        "settings": app.settings_view(), "base_dir": str(app.base_dir),
                        "host_dir": host_download_dir(app.base_dir),   # 宿主机路径（仅展示用）
                        "host_dir_set": bool((os.environ.get("HOST_DOWNLOAD_DIR") or "").strip()),
                        "now": time.strftime("%Y-%m-%d %H:%M:%S"), "busy": app.jobs.active()}
            if path == "/api/jobs":
                def num(key, default, lo, hi):
                    try:
                        return max(lo, min(hi, int(q(key) or default)))
                    except ValueError:
                        return default
                trigger = q("trigger") if q("trigger") in ("schedule", "manual") else ""
                return app.jobs.list(limit=num("limit", 30, 1, 1000), offset=num("offset", 0, 0, 100000),
                                     trigger=trigger, failed_only=q("failed") == "1")
            if path == "/api/job":
                job = app.jobs.get(q("id"), detail=True)
                if not job:
                    raise ApiError(404, "任务不存在")
                return job
            if path == "/api/schedules":
                return {"schedules": app.scheduler.list()}
            if path == "/api/playlists":
                return app.playlists()
            if path == "/api/dirs":
                return {"dirs": list_subdirs(app.base_dir)}
            if path == "/api/qr/check":
                return app.qr_poll(q("key"))
            if path == "/api/lb/settings":
                return app.lb_settings_view()
            if path == "/api/lb/dashboard":
                return app.lb.dashboard()
            if path == "/api/lb/todo":
                return lb_queries.todo(app.store.db, app.kv, app.lb.settings())
            if path == "/api/lb/schedule":
                return {"upcoming": app.lb_scheduler.upcoming(), "timezone": os.environ.get("TZ") or time.tzname[0]}
            if path == "/api/lb/batches":
                return {"batches": lb_queries.batches(app.store.db, q("type"))}
            if path == "/api/lb/items":
                try:
                    batch = int(q("batch"))
                except ValueError:
                    raise ValueError("缺少批次编号")
                return lb_queries.items(app.store.db, batch, q("state"))
            if path == "/api/lb/runs":
                return {"runs": lb_queries.runs(app.store.db, 30), "current": app.tm.current()}
            if path == "/api/lb/run":
                try:
                    rid, after = int(q("id")), int(q("after") or 0)
                except ValueError:
                    raise ValueError("参数不合法")
                return app.lb_run_detail(rid, after)
            if path == "/api/lb/cleanup/preview":
                return app.lb.cleanup_preview()
            if path == "/api/lb/love/preview":
                return {"preview": app.lb.love_preview()}
            if path == "/api/lb/love/unresolved":
                return lb_manual.view(app.store.db, app.kv)
            if path == "/api/lb/love/mb_search":
                try:
                    return {"results": mb_client.search_recordings(q("artist"), q("title"))}
                except mb_client.MBError as e:
                    raise ValueError(str(e))
            raise ApiError(404, "Not Found")

        body = self._body()
        if path == "/api/qr/new":
            return app.qr_new(body.get("purpose") or "login")
        if path == "/api/logout":
            app.logout()
            return {"ok": True}
        if path == "/api/borrow/import":
            return app.borrow_import(str(body.get("text") or ""))
        if path == "/api/borrow/remove":
            return app.borrow_remove()
        if path == "/api/preview":
            return app.preview(body.get("source"))
        if path == "/api/download":
            ids = body.get("track_ids")
            if ids is not None and (not isinstance(ids, list) or not ids):
                raise ValueError("请至少选择一首歌曲")
            if not app.store.get().get("account"):
                raise PermissionError("尚未登录，请先扫码登录")
            return app.jobs.submit(body.get("source"), body.get("subdir", ""), track_ids=ids)
        if path == "/api/job/cancel":
            return {"ok": app.jobs.cancel(str(body.get("id", "")))}
        if path == "/api/mkdir":
            target = resolve_subdir(app.base_dir, body.get("path", ""))
            target.mkdir(parents=True, exist_ok=True)
            return {"dirs": list_subdirs(app.base_dir)}
        if path == "/api/settings":
            return app.settings_update(body)
        if path == "/api/bark/test":
            return {"ok": app.test_bark()}
        if path == "/api/library/test":
            return {"ok": True, "message": app.library_test(body.get("library") or {})}
        if path == "/api/lb/settings":
            return app.lb_settings_update(body)
        if path == "/api/lb/action":
            return app.lb_action(body)
        if path == "/api/lb/item":
            try:
                item_id = int(body.get("id"))
            except (TypeError, ValueError):
                raise ValueError("缺少条目编号")
            return lb_queries.item_action(app.store.db, item_id, str(body.get("action", "")), str(body.get("arg", "") or ""))
        if path == "/api/lb/love/resolve":
            return lb_manual.resolve(app.store.db, app.kv, str(body.get("id", "")), mbid=str(body.get("mbid", "") or ""),
                                     ignore=bool(body.get("ignore")))
        if path == "/api/lb/love/undo":
            return lb_manual.undo(app.store.db, str(body.get("na", "")), str(body.get("nt", "")))
        if path == "/api/lb/test":
            return app.lb_test(str(body.get("target", "")), body.get("settings") or {})
        if path == "/api/schedule/save":
            return app.scheduler.upsert(body)
        if path == "/api/schedule/delete":
            return {"ok": app.scheduler.delete(str(body.get("id", "")))}
        if path == "/api/schedule/run":
            return app.scheduler.run_now(str(body.get("id", "")))
        raise ApiError(404, "Not Found")


def serve(host: str, port: int, app: App) -> ThreadingHTTPServer:
    Handler.app = app
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    setup_logger(logging.INFO)
    app = App()
    level = str(os.environ.get("LOG_LEVEL", "INFO")).upper()
    logging.getLogger().setLevel(getattr(logging, level, logging.INFO))
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    app.start()
    server = serve(host, port, app)
    if not os.environ.get("WEB_PASSWORD"):
        logger.warning("未设置 WEB_PASSWORD：页面可操作登录凭证与下载，请仅在可信内网使用")
    logger.info(f"Web 控制台已启动: http://{host}:{port}  下载根目录: {app.base_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.scheduler.stop()
        app.lb_scheduler.stop()
        server.server_close()
        logging.shutdown()


if __name__ == "__main__":
    main()

