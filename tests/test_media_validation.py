"""校验必须以视频流为准；时长容差随源视频帧率变化。"""

from dataclasses import replace

import pytest

from ivdc.errors import MediaError
from ivdc.media_validation import expected_frames, validate_media, validate_streams
from ivdc.probe import MediaInfo, MediaStream
from ivdc.probe_runtime import Toolchain, VideoTimeline
from ivdc.runner import ProcessRunner


@pytest.mark.parametrize(
    "change",
    [
        {"language": "zho"},
        {"dispositions": ()},
        {"channels": 1},
        {"sample_rate": 48000},
        {"codec": "mp3"},
    ],
)
def test_same_stream_count_cannot_hide_changed_audio(change):
    video = MediaStream(0, "video", "h264")
    audio = MediaStream(1, "audio", "aac", "eng", ("default",), 2, 44100)
    reference = MediaInfo(1, "h264", stream_types=("audio", "video"), streams=(video, audio))
    output = replace(
        reference,
        codec_name="hevc",
        streams=(replace(video, codec="hevc"), replace(audio, **change)),
    )
    with pytest.raises(MediaError):
        validate_streams(reference, output, "hevc")


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


@pytest.mark.parametrize("mismatch", ["duration", "rate"])
@pytest.mark.parametrize("damage", [None, "missing", "duplicate", "shift", "tail"])
@pytest.mark.parametrize("time_base", [1 / 90000, 1 / 25])
def test_whole_metadata_mismatch_requires_matching_actual_frames(
    tmp_path, monkeypatch, mismatch, damage, time_base
):
    reference = MediaInfo(
        1.2,
        "h264",
        1.0,
        25,
        time_base,
        64,
        64,
        ("video",),
        nominal_frame_rate=25,
        frame_count=25,
        start_time=0.1,
        video_start_time=0.18,
    )
    output = replace(reference, codec_name="hevc", video_duration=1.03 + 4 * time_base)
    if mismatch == "rate":
        output = replace(output, video_duration=1.0, frame_rate=24.999)
    source = tmp_path / "original.mp4"
    target = tmp_path / "encoded.mp4"
    original = VideoTimeline(tuple(0.18 + i / 25 for i in range(25)), 0.04)
    # 输出保持相对容器起点的 80ms 音画偏移。
    stamps = tuple(t - reference.start_time for t in original.timestamps)
    last_duration = original.last_duration
    if damage == "missing":
        stamps = stamps[:-1]
    elif damage == "duplicate":
        stamps = (*stamps[:12], stamps[11], *stamps[13:])
    elif damage == "shift":
        stamps = tuple(t - 0.08 for t in stamps)
    elif damage == "tail":
        last_duration = 0.01
    changed = VideoTimeline(stamps, last_duration)
    monkeypatch.setattr("ivdc.media_validation.probe_media", lambda *args: output)
    monkeypatch.setattr(
        "ivdc.media_validation.probe_video_timeline",
        lambda toolchain, path, runner: original if path == source else changed,
    )
    with ProcessRunner() as runner:

        def validate():
            validate_media(
                Toolchain("ffmpeg", "ffprobe"),
                runner,
                target,
                reference,
                "hevc",
                reference.video_duration,
                source=source,
                decode=False,
            )

        if damage is None:
            validate()
        else:
            with pytest.raises(MediaError, match="逐帧复核失败"):
                validate()


@pytest.mark.parametrize("tail_tolerance", [0.0, 0.2])
def test_segment_must_not_use_whole_file_timeline_fallback(tmp_path, monkeypatch, tail_tolerance):
    reference = MediaInfo(1, "h264", 1, 25, 1 / 90000, 64, 64, ("video",))
    monkeypatch.setattr(
        "ivdc.media_validation.probe_media",
        lambda *args: replace(reference, codec_name="hevc", video_duration=0.5),
    )
    monkeypatch.setattr(
        "ivdc.media_validation.probe_video_timeline",
        lambda *args: pytest.fail("分片不能使用整片复核"),
    )
    with ProcessRunner() as runner, pytest.raises(MediaError, match="视频时长不完整"):
        validate_media(
            Toolchain("ffmpeg", "ffprobe"),
            runner,
            tmp_path / "out.mp4",
            reference,
            "hevc",
            1,
            video_only=True,
            source=tmp_path / "in.mp4",
            decode=False,
            tail_tolerance=tail_tolerance,
        )


@pytest.mark.parametrize("known_tail", [False, True])
@pytest.mark.parametrize("tail_tolerance", [0.0, 0.1, 0.2])
@pytest.mark.parametrize("damage", [None, "missing", "timestamp", "unknown_output", "long_tail"])
def test_explicit_tail_tolerance_never_bypasses_frame_checks(
    tmp_path, monkeypatch, known_tail, tail_tolerance, damage
):
    reference = MediaInfo(
        1,
        "h264",
        1,
        25,
        1 / 90000,
        64,
        64,
        ("video",),
        nominal_frame_rate=25,
        frame_count=25,
    )
    output_info = replace(reference, codec_name="hevc", video_duration=1.12)
    original = VideoTimeline(tuple(i / 25 for i in range(25)), 0.04 if known_tail else None)
    output = VideoTimeline(original.timestamps, 0.16)
    if damage == "missing":
        output = replace(output, timestamps=output.timestamps[:-1])
    elif damage == "timestamp":
        output = replace(output, timestamps=tuple(t + 0.1 for t in output.timestamps))
    elif damage == "unknown_output":
        output = replace(output, last_duration=None)
    elif damage == "long_tail":
        output = replace(output, last_duration=0.5)
    source = tmp_path / "source.mp4"
    monkeypatch.setattr("ivdc.media_validation.probe_media", lambda *args: output_info)
    monkeypatch.setattr(
        "ivdc.media_validation.probe_video_timeline",
        lambda toolchain, path, runner: original if path == source else output,
    )
    with ProcessRunner() as runner:

        def validate():
            validate_media(
                Toolchain("ffmpeg", "ffprobe"),
                runner,
                tmp_path / "out.mp4",
                reference,
                "hevc",
                1,
                source=source,
                tail_tolerance=tail_tolerance,
                decode=False,
            )

        if tail_tolerance == 0.2 and damage is None:
            validate()
        else:
            with pytest.raises(MediaError):
                validate()


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_tail_tolerance_is_rejected_before_processing(tmp_path, value):
    from ivdc.errors import SetupError
    from ivdc.optimization_plan import OptimizeOptions

    with pytest.raises(SetupError, match="tail-tolerance"):
        OptimizeOptions(tmp_path, tail_tolerance=value)
