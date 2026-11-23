"""ListenBrainz 推荐功能的设置：默认值、校验、对外视图（不含密码）。

存储键 `lb.settings`（见 settings_store.py）。凭据（LB Token、Navidrome 密码）明文保存在
权限 600 的数据库里，接口永不回显：视图里只给 has_xxx，保存时留空表示"不修改"。
"""

import copy
import posixpath
import re
from typing import Any, Dict, Optional

SETTINGS_KEY = "lb.settings"
# 推荐「类型」= 一个来源提供的一种推荐列表；批次表 recommendation_batches.type 存的就是这里的 key
LB_TYPES = ("daily", "weekly_exploration", "weekly_jams")
NETEASE_DAILY = "netease_daily"          # 音乐平台自己的每日推荐（key 保持内部名，页面上不出现具体平台名）
REC_TYPES = LB_TYPES + (NETEASE_DAILY,)
# 推荐来源注册表：设置里选一个来源，「推荐」页就只显示它的列表。以后加来源只需在这里登记 + 实现取歌单（lb_pipeline.fetch_new_batches）
SOURCES = {
    "listenbrainz": {"label": "ListenBrainz", "types": LB_TYPES,
                     "desc": "ListenBrainz 按你的听歌记录生成的每日熟悉 / 每周熟悉 / 每周探新"},
    "netease": {"label": "音乐平台每日推荐", "types": (NETEASE_DAILY,),
                "desc": "音乐平台账号里的「每日推荐」（每天约 30 首）"},
}
DEFAULT_SOURCE = "netease"                # 全新安装的默认：音乐平台每日推荐（升级前已保存过设置的老用户见 load()，保持原来的 ListenBrainz）

# 下载渠道注册表：把「艺人+歌名（可带平台歌曲 id）」变成目录里的一个音频文件。实现与约定见 download_channels.py
CHANNELS = {
    "platform": {"label": "音乐平台", "desc": "在音乐平台上找到并下载无损（需要 VIP 账号，或借用 VIP 账号），自动写好标签和封面"},
}
DEFAULT_CHANNEL = "platform"

# 下载方式：navidrome = 与 Navidrome 对接（试听库 → 歌单 → 点红心转正 → 过期清理）；folder = 只按推荐下载到指定目录
MODES = {"navidrome": "与 Navidrome 联动", "folder": "仅下载到指定目录"}
DEFAULT_MODE = "folder"                  # 全新安装的默认：只下载到目录，不连接 Navidrome（老用户同上，保持联动）


def types_of(source: str) -> tuple:
    return SOURCES.get(source, SOURCES[DEFAULT_SOURCE])["types"]


# 页面/日志/通知里显示的中文名（≤6 字）、来源原名与含义说明
REC_LABELS = {"daily": "每日熟悉", "weekly_exploration": "每周探新", "weekly_jams": "每周熟悉", NETEASE_DAILY: "每日推荐"}
REC_EN = {"daily": "Daily Jams", "weekly_exploration": "Weekly Exploration", "weekly_jams": "Weekly Jams",
          NETEASE_DAILY: "Daily Recommend"}
