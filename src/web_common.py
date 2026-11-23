"""Web 控制台的公共辅助：下载目录安全、下载来源解析、设置解析。

只复用现有的 PlatformMusic，不改变其行为。
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from http_client import SESSION
from library import MusicTagWebChecker, NavidromeChecker, make_library_checker
from models import QualityLevel
from web_platform import WebPlatform

logger = logging.getLogger(__name__)

SOURCE_TYPES = ("daily", "today", "playlist", "album")
NON_VIP_QUALITY = "exhigh"   # 非 VIP 账号最高只能拿到 MP3（极高 320k）
MAX_DIR_DEPTH = 3


# ---------------------------------------------------------------- 目录安全

def base_download_dir() -> Path:
    """下载根目录：与 SongDownloader 同一套判定（Docker=/app/downloads，本地=项目根/downloads）。"""
    env = os.environ.get("WEB_DOWNLOAD_DIR")
    if env:
        return Path(env)
    if os.path.exists("/.dockerenv"):
        return Path("/app/downloads")
    return Path(__file__).resolve().parent.parent / "downloads"


def host_download_dir(base: Path) -> str:
    """宿主机上的下载根目录，仅用于页面展示（容器里看不到真实映射路径，靠 HOST_DOWNLOAD_DIR 环境变量告知）。

    未设置时回退为容器内路径。去掉末尾斜杠；只用于显示，不参与任何文件操作。
    """
    host = (os.environ.get("HOST_DOWNLOAD_DIR") or "").strip()
    if len(host) > 1:
        host = host.rstrip("/\\")
    return host or str(base)


def resolve_subdir(base: Path, subdir: Optional[str]) -> Path:
    """把用户给的子目录解析为 base 下的绝对路径；任何越界写法一律拒绝（ValueError）。"""
    base_resolved = base.resolve()
    raw = (subdir or "").replace("\\", "/").strip()
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("请填写相对于下载根目录的子目录，不能是绝对路径")
    sub = raw.strip("/")
    if not sub:
        return base_resolved
    parts = [p for p in sub.split("/") if p]
    for p in parts:
        if p in (".", "..") or "\x00" in p or p != p.strip():
            raise ValueError("子目录名称不合法")
    if len(parts) > MAX_DIR_DEPTH:
        raise ValueError(f"子目录层级不能超过 {MAX_DIR_DEPTH} 层")
    target = base_resolved.joinpath(*parts).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        raise ValueError("子目录必须位于下载目录内")
    return target


def list_subdirs(base: Path) -> List[str]:
    """列出下载根目录下的子目录（相对路径，最多 MAX_DIR_DEPTH 层，跳过隐藏目录与符号链接）。"""
    base_resolved = base.resolve()
    result: List[str] = []

    def walk(current: Path, depth: int) -> None:
        try:
            entries = sorted(e for e in current.iterdir() if e.is_dir() and not e.name.startswith("."))
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            result.append(entry.relative_to(base_resolved).as_posix())
            if depth < MAX_DIR_DEPTH:
                walk(entry, depth + 1)

    walk(base_resolved, 1)
    return result


# ---------------------------------------------------------------- 来源解析

_ID_RE = re.compile(r"(?:[?&]id=|/(?:playlist|album)/)(\d+)")


def parse_platform_id(text: Any) -> Optional[int]:
    """从纯数字 / 分享链接（含 #/playlist?id=xxx、/album?id=xxx）里提取 ID。"""
    s = str(text or "").strip()
    if s.isdigit():
        return int(s)
    m = _ID_RE.search(s)
    return int(m.group(1)) if m else None


