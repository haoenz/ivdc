"""编码器表与编码参数装配（纯逻辑，不碰 I/O）。

编码器及参数模板集中在 CODECS，画质档位与数值约束由 quality 模块维护。
探测结果以参数形式传入，真正的子进程调用在调用方，判定分支可独立测试。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ivdc.errors import SetupError
from ivdc.probe import MediaInfo, has_cuda_hwaccel, has_encoder
from ivdc.quality import (
    DEFAULT_QUALITY_PROFILES,
    QUALITY_SPECS,
    Quality,
    QualityLevels,
    validate_quality_levels,
)

__all__ = [
    "CODECS",
    "CUDA_MODES",
    "CodecSpec",
    "EncodingConfig",
    "build_ffmpeg_args",
    "resolve_encode_config",
    "preserve_stream_args",
]


@dataclass(frozen=True)
class CodecSpec:
    """一个目标编码器的元数据。

    ``sw_args`` / ``nvenc_args`` 是参数模板，``{quality}`` 占位符在装配时替换。模板内的
    **参数先后顺序有语义**（``-tag:v hvc1`` 必须紧跟 ``-c:v``），调整模板前先看
    :func:`build_ffmpeg_args` 的说明。
    """

    key: str
    codec_name: str
    sw_encoder: str
    nvenc_encoder: str | None
    sw_args: tuple[str, ...]
    nvenc_args: tuple[str, ...] | None


CODECS: dict[str, CodecSpec] = {
    "x265": CodecSpec(
        key="x265",
        codec_name="hevc",
        sw_encoder="libx265",
        nvenc_encoder="hevc_nvenc",
        sw_args=("-c:v", "libx265", "-tag:v", "hvc1", "-crf", "{quality}"),
        nvenc_args=(
            "-c:v",
            "hevc_nvenc",
            "-tag:v",
            "hvc1",
            "-rc",
            "vbr",
            "-cq",
            "{quality}",
            "-b:v",
            "0",
        ),
    ),
    "av1": CodecSpec(
        key="av1",
        codec_name="av1",
        sw_encoder="libsvtav1",
        nvenc_encoder="av1_nvenc",
        sw_args=("-c:v", "libsvtav1", "-crf", "{quality}"),
        nvenc_args=("-c:v", "av1_nvenc", "-rc", "vbr", "-cq", "{quality}", "-b:v", "0"),
    ),
    "vp9": CodecSpec(
        key="vp9",
        codec_name="vp9",
        sw_encoder="libvpx-vp9",
        nvenc_encoder=None,
        sw_args=("-c:v", "libvpx-vp9", "-crf", "{quality}", "-b:v", "0"),
        nvenc_args=None,
    ),
}

CUDA_MODES = ("off", "decode", "encode")


@dataclass(frozen=True)
class EncodingConfig:
    """一次压制任务最终采用的编码器与硬件加速配置。

    回退警告与模式提示以 ``notices`` 数据返回，由调用方负责展示：这里不碰 I/O，
    判定分支才能被单独断言。
    """

    codec: str
    codec_name: str
    encoder_args: tuple[str, ...] = ()
    hwaccel_args: tuple[str, ...] = ()
    use_nvenc: bool = False
    has_cuda: bool = False
    encoder: str = ""
    quality: Quality = Quality.medium
    quality_parameter: str = ""
    quality_value: int = 0
    notices: tuple[str, ...] = ()

    @property
    def quality_description(self) -> str:
        return (
            f"画质 {self.quality.value} | 编码器 {self.encoder} | "
            f"{self.quality_parameter.upper()} {self.quality_value}"
        )


def resolve_encode_config(
    codec: str,
    cuda: str,
    *,
    encoders_output: str,
    hwaccels_output: str,
    gpu_available: bool,
    quality: Quality = Quality.medium,
    quality_profiles: Mapping[str, QualityLevels] = DEFAULT_QUALITY_PROFILES,
) -> EncodingConfig:
    """结合 ffmpeg 能力与显卡情况，决定最终用哪个编码器、要不要开硬件加速。

    ``gpu_available`` 由调用方实际探测（nvidia-smi）后传入——ffmpeg 编译
    带 CUDA 并不代表机器上真有 N 卡，用编译信息代替实探测会引入假阳性。
    """
    if codec not in CODECS:
        raise SetupError(f"未知编码器: {codec}")
    spec = CODECS[codec]
    try:
        quality = Quality(quality)
    except ValueError as exc:
        raise SetupError("quality 必须是 high、medium 或 low") from exc

    if cuda not in CUDA_MODES:
        raise SetupError(f"未知的 CUDA 模式: {cuda}")

    # 软件编码器必须可用，它始终是回退路径
    if not has_encoder(encoders_output, spec.sw_encoder):
        raise SetupError(f"ffmpeg 未包含编码器 '{spec.sw_encoder}'，无法压制为 {spec.key}。")

    notices: list[str] = []

    has_cuda = cuda != "off" and gpu_available and has_cuda_hwaccel(hwaccels_output)

    use_nvenc = False
    if cuda == "encode" and has_cuda:
        if spec.nvenc_encoder is None:
            notices.append("VP9 没有 NVENC 硬件编码器，回退到软件编码。")
        elif not has_encoder(encoders_output, spec.nvenc_encoder):
            notices.append(f"未找到 NVENC 编码器 '{spec.nvenc_encoder}'，回退到软件编码。")
        else:
            use_nvenc = True

    template = spec.nvenc_args if use_nvenc else spec.sw_args
    assert template is not None
    encoder = spec.nvenc_encoder if use_nvenc else spec.sw_encoder
    assert encoder is not None
    levels = quality_profiles.get(encoder)
    if levels is None:
        raise SetupError(f"缺少编码器 {encoder} 的画质档位")
    try:
        validate_quality_levels(encoder, levels)
    except ValueError as exc:
        raise SetupError(str(exc)) from exc
    quality_value = levels.value(quality)
    encoder_args = tuple(arg.format(quality=quality_value) for arg in template)

    hwaccel_args: tuple[str, ...] = ()
    if has_cuda:
        hwaccel_args = ("-hwaccel", "cuda")
        if use_nvenc:
            hwaccel_args += ("-hwaccel_output_format", "cuda")

    if has_cuda:
        mode = "解码 + NVENC 编码" if use_nvenc else "仅解码"
        notices.append(f"检测到 CUDA 支持，硬件加速：{mode}。")
    elif cuda == "off":
        notices.append("已禁用 CUDA（--cuda off），使用纯 CPU 模式工作。")
    else:
        notices.append(f"未检测到 CUDA 支持，--cuda {cuda} 未生效，使用纯 CPU 模式工作。")

    return EncodingConfig(
        codec=spec.key,
        codec_name=spec.codec_name,
        encoder_args=encoder_args,
        hwaccel_args=hwaccel_args,
        use_nvenc=use_nvenc,
        has_cuda=has_cuda,
        encoder=encoder,
        quality=quality,
        quality_parameter=QUALITY_SPECS[encoder].parameter,
        quality_value=quality_value,
        notices=tuple(notices),
    )


def _number(value: float) -> str:
    """把秒数渲染成 ffmpeg 能接受的简洁文本（整数不带小数点）。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def preserve_stream_args(
    media: MediaInfo | None, *, original_input: int = 0, video_input: int = 0
) -> list[str]:
    """按源顺序映射所有流；分片成片的视频来自拼接输入，其余流直接来自原文件。"""
    args: list[str] = []
    if media is None or not media.streams:
        args += ["-map", str(original_input)]
    else:
        for output_index, stream in enumerate(media.streams):
            source = (
                f"{video_input}:v:0"
                if stream.kind == "video"
                else f"{original_input}:{stream.index}"
            )
            args += [
                "-map",
                source,
                f"-map_metadata:s:{output_index}",
                f"{original_input}:s:{stream.index}",
                f"-disposition:{output_index}",
                "+".join(stream.dispositions) or "0",
            ]
    return args + [
        "-map_metadata",
        str(original_input),
        "-map_chapters",
        str(original_input),
        "-c",
        "copy",
    ]


