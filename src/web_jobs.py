"""Web 控制台的下载任务引擎：筛选 → 下载 → 进度，单线程任务队列。

只复用现有的 SongDownloader / library / BarkNotifier，不改变其行为。
同一时刻只执行一个任务（其余排队），避免对音乐平台造成突发请求而触发风控。
"""

import contextlib
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from bark import BarkNotifier
from downloader import DownloadException, SongDownloader
from utils import quality_display_name
from web_common import (build_library_checker, download_account_of, effective_bark_api, effective_quality,
                        effective_request_delay, fetch_tracks, normalize_source,
                        resolve_subdir, source_label)
from web_platform import WebPlatform
from web_store import WebStore

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 200
HISTORY_LIMIT = 1000   # 保留最近多少个已结束任务（汇总在 web_state.json，逐首明细单独存文件）
ACTIVE = ("queued", "running")


class Job:
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.cancel = threading.Event()

    def log(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        logs = self.data["log"]
        logs.append(line)
        if len(logs) > MAX_LOG_LINES:
            del logs[: len(logs) - MAX_LOG_LINES]
        logger.info(f"[任务 {self.data['id']}] {msg}")


class JobManager:
    def __init__(self, store: WebStore, base_dir: Path, on_finish=None, task_lock=None):
        self.store = store
        # 与推荐联动任务共用的全局锁（TaskManager）：轮到本队列的任务时，若有推荐任务在跑就等它结束再开始
        self.task_lock = task_lock
        self.base_dir = base_dir
        # 音乐库去重用的设置：默认就是页面里的「音乐库去重」；与 Navidrome 联动开启时由 App 换成联动用的那一份 Navidrome 连接（只配置一遍）
        self.library_settings = lambda settings: settings
        self.on_finish = on_finish  # 回调(job_snapshot)：定时任务据此记录最近结果
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._seq = 0
        # 逐首明细（items + 完整日志）单独存 data/job_details/<任务id>.json：
        # web_state.json 每次保存都整体重写且含登录 cookie，不适合塞几千首歌的明细
        self.detail_dir = store.path.parent / "job_details"
        # 重启后载入历史任务（均为已结束状态）
        for item in store.get().get("history", []):
            if isinstance(item, dict) and item.get("id"):
                item.setdefault("log", [])
                item.setdefault("counts", {"success": 0, "failed": 0, "library": 0, "local": 0, "skipped": 0})
                item.setdefault("fail_kinds", {})
                self._jobs[item["id"]] = Job(item)
                self._order.append(item["id"])
        self._cleanup_details()

    def start(self) -> None:
        threading.Thread(target=self._worker, name="web-job-worker", daemon=True).start()

    # ------------------------------------------------------------ 对外接口
    def submit(self, source: Dict[str, Any], subdir: str, trigger: str = "manual",
               track_ids: Optional[List[int]] = None, schedule_id: Optional[str] = None) -> Dict[str, Any]:
        source = normalize_source(source)
        resolve_subdir(self.base_dir, subdir)  # 提前校验，非法目录直接拒绝
        with self._lock:
            if schedule_id and any(j.data.get("schedule_id") == schedule_id and j.data["status"] in ACTIVE
                                   for j in self._jobs.values()):
                raise ValueError("该定时任务上一次运行尚未结束")
            self._seq += 1
            job_id = f"{int(time.time() * 1000):x}{self._seq}"
            job = Job({
                "id": job_id, "title": source_label(source), "source": source,
                "subdir": (subdir or "").strip().strip("/"), "trigger": trigger,
                "schedule_id": schedule_id, "status": "queued",
                "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "started": None, "finished": None, "message": "",
                "total": 0, "processed": 0, "current": "",
                "counts": {"success": 0, "failed": 0, "library": 0, "local": 0, "skipped": 0},
                "track_ids": [int(i) for i in track_ids] if track_ids else None,
                "fail_kinds": {},      # 失败原因分类 -> 歌曲数
                "has_detail": False,   # 逐首明细是否已写入 job_details/
                "items": [],           # 逐首记录（结束后落盘并从内存丢弃）
                "log": [],
            })
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._trim()
            job.log("已加入队列")
        self._queue.put(job_id)
        return self.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.data["status"] not in ACTIVE:
                return False
            job.cancel.set()
            return True

    def get(self, job_id: str, detail: bool = False) -> Optional[Dict[str, Any]]:
        """任务快照。detail=True 时带上逐首明细 items 与完整日志（已结束任务从 job_details/ 读取）。"""
        with self._lock:
            job = self._jobs.get(job_id)  # 只认内存里已知的 id，避免按用户输入拼文件路径
            if not job:
                return None
            snap = self._snapshot(job, with_items=detail)
        if detail and snap.get("items") is None and snap.get("has_detail"):
            saved = self._read_detail(job_id)
            if saved:
                snap["items"] = saved.get("items") or []
                snap["log"] = saved.get("log") or snap["log"]
        return snap

    def list(self, limit: int = 50, offset: int = 0, trigger: str = "",
             failed_only: bool = False) -> Dict[str, Any]:
        """最新的在前，支持分页与筛选。只返回汇总（不含逐首明细），每个任务只带最后几行日志，减小轮询负载。

        trigger: ''=全部；'schedule'=仅定时触发；'manual'=其余（手动下载、手动运行定时任务）。
        """
        with self._lock:
            matched = []
            latest = None  # 最新一个已结束任务（不受筛选影响），前端据此显示红点
            for job_id in reversed(self._order):
                d = self._jobs[job_id].data
                failed = int((d.get("counts") or {}).get("failed", 0)) + (1 if d["status"] == "failed" else 0)
                if latest is None and d["status"] not in ACTIVE:
                    latest = {"id": job_id, "failed": failed}
                if trigger == "schedule" and d.get("trigger") != "schedule":
                    continue
                if trigger == "manual" and d.get("trigger") == "schedule":
                    continue
                if failed_only and not failed:
                    continue
                matched.append(job_id)
            out = []
            for job_id in matched[offset: offset + limit]:
                snap = self._snapshot(self._jobs[job_id])
                snap["log"] = snap["log"][-3:]
                out.append(snap)
            active = any(j.data["status"] in ACTIVE for j in self._jobs.values())
            return {"jobs": out, "total": len(matched), "active": active, "latest": latest}

    def active(self) -> bool:
        with self._lock:
            return any(j.data["status"] in ACTIVE for j in self._jobs.values())

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _snapshot(job: Job, with_items: bool = False) -> Dict[str, Any]:
        d = dict(job.data)
        d["counts"] = dict(d["counts"])
        d["fail_kinds"] = dict(d.get("fail_kinds") or {})
        d["log"] = list(d.get("log") or [])
        items = d.pop("items", None)
        if with_items:
            d["items"] = list(items) if items is not None else None
        d.pop("track_ids", None)
        return d

    # ------------------------------------------------------------ 逐首明细文件
    def _detail_path(self, job_id: str) -> Path:
        return self.detail_dir / f"{job_id}.json"

    def _write_detail(self, job: Job) -> bool:
        d = job.data
        try:
            self.detail_dir.mkdir(parents=True, exist_ok=True)
            path = self._detail_path(d["id"])
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"items": d.get("items") or [], "log": d.get("log") or []}, f, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except OSError as e:
            logger.error(f"保存任务明细失败: {e}")
            return False

    def _read_detail(self, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._detail_path(job_id), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _remove_detail(self, job_id: str) -> None:
        try:
            self._detail_path(job_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning(f"删除任务明细失败: {e}")

    def _cleanup_details(self) -> None:
        """启动时清理孤儿明细文件（对应任务已不在历史里）和残留的临时文件。"""
        if not self.detail_dir.is_dir():
            return
        for p in self.detail_dir.iterdir():
            if p.suffix == ".tmp" or (p.suffix == ".json" and p.stem not in self._jobs):
                try:
                    p.unlink()
                except OSError:
                    pass

    def _trim(self) -> None:
        """只保留最近 HISTORY_LIMIT 个已结束任务 + 全部未结束任务。"""
        finished = [i for i in self._order if self._jobs[i].data["status"] not in ACTIVE]
        for job_id in finished[: max(0, len(finished) - HISTORY_LIMIT)]:
            self._jobs.pop(job_id, None)
            self._order.remove(job_id)
            self._remove_detail(job_id)

    def _persist_history(self) -> None:
        with self._lock:
            items = []
            for job_id in self._order:
                d = self._jobs[job_id].data
                if d["status"] in ACTIVE:
                    continue
                item = dict(d)
                item.pop("track_ids", None)
                item.pop("items", None)
                if d.get("has_detail"):
                    item.pop("log", None)  # 完整日志在明细文件里
                else:
                    item["log"] = list(d.get("log", [])[-40:])  # 旧版任务/明细写入失败：保留最后几行
                items.append(item)
        items = items[-HISTORY_LIMIT:]
        self.store.update(lambda s: s.__setitem__("history", items))

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None:
                continue
            try:
                with (self.task_lock.hold("音乐平台下载任务") if self.task_lock else contextlib.nullcontext()):
                    if job.cancel.is_set():
                        self._finish(job, "cancelled", "已取消")
                    else:
                        self._run(job)
            except Exception as e:  # 兜底：任何意外都不能让工作线程退出
                logger.error(f"任务 {job_id} 异常: {e}", exc_info=True)
                self._finish(job, "failed", f"任务异常: {e}")

    def _finish(self, job: Job, status: str, message: str) -> None:
        with self._lock:
            job.data["status"] = status
            job.data["message"] = message
            job.data["current"] = ""
            job.data["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job.log(message)
        saved = self._write_detail(job)
        with self._lock:
            if saved:  # 明细已落盘：内存里不再保留 items，日志只留最后几行
                job.data["has_detail"] = True
                job.data.pop("items", None)
                job.data["log"] = job.data["log"][-3:]
        try:
            self._persist_history()
        except OSError as e:
            logger.error(f"保存任务历史失败: {e}")
        if self.on_finish:
            try:
                self.on_finish(self._snapshot(job))
            except Exception as e:
                logger.error(f"任务结束回调失败: {e}")

    def _notify(self, title: str, body: str) -> None:
        settings = self.store.get()["settings"]
        BarkNotifier(effective_bark_api(settings)).send_notification(title, body)


    def _run(self, job: Job) -> None:
        d = job.data
        with self._lock:
            d["status"] = "running"
            d["started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = self.store.get()
        account = state.get("account")
        if not account or not account.get("cookies"):
            return self._finish(job, "failed", "尚未登录，请先扫码登录")

        wn = WebPlatform(account["cookies"])
        info = wn.account_info()
        if info is None:
            def mark(s):
                if s.get("account"):
                    s["account"]["invalid"] = True
            self.store.update(mark)
            self._notify("云音乐 Web 任务失败", "登录已失效，请重新扫码")
            return self._finish(job, "failed", "登录已失效，请重新扫码")
        is_vip = info["is_vip"]

        def refresh(s):
            if s.get("account"):
                s["account"].update({k: info[k] for k in ("uid", "nickname", "avatar", "vip_type")})
                s["account"]["invalid"] = False
        self.store.update(refresh)

        settings = state["settings"]

        # 下载账号：默认就是登录账号；选了"借用账号"时，取下载链接/详情/歌词都改用借用账号的 cookie
        # （歌曲列表仍用登录账号取——每日推荐、我的歌单是它自己的）。
        # 借用账号失效/缺失时直接失败并推送，不静默退回登录账号：登录账号若是非 VIP，
        # 悄悄退回会变成只下 MP3 且跳过 VIP 歌曲，用户很难察觉原因。
        pick = download_account_of(state)
        dl_cookies = account["cookies"]
        dl_api = wn.api
        if pick["kind"] == "borrowed" and not is_vip:
            borrowed = pick["account"]
            if not borrowed or not borrowed.get("cookies"):
                msg = "已选择使用借用账号下载，但借用账号不存在，请在设置里重新添加"
                self._notify("云音乐 Web 任务失败", msg)
                return self._finish(job, "failed", msg)
            try:
                binfo = WebPlatform(borrowed["cookies"]).account_info()
            except Exception as e:
                msg = f"借用账号校验时连接音乐平台失败：{e}"
                self._notify("云音乐 Web 任务失败", msg)
                return self._finish(job, "failed", msg)
            if binfo is None:
                def mark_borrowed(s):
                    if s.get("download_account"):
                        s["download_account"]["invalid"] = True
                self.store.update(mark_borrowed)
                msg = f"借用账号「{borrowed.get('nickname', '')}」登录已失效，请在设置里重新添加"
                self._notify("云音乐 Web 任务失败", msg)
                return self._finish(job, "failed", msg)
            if not binfo["is_vip"]:
                msg = f"借用账号「{binfo['nickname']}」已不是VIP，无法下载，请更换借用账号"
                self._notify("云音乐 Web 任务失败", msg)
                return self._finish(job, "failed", msg)

            def refresh_borrowed(s):
                if s.get("download_account"):
                    s["download_account"].update({k: binfo[k] for k in ("uid", "nickname", "avatar", "vip_type")})
                    s["download_account"]["invalid"] = False
            self.store.update(refresh_borrowed)
            dl_cookies = borrowed["cookies"]
            dl_api = WebPlatform(dl_cookies).api
            is_vip = True
            who = f"借用账号 {binfo['nickname']}（VIP）"
            job.log(f"列表账号：{info['nickname']}；下载账号：{who}")
        else:
            who = f"账号 {info['nickname']}（{'VIP' if is_vip else '非 VIP，仅可下载 MP3'}）"
            if pick["kind"] == "borrowed":
                # 登录账号本身就是VIP，不需要借用账号：直接用它，也就不校验、不动借用账号
                # （借用账号的 cookie 不被使用，也少一份被风控的风险）
                job.log("登录账号已是VIP，无需借用账号，直接用登录账号下载")

        quality = effective_quality(settings, is_vip)
        job.log(f"{who}，音质：{quality_display_name(quality)}")

        try:
            target = resolve_subdir(self.base_dir, d["subdir"])
            target.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as e:
            return self._finish(job, "failed", f"下载目录不可用: {e}")

        try:
            fetched = fetch_tracks(wn, d["source"], info["uid"])
        except Exception as e:
            return self._finish(job, "failed", f"获取歌曲列表失败: {e}")
        if fetched["missing"]:
            return self._finish(job, "done", f"未找到名为 {fetched['name']} 的歌单")
        tracks = fetched["tracks"]
        if d.get("track_ids"):
            wanted = set(d["track_ids"])
            tracks = [t for t in tracks if t.get("id") in wanted]
        if not tracks:
            return self._finish(job, "done", "没有可处理的歌曲")
        with self._lock:
            d["total"] = len(tracks)
        job.log(f"《{fetched['name']}》共 {len(tracks)} 首，目标目录：{target}")
        self._download_all(job, dl_api, dl_cookies, target, tracks, quality, settings)


    def _download_all(self, job: Job, api, cookies: Dict[str, str], target: Path,
                      tracks: List[Dict[str, Any]], quality: str, settings: Dict[str, Any]) -> None:
        d = job.data
        # 下载器沿用原实现，仅把目标目录指向所选子目录；api/cookies 是"下载账号"的
        # （借用账号时与取列表用的登录账号不同，每次请求显式传 cookies，互不串用）
        downloader = SongDownloader(cookies, api)
        downloader.download_dir = target

        checker = None
        try:
            checker = build_library_checker(self.library_settings(settings))
        except Exception as e:
            job.log(f"音乐库检查不可用，仅做本地去重：{e}")
        if checker is not None:
            job.log("已启用音乐库去重")

        delay = effective_request_delay(settings)
        counts = d["counts"]
        try:
            for track in tracks:
                if job.cancel.is_set():
                    break
                title = track.get("name") or "未知歌曲"
                with self._lock:
                    d["current"] = f"{'&'.join(track.get('artists') or [])} - {title}"
                try:
                    status, reason, kind = self._process_track(job, downloader, checker, track, quality)
                except DownloadException as e:
                    status, reason, kind = "failed", str(e), e.kind
                except Exception as e:
                    status, reason, kind = "failed", str(e), "other"
                self._record(job, track, status, reason, kind)
                with self._lock:
                    d["processed"] += 1
                if delay > 0 and not job.cancel.is_set():
                    time.sleep(delay)
        finally:
            if checker is not None:
                checker.close()

        summary = (f"成功 {counts['success']} · 失败 {counts['failed']} · "
                   f"库中已有 {counts['library']} · 本地已有 {counts['local']} · 跳过 {counts['skipped']}")
        if job.cancel.is_set():
            return self._finish(job, "cancelled", f"已取消：{summary}")
        self._finish(job, "done", summary)
        self._notify(f"云音乐下载 · {d['title']}",
                     f"共{d['total']}首\n" + summary.replace(" · ", "\n"))

    def _record(self, job: Job, track: Dict[str, Any], status: str, reason: str, kind: str) -> None:
        """记一首歌的处理结果：计数 + 逐首明细 +（失败/跳过的）日志。

        status：success | failed | skipped（试听/格式不支持）| library（库中已有）| local（本地已有）
        """
        d = job.data
        name = track.get("name") or "未知歌曲"
        artists = track.get("artists") or []
        key = {"success": "success", "failed": "failed", "skipped": "skipped",
               "library": "library", "local": "local"}[status]
        with self._lock:
            d["counts"][key] += 1
            if status == "failed":
                d["fail_kinds"][kind] = d["fail_kinds"].get(kind, 0) + 1
            d["items"].append({"id": track.get("id"), "name": name, "artists": artists,
                               "album": track.get("album", ""), "status": status,
                               "kind": kind if status == "failed" else "", "reason": reason})
        who = f"{'&'.join(artists)} - {name}" if artists else name
        if status == "success":
            job.log(f"完成：{who}（{reason}）")  # success 时 reason 存的是文件格式
        elif status == "failed":
            job.log(f"失败：{who}（ID {track.get('id')}）{reason}")
        elif status == "skipped":
            job.log(f"跳过：{who}（ID {track.get('id')}）{reason}")

    def _process_track(self, job: Job, downloader: SongDownloader, checker, track: Dict[str, Any],
                       quality: str):
        """处理一首歌，返回 (status, reason, kind)；失败可直接抛异常，由调用方归类记录。"""
        title = track.get("name") or "未知歌曲"
        artists = track.get("artists") or []

        # 1) 库去重（便宜的检查在前；库后端异常视为不在库中）
        if checker is not None:
            try:
                if checker.exists(title, artists, track.get("album", "")):
                    return "library", "音乐库中已有", ""
            except Exception as e:
                job.log(f"库检查出错，按不在库中处理：{e}")
        # 2) 本地去重（零网络请求）
        if downloader.is_song_already_downloaded(title, artists):
            return "local", "本地已有", ""
        # 3) 取下载信息：Web 端接受 MP3；试听片段/不支持的格式返回 None
        music_info = downloader.get_music_info(track.get("id"), quality, allow_mp3=True)
        if music_info is None:
            return "skipped", downloader.last_skip_reason or "仅试听/格式不支持", ""
        result = downloader.download_song(music_info, quality)
        if result.success:
            return "success", music_info.file_type, ""
        return "failed", result.error_message or "下载失败", "download"
