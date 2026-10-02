"""通过运行所拥有的 ProcessRunner 探测工具、硬件和媒体。"""

import json
import logging
import os
import shutil
from collections.abc import Sequence
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path

from ivdc.errors import MediaError, SetupError
from ivdc.probe import MediaInfo, parse_media_info
from ivdc.runner import CommandError, ProcessRunner, worker_pool

GPU_PROBE_TIMEOUT_SECONDS = 10.0
logger = logging.getLogger(__name__)


class MissingToolsError(SetupError):
    """缺少必需工具。"""


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: str
    ffprobe: str


@dataclass(frozen=True)
class Environment:
    encoders: str
    hwaccels: str
    gpu_available: bool


def find_toolchain() -> Toolchain:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path]
        raise MissingToolsError(f"未找到必需的命令: {', '.join(missing)}，请检查 PATH。")
    return Toolchain(ffmpeg, ffprobe)


def detect_gpu(runner: ProcessRunner) -> bool:
    smi = shutil.which("nvidia-smi")
    if not smi and os.name == "nt" and (system_root := os.environ.get("SystemRoot")):
        candidate = Path(system_root, "System32", "nvidia-smi.exe")
        if candidate.is_file():
            smi = str(candidate)
    if smi:
        try:
            result = runner.capture([smi, "-L"], timeout=GPU_PROBE_TIMEOUT_SECONDS)
            if result.ok and "GPU" in result.stdout:
                return True
        except CommandError as exc:
            logger.warning("可选 GPU 探测失败: %s", exc)
    return False


def probe_environment(toolchain: Toolchain, runner: ProcessRunner) -> Environment:
    outputs: list[str] = []
    for flag in ("-encoders", "-hwaccels"):
        try:
            result = runner.capture([toolchain.ffmpeg, "-hide_banner", flag])
        except CommandError as exc:
            raise SetupError(f"ffmpeg 能力探测无法执行: {exc}") from exc
        if not result.ok:
            raise SetupError(f"ffmpeg {flag} 探测失败 ({result.returncode}): {result.stderr}")
        outputs.append(f"{result.stdout}\n{result.stderr}")
    return Environment(*outputs, gpu_available=detect_gpu(runner))


def probe_media(toolchain: Toolchain, path: str | Path, runner: ProcessRunner) -> MediaInfo:
    result = runner.capture(
        [toolchain.ffprobe, "-v", "error", "-of", "json", "-show_format", "-show_streams", path]
    )
    if not result.ok:
        raise MediaError(f"无法探测 {path} ({result.returncode}): {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("ffprobe 根节点不是对象")
        info = parse_media_info(payload)
        if not info.codec_name:
            raise ValueError("没有视频流")
        return info
    except (ValueError, TypeError) as exc:
        raise MediaError(f"无效的媒体信息 {path}: {exc}") from exc


def is_media_file(toolchain: Toolchain, path: str | Path, runner: ProcessRunner) -> bool:
    try:
        return probe_media(toolchain, path, runner).duration > 0
    except MediaError:
        return False


def probe_many(
    toolchain: Toolchain, paths: Sequence[Path], workers: int, runner: ProcessRunner
) -> dict[Path, MediaInfo | MediaError]:
    results: dict[Path, MediaInfo | MediaError] = {}
    with worker_pool(runner, workers) as pool:
        futures = {
            pool.submit(probe_media, toolchain, path, runner): path for path in dict.fromkeys(paths)
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                results[path] = future.result()
            except MediaError as exc:
                results[path] = exc
    return results
