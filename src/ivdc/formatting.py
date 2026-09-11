"""输出格式化（纯逻辑，不碰 I/O）。

这里的字符串是**对外契约**：屏幕输出与日志文件都依赖它逐字符稳定，改动即改变
既有日志的样子，跨时间的日志比对会因此对不上。格式清单见 SPEC.md §4。
"""

from __future__ import annotations

import math
from datetime import date

_MIB = 1024 * 1024

_SIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1024**4),
    ("GiB", 1024**3),
    ("MiB", 1024**2),
    ("KiB", 1024),
)

_DEFAULT_LABEL_LIMIT = 15

__all__ = [
    "format_clock",
    "format_done_line",
    "format_download_line",
    "format_download_status",
    "format_fail_line",
    "format_log_line",
    "format_media_size",
    "format_size_mib",
    "progress_percent",
    "shorten_label",
]


def progress_percent(elapsed: float, total: float) -> int:
    """已处理时长 / 总时长 → 0-100 的整数百分比。

    向下取整后夹紧到 [0, 100]；总时长非法（≤ 0）时返回 0。
    """
    if total <= 0:
        return 0
    percent = math.floor(elapsed / total * 100)
    return max(0, min(100, int(percent)))


def format_size_mib(num_bytes: int) -> str:
    """字节数 → 按 MiB 换算、带千分位、无小数的文本。

    换算基数是 1024²（MiB），但日志行与屏幕输出里的单位后缀一律写作 ``MB``——
    那是既定的输出格式，不随这个函数名走。
    """
    return f"{num_bytes / _MIB:,.0f}"


def _format_minutes(minutes: float) -> str:
    """耗时 → 一位小数的分钟文本（日志与屏幕输出里 ``3.5m`` 的 ``3.5``）。"""
    return f"{minutes:.1f}"


def format_log_line(
    day: date, name: str, old_bytes: int, new_bytes: int, minutes: float
) -> str:
    """压制成功写入日志的一行。

    格式：``26-09-11 a.mp4 | 1,234MB -> 567MB | 3.5m``
    """
    return (
        f"{day:%y-%m-%d} {name} | "
        f"{format_size_mib(old_bytes)}MB -> {format_size_mib(new_bytes)}MB | "
        f"{_format_minutes(minutes)}m"
    )


def format_fail_line(day: date, name: str, message: str) -> str:
    """压制失败写入日志的一行。"""
    return f"{day:%y-%m-%d} {name} | 压制失败: {message}"


def format_done_line(name: str, old_bytes: int, new_bytes: int, minutes: float) -> str:
    """压制成功后打印到屏幕的一行（带 ✓ 前缀）。"""
    return (
        f"✓ {name} | {format_size_mib(old_bytes)}MB -> "
        f"{format_size_mib(new_bytes)}MB | {_format_minutes(minutes)}m"
    )


def format_download_line(name: str, size: str, minutes: float) -> str:
    """下载成功后打印到屏幕的一行；size 为空时省略该段。"""
    tail = f"{size} | " if size else ""
    return f"✓ {name} | {tail}{_format_minutes(minutes)}m"


def format_media_size(num_bytes: int) -> str:
    """字节数 → ``12.5MiB`` 这样的文本（进度条与下载摘要用）。

    与 :func:`format_size_mib` 不同：这里保留一位小数并自动选单位，
    对应 yt-dlp 自己打印的 ``of ~123.45MiB``。
    """
    for unit, scale in _SIZE_UNITS:
        if num_bytes >= scale:
            return f"{num_bytes / scale:.1f}{unit}"
    return f"{num_bytes}B"


def format_clock(seconds: float) -> str:
    """秒数 → ``MM:SS``（不足一小时）或 ``H:MM:SS``。"""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_download_status(
    downloaded: int, total: int, speed: float | None = None, eta: float | None = None
) -> str:
    """下载进度行右侧的状态文本。"""
    if total > 0:
        head = f"{format_media_size(downloaded)}/{format_media_size(total)}"
    else:
        head = format_media_size(downloaded)
    parts = [head]
    if speed:
        parts.append(f"at {format_media_size(int(speed))}/s")
    if eta:
        parts.append(f"ETA {format_clock(eta)}")
    return " ".join(parts)


def shorten_label(name: str, limit: int = _DEFAULT_LABEL_LIMIT) -> str:
    """过长的文件名截成 ``前10..后3``（截断点固定，改动会让进度条标签左右不一致）。"""
    if len(name) <= limit:
        return name
    head = max(1, limit - 5)
    return f"{name[:head]}..{name[-3:]}"
