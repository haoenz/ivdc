"""ivdc —— 基于 yt-dlp 与 ffmpeg 的视频下载 / 压制工具。

公开面由各子模块自己的 ``__all__`` 定义，这里只暴露版本号。不做批量再导出：
一份手工维护的再导出列表会在每次重构后悄悄漂移（历史上就出现过列出未导入名字、
使 ``from ivdc import *`` 抛 AttributeError 的情况）。
"""

from __future__ import annotations

from ivdc.__about__ import __version__

__all__ = ["__version__"]
