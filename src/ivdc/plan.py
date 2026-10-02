"""分片规划（纯逻辑，不碰 I/O）。"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 计算 ceil 时的噪声容差（比例域），只用来抹掉纯浮点误差
_RATIO_EPSILON = 1e-9
# 尾片短于此长度（秒）就并入前一片，避免产生近乎零长度的分片
_TAIL_EPSILON = 1e-6

__all__ = ["Segment", "plan_segments"]


@dataclass(frozen=True)
class Segment:
    """一个分片：序号、起始秒、时长秒。"""

    index: int
    start: float
    duration: float


def plan_segments(duration: float, segment_seconds: float) -> list[Segment]:
    """把总时长切成等长分片，最后一片按剩余时长收尾。

    ``duration`` 或 ``segment_seconds`` 不合法时抛 ValueError，由调用方提示并跳过该
    文件——取不到时长的文件无法规划分片。

    ``ceil`` 前减去 ``_RATIO_EPSILON`` 抹掉纯
    浮点噪声；尾片短于 ``_TAIL_EPSILON`` 时并入前一片。一旦多切出一片，分片计数与
    拼接清单都会按那个数量走，拼接阶段就会因为缺文件而整体失败。
    """
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("时长必须大于 0")
    if not math.isfinite(segment_seconds) or segment_seconds <= 0:
        raise ValueError("分片时长必须大于 0")

    count = max(1, math.ceil(duration / segment_seconds - _RATIO_EPSILON))
    segments = [
        Segment(
            index=index,
            start=index * segment_seconds,
            duration=min(segment_seconds, duration - index * segment_seconds),
        )
        for index in range(count)
    ]

    if len(segments) > 1 and segments[-1].duration < _TAIL_EPSILON:
        segments.pop()
        prev = segments[-1]
        segments[-1] = Segment(index=prev.index, start=prev.start, duration=duration - prev.start)

    return segments