def build_ffmpeg_args(
    config: EncodingConfig,
    source: str,
    target: str,
    *,
    start: float | None = None,
    duration: float | None = None,
    progress: bool = True,
    media: MediaInfo | None = None,
    video_only: bool = False,
) -> list[str]:
    """装配一次 ffmpeg 调用的参数列表。

    参数顺序有语义，不能重排：``-ss`` 必须出现在 ``-i`` 之前（输入级定位）且避开
    硬件加速参数，``-t`` 必须出现在 ``-i`` 之后（输出级截断），编码参数紧随其后。
    """
    # 解码错误不能被 ffmpeg 的默认容错行为转成“成功但缺帧”的结果。
    args = ["-v", "error", "-xerror", "-err_detect", "explode"]
    if progress:
        args += ["-progress", "-"]
    if start is not None:
        args += ["-ss", _number(start)]
    args += list(config.hwaccel_args)
    args += ["-i", source]
    if duration is not None:
        args += ["-t", _number(duration)]
    if video_only:
        args += ["-map", "0:v:0", "-map_metadata", "-1", "-map_chapters", "-1"]
    else:
        args += preserve_stream_args(media)
    args += list(config.encoder_args)
    if not video_only:
        # 默认编码 time base 会把非整帧的音画偏移取整，整片保留输入时间精度。
        # 显式透传帧时间戳：FFmpeg 6 的 auto 模式在音频流排首位时会选择 CFR，
        # 配合 demux time base 会按其倒数大量补帧。
        args += ["-enc_time_base:v", "demux", "-fps_mode:v", "passthrough"]
    args += ["-f", "mp4", target, "-y"]
    return args
