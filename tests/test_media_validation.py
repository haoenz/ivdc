"""校验必须以视频流为准；时长容差随源视频帧率变化。"""

from dataclasses import replace

import pytest

from ivdc.errors import MediaError
from ivdc.media_validation import expected_frames, validate_media
from ivdc.probe import MediaInfo
from ivdc.probe_runtime import Toolchain
from ivdc.runner import ProcessRunner


@pytest.mark.parametrize(
    "change",
    [
        {"video_duration": 0.6},
        {"video_duration": 0.9},
        {"stream_types": ("video",)},
        {"width": 32},
        {"codec_name": "h264"},
    ],
)
def test_padded_container_duration_cannot_hide_incomplete_video(tmp_path, monkeypatch, change):
    reference = MediaInfo(1, "h264", 1, 10, 1 / 10240, 64, 64, ("audio", "video"))
    output = replace(replace(reference, codec_name="hevc"), **change)
    monkeypatch.setattr("ivdc.media_validation.probe_media", lambda *args: output)
    with ProcessRunner() as runner, pytest.raises(MediaError):
        validate_media(
            Toolchain("ffmpeg", "ffprobe"),
            runner,
            tmp_path / "segment.mp4",
            reference,
            "hevc",
            1,
            decode=False,
        )


@pytest.mark.parametrize("frame_rate", [10, 30, 60])
def test_one_missing_frame_is_not_accepted(tmp_path, monkeypatch, frame_rate):
    reference = MediaInfo(1, "h264", 1, frame_rate, 1 / 30000, 64, 64, ("video",))
    output = replace(reference, codec_name="hevc", video_duration=1 - 1 / frame_rate)
    monkeypatch.setattr("ivdc.media_validation.probe_media", lambda *args: output)
    with ProcessRunner() as runner, pytest.raises(MediaError, match="视频时长不完整"):
        validate_media(
            Toolchain("ffmpeg", "ffprobe"),
            runner,
            tmp_path / "segment.mp4",
            reference,
            "hevc",
            1,
            decode=False,
        )


@pytest.mark.parametrize("field", ["frame_rate", "time_base"])
def test_unknown_reference_timing_fails_closed(tmp_path, monkeypatch, field):
    reference = MediaInfo(1, "h264", 1, 10, 1 / 10240, 64, 64, ("video",))
    output = replace(reference, codec_name="hevc")
    monkeypatch.setattr("ivdc.media_validation.probe_media", lambda *args: output)
    with ProcessRunner() as runner, pytest.raises(MediaError, match="不能确认完整性"):
        validate_media(
            Toolchain("ffmpeg", "ffprobe"),
            runner,
            tmp_path / "segment.mp4",
            replace(reference, **{field: 0}),
            "hevc",
            1,
            decode=False,
        )


def test_constant_frame_rate_quantization_covers_each_source_frame_once():
    reference = MediaInfo(
        61.027633,
        "h264",
        video_duration=61.027633,
        frame_rate=30000 / 1001,
        nominal_frame_rate=30000 / 1001,
        frame_count=1829,
    )
    assert expected_frames(reference, 0, 60) == 1799
    assert expected_frames(reference, 60, reference.video_duration - 60) == 30
    assert expected_frames(reference, 0, reference.video_duration) == 1829
    assert expected_frames(replace(reference, nominal_frame_rate=60), 0, 60) is None


def test_low_frame_rate_does_not_reintroduce_half_second_truncation(tmp_path, monkeypatch):
    reference = MediaInfo(
        1,
        "h264",
        1,
        1,
        1 / 16384,
        64,
        64,
        ("video",),
        nominal_frame_rate=1,
        frame_count=1,
    )
    output = replace(reference, codec_name="hevc", video_duration=0.6, frame_rate=5 / 3)
    monkeypatch.setattr("ivdc.media_validation.probe_media", lambda *args: output)
    with ProcessRunner() as runner, pytest.raises(MediaError, match="帧率不符"):
        validate_media(
            Toolchain("ffmpeg", "ffprobe"),
            runner,
            tmp_path / "segment.mp4",
            reference,
            "hevc",
            1,
            decode=False,
        )
