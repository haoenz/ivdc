"""输入解析（纯逻辑，不碰 I/O）：任务清单行、ffmpeg 进度、ffprobe JSON。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_URL_RE = re.compile(r"https?://\S+")
# 只在「最后一段非空白之前」的那个空格处切分，标题里自带的空格不会被误切
_LAST_SPACE_RE = re.compile(r" (?=\S+$)")
_PROGRESS_RE = re.compile(r"^([a-z_]+)=(.*)$")
_ANY_KEY_RE = re.compile(r"^[A-Za-z0-9_]+=")

OUT_TIME_KEY = "out_time"
"""``ffmpeg -progress`` 里表示「已输出时长」的那个键，进度条只认它。"""

__all__ = [
    "OUT_TIME_KEY",
    "TaskLine",
    "is_progress_line",
    "parse_out_time",
    "parse_progress_line",
    "parse_task_line",
    "strip_fraction",
]


@dataclass(frozen=True)
class TaskLine:
    """任务清单的一行。只给了 URL 时 title 为 None。"""

    title: str | None
    url: str


def parse_task_line(line: str) -> TaskLine:
    """解析 pending_downloads.txt 的一行，标题与链接顺序可互换。

    先按 ``http(s)://`` 定位链接，链接之外的部分算标题；定位不到链接时按「最后一个
    空格」切成标题 + 链接，切不出两部分就把整行当 URL。结果统一 strip。
    """
    text = line.strip()
    match = _URL_RE.search(text)
    if match:
        # 链接可以在行首也可以在行尾，取链接两侧的残余即标题。不用「把链接子串从整行里
        # 抹掉」的写法：那样同一个链接在标题里重复出现时会被一起删掉。
        remainder = f"{text[: match.start()]} {text[match.end() :]}".strip()
        return TaskLine(remainder or None, match.group(0))

    match = _LAST_SPACE_RE.search(text)
    if match:
        title = text[: match.start()].strip()
        return TaskLine(title or None, text[match.end() :].strip())

    return TaskLine(None, text)


def parse_progress_line(line: str) -> tuple[str, str] | None:
    """解析 ffmpeg ``-progress`` 输出的一行 ``key=value``，不匹配返回 None。"""
    match = _PROGRESS_RE.match(line.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def is_progress_line(line: str) -> bool:
    """判断一行是不是 ffmpeg ``-progress`` 的输出。

    比 :func:`parse_progress_line` 宽：``stream_0_0_q`` 这类键带数字，也是进度输出。
    用来把进度行从 ffmpeg 的报错文本里择出去。
    """
    return _ANY_KEY_RE.match(line.strip()) is not None


def parse_out_time(value: str) -> float | None:
    """把 ffmpeg 的 ``out_time`` 值（HH:MM:SS.ffffff）转成秒。

    无法解析或值为 ``N/A`` 时返回 None，交由调用方决定是否忽略这一帧进度。
    小时数可以超过 24，所以不按日期时间解析。
    """
    text = value.strip()
    if not text or text.startswith("N/A"):
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def strip_fraction(value: str) -> str:
    """去掉 out_time 的小数部分及其后内容，用于进度条上的 ``HH:MM:SS`` 文本。"""
    return re.sub(r"\.\d+.*$", "", value.strip())
