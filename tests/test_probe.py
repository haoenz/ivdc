"""能力探测输出的判定与 ffprobe JSON 解析。"""

from __future__ import annotations

import pytest

from ivdc.probe import has_cuda_hwaccel, has_encoder, parse_media_info

ENCODERS = """
 V..... libx265              libx265 H.265 / HEVC (codec hevc)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 V....D av1_nvenc            NVIDIA NVENC av1 encoder (codec av1)
 V..... libvpx-vp9           libvpx VP9 (codec vp9)
"""


def test_has_encoder_matches_whole_word() -> None:
    assert has_encoder(ENCODERS, "libx265") is True
    assert has_encoder(ENCODERS, "hevc_nvenc") is True
    assert has_encoder(ENCODERS, "av1_nvenc") is True
    assert has_encoder(ENCODERS, "libvpx-vp9") is True


def test_has_encoder_absent() -> None:
    assert has_encoder(ENCODERS, "libsvtav1") is False
    assert has_encoder(ENCODERS, "libx264") is False


def test_has_encoder_does_not_match_identifier_prefix() -> None:
    # 下划线属于单词字符，\bhevc\b 不应该在 hevc_nvenc 中间命中
    dump = " V....D hevc_nvenc           NVIDIA NVENC encoder"
    assert has_encoder(dump, "hevc") is False
    assert has_encoder(dump, "hevc_nvenc") is True


def test_has_encoder_is_case_sensitive() -> None:
    assert has_encoder(" V..... LIBX265   ", "libx265") is False


def test_has_cuda_hwaccel_uses_substring_semantics() -> None:
    assert has_cuda_hwaccel("Hardware acceleration methods:\ncuda\ndxva2\nqsv") is True
    assert has_cuda_hwaccel("Hardware acceleration methods:\ndxva2\nqsv") is False


def test_parse_media_info() -> None:
    payload = {
        "format": {"duration": "70.024000"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264"},
        ],
    }
    info = parse_media_info(payload)
    assert info.duration == pytest.approx(70.024)
    assert info.codec_name == "h264"


def test_parse_media_info_skips_leading_audio_stream() -> None:
    payload = {
        "format": {"duration": "10.0"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "hevc"},
        ],
    }
    assert parse_media_info(payload).codec_name == "hevc"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"format": {}},
        {"format": {"duration": None}},
        {"format": {"duration": "N/A"}},
        {"format": {"duration": ""}},
        {"streams": []},
    ],
)
def test_parse_media_info_tolerates_missing_fields(payload: dict) -> None:
    info = parse_media_info(payload)
    assert info.duration == 0.0
    assert info.codec_name == ""
