"""推荐联动的服务层：把同步/转正/清理/索引/红心同步包成「后台任务」，供页面按钮与定时器触发。

- 所有任务经 TaskManager.run_async 启动：全局互斥（已有任务在跑立刻 TaskBusy，不排队），结果与日志入库；
- 门禁：总开关 `enabled` 关闭时，会改动库/下载/写 LB 的任务一律拒绝（只读预览与「同步索引」不受限）；
  配置不全时给出明确提示，而不是跑一半失败；
- 网易云账号选择与现有下载任务一致：登录账号是黑胶 VIP 就用它；否则必须有有效的借用 VIP 账号（推荐联动只要无损）；
- 通知（Bark）：失败/暂停/有实质变化（新下载、转正、过期、提交红心）才发，没变化的定时空跑不打扰。
"""

import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import lb_cleanup
import lb_love
import lb_pipeline
import lb_promote
import lb_queries
import lb_settings
from db import Database
from lb_adapter import NeteaseAdapter
from lb_client import ListenBrainzClient
from local_index import sync_index
from nd_client import NavidromeClient
from settings_store import SettingsStore
from task_manager import RunHandle, TaskBusy, TaskManager

logger = logging.getLogger(__name__)

KINDS = ("sync", "promote", "cleanup", "reindex", "love")
# 这些任务会改动库内容 / 下载 / 写 ListenBrainz：总开关关闭时拒绝
GATED = ("sync", "promote", "cleanup")
LABELS = {"sync": "推荐同步", "promote": "红心转正", "cleanup": "过期清理", "reindex": "同步索引", "love": "红心同步"}
LOVE_PREVIEW_KEY = "lb.love_preview"


class NotReady(ValueError):
    """配置不完整或总开关关闭：页面直接显示 message。"""


def default_adapter_factory(store, config, settings: Dict[str, Any], request_delay: float):
    """用现有 Web 的网易云账号装配下载渠道：登录账号是 VIP 就直接用；否则要有效的借用 VIP 账号。推荐联动只要无损。"""
    from downloader import SongDownloader
    from web_common import download_account_of, effective_quality
    from web_netease import WebNetease

    state = store.get()
    account = state.get("account")
    if not account or not account.get("cookies"):
        raise NotReady("请先在「下载」页扫码登录网易云账号")
    wn = WebNetease(account["cookies"])
    info = wn.account_info()
    if info is None:
        raise NotReady("网易云登录已失效，请重新扫码")
    cookies, api = account["cookies"], wn.api
    if not info["is_vip"]:
        pick = download_account_of(state)
        borrowed = pick["account"] if pick["kind"] == "borrowed" else None
        if not borrowed or not borrowed.get("cookies"):
            raise NotReady("推荐联动只下载无损，需要黑胶 VIP：请登录 VIP 账号，或在「设置 → 下载账号」添加借用的 VIP 账号并选用")
        bw = WebNetease(borrowed["cookies"])
        binfo = bw.account_info()
        if binfo is None:
            raise NotReady("借用账号登录已失效，请在设置里重新添加")
        if not binfo["is_vip"]:
            raise NotReady("借用账号已不是黑胶 VIP，请更换")
        cookies, api = borrowed["cookies"], bw.api
    quality = effective_quality(config, state["settings"], True)
    return NeteaseAdapter(cookies, SongDownloader(cookies, api), quality, request_delay=request_delay)


