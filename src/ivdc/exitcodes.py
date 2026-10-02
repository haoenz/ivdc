"""进程退出码（仅用于 CLI 边界）。"""

from __future__ import annotations

EXIT_OK = 0
"""全部成功，含「没有需要处理的文件」。"""

EXIT_FAILURE = 1
"""有任务处理失败。"""

EXIT_ENV = 2
"""环境或参数错误：缺 ffmpeg / ffprobe / yt-dlp，路径不存在等。"""

__all__ = ["EXIT_ENV", "EXIT_FAILURE", "EXIT_OK"]
