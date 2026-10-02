"""能力探测输出的判定（纯逻辑，不碰 I/O）。

真正的子进程调用放在 ``probe_runtime`` 那一层，这里只处理「拿到输出之后怎么判断」，
这样硬件与编码器的判定分支可以完全用测试覆盖。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction

__all__ = ["MediaInfo", "MediaStream", "has_cuda_hwaccel", "has_encoder", "parse_media_info"]


@dataclass(frozen=True)
class MediaStream:
    index: int
    kind: str
    codec: str
    language: str = "und"
    dispositions: tuple[str, ...] = ()
    channels: int = 0
    sample_rate: int = 0


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 探测到的媒体信息。"""

    duration: float
    codec_name: str
    video_duration: float = 0.0
    frame_rate: float = 0.0
    time_base: float = 0.0
    width: int = 0
    height: int = 0
    stream_types: tuple[str, ...] = ()
    nominal_frame_rate: float = 0.0
    frame_count: int = 0
    streams: tuple[MediaStream, ...] = ()
    start_time: float = 0.0
    video_start_time: float = 0.0
    rotation: float = 0.0

    @property
    def video_offset(self) -> float:
        """视频相对输入容器零点的偏移；seek 和拼接必须使用同一基准。"""
        return self.video_start_time - self.start_time

    @property
    def display_size(self) -> tuple[int, int]:
        """ffmpeg autorotate 后的尺寸；直角旋转交换宽高，其它角度沿用画布。"""
        if math.isclose(self.rotation % 180, 90, abs_tol=1e-4):
            return self.height, self.width
        return self.width, self.height


def _timestamp(value: object) -> float:
    try:
        number = float(str(value))
    except ValueError, OverflowError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _positive_number(value: object) -> float:
    try:
        number = float(Fraction(str(value)))
    except ValueError, ZeroDivisionError, OverflowError:
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


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
    streams = payload.get("streams")
    streams = (
        [item for item in streams if isinstance(item, dict)] if isinstance(streams, list) else []
    )
    kinds = tuple(sorted(str(item.get("codec_type", "")) for item in streams))
    details: list[MediaStream] = []
    for index, stream in enumerate(streams):
        tags = stream.get("tags")
        dispositions = stream.get("disposition")
        stream_index = stream.get("index", index)
        details.append(
            MediaStream(
                index=stream_index if type(stream_index) is int else index,
                kind=str(stream.get("codec_type") or ""),
                codec=str(stream.get("codec_name") or ""),
                language=str(tags.get("language") or "und") if isinstance(tags, dict) else "und",
                dispositions=tuple(sorted(key for key, value in dispositions.items() if value == 1))
                if isinstance(dispositions, dict)
                else (),
                channels=int(_positive_number(stream.get("channels"))),
                sample_rate=int(_positive_number(stream.get("sample_rate"))),
            )
        )
    for stream in streams:
        if stream.get("codec_type") == "video":
            width, height = stream.get("width"), stream.get("height")
            side_data = stream.get("side_data_list")
            rotation = (
                next(
                    (
                        _timestamp(item.get("rotation"))
                        for item in side_data
                        if isinstance(item, dict) and item.get("side_data_type") == "Display Matrix"
                    ),
                    0.0,
                )
                if isinstance(side_data, list)
                else 0.0
            )
            return MediaInfo(
                duration=duration,
                codec_name=str(stream.get("codec_name") or "").strip(),
                video_duration=_positive_number(stream.get("duration")),
                frame_rate=_positive_number(stream.get("avg_frame_rate")),
                time_base=_positive_number(stream.get("time_base")),
                width=width if type(width) is int else 0,
                height=height if type(height) is int else 0,
                stream_types=kinds,
                nominal_frame_rate=_positive_number(stream.get("r_frame_rate")),
                frame_count=int(_positive_number(stream.get("nb_frames"))),
                streams=tuple(details),
                start_time=_timestamp(format_info.get("start_time"))
                if isinstance(format_info, dict)
                else 0.0,
                video_start_time=_timestamp(stream.get("start_time")),
                rotation=rotation,
            )
    return MediaInfo(duration=duration, codec_name="", stream_types=kinds, streams=tuple(details))
