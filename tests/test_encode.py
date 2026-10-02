"""编码器判定与 ffmpeg 参数装配。"""

from __future__ import annotations

import pytest

from ivdc.encode import (
    CODECS,
    build_ffmpeg_args,
    resolve_encode_config,
)

ENCODERS = """
 V..... = Video
 ------
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V..... libx265              libx265 H.265 / HEVC (codec hevc)
 V..... libsvtav1            SVT-AV1 encoder (codec av1)
 V..... libvpx-vp9           libvpx VP9 (codec vp9)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 V....D av1_nvenc            NVIDIA NVENC av1 encoder (codec av1)
"""

ENCODERS_NO_NVENC = """
 V..... libx265              libx265 H.265 / HEVC (codec hevc)
 V..... libsvtav1            SVT-AV1 encoder (codec av1)
 V..... libvpx-vp9           libvpx VP9 (codec vp9)
"""

ENCODERS_NO_X265 = """
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
"""

HWACCELS = """
Hardware acceleration methods:
cuda
dxva2
qsv
"""

HWACCELS_NO_CUDA = """
Hardware acceleration methods:
dxva2
qsv
"""


def resolve(codec: str, cuda: str, **overrides):
    kwargs = {
        "encoders_output": ENCODERS,
        "hwaccels_output": HWACCELS,
        "gpu_available": True,
    }
    kwargs.update(overrides)
    return resolve_encode_config(codec, cuda, **kwargs)


def test_software_encoder_when_cuda_off() -> None:
    config = resolve("x265", "off")
    assert config.has_cuda is False
    assert config.use_nvenc is False
    assert config.encoder_args == ("-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28")
    assert config.hwaccel_args == ()
    assert any("已禁用 CUDA" in notice for notice in config.notices)


def test_decode_only_uses_hwaccel_without_nvenc() -> None:
    config = resolve("x265", "decode")
    assert config.has_cuda is True
    assert config.use_nvenc is False
    assert config.hwaccel_args == ("-hwaccel", "cuda")
    assert config.encoder_args[:2] == ("-c:v", "libx265")
    assert any("仅解码" in notice for notice in config.notices)


def test_encode_mode_switches_to_nvenc() -> None:
    config = resolve("x265", "encode")
    assert config.use_nvenc is True
    assert config.encoder_args == ("-c:v", "hevc_nvenc", "-tag:v", "hvc1", "-cq", "28")
    assert config.hwaccel_args == ("-hwaccel", "cuda", "-hwaccel_output_format", "cuda")
    assert any("解码 + NVENC 编码" in notice for notice in config.notices)


def test_nvenc_missing_falls_back_to_software() -> None:
    config = resolve("x265", "encode", encoders_output=ENCODERS_NO_NVENC)
    assert config.use_nvenc is False
    assert config.encoder_args[:2] == ("-c:v", "libx265")
    assert any("未找到 NVENC 编码器 'hevc_nvenc'" in notice for notice in config.notices)


def test_vp9_has_no_nvenc_variant() -> None:
    config = resolve("vp9", "encode")
    assert config.use_nvenc is False
    assert config.encoder_args == ("-c:v", "libvpx-vp9", "-crf", "31", "-b:v", "0")
    assert any("VP9 没有 NVENC" in notice for notice in config.notices)


def test_missing_software_encoder_is_an_error() -> None:
    with pytest.raises(ValueError, match="libx265"):
        resolve("x265", "decode", encoders_output=ENCODERS_NO_X265)


def test_no_cuda_compiled_in_disables_hwaccel() -> None:
    config = resolve("x265", "encode", hwaccels_output=HWACCELS_NO_CUDA)
    assert config.has_cuda is False
    assert config.hwaccel_args == ()
    assert any("未检测到 CUDA 支持" in notice for notice in config.notices)


def test_missing_gpu_disables_hwaccel() -> None:
    config = resolve("x265", "decode", gpu_available=False)
    assert config.has_cuda is False
    assert config.hwaccel_args == ()


def test_crf_override_applies_to_software_and_nvenc() -> None:
    software = resolve("x265", "off", crf=18)
    assert software.encoder_args[-1] == "18"
    hardware = resolve("x265", "encode", crf=18)
    assert hardware.encoder_args[-1] == "18"
    assert hardware.crf == 18


@pytest.mark.parametrize("codec", sorted(CODECS))
def test_default_crf_matches_codec_table(codec: str) -> None:
    config = resolve(codec, "off")
    spec = CODECS[codec]
    assert spec.sw_encoder in config.encoder_args
    assert str(spec.default_crf) in config.encoder_args


def test_unknown_cuda_mode_raises() -> None:
    with pytest.raises(ValueError):
        resolve("x265", "turbo")


def test_build_ffmpeg_args_full_file() -> None:
    config = resolve("x265", "off")
    args = build_ffmpeg_args(config, "in.mp4", "out.mp4")
    assert args == [
        "-v",
        "error",
        "-progress",
        "-",
        "-i",
        "in.mp4",
        "-c:v",
        "libx265",
        "-tag:v",
        "hvc1",
        "-crf",
        "28",
        "-f",
        "mp4",
        "out.mp4",
        "-y",
    ]


def test_build_ffmpeg_args_segment_places_ss_before_input() -> None:
    config = resolve("x265", "off")
    args = build_ffmpeg_args(config, "in.mp4.original", "seg_000.mp4", start=60.0, duration=60.0)
    assert args == [
        "-v",
        "error",
        "-progress",
        "-",
        "-ss",
        "60",
        "-i",
        "in.mp4.original",
        "-t",
        "60",
        "-c:v",
        "libx265",
        "-tag:v",
        "hvc1",
        "-crf",
        "28",
        "-f",
        "mp4",
        "seg_000.mp4",
        "-y",
    ]


def test_build_ffmpeg_args_keeps_ss_before_hwaccel() -> None:
    config = resolve("x265", "encode")
    args = build_ffmpeg_args(config, "in.mp4", "out.mp4", start=90.0)
    assert args[:12] == [
        "-v",
        "error",
        "-progress",
        "-",
        "-ss",
        "90",
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-i",
        "in.mp4",
    ]


def test_build_ffmpeg_args_fractional_seconds() -> None:
    config = resolve("x265", "off")
    args = build_ffmpeg_args(config, "in.mp4", "out.mp4", start=60.5, duration=0.25)
    assert "-ss" in args and args[args.index("-ss") + 1] == "60.5"
    assert args[args.index("-t") + 1] == "0.25"


def test_build_ffmpeg_args_can_disable_progress() -> None:
    config = resolve("x265", "off")
    args = build_ffmpeg_args(config, "in.mp4", "out.mp4", progress=False)
    assert "-progress" not in args
