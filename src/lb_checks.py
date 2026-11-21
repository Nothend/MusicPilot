"""设置页的「测试连接 / 目录检查」：用页面当前填写的值检查，不保存。

目录检查只会在试听库内写测试文件；主库只做只读/权限检查，绝不写入。
"""

import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from lb_client import ListenBrainzClient, LBError
from lb_settings import REC_LABELS, STAGING_DIRNAME, TRASH_DIRNAME
from nd_client import NavidromeClient, NavidromeError


def test_listenbrainz(s: Dict[str, Any], client: ListenBrainzClient = None) -> Dict[str, Any]:
    lb = s["listenbrainz"]
    if not lb["username"] or not lb["token"]:
        raise ValueError("请先填写 ListenBrainz 用户名和 Token")
    client = client or ListenBrainzClient(lb["token"], lb["username"])
    try:
        info = client.validate_token()
        if not info["valid"]:
            raise ValueError("Token 无效，请检查是否复制完整")
        if info["user_name"].lower() != lb["username"].lower():
            raise ValueError(f"Token 属于用户 {info['user_name']}，与填写的用户名不一致")
        lists = client.created_for(25)
    except LBError as e:
        raise ValueError(str(e))
    latest: Dict[str, Dict[str, str]] = {}
    for p in lists:  # 新的在前，每类只取第一个
        if p["type"] in REC_LABELS and p["type"] not in latest:
            latest[p["type"]] = {"title": p["title"], "date": p["date"][:10]}
    parts = [f"{REC_LABELS[t]} {latest[t]['date']}" if t in latest else f"{REC_LABELS[t]} 暂无"
             for t in REC_LABELS]
    return {"message": f"连接成功（用户 {info['user_name']}）。最近歌单：" + "；".join(parts), "latest": latest}


def test_navidrome(s: Dict[str, Any], client: NavidromeClient = None) -> Dict[str, Any]:
    n = s["navidrome"]
    if not n["url"] or not n["user"] or not n["password"]:
        raise ValueError("请先填写 Navidrome 地址、用户名和密码")
    client = client or NavidromeClient(n["url"], n["user"], n["password"])
    try:
        info = client.ping()
        folders = client.music_folders()
    except NavidromeError as e:
        raise ValueError(str(e))
    ids = {f["id"] for f in folders}
    notes = []
    for key, label in (("main_folder_id", "主库"), ("discovery_folder_id", "试听库")):
        if n[key] is not None and n[key] not in ids:
            notes.append(f"{label}编号 {n[key]} 不在该账号可访问的音乐库里")
    msg = f"连接成功（Navidrome {info['version']}），该账号可访问 {len(folders)} 个音乐库"
    if len(folders) < 2:
        notes.append("试听库需要在 Navidrome 里单独添加为第二个音乐库并授权给该账号")
    return {"message": msg + ("；注意：" + "；".join(notes) if notes else ""), "folders": folders,
            "warnings": notes}


def _mount_point(path: Path, mountinfo: Optional[str] = None) -> Optional[str]:
    """path 所在的挂载点（读 /proc/self/mountinfo，取最长的前缀匹配）；读不到返回 None。

    不能用 st_dev 判断：Docker 里两个独立的 bind mount 即使来自同一块磁盘，st_dev 也相同，
    但硬链接/改名跨 bind mount 会失败（EXDEV）。"""
    try:
        text = mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        real = str(Path(path).resolve())
    except OSError:
        return None
    best = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        mp = parts[4].replace("\\040", " ")
        if (real == mp or real.startswith(mp.rstrip("/") + "/") or mp == "/") and (best is None or len(mp) > len(best)):
            best = mp
    return best


def check_dirs(s: Dict[str, Any], mountinfo: Optional[str] = None) -> Dict[str, Any]:
    main, disc = Path(s["dirs"]["main"]), Path(s["dirs"]["discovery"])
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, msg: str, warn: bool = False) -> None:
        checks.append({"name": name, "ok": ok, "warn": warn and ok is False, "msg": msg})

    # 主库：只读检查
    if not main.is_dir():
        add("主库目录", False, f"{main} 不存在或不是目录，请检查容器的目录映射")
    else:
        readable = os.access(main, os.R_OK | os.X_OK)
        add("主库目录", readable, f"{main} 存在" + ("" if readable else "，但没有读取权限"))
        if readable:
            add("主库写权限", os.access(main, os.W_OK),
                "可写（转正时需要，仅会新建文件，不会覆盖或删除）" if os.access(main, os.W_OK)
                else "不可写：红心转正将无法把歌曲移入主库（只读挂载时这是预期的，转正功能需可写）", warn=True)

    # 试听库：可创建，并做写入测试（只写自己的 .staging）
    try:
        disc.mkdir(parents=True, exist_ok=True)
        staging, trash = disc / STAGING_DIRNAME, disc / TRASH_DIRNAME
        staging.mkdir(exist_ok=True)
        trash.mkdir(exist_ok=True)
        probe = staging / f".probe-{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"x")
        probe.unlink()
        add("试听库目录", True, f"{disc} 可读写（已就绪 {STAGING_DIRNAME}、{TRASH_DIRNAME}）")
    except OSError as e:
        add("试听库目录", False, f"{disc} 不可写：{e.strerror or e}")

    # 两个目录是否在同一个挂载点：决定转正是瞬间改名，还是「复制 → 校验 → 改名 → 删源」
    if main.is_dir() and disc.is_dir():
        mm, dm = _mount_point(main, mountinfo), _mount_point(disc, mountinfo)
        if mm is None or dm is None:
            same = os.stat(main).st_dev == os.stat(disc).st_dev          # 读不到挂载信息：退回 st_dev（可能高估）
            add("挂载关系", True, "无法读取挂载信息；转正会先尝试直接改名，不行自动改为复制再删源" + ("（两目录在同一磁盘）" if same else ""))
        elif mm == dm:
            add("挂载关系", True, "同一挂载点，转正可直接改名移动")
        else:
            add("挂载关系", True, "两个目录是独立的挂载（Docker 里各自映射的目录就是这样）：转正会自动改用「复制 → 校验大小 → 改名 → 删源」，"
                                  "比同盘改名慢一点（要多复制一份文件），但安全；实测不影响 Navidrome 红心")
    ok = all(c["ok"] or c["warn"] for c in checks)
    return {"ok": ok, "checks": checks,
            "message": "目录检查通过" if ok and not any(c["warn"] for c in checks) else
            ("目录可用，但有需要注意的项" if ok else "目录检查未通过")}
