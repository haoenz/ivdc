"""控制台输出。

所有面向用户的文本都从这里出去，样式集中定义成一组语义常量——各模块只挑语义
（``STYLE_WARN`` / ``STYLE_DONE`` …），不自己拼颜色。

输出一律走 ``markup=False``：文件名和命令行里出现 ``[`` 时不会被当成 rich 标记。
"""

from __future__ import annotations

from functools import lru_cache

from rich.console import Console

STYLE_DONE = "green"
STYLE_INFO = "cyan"
STYLE_WARN = "yellow"
STYLE_FAIL = "yellow"
STYLE_SKIP = "grey62"
STYLE_ERROR = "red"
STYLE_PLAIN = ""


__all__ = [
    "STYLE_DONE",
    "STYLE_ERROR",
    "STYLE_FAIL",
    "STYLE_INFO",
    "STYLE_PLAIN",
    "STYLE_SKIP",
    "STYLE_WARN",
    "emit",
    "get_console",
]


@lru_cache(maxsize=1)
def get_console() -> Console:
    """进程内共享的 Console（延迟创建，便于测试替换 stdout）。"""
    return Console(highlight=False)


def emit(console: Console, message: str, style: str = STYLE_PLAIN) -> None:
    """打印一行不受 rich 标记语法影响的文本。"""
    console.print(message, style=style or None, markup=False, highlight=False, soft_wrap=True)