def normalize_source(source: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化来源 {type, id, name}。"""
    if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
        raise ValueError("无效的下载来源")
    stype = source["type"]
    out = {"type": stype, "id": None, "name": str(source.get("name") or "").strip()[:60]}
    if stype in ("playlist", "album"):
        sid = parse_platform_id(source.get("id"))
        if not sid:
            raise ValueError("请填写有效的歌单/专辑 ID 或链接")
        out["id"] = sid
    return out


def source_label(source: Dict[str, Any]) -> str:
    if source.get("name"):
        return source["name"]
    return {
        "daily": "每日推荐",
        "today": "今日日期歌单",
        "playlist": f"歌单 {source.get('id')}",
        "album": f"专辑 {source.get('id')}",
    }[source["type"]]


def fetch_tracks(wn: WebPlatform, source: Dict[str, Any], uid: Optional[int]) -> Dict[str, Any]:
    """按来源取歌曲列表，返回 {'name': 来源名称, 'tracks': [...], 'missing': bool}。"""
    stype = source["type"]
    if stype == "daily":
        return {"name": "每日推荐", "tracks": wn.get_daily_recommend(), "missing": False}
    if stype == "today":
        today = datetime.now().strftime("%Y%m%d")
        playlist = wn.api.find_todays_playlist(uid, today)
        if not playlist:
            return {"name": today, "tracks": [], "missing": True}
        detail = wn.api.get_playlist_detail(playlist["id"], wn.cookies)
        return {"name": detail.get("name") or today, "tracks": detail.get("tracks", []), "missing": False}
    if stype == "playlist":
        detail = wn.api.get_playlist_detail(source["id"], wn.cookies)
        return {"name": detail.get("name") or str(source["id"]),
                "tracks": detail.get("tracks", []), "missing": False}
    detail = wn.api.get_album_detail(source["id"], wn.cookies)
    return {"name": detail.get("name") or str(source["id"]),
            "tracks": detail.get("songs", []), "missing": False}


# ---------------------------------------------------------------- 设置解析

# 音乐库设置：类型 + 两个后端各自的连接参数
LIBRARY_TYPES = ("none", "navidrome", "music-tag-web")


def default_library() -> Dict[str, Any]:
    """从未在页面保存过音乐库设置时的默认值：不使用（只检查本地是否已下载）。"""
    return {"type": "none", "navidrome": {"host": "", "user": "", "password": ""},
            "mysql": {"host": "", "port": 3306, "user": "", "password": "", "database": ""}}


def effective_library(settings: Dict[str, Any]) -> Dict[str, Any]:
    lib = settings.get("library")
    return lib if isinstance(lib, dict) else default_library()


def merge_library(current: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """把页面提交的音乐库设置并入当前值并校验。密码留空表示保持原值（页面从不回显密码）。"""
    ltype = payload.get("type", current["type"])
    if ltype not in LIBRARY_TYPES:
        raise ValueError("无效的音乐库类型")
    out = {"type": ltype, "navidrome": dict(current["navidrome"]), "mysql": dict(current["mysql"])}
    for section, keys in (("navidrome", ("host", "user", "password")),
                          ("mysql", ("host", "port", "user", "password", "database"))):
        given = payload.get(section)
        if not isinstance(given, dict):
            continue
        for k in keys:
            if k not in given:
                continue
            v = given[k]
            if k == "password" and (v is None or str(v) == ""):
                continue  # 留空 = 不修改
            if k == "port":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ValueError("MySQL 端口必须是数字")
                if not 1 <= v <= 65535:
                    raise ValueError("MySQL 端口范围应为 1-65535")
            else:
                v = str(v if v is not None else "").strip()
            out[section][k] = v
    if ltype == "navidrome":
        n = out["navidrome"]
        if not n["host"] or not n["user"] or not n["password"]:
            raise ValueError("Navidrome 需要填写地址、用户名和密码")
    elif ltype == "music-tag-web":
        m = out["mysql"]
        if not (m["host"] and m["user"] and m["database"]):
            raise ValueError("music-tag-web 需要填写 MySQL 地址、用户名和数据库名")
    return out


def build_library_checker(settings: Dict[str, Any]):
    """按当前设置创建库检查器；未启用返回 None。连接失败会抛异常，由调用方降级为仅本地去重。"""
    if not settings.get("use_library", True):
        return None
    return make_library_checker(effective_library(settings))


def test_library(lib: Dict[str, Any]) -> str:
    """测试音乐库连接，成功返回说明文字，失败抛 ValueError(原因)。"""
    if lib["type"] == "none":
        raise ValueError("当前选择的是「不使用」，无需测试")
    if lib["type"] == "navidrome":
        n = lib["navidrome"]
        checker = NavidromeChecker(n["host"], n["user"], n["password"])
        if not checker.base_url:
            raise ValueError("Navidrome 地址无效")
        try:
            resp = SESSION.get(f"{checker.base_url}/rest/ping", params=checker._auth_params(), timeout=10)
            resp.raise_for_status()
            sub = resp.json().get("subsonic-response", {})
        except Exception as e:
            raise ValueError(f"无法连接 Navidrome：{e}")
        if sub.get("status") != "ok":
            raise ValueError(f"Navidrome 认证失败：{(sub.get('error') or {}).get('message', '用户名或密码错误')}")
        return "Navidrome 连接成功"
    try:
        checker = MusicTagWebChecker(lib["mysql"])  # 构造即建立连接
        checker.close()
    except Exception as e:
        raise ValueError(f"无法连接 MySQL：{e}")
    return "MySQL 连接成功"


def effective_request_delay(settings: Dict[str, Any]) -> float:
    """每首歌之间的间隔秒数：页面设置，未设置为 0.5。"""
    v = settings.get("request_delay")
    if v is None:
        v = 0.5
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 0.5


def effective_bark_api(settings: Dict[str, Any]) -> str:
    """页面里填的 Bark 地址；未设置或空串都表示不推送。"""
    api = settings.get("bark_api")
    return (api or "").strip()


def parse_account_paste(text: str) -> Dict[str, str]:
    """解析用户粘贴的借用账号内容，返回 cookies 字典。支持三种写法：
    1) 整个 web_state.json（取其中的 account）  2) account 节点 JSON（含 cookies）
    3) 纯 cookie 字符串（MUSIC_U=xxx; os=pc ...）。必须包含 MUSIC_U，否则抛 ValueError。
    """
    import json
    from utils import parse_cookie
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请粘贴借用账号的 account 节点内容或 cookie")
    cookies: Dict[str, str] = {}
    if raw[0] in "{\"":
        # 允许只粘了 "account": {...} 这一段（前面带键名）
        candidate = raw if raw[0] == "{" else "{" + raw.rstrip(",") + "}"
        try:
            data = json.loads(candidate)
        except ValueError:
            raise ValueError("粘贴内容不是合法的 JSON，请检查是否复制完整")
        if isinstance(data, dict) and isinstance(data.get("account"), dict):
            data = data["account"]
        c = data.get("cookies") if isinstance(data, dict) else None
        if isinstance(c, dict):
            cookies = {str(k): str(v) for k, v in c.items() if v not in (None, "")}
        elif isinstance(c, str):
            cookies = parse_cookie(c)
        elif isinstance(data, dict) and "MUSIC_U" in data:
            cookies = {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int)) and v != ""}
    else:
        cookies = parse_cookie(raw)
    if not cookies.get("MUSIC_U"):
        raise ValueError("未找到登录凭证（MUSIC_U），请粘贴 web_state.json 里的 account 节点")
    return cookies


def download_account_of(state: Dict[str, Any]) -> Dict[str, Any]:
    """按设置选出实际用于下载的账号。返回 {kind: 'self'|'borrowed', account: {...}|None}。

    选了借用但借用账号不存在时不静默回退到 self——上层据此让任务失败，避免非 VIP 账号惄惄变成只下 MP3。
    """
    if (state.get("settings") or {}).get("download_source") == "borrowed":
        return {"kind": "borrowed", "account": state.get("download_account")}
    return {"kind": "self", "account": state.get("account")}


def effective_quality(settings: Dict[str, Any], is_vip: bool) -> str:
    """非 VIP 固定 exhigh（只能拿 MP3）；VIP 取页面设置，未设置为 lossless。"""
    if not is_vip:
        return NON_VIP_QUALITY
    quality = settings.get("quality") or "lossless"
    if quality not in {q.value for q in QualityLevel}:
        quality = "lossless"
    return quality
