"""分片与拼接成片的保守校验；无法确认完整时拒绝提交。"""

import logging
import math
from itertools import pairwise
from pathlib import Path

from ivdc.errors import MediaError
from ivdc.probe import MediaInfo
from ivdc.probe_runtime import Toolchain, probe_media, probe_video_timeline
from ivdc.runner import ProcessRunner


def validate_streams(
    reference: MediaInfo, info: MediaInfo, codec: str, *, video_only: bool = False
) -> None:
    if info.codec_name != codec:
        raise MediaError("视频编码不符")
    if video_only:
        if info.stream_types.count("video") != 1:
            raise MediaError("分片视频流数量不符")
        return
    if info.stream_types != reference.stream_types:
        raise MediaError("媒体流数量或类型不符，拒绝丢弃音轨或字幕")
    if len(reference.streams) != len(info.streams):
        raise MediaError("媒体流描述数量不符")
    for original, output in zip(reference.streams, info.streams, strict=True):
        if original.kind != output.kind:
            raise MediaError("媒体流顺序不符")
        if original.kind != "video" and original.codec != output.codec:
            raise MediaError("音轨或字幕编码发生变化，要求原样复制")
        if (original.language, original.dispositions) != (output.language, output.dispositions):
            raise MediaError("媒体流语言或默认/强制标志不符")
        if original.kind == "audio" and (original.channels, original.sample_rate) != (
            output.channels,
            output.sample_rate,
        ):
            raise MediaError("音轨声道数或采样率不符")


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


def _validate_timing(
    reference: MediaInfo, info: MediaInfo, path: Path, start: float, duration: float
) -> None:
    # 快速路径保留分片边界量化及严格时长检查。
    frames = expected_frames(reference, start, duration)
    tolerance = 2 * reference.time_base
    if frames is not None:
        tolerance += 0.5 / reference.frame_rate
        if not math.isclose(info.frame_rate, reference.frame_rate, rel_tol=1e-6):
            raise MediaError(f"视频帧率不符: {path}")
    expected = frames / reference.frame_rate if frames is not None else duration
    if frames is not None and info.frame_count and info.frame_count != frames:
        raise MediaError(f"视频帧数不完整: {path}，预期 {frames} 帧，实际 {info.frame_count} 帧")
    if abs(info.video_duration - expected) > tolerance:
        raise MediaError(
            f"视频时长不完整: {path}，预期 {expected:.6f}s，实际 {info.video_duration:.6f}s"
        )


def _validate_whole_timeline(
    toolchain: Toolchain,
    runner: ProcessRunner,
    source: Path,
    path: Path,
    reference: MediaInfo,
    info: MediaInfo,
    tail_tolerance: float = 0.0,
) -> None:
    if info.time_base <= 0:
        raise MediaError("缺少输出视频时间基准")
    original = probe_video_timeline(toolchain, source, runner)
    output = probe_video_timeline(toolchain, path, runner)
    if len(original.timestamps) != len(output.timestamps):
        raise MediaError(
            f"视频帧数不完整，预期 {len(original.timestamps)} 帧，实际 {len(output.timestamps)} 帧"
        )
    # ffmpeg 将输入容器起点归零；保留视频相对音频的偏移，不能各自减去首帧。
    # 两个 time base 单位覆盖量化，额外 2us 覆盖 ffprobe 的六位小数序列化。
    # 时间基准较粗时也不能让容差覆盖完整一帧或不规则的短末帧。
    intervals = (right - left for left, right in pairwise(original.timestamps) if right > left)
    shortest = min(intervals, default=1 / reference.frame_rate)
    tolerance = min(
        2 * max(reference.time_base, info.time_base) + 2e-6,
        min(shortest, original.last_duration or shortest) / 4,
    )
    for index, (before, after) in enumerate(
        zip(original.timestamps, output.timestamps, strict=True)
    ):
        expected = before - reference.start_time
        if abs(after - expected) > tolerance:
            raise MediaError(
                f"视频帧时间戳不符，第 {index + 1} 帧预期 {expected:.6f}s，实际 {after:.6f}s"
            )
    if output.last_duration is None:
        raise MediaError(f"无法确认输出视频末帧时长: {path}")
    if original.last_duration is None:
        if tail_tolerance == 0:
            raise MediaError(f"无法确认视频末帧时长: {source}")
        # 所有实际显示帧均已匹配。仅在末帧自身和实际视频总时长差异都
        # 不超过用户容差时，接受源文件缺失末帧显示时长的情况。
        actual_duration = output.timestamps[-1] - output.timestamps[0] + output.last_duration
        difference = abs(actual_duration - reference.video_duration)
        if max(difference, output.last_duration) > tail_tolerance + 2e-6:
            raise MediaError(
                f"末尾时长差异超出容差 {tail_tolerance:.6f}s："
                f"总时长差 {difference:.6f}s，输出末帧 {output.last_duration:.6f}s"
            )
    else:
        difference = abs(original.last_duration - output.last_duration)
        if difference <= tolerance:
            return
        if difference > tail_tolerance + 2e-6 or tail_tolerance == 0:
            raise MediaError(f"视频末帧时长不符，差异 {difference:.6f}s")
    logging.getLogger(__name__).info(
        "逐帧复核通过，按 %.3fs 末尾容差接受 %.6fs 时长差异", tail_tolerance, difference
    )


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
    video_only: bool = False,
    source: Path | None = None,
    tail_tolerance: float = 0.0,
) -> None:
    info = probe_media(toolchain, path, runner)
    validate_streams(reference, info, codec, video_only=video_only)
    if (
        reference.width <= 0
        or reference.height <= 0
        or (info.width, info.height) != reference.display_size
    ):
        raise MediaError(f"视频尺寸无法确认或不符: {path}")
    if not math.isclose(info.rotation % 360, 0, abs_tol=1e-4):
        raise MediaError(f"视频已经旋转像素，输出不能再次应用旋转矩阵: {path}")
    if reference.frame_rate <= 0 or reference.time_base <= 0 or info.video_duration <= 0:
        raise MediaError(f"缺少可靠的视频时长、帧率或时间基准，不能确认完整性: {path}")
    try:
        _validate_timing(reference, info, path, start, duration)
    except MediaError as exc:
        if (
            source is None
            or video_only
            or start != 0
            or not math.isclose(duration, reference.video_duration, rel_tol=0, abs_tol=1e-6)
        ):
            raise
        # 整片元数据不一致时，以实际显示帧复核，不能简单放宽时长/平均帧率阈值。
        try:
            _validate_whole_timeline(
                toolchain, runner, source, path, reference, info, tail_tolerance
            )
        except MediaError as timeline_error:
            raise MediaError(f"{exc}；逐帧复核失败: {timeline_error}") from timeline_error
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
                *([] if video_only else ["-map", "0:a?"]),
                # 解码复核也须保留时间精度，否则不规则末帧会在 null 输出中
                # 被量化到同一个 DTS，造成“解码失败”的二次误报。
                "-fps_mode:v",
                "passthrough",
                "-enc_time_base:v",
                "demux",
                "-f",
                "null",
                "-",
            ]
        )
        if not result.ok or result.stderr.strip():
            raise MediaError(f"媒体解码校验失败: {path}: {result.stderr.strip()}")
