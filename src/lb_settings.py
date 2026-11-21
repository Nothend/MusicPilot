"""ListenBrainz 推荐功能的设置：默认值、校验、对外视图（不含密码）。

存储键 `lb.settings`（见 settings_store.py）。凭据（LB Token、Navidrome 密码）明文保存在
权限 600 的数据库里，接口永不回显：视图里只给 has_xxx，保存时留空表示"不修改"。
"""

import copy
import posixpath
import re
from typing import Any, Dict, Optional

SETTINGS_KEY = "lb.settings"
REC_TYPES = ("daily", "weekly_exploration", "weekly_jams")
REC_LABELS = {"daily": "Daily Jams", "weekly_exploration": "Weekly Exploration", "weekly_jams": "Weekly Jams"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
STAGING_DIRNAME = ".staging"   # discovery 内的下载暂存目录（Navidrome 忽略以 . 开头的目录）
TRASH_DIRNAME = ".trash"       # discovery 内的软删除目录


def defaults() -> Dict[str, Any]:
    return {
        "enabled": False,  # 总开关：配置完成前不做任何事（不调度、不下载）
        "listenbrainz": {"username": "", "token": ""},
        "navidrome": {"url": "", "user": "", "password": "",
                      "main_folder_id": None, "discovery_folder_id": None},
        # 容器内路径（只写容器路径，不写宿主机路径）
        "dirs": {"main": "/music/songs", "discovery": "/music/discovery"},
        "strategy": {
            "daily": {"enabled": True, "max_new": 0},                 # 0 = 不限
            "weekly_exploration": {"enabled": True, "max_new": 30},
            "weekly_jams": {"enabled": True, "max_new": 30},
        },
        # 转正进主库时的命名：多歌手连接符。默认「, 」（社区通行，且与用户库中较早期目录一致）；
        # 填「 / 」会精确复现 MoviePilot 现在的写法（再换成全角：`A ／ B`）
        "naming": {"artist_separator": ", "},
        "discovery": {"retention_days": 7, "auto_promote": True, "auto_cleanup": True,
                      "trash_days": 7, "playlist_keep_days": 30},
        "retry": {"not_found_cooldown_days": 7, "download_max_retries": 3},
        # 红心同步（Navidrome 红心 → ListenBrainz love）：会真实写入 ListenBrainz，默认关闭；只增不减
        "love": {"enabled": False, "max_per_run": 100},
        # 时间均按容器 TZ 环境变量解释
        "schedule": {"daily_time": "08:30", "weekly_weekday": 0, "weekly_time": "10:00",
                     "promote_interval_min": 30, "love_interval_min": 60, "cleanup_time": "04:00"},
    }


def _merge(base: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    """把已保存的原始数据并入默认值：只保留已知键（兼容后续新增设置项）。"""
    if not isinstance(raw, dict):
        return base
    for k, v in base.items():
        if k in raw:
            if isinstance(v, dict) and isinstance(raw[k], dict):
                base[k] = _merge(copy.deepcopy(v), raw[k])
            elif not isinstance(v, dict):
                base[k] = raw[k]
    return base


def load(raw: Any) -> Dict[str, Any]:
    return _merge(defaults(), raw)


# ---------------------------------------------------------------- 校验辅助
def _str(v: Any, name: str, maxlen: int = 200) -> str:
    v = "" if v is None else str(v).strip()
    if len(v) > maxlen:
        raise ValueError(f"{name}过长")
    return v


def _int(v: Any, name: str, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name}必须是整数")
    if not lo <= n <= hi:
        raise ValueError(f"{name}范围应为 {lo}-{hi}")
    return n


def _time(v: Any, name: str) -> str:
    v = _str(v, name, 5)
    if not _TIME_RE.match(v):
        raise ValueError(f"{name}格式应为 HH:MM")
    return v


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _abs_dir(v: Any, name: str) -> str:
    p = _str(v, name, 300)
    if not p.startswith("/"):
        raise ValueError(f"{name}必须是容器内的绝对路径（如 /music/songs）")
    if ".." in p.split("/"):
        raise ValueError(f"{name}不能包含 ..")
    p = posixpath.normpath(p)
    if p == "/":
        raise ValueError(f"{name}不能是根目录")
    return p


def _nested(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _artist_sep(v: Any) -> str:
    """多歌手连接符：保留首尾空格（「 & 」与「&」不同），须含可见字符、不超过 10 个字符、无控制字符。
    文件名非法字符（如 /）不在这里拒绝：路径各层最终都经 lb_localfs.safe_component 清洗（/ 会换成全角 ／），
    所以填「 / 」会得到 MoviePilot 现在的写法，且不会产生任何路径穿越。"""
    v = "" if v is None else str(v)
    if not v.strip() or len(v) > 10:
        raise ValueError("多歌手连接符不能为空，且不超过 10 个字符")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError("多歌手连接符不能包含控制字符")
    return v


def _folder_id(v: Any, name: str) -> Optional[int]:
    if v is None or str(v).strip() == "":
        return None
    return _int(v, name, 0, 1_000_000)


# ---------------------------------------------------------------- 保存
def apply(current: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """把页面提交的（可能只含部分分组的）数据并入当前设置并校验，返回新设置；出错抛 ValueError。"""
    s = copy.deepcopy(current)
    if "enabled" in payload:
        s["enabled"] = _bool(payload["enabled"])

    p = payload.get("listenbrainz")
    if isinstance(p, dict):
        if "username" in p:
            s["listenbrainz"]["username"] = _str(p["username"], "ListenBrainz 用户名", 64)
        if p.get("token"):  # 留空 = 不修改
            s["listenbrainz"]["token"] = _str(p["token"], "ListenBrainz Token", 128)

    p = payload.get("navidrome")
    if isinstance(p, dict):
        n = s["navidrome"]
        if "url" in p:
            url = _str(p["url"], "Navidrome 地址", 200).rstrip("/")
            if url and not url.lower().startswith(("http://", "https://")):
                raise ValueError("Navidrome 地址须以 http:// 或 https:// 开头")
            n["url"] = url
        if "user" in p:
            n["user"] = _str(p["user"], "Navidrome 用户名", 64)
        if p.get("password"):
            n["password"] = _str(p["password"], "Navidrome 密码", 128)
        for k, label in (("main_folder_id", "主库"), ("discovery_folder_id", "试听库")):
            if k in p:
                n[k] = _folder_id(p[k], f"Navidrome {label}编号")
        if n["main_folder_id"] is not None and n["main_folder_id"] == n["discovery_folder_id"]:
            raise ValueError("主库与试听库不能是 Navidrome 里的同一个音乐库")

    p = payload.get("dirs")
    if isinstance(p, dict):
        d = s["dirs"]
        for k, label in (("main", "主库目录"), ("discovery", "试听库目录")):
            if k in p:
                d[k] = _abs_dir(p[k], label)
        if _nested(d["main"], d["discovery"]):
            raise ValueError("主库目录与试听库目录不能相同或互相包含")

    p = payload.get("naming")
    if isinstance(p, dict) and "artist_separator" in p:
        s["naming"]["artist_separator"] = _artist_sep(p["artist_separator"])

    p = payload.get("strategy")
    if isinstance(p, dict):
        for t in REC_TYPES:
            if isinstance(p.get(t), dict):
                if "enabled" in p[t]:
                    s["strategy"][t]["enabled"] = _bool(p[t]["enabled"])
                if "max_new" in p[t]:
                    s["strategy"][t]["max_new"] = _int(p[t]["max_new"], f"{REC_LABELS[t]} 最大新下载数", 0, 1000)

    p = payload.get("discovery")
    if isinstance(p, dict):
        d = s["discovery"]
        if "retention_days" in p:
            d["retention_days"] = _int(p["retention_days"], "试听保留天数", 1, 365)
        if "trash_days" in p:
            d["trash_days"] = _int(p["trash_days"], "回收站保留天数", 0, 365)
        if "playlist_keep_days" in p:
            d["playlist_keep_days"] = _int(p["playlist_keep_days"], "歌单保留天数", 1, 365)
        for k in ("auto_promote", "auto_cleanup"):
            if k in p:
                d[k] = _bool(p[k])

    p = payload.get("retry")
    if isinstance(p, dict):
        if "not_found_cooldown_days" in p:
            s["retry"]["not_found_cooldown_days"] = _int(p["not_found_cooldown_days"], "未找到冷却天数", 0, 90)
        if "download_max_retries" in p:
            s["retry"]["download_max_retries"] = _int(p["download_max_retries"], "下载最大重试次数", 0, 10)

    p = payload.get("love")
    if isinstance(p, dict):
        if "enabled" in p:
            s["love"]["enabled"] = _bool(p["enabled"])
        if "max_per_run" in p:
            s["love"]["max_per_run"] = _int(p["max_per_run"], "红心同步每轮上限", 1, 1000)

    p = payload.get("schedule")
    if isinstance(p, dict):
        sc = s["schedule"]
        for k, label in (("daily_time", "每日同步时间"), ("weekly_time", "每周同步时间"), ("cleanup_time", "清理时间")):
            if k in p:
                sc[k] = _time(p[k], label)
        if "weekly_weekday" in p:
            sc["weekly_weekday"] = _int(p["weekly_weekday"], "每周同步日", 0, 6)
        if "promote_interval_min" in p:
            sc["promote_interval_min"] = _int(p["promote_interval_min"], "转正检查间隔", 5, 1440)
        if "love_interval_min" in p:
            sc["love_interval_min"] = _int(p["love_interval_min"], "红心同步间隔", 10, 1440)
    return s


def view(s: Dict[str, Any], tz: str) -> Dict[str, Any]:
    """对外视图：密码/Token 只给 has_xxx，不回显。"""
    v = copy.deepcopy(s)
    v["listenbrainz"] = {"username": s["listenbrainz"]["username"], "has_token": bool(s["listenbrainz"]["token"])}
    n = s["navidrome"]
    v["navidrome"] = {"url": n["url"], "user": n["user"], "has_password": bool(n["password"]),
                      "main_folder_id": n["main_folder_id"], "discovery_folder_id": n["discovery_folder_id"]}
    v["timezone"] = tz
    v["rec_types"] = [{"key": t, "label": REC_LABELS[t]} for t in REC_TYPES]
    return v
