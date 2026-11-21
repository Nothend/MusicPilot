"""推荐条目的状态机：状态常量与合法转换表。

状态说明（「已标星/用户保留」是独立标记 starred/keep，不是状态）：
  PENDING          待处理（含：因限流/中断而尚未处理；重试到期的失败项）
  DOWNLOADING      正在下载（仅进程内的中间态；重启后复位为 PENDING）
  LOCAL_MAIN       主库里已有，直接复用
  LOCAL_DISCOVERY  试听库里已有（含刚下载的）
  NOT_FOUND        网易云没有可用的无损版本（reason_kind: not_found / only_mp3 / no_copyright / cooldown）
  DOWNLOAD_FAILED  下载环节出错，可按重试规则再试
  MULTIPLE_MATCH   本地有多个无法区分的候选，等人工选择
  SKIPPED          按规则跳过（如超出最大新下载数）
  PROMOTING        红心转正进行中（移动文件 → 扫描 → 校验红心）
  PROMOTED         已转正到主库
  EXPIRED          试听期满已清理
"""

from typing import Dict, FrozenSet

PENDING = "PENDING"
DOWNLOADING = "DOWNLOADING"
LOCAL_MAIN = "LOCAL_MAIN"
LOCAL_DISCOVERY = "LOCAL_DISCOVERY"
NOT_FOUND = "NOT_FOUND"
DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
MULTIPLE_MATCH = "MULTIPLE_MATCH"
SKIPPED = "SKIPPED"
PROMOTING = "PROMOTING"
PROMOTED = "PROMOTED"
EXPIRED = "EXPIRED"

ALL_STATES = (PENDING, DOWNLOADING, LOCAL_MAIN, LOCAL_DISCOVERY, NOT_FOUND, DOWNLOAD_FAILED,
              MULTIPLE_MATCH, SKIPPED, PROMOTING, PROMOTED, EXPIRED)

# 有本地文件可用（歌单里会放进去）的状态
PLAYABLE = frozenset({LOCAL_MAIN, LOCAL_DISCOVERY, PROMOTING, PROMOTED})

TRANSITIONS: Dict[str, FrozenSet[str]] = {
    PENDING: frozenset({DOWNLOADING, LOCAL_MAIN, LOCAL_DISCOVERY, NOT_FOUND, MULTIPLE_MATCH, SKIPPED}),
    DOWNLOADING: frozenset({LOCAL_DISCOVERY, NOT_FOUND, DOWNLOAD_FAILED, PENDING}),
    DOWNLOAD_FAILED: frozenset({PENDING, LOCAL_MAIN, LOCAL_DISCOVERY}),          # 重试：先回 PENDING
    NOT_FOUND: frozenset({PENDING, LOCAL_MAIN, LOCAL_DISCOVERY}),                # 冷却期满重试 / 手动放入库后被识别
    MULTIPLE_MATCH: frozenset({PENDING, LOCAL_MAIN, LOCAL_DISCOVERY}),           # 人工选择后
    SKIPPED: frozenset({PENDING}),
    LOCAL_MAIN: frozenset(),
    LOCAL_DISCOVERY: frozenset({PROMOTING, EXPIRED, LOCAL_MAIN}),               # LOCAL_MAIN：用户手动把文件移进了主库
    PROMOTING: frozenset({PROMOTED, LOCAL_DISCOVERY}),                            # 失败回到 LOCAL_DISCOVERY
    PROMOTED: frozenset(),
    EXPIRED: frozenset(),
}


class InvalidTransition(ValueError):
    pass


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, frozenset())
