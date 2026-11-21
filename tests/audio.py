"""测试辅助：生成 mutagen 能真实读取的最小 FLAC（只有 STREAMINFO，无音频帧）。"""
import struct
from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC


def make_flac(path, artist: Optional[str] = "A", title: Optional[str] = "T", seconds: int = 10, extra: Optional[dict] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rate, channels, bps, total = 44100, 2, 16, 44100 * seconds
    # STREAMINFO：16+16 位块大小、24+24 位帧大小、20 位采样率、3 位声道-1、5 位位深-1、36 位总采样数、16 字节 MD5
    packed = (rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36) | total
    body = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + packed.to_bytes(8, "big") + b"\x00" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + body)   # 0x80：最后一个元数据块，类型 0 = STREAMINFO
    if artist is not None or title is not None or extra:
        f = FLAC(str(path))
        if artist is not None:
            f["artist"] = artist if isinstance(artist, list) else [artist]
        if title is not None:
            f["title"] = title
        for k, v in (extra or {}).items():
            f[k] = v
        f.save()
    return path