REC_DESCS = {
    NETEASE_DAILY: "音乐平台每天为你挑的歌，每日约 30 首；按曲目 id 直接下载，不需要搜索匹配",
    "daily": "每日更新，顺着你熟悉的口味延续：同风格、听着舒服的歌",
    "weekly_exploration": "每周一刷新，主打完全陌生的新艺人：探索新口味",
    "weekly_jams": "每周更新，顺着你熟悉的口味延续",
}
# Navidrome 歌单名模板：{date} 会替换为 ListenBrainz 歌单日期（如 2026-11-20）
DEFAULT_PLAYLIST_NAMES = {"daily": "LB Daily Jams {date}", "weekly_exploration": "LB Weekly Exploration {date}",
                          "weekly_jams": "LB Weekly Jams {date}", NETEASE_DAILY: "每日推荐 {date}"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
STAGING_DIRNAME = ".staging"   # discovery 内的下载暂存目录（Navidrome 忽略以 . 开头的目录）
TRASH_DIRNAME = ".trash"       # discovery 内的软删除目录


def defaults() -> Dict[str, Any]:
    return {
        # 获取推荐后是否自动下载。默认关：推荐是主、下载是辅——只获取推荐歌单并与本地比对，要下载的在推荐歌单里手动点「下载」
        # （升级前已保存过设置的安装保持原来的「自动下载」，见 load()）
        "auto_download": False,
        "mode": DEFAULT_MODE,       # 下载方式（MODES 的 key）：folder 时不连接 Navidrome，没有试听库/转正/清理/红心同步
        "channel": DEFAULT_CHANNEL, # 下载渠道（CHANNELS 的 key）
        "source": DEFAULT_SOURCE,   # 推荐来源（SOURCES 的 key）：决定「推荐」页显示哪些列表、定时同步哪些
        "listenbrainz": {"username": "", "token": ""},
        "navidrome": {"url": "", "user": "", "password": "",
                      "main_folder_id": None, "discovery_folder_id": None},
        # 容器内路径（只写容器路径，不写宿主机路径）
        "dirs": {"main": "/music/songs", "discovery": "/music/discovery",
                 "output": "/app/downloads"},    # output：仅下载模式的下载目录
        "strategy": {
            "daily": {"enabled": True, "max_new": 0},                 # 0 = 不限
            "weekly_exploration": {"enabled": True, "max_new": 30},
            "weekly_jams": {"enabled": True, "max_new": 30},
            NETEASE_DAILY: {"enabled": True, "max_new": 0},           # enabled = 每天定时自动获取并下载
        },
        # 转正进主库时的命名：多歌手连接符。默认「, 」（社区通行，且与用户库中较早期目录一致）；
        # 填「 / 」会精确复现 MoviePilot 现在的写法（再换成全角：`A ／ B`）
        "naming": {"artist_separator": ", "},
        # 在 Navidrome 里生成的歌单名（每类一个模板，{date} = 歌单日期）；只有我们自己建的歌单会被改名/重建
        "playlist_names": dict(DEFAULT_PLAYLIST_NAMES),
        "discovery": {"retention_days": 7, "auto_promote": True, "auto_cleanup": True,
                      "trash_days": 7, "playlist_keep_days": 30},
        "retry": {"not_found_cooldown_days": 7, "download_max_retries": 3},
        # 红心同步（Navidrome 红心 → ListenBrainz love）：会真实写入 ListenBrainz，默认关闭；只增不减
        "love": {"enabled": False, "max_per_run": 100},
        # 时间均按容器 TZ 环境变量解释
        "schedule": {"daily_time": "08:30", "weekly_weekday": 0, "weekly_time": "10:00",
                     "promote_interval_min": 30, "love_interval_min": 60, "cleanup_time": "04:00",
                     "netease_daily_time": "07:30"},     # 音乐平台每日推荐约 6 点刷新
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


# 升级前就已保存过设置的安装：当时只有 ListenBrainz + Navidrome 联动这一种玩法，没有 source/mode 这两个键。
# 它们不能被新的默认值悄悄改成「音乐平台 + 仅下载」，否则升级后原有的推荐联动会无声地停掉。
LEGACY_SOURCE = "listenbrainz"
LEGACY_MODE = "navidrome"


def load(raw: Any) -> Dict[str, Any]:
    s = _merge(defaults(), raw)
    if isinstance(raw, dict) and raw:
        if "source" not in raw:
            s["source"] = LEGACY_SOURCE
        if "mode" not in raw:
            s["mode"] = LEGACY_MODE
        if "auto_download" not in raw:
            s["auto_download"] = True       # 旧版推荐同步总是顺带下载
        if raw.get("enabled") is False:
            # 旧版有一个「推荐联动总开关」，关着时同步/转正/清理（会删文件）/红心同步一律不跑。新版去掉了这个开关（选了推荐来源就是要用），
            # 为了让「原本关着」的安装升级后仍然什么都不自动做：把会自动运行的各项关掉（页面上可见，需要时自己勾回来）。
            for t in s["strategy"].values():
                t["enabled"] = False
            s["discovery"]["auto_promote"] = False
            s["discovery"]["auto_cleanup"] = False
            s["love"]["enabled"] = False
    return s


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


def _playlist_name(v: Any, name: str) -> str:
    """歌单名模板：非空、≤60 字符、无控制字符；花括号只允许 {date}。"""
    v = _str(v, name, 60)
    if not v:
        raise ValueError(f"{name}不能为空")
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ValueError(f"{name}不能包含控制字符")
    if re.sub(r"\{date\}", "", v).count("{") or re.sub(r"\{date\}", "", v).count("}"):
        raise ValueError(f"{name}里只能使用 {{date}} 这一个占位符")
    return v


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
    if "auto_download" in payload:
        s["auto_download"] = _bool(payload["auto_download"])
    if "mode" in payload:
        m = _str(payload["mode"], "下载方式", 32)
        if m not in MODES:
            raise ValueError("未知的下载方式")
        s["mode"] = m
    if "channel" in payload:
        c = _str(payload["channel"], "下载渠道", 32)
        if c not in CHANNELS:
            raise ValueError("未知的下载渠道")
        s["channel"] = c
    if "source" in payload:
        src = _str(payload["source"], "推荐来源", 32)
        if src not in SOURCES:
            raise ValueError("未知的推荐来源")
        s["source"] = src

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
        if "output" in p:
            d["output"] = _abs_dir(p["output"], "下载目录")
        if _nested(d["main"], d["discovery"]):
            raise ValueError("主库目录与试听库目录不能相同或互相包含")

    p = payload.get("naming")
    if isinstance(p, dict) and "artist_separator" in p:
        s["naming"]["artist_separator"] = _artist_sep(p["artist_separator"])

    p = payload.get("playlist_names")
    if isinstance(p, dict):
        for t in REC_TYPES:
            if t in p:
                s["playlist_names"][t] = _playlist_name(p[t], f"{REC_LABELS[t]}歌单名")

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
        for k, label in (("daily_time", "每日同步时间"), ("weekly_time", "每周同步时间"), ("cleanup_time", "清理时间"),
                         ("netease_daily_time", "每日推荐同步时间")):
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
    # rec_types = 当前来源的列表（页面的设置项与「推荐歌单」按钮都只用它）；sources = 可选的来源
    v["rec_types"] = [{"key": t, "label": REC_LABELS[t], "en": REC_EN[t], "desc": REC_DESCS[t]} for t in types_of(s["source"])]
    v["sources"] = [{"key": k, "label": x["label"], "desc": x["desc"]} for k, x in SOURCES.items()]
    v["channels"] = [{"key": k, "label": x["label"], "desc": x["desc"]} for k, x in CHANNELS.items()]
    v["modes"] = [{"key": k, "label": x} for k, x in MODES.items()]
    return v
