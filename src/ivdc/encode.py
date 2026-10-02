"""编码器表与编码参数装配（纯逻辑，不碰 I/O）。

编码器元数据集中在这一张表里，新增编码器只改这里；探测结果以参数形式传入，
真正的子进程调用在调用方，因此所有判定分支都能被测试覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass

from ivdc.errors import SetupError
from ivdc.probe import MediaInfo, has_cuda_hwaccel, has_encoder

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

    ``sw_args`` / ``nvenc_args`` 是参数模板，``{crf}`` 占位符在装配时替换。模板内的
    **参数先后顺序有语义**（``-tag:v hvc1`` 必须紧跟 ``-c:v``），调整模板前先看
    :func:`build_ffmpeg_args` 的说明。
    """

    key: str
    codec_name: str
    sw_encoder: str
    nvenc_encoder: str | None
    default_crf: int
    sw_args: tuple[str, ...]
    nvenc_args: tuple[str, ...] | None


CODECS: dict[str, CodecSpec] = {
    "x265": CodecSpec(
        key="x265",
        codec_name="hevc",
        sw_encoder="libx265",
        nvenc_encoder="hevc_nvenc",
        default_crf=28,
        sw_args=("-c:v", "libx265", "-tag:v", "hvc1", "-crf", "{crf}"),
        nvenc_args=("-c:v", "hevc_nvenc", "-tag:v", "hvc1", "-cq", "{crf}"),
    ),
    "av1": CodecSpec(
        key="av1",
        codec_name="av1",
        sw_encoder="libsvtav1",
        nvenc_encoder="av1_nvenc",
        default_crf=30,
        sw_args=("-c:v", "libsvtav1", "-crf", "{crf}"),
        nvenc_args=("-c:v", "av1_nvenc", "-cq", "{crf}"),
    ),
    "vp9": CodecSpec(
        key="vp9",
        codec_name="vp9",
        sw_encoder="libvpx-vp9",
        nvenc_encoder=None,
        default_crf=31,
        sw_args=("-c:v", "libvpx-vp9", "-crf", "{crf}", "-b:v", "0"),
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
    crf: int = 0
    notices: tuple[str, ...] = ()


def resolve_encode_config(
    codec: str,
    cuda: str,
    *,
    encoders_output: str,
    hwaccels_output: str,
    gpu_available: bool,
    crf: int | None = None,
) -> EncodingConfig:
    """结合 ffmpeg 能力与显卡情况，决定最终用哪个编码器、要不要开硬件加速。

    ``gpu_available`` 由调用方实际探测（nvidia-smi）后传入——ffmpeg 编译
    带 CUDA 并不代表机器上真有 N 卡，用编译信息代替实探测会引入假阳性。
    """
    if codec not in CODECS:
        raise SetupError(f"未知编码器: {codec}")
    spec = CODECS[codec]
    chosen_crf = spec.default_crf if crf is None else crf

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
    encoder_args = tuple(arg.format(crf=chosen_crf) for arg in template)

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
        crf=chosen_crf,
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
    args = ["-v", "error"]
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
    args += ["-f", "mp4", target, "-y"]
    return args
