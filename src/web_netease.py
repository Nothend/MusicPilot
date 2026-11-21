"""Web 控制台专用的网易云接口扩展：扫码登录、账号资料、每日推荐。

以子类方式扩展 NeteaseMusic，不改动原有 netease.py。
"""

import json
import logging
import re
from random import randrange
from typing import Any, Dict, List, Optional

import requests

from constants import APIConstants
from crypto import CryptoUtils
from http_client import SESSION, APIException, netease_headers

DAILY_RECOMMEND_API = "https://interface3.music.163.com/eapi/v3/discovery/recommend/songs"

# 与 config.yaml 示例保持一致的附加 cookie（pc 客户端标识）
_EXTRA_COOKIES = {"os": "pc", "appver": "8.9.75"}

# 扫码状态码
QR_EXPIRED, QR_WAITING, QR_SCANNED, QR_OK = 800, 801, 802, 803

# 扫码流程专用 Session：申请 key 与轮询共用同一个会话，网易云下发的设备 cookie（如 NMTID）
# 才能从"申请二维码"延续到"查询状态"，与参考实现一致；也不污染全局 SESSION 里的账号 cookie。
_QR_SESSION = requests.Session()


def qr_login_url(key: str) -> str:
    """扫码内容：网易云 App 识别的登录链接。"""
    return f"https://music.163.com/login?codekey={key}"


def _qr_payload(extra: Dict[str, Any]) -> Dict[str, Any]:
    """扫码接口的请求体：与 WanYiCloud 参考实现一致，带 header（设备参数 + 随机 requestId）。"""
    config = APIConstants.DEFAULT_CONFIG.copy()
    config["requestId"] = str(randrange(20000000, 30000000))
    return {**extra, "header": json.dumps(config)}


def _eapi_post(session: requests.Session, url: str, payload: Dict[str, Any]) -> requests.Response:
    """用独立 Session 发 eapi 请求（登录阶段不污染全局共享 SESSION 的 cookie）。"""
    params = CryptoUtils.encrypt_params(url, payload)
    response = session.post(
        url, headers=netease_headers(), cookies=APIConstants.DEFAULT_COOKIES.copy(),
        data={"params": params}, timeout=30,
    )
    response.raise_for_status()
    return response


def qr_create() -> str:
    """申请扫码登录用的 unikey。"""
    try:
        result = _eapi_post(_QR_SESSION, APIConstants.QR_UNIKEY_API, _qr_payload({"type": 1})).json()
    except (requests.RequestException, ValueError) as e:
        raise APIException(f"获取二维码失败: {e}")
    if result.get("code") != 200 or not result.get("unikey"):
        raise APIException(f"获取二维码失败: {result.get('message', result)}")
    return result["unikey"]


def qr_check(key: str) -> Dict[str, Any]:
    """轮询扫码状态。返回 {'code', 'message', 'cookies'(仅 803 时有值)}。"""
    try:
        response = _eapi_post(_QR_SESSION, APIConstants.QR_LOGIN_API, _qr_payload({"key": key, "type": 1}))
        result = response.json()
    except (requests.RequestException, ValueError) as e:
        raise APIException(f"查询扫码状态失败: {e}")

    code = result.get("code")
    out: Dict[str, Any] = {"code": code, "message": result.get("message", ""), "cookies": {}}
    if code == QR_OK:
        cookies = {c.name: c.value for c in response.cookies}
        # 兜底：个别情况 Set-Cookie 没被解析，从响应 JSON 的 cookie 字段里抠 MUSIC_U
        if "MUSIC_U" not in cookies:
            m = re.search(r"MUSIC_U=([^;\s]+)", str(result.get("cookie", "")))
            if m:
                cookies["MUSIC_U"] = m.group(1)
        if "MUSIC_U" not in cookies:
            raise APIException("扫码成功但未取得登录凭证（MUSIC_U）")
        cookies.update(_EXTRA_COOKIES)
        out["cookies"] = cookies
    return out


class WebNetease:
    """组合而非继承：持有一个 NeteaseMusic 实例供下载流程复用，并补充 Web 专用接口。"""

    def __init__(self, cookies: Dict[str, str]):
        from netease import NeteaseMusic  # 延迟导入，避免与 netease 的导入顺序耦合
        self.api = NeteaseMusic(cookies)
        self.cookies = cookies
        self.logger = logging.getLogger(__name__)

    def account_info(self) -> Optional[Dict[str, Any]]:
        """账号资料；cookie 无效返回 None。网络异常抛 APIException（区别于\"确实失效\"）。"""
        if not self.cookies:
            return None
        try:
            response = SESSION.post(APIConstants.USER_ACCOUNT_API, headers=netease_headers(),
                                    cookies=self.cookies, timeout=30)
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as e:
            raise APIException(f"获取账号信息失败: {e}")
        profile = result.get("profile")
        if result.get("code") != 200 or not profile:
            return None
        vip_type = (result.get("account") or {}).get("vipType", 0) or 0
        return {
            "uid": profile.get("userId"),
            "nickname": profile.get("nickname", ""),
            "avatar": profile.get("avatarUrl", ""),
            "vip_type": vip_type,
            "is_vip": vip_type != 0,  # 与 netease.is_cookie_valid 的判定一致
        }

    def get_daily_recommend(self) -> List[Dict[str, Any]]:
        """今日每日推荐歌曲，字段与 get_playlist_detail 的 tracks 对齐。"""
        config = APIConstants.DEFAULT_CONFIG.copy()
        config["requestId"] = str(randrange(20000000, 30000000))
        payload = {"total": "true", "header": json.dumps(config)}
        try:
            params = self.api.crypto_utils.encrypt_params(DAILY_RECOMMEND_API, payload)
            text = self.api.http_client.post_request(DAILY_RECOMMEND_API, params, self.cookies)
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise APIException(f"解析每日推荐响应失败: {e}")
        if result.get("code") != 200:
            raise APIException(f"获取每日推荐失败: {result.get('message', result.get('code'))}")
        songs = (result.get("data") or {}).get("dailySongs") or []
        tracks = []
        for song in songs:
            al = song.get("al") or {}
            tracks.append({
                "id": song.get("id"),
                "name": song.get("name"),
                "artists": [a.get("name", "") for a in (song.get("ar") or [])],
                "album": al.get("name", ""),
                "picUrl": al.get("picUrl", ""),
            })
        return tracks
