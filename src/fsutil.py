"""文件操作工具：绝不覆盖已有文件的安全移动。

用于「下载暂存 → 试听库」与「试听库 → 主库」。容器里不同的 bind mount 是不同的挂载点，
`rename` 会因跨设备失败（EXDEV），所以要有「复制 → 校验 → 改名 → 删源」的退路。
"""

import errno
import os
import shutil
import uuid
from pathlib import Path


def safe_move(src, dst) -> str:
    """把 src 移动到 dst；dst 已存在则抛 FileExistsError（绝不覆盖）。返回 'link'（同挂载点）或 'copy'。

    同挂载点：硬链接 + 删源（link 在目标存在时原子地失败，天然不会覆盖）。
    其它情况（跨挂载点、文件系统不支持硬链接）：复制到目标同目录的临时文件，校验大小后改名，再删源；
    任何一步失败都会清理临时文件，源文件保持原样。
    """
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(str(src))
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(str(dst))
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EACCES):
            raise
    else:
        os.unlink(src)
        return "link"

    tmp = dst.parent / f".{dst.name}.{uuid.uuid4().hex[:8]}.part"
    try:
        shutil.copyfile(src, tmp)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(f"复制后大小不一致：{src.stat().st_size} → {tmp.stat().st_size}")
        if dst.exists():
            raise FileExistsError(str(dst))
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.unlink(src)
    return "copy"
