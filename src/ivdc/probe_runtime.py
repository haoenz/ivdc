"""能力探测的运行时层（唯一碰子进程的探测代码）。

判定逻辑不在这里：拿到文本之后怎么判断属于 :mod:`ivdc.probe`，这里只负责
「把文本取回来」和「用 ffprobe 取元数据」。
"""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ivdc.probe import MediaInfo, parse_media_info
from ivdc.runner import CommandResult, capture_command

__all__ = [
    "GPU_PROBE_TIMEOUT_SECONDS",
    "Environment",
    "MissingToolsError",
    "Toolchain",
    "detect_gpu",
    "find_toolchain",
    "is_media_file",
    "probe_environment",
    "probe_many",
    "probe_media",
]

GPU_PROBE_TIMEOUT_SECONDS = 10.0


class MissingToolsError(RuntimeError):
    """缺少必需的外部命令。"""


@dataclass(frozen=True)
class Toolchain:
    """外部命令的可执行文件路径。"""

    ffmpeg: str
    ffprobe: str


@dataclass(frozen=True)
class Environment:
    """ffmpeg 与显卡的能力快照。"""

    encoders: str
    hwaccels: str
    gpu_available: bool


def find_toolchain() -> Toolchain:
    """定位 ffmpeg / ffprobe，缺失时抛 :class:`MissingToolsError`。"""
    resolved = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    missing = [name for name, path in resolved.items() if not path]
    if missing:
        raise MissingToolsError(
            "未找到必需的命令: " + ", ".join(missing) + "，请确保已安装并在 PATH 中。"
        )
    return Toolchain(ffmpeg=resolved["ffmpeg"] or "", ffprobe=resolved["ffprobe"] or "")


def detect_gpu() -> bool:
    """是否真的有可用的 NVIDIA 显卡。

    ffmpeg 编译时带 CUDA 并不代表机器上有卡，所以这里实际执行一次 ``nvidia-smi``；
    它不在 PATH 时退回检查 ``nvcuda.dll``。
    """
    smi = shutil.which("nvidia-smi")
    if smi:
        result = capture_command([smi, "-L"], timeout=GPU_PROBE_TIMEOUT_SECONDS)
        if result.ok:
            return True
    system_root = os.environ.get("SystemRoot")
    if os.name == "nt" and system_root:
        return Path(system_root, "System32", "nvcuda.dll").exists()
    return False


def _combined(result: CommandResult) -> str:
    return f"{result.stdout}\n{result.stderr}"


def probe_environment(toolchain: Toolchain) -> Environment:
    """一次性取回 ffmpeg 的编码器列表与硬件加速列表，并探测显卡。"""
    encoders = capture_command([toolchain.ffmpeg, "-hide_banner", "-encoders"])
    hwaccels = capture_command([toolchain.ffmpeg, "-hide_banner", "-hwaccels"])
    return Environment(
        encoders=_combined(encoders),
        hwaccels=_combined(hwaccels),
        gpu_available=detect_gpu(),
    )


def probe_media(toolchain: Toolchain, path: str | Path) -> MediaInfo:
    """取回一个视频的时长与视频编码。

    一次 ``-show_format -show_streams`` 取回全部需要的字段（见 SPEC.md §6.7）。
    探测失败返回时长 0，由调用方按「无法获取时长」处理。
    """
    result = capture_command(
        [
            toolchain.ffprobe,
            "-v",
            "error",
            "-of",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if not result.ok:
        return MediaInfo(duration=0.0, codec_name="")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return MediaInfo(duration=0.0, codec_name="")
    if not isinstance(payload, dict):
        return MediaInfo(duration=0.0, codec_name="")
    return parse_media_info(payload)


def is_media_file(toolchain: Toolchain, path: str | Path) -> bool:
    """用 ffprobe 判断一个文件是不是能解析的媒体（分片续压时的有效性校验）。"""
    return capture_command([toolchain.ffprobe, "-v", "error", str(path)]).ok


def probe_many(
    toolchain: Toolchain, paths: Sequence[Path], workers: int
) -> dict[Path, MediaInfo]:
    """并行探测多个文件（ffprobe 是子进程，线程池足够）。"""
    unique = list(dict.fromkeys(paths))
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda item: probe_media(toolchain, item), unique))
    return dict(zip(unique, results))