class LbService:
    def __init__(self, *, db: Database, kv: SettingsStore, tm: TaskManager, store=None, config=None,
                 notify: Optional[Callable[[str, str], Any]] = None,
                 lb_factory: Callable[[Dict[str, Any]], Any] = None,
                 nd_factory: Callable[[Dict[str, Any]], Any] = None,
                 adapter_factory: Optional[Callable[[Dict[str, Any], float], Any]] = None,
                 request_delay: Callable[[], float] = lambda: 2.0,
                 now: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep):
        self.db, self.kv, self.tm, self.store, self.config = db, kv, tm, store, config
        self.notify = notify or (lambda title, body: None)
        self.lb_factory = lb_factory or (lambda s: ListenBrainzClient(s["listenbrainz"]["token"], s["listenbrainz"]["username"]))
        self.nd_factory = nd_factory or (lambda s: NavidromeClient(s["navidrome"]["url"], s["navidrome"]["user"], s["navidrome"]["password"]))
        self.adapter_factory = adapter_factory or (lambda s, delay: default_adapter_factory(self.store, self.config, s, delay))
        self.request_delay = request_delay
        self.now, self.sleep = now, sleep

    # ------------------------------------------------------------ 设置与门禁
    def settings(self) -> Dict[str, Any]:
        return lb_settings.load(self.kv.get(lb_settings.SETTINGS_KEY))

    def check_ready(self, kind: str, s: Dict[str, Any], *, dry_run: bool = False) -> None:
        if kind not in KINDS:
            raise NotReady("未知的任务类型")
        if kind in GATED or (kind == "love" and not dry_run):
            if not s["enabled"]:
                raise NotReady("推荐联动还没有启用：请先在「设置」页的「推荐联动设置」里配置并打开总开关")
            if kind == "love" and not s["love"]["enabled"]:
                raise NotReady("红心同步还没有启用（会写入 ListenBrainz，需要你在「红心同步」里明确打开）")
        nd = s["navidrome"]
        if not (nd["url"] and nd["user"] and nd["password"]):
            raise NotReady("请先在设置里填写 Navidrome 地址、用户名和密码")
        if kind in ("sync", "promote", "cleanup", "reindex") and (nd["main_folder_id"] is None or nd["discovery_folder_id"] is None):
            raise NotReady("请先在设置里选择 Navidrome 的主库和试听库（点「测试连接」后下拉选择）")
        if kind in ("sync", "love") and not (s["listenbrainz"]["username"] and s["listenbrainz"]["token"]):
            raise NotReady("请先在设置里填写 ListenBrainz 用户名和 Token")

    # ------------------------------------------------------------ 启动任务
    def start(self, kind: str, *, trigger: str = "manual", types: Optional[List[str]] = None,
              scan: bool = False, dry_run: bool = False) -> int:
        """启动后台任务，返回运行 id。已有任务在跑 → TaskBusy；不满足条件 → NotReady（都是 ValueError 的子类/页面可读）。"""
        s = self.settings()
        self.check_ready(kind, s, dry_run=dry_run)
        label = LABELS[kind] + ("（预览）" if kind == "love" and dry_run else "")

        def body(h: RunHandle) -> None:
            self._execute(kind, s, h, types=types, scan=scan, dry_run=dry_run)

        return self.tm.run_async(kind + ("_preview" if kind == "love" and dry_run else ""), trigger, body,
                                 on_done=lambda h, err: self._after(label, h, err))

    def _execute(self, kind: str, s: Dict[str, Any], h: RunHandle, *, types, scan, dry_run) -> None:
        nd = self.nd_factory(s)
        if kind == "sync":
            delay = self.request_delay()
            adapter = self.adapter_factory(s, delay)
            lb_pipeline.run_sync(lb_pipeline.SyncDeps(self.db, s, self.lb_factory(s), nd, adapter, h, request_delay=delay,
                                                      sleep=self.sleep, now=self.now, types=types))
        elif kind == "promote":
            lb_promote.run_promote(lb_promote.PromoteDeps(self.db, s, nd, h, now=self.now))
        elif kind == "cleanup":
            lb_cleanup.run_cleanup(lb_cleanup.CleanupDeps(self.db, s, nd, h, now=self.now))
        elif kind == "reindex":
            n = s["navidrome"]
            if scan:
                h.log("触发 Navidrome 扫描并等待完成")
                nd.scan_and_wait(timeout=600)
            st = sync_index(self.db, nd, n["main_folder_id"], n["discovery_folder_id"])
            h.add("main", st["main"]["total"]); h.add("discovery", st["discovery"]["total"])
            h.log(f"本地索引已更新：主库 {st['main']['total']} 首（新增 {st['main']['added']}，移除 {st['main']['removed']}），"
                  f"试听库 {st['discovery']['total']} 首")
        elif kind == "love":
            deps = lb_love.LoveDeps(self.db, s, self.lb_factory(s), nd, h, dry_run=dry_run, now=self.now)
            lb_love.run_love_sync(deps)
            if dry_run:                          # 预览结果存起来，页面随时可看（可能是一份很长的清单）
                self.kv.set(LOVE_PREVIEW_KEY, {"at": self.now().isoformat(timespec="seconds"), "plan": deps.plan,
                                               "stats": dict(h.stats)})

    # ------------------------------------------------------------ 结束后的通知
    def _after(self, label: str, h: RunHandle, err: Optional[BaseException]) -> None:
        msg = self.summarize(label, h, err)
        if msg:
            try:
                self.notify(f"推荐联动：{label}", msg)
            except Exception as e:
                logger.error(f"发送通知失败: {e}")

    @staticmethod
    def summarize(label: str, h: RunHandle, err: Optional[BaseException]) -> str:
        """需要通知时返回正文，否则返回空串：失败、暂停、有实质变化才通知。"""
        st = h.stats
        if err is not None:
            return f"失败：{type(err).__name__}: {err}"
        if h.status == "paused":
            return f"已暂停：{h.summary}"
        parts = []
        for key, text in (("downloaded", "新下载"), ("promoted", "转正"), ("expired", "过期清理"), ("submitted", "提交红心"),
                          ("moved", "移入主库")):
            if st.get(key):
                parts.append(f"{text} {st[key]} 首")
        if st.get("errors") or st.get("failed"):
            parts.append(f"出错/失败 {st.get('errors', 0) + st.get('failed', 0)} 首")
        if not parts:
            return ""
        if st.get("not_found"):
            parts.append(f"未找到 {st['not_found']} 首")
        return "，".join(parts)

    # ------------------------------------------------------------ 页面用的查询
    def dashboard(self) -> Dict[str, Any]:
        return lb_queries.dashboard(self.db, self.settings(), self.tm.current(), self.now())

    def love_preview(self) -> Optional[Dict[str, Any]]:
        return self.kv.get(LOVE_PREVIEW_KEY)

    def cleanup_preview(self) -> Dict[str, Any]:
        s = self.settings()
        nd = None
        n = s["navidrome"]
        if n["url"] and n["user"] and n["password"] and n["discovery_folder_id"] is not None:
            try:
                nd = self.nd_factory(s)
                return lb_cleanup.preview(self.db, s, nd=nd, now=self.now())
            except Exception as e:                # Navidrome 暂时连不上：用库里已有的红心标记预览，并说明
                logger.warning(f"预览待清理时无法读取 Navidrome 红心，使用已缓存的标记: {e}")
        pv = lb_cleanup.preview(self.db, s, now=self.now())
        pv["stale_stars"] = True
        return pv
