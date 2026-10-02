"""能力探测输出的判定（纯逻辑，不碰 I/O）。

真正的子进程调用放在 ``probe_runtime`` 那一层，这里只处理「拿到输出之后怎么判断」，
这样硬件与编码器的判定分支可以完全用测试覆盖。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = ["MediaInfo", "has_cuda_hwaccel", "has_encoder", "parse_media_info"]


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 探测到的媒体信息。"""

    duration: float
    codec_name: str


def has_encoder(encoders_output: str, name: str) -> bool:
    """在 ``ffmpeg -encoders`` 的输出里查找编码器名。

    按整词匹配，这一条很重要：``hevc`` 不能命中 ``hevc_nvenc``（下划线属于单词字符，
    所以必须用 ``\\b`` 而不是子串包含）。
    """
    return re.search(rf"\b{re.escape(name)}\b", encoders_output) is not None


def has_cuda_hwaccel(hwaccels_output: str) -> bool:
    """``ffmpeg -hwaccels`` 的输出里是否列出 cuda。

    按子串匹配（不做整词匹配）：``-hwaccels`` 里的条目可能带后缀，整词匹配会漏判。
    """
    return "cuda" in hwaccels_output


def parse_media_info(payload: dict[str, object]) -> MediaInfo:
    """解析 ``ffprobe -of json`` 的结果。

    一次 ``-show_format -show_streams`` 同时取回时长与视频编码：
    分两次调用会让探测开销翻倍，中间还多出一个两次结果不一致的窗口。
    取不到时长返回 0，由调用方按「无法获取时长」处理。
    """
    duration = 0.0
    format_info = payload.get("format")
    raw_duration = format_info.get("duration") if isinstance(format_info, dict) else None
    if raw_duration is not None:
        try:
            duration = float(str(raw_duration))
        except TypeError, ValueError:
            duration = 0.0

    if not math.isfinite(duration) or duration < 0:
        duration = 0.0
    codec_name = ""
    streams = payload.get("streams")
    for stream in streams if isinstance(streams, list) else []:
        if isinstance(stream, dict) and stream.get("codec_type") == "video":
            codec_name = str(stream.get("codec_name") or "").strip()
            break

    return MediaInfo(duration=duration, codec_name=codec_name)
