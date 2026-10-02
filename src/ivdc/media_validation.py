"""分片与拼接成片的保守校验；无法确认完整时拒绝提交。"""

import math
from pathlib import Path

from ivdc.errors import MediaError
from ivdc.probe import MediaInfo
from ivdc.probe_runtime import Toolchain, probe_media
from ivdc.runner import ProcessRunner


def expected_frames(reference: MediaInfo, start: float, duration: float) -> int | None:
    """恒定帧率按实际落在切片区间内的帧计算，避免把正常帧量化误差当成截断。

    ffprobe 时长文本通常只有六位小数；1e-4 帧只补偿该序列化误差。
    非恒定帧率不能由平均帧率推断帧数，仍采用保守的时长校验。
    """
    rate = reference.frame_rate
    if rate <= 0 or not math.isclose(rate, reference.nominal_frame_rate, rel_tol=1e-6):
        return None
    first = math.ceil(start * rate - 1e-4)
    last = math.ceil((start + duration) * rate - 1e-4)
    if reference.frame_count:
        last = min(last, reference.frame_count)
    return max(0, last - first)


def validate_media(
    toolchain: Toolchain,
    runner: ProcessRunner,
    path: Path,
    reference: MediaInfo,
    codec: str,
    duration: float,
    *,
    decode: bool = True,
    start: float = 0.0,
) -> None:
    info = probe_media(toolchain, path, runner)
    if info.codec_name != codec:
        raise MediaError(f"视频编码不符: {path}")
    if (
        reference.width <= 0
        or reference.height <= 0
        or (info.width, info.height) != (reference.width, reference.height)
    ):
        raise MediaError(f"视频尺寸无法确认或不符: {path}")
    if info.stream_types != reference.stream_types:
        raise MediaError(f"媒体流数量或类型不符: {path}")
    if reference.frame_rate <= 0 or reference.time_base <= 0 or info.video_duration <= 0:
        raise MediaError(f"缺少可靠的视频时长、帧率或时间基准，不能确认完整性: {path}")
    # 不能用容器时长（可能被音频填满）代替视频时长。
    frames = expected_frames(reference, start, duration)
    tolerance = 2 * reference.time_base
    if frames is not None:
        tolerance += 0.5 / reference.frame_rate
        if not math.isclose(info.frame_rate, reference.frame_rate, rel_tol=1e-6):
            raise MediaError(f"视频帧率不符: {path}")
    # VFR 无法用平均帧率推断边界误差，只容许时间戳精度误差，不能确认时拒绝提交。
    expected = frames / reference.frame_rate if frames is not None else duration
    if frames is not None and info.frame_count and info.frame_count != frames:
        raise MediaError(f"视频帧数不完整: {path}，预期 {frames} 帧，实际 {info.frame_count} 帧")
    if abs(info.video_duration - expected) > tolerance:
        raise MediaError(
            f"视频时长不完整: {path}，预期 {expected:.6f}s，实际 {info.video_duration:.6f}s"
        )
    if decode:
        result = runner.capture(
            [
                toolchain.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                path,
                "-map",
                "0:v",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ]
        )
        if not result.ok or result.stderr.strip():
            raise MediaError(f"媒体解码校验失败: {path}: {result.stderr.strip()}")
