"""真实 ffmpeg：音轨、字幕和元数据必须在整片及分片提交后保持完整。"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ivdc.cli import execute_optimize
from ivdc.encoding import Encoder
from ivdc.exitcodes import EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions
from ivdc.plan import plan_segments
from ivdc.runner import ProcessRunner

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="需要真实 ffmpeg 与 ffprobe"),
]


def ffmpeg(*args):
    assert FFMPEG is not None
    return subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-v", "error", *map(str, args)],
        check=True,
        capture_output=True,
        text=True,
    )


def probe(path, *args):
    assert FFPROBE is not None
    result = subprocess.run(
        [FFPROBE, "-v", "error", *args, "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def nonvideo_packets(path):
    streams = probe(path, "-show_streams")["streams"]
    nonvideo = {item["index"] for item in streams if item["codec_type"] != "video"}
    return [
        (
            packet["stream_index"],
            packet.get("pts_time"),
            packet.get("duration_time"),
            packet["data_hash"],
        )
        for packet in probe(path, "-show_packets", "-show_data_hash", "sha256")["packets"]
        if packet["stream_index"] in nonvideo
    ]


def stream_signature(path):
    return [
        (
            item["codec_type"],
            item["codec_name"] if item["codec_type"] != "video" else "video",
            item.get("tags", {}).get("language", "und"),
            item["disposition"],
            item.get("channels"),
            item.get("sample_rate"),
        )
        for item in probe(path, "-show_streams")["streams"]
    ]


@pytest.fixture
def source(tmp_path):
    first, second = tmp_path / "first.srt", tmp_path / "second.srt"
    first.write_text(
        "1\n00:00:00,200 --> 00:00:02,800\nAcross segment boundaries\n", encoding="utf-8"
    )
    second.write_text("1\n00:00:01,200 --> 00:00:03,200\n第二条字幕\n", encoding="utf-8")
    source = tmp_path / "multiple.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=10:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3.4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=3.2",
        "-i",
        first,
        "-i",
        second,
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:a",
        "-map",
        "3:s",
        "-map",
        "4:s",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-c:s",
        "mov_text",
        "-metadata:s:a:0",
        "language=eng",
        "-metadata:s:a:1",
        "language=zho",
        "-metadata:s:s:0",
        "language=eng",
        "-metadata:s:s:1",
        "language=zho",
        "-disposition:a:0",
        "0",
        "-disposition:a:1",
        "default",
        "-disposition:s:0",
        "0",
        "-disposition:s:1",
        "forced",
        source,
    )
    return source


def options(source, segment_minutes, monkeypatch):
    # 三秒样本也实际执行三片编码及拼接；视频之后的音频、字幕尾部仍须保留。
    monkeypatch.setattr(
        "ivdc.optimization_plan.plan_segments",
        lambda duration, _, **kw: plan_segments(duration, 1, **kw),
    )
    return OptimizeOptions(
        source.parent,
        pattern=source.name,
        cuda="off",
        workers=2,
        segment_minutes=segment_minutes,
        keep_backup=False,
    )


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_all_tracks_payload_timestamps_and_flags_are_preserved(
    source,
    monkeypatch,
    console_pair,
    segment_minutes,
):
    signature = stream_signature(source)
    packets = sorted(nonvideo_packets(source))
    assert len(signature) == 5
    console, output = console_pair
    assert execute_optimize(options(source, segment_minutes, monkeypatch), console) == EXIT_OK, (
        output.getvalue()
    )
    assert stream_signature(source) == signature
    assert sorted(nonvideo_packets(source)) == packets
    assert (
        probe(source, "-select_streams", "v:0", "-show_streams")["streams"][0]["codec_name"]
        == "hevc"
    )
    assert not IvdcLayout(source.parent).backup(source.name).exists()


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_silent_stream_loss_prevents_commit_even_with_successful_ffmpeg_exit(
    source,
    monkeypatch,
    console_pair,
    segment_minutes,
):
    original = source.read_bytes()
    stream, capture = ProcessRunner.stream, ProcessRunner.capture

    def discard_tracks(target):
        temporary = target.with_name("dropped.mp4")
        ffmpeg("-i", target, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", temporary)
        os.replace(temporary, target)

    def damaged_stream(self, argv, **kwargs):
        result = stream(self, argv, **kwargs)
        if not segment_minutes and "-progress" in argv:
            discard_tracks(Path(argv[-2]))
        return result

    def damaged_capture(self, argv, **kwargs):
        result = capture(self, argv, **kwargs)
        if "concat" in argv:
            discard_tracks(Path(argv[-2]))
        return result

    monkeypatch.setattr(ProcessRunner, "stream", damaged_stream)
    monkeypatch.setattr(ProcessRunner, "capture", damaged_capture)
    console, output = console_pair
    assert execute_optimize(options(source, segment_minutes, monkeypatch), console) == EXIT_FAILURE
    assert "媒体流数量或类型不符" in output.getvalue()
    assert source.read_bytes() == original
    layout = IvdcLayout(source.parent)
    assert layout.backup(source.name).read_bytes() == original
    assert not layout.tmp(source.name).exists()


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_subtitle_unsupported_by_output_container_fails_without_losing_data(
    tmp_path,
    monkeypatch,
    console_pair,
    segment_minutes,
):
    subtitle = tmp_path / "subtitle.srt"
    subtitle.write_text("1\n00:00:00,100 --> 00:00:02,800\nKeep this subtitle\n")
    source = tmp_path / "unsupported.mkv"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=10:duration=3",
        "-i",
        subtitle,
        "-c:v",
        "libx264",
        "-c:s",
        "srt",
        source,
    )
    original = source.read_bytes()
    console, output = console_pair
    assert execute_optimize(options(source, segment_minutes, monkeypatch), console) == EXIT_FAILURE
    assert "subrip" in output.getvalue(), output.getvalue()
    assert source.read_bytes() == original
    assert IvdcLayout(tmp_path).backup(source.name).read_bytes() == original


def test_legacy_single_audio_segments_do_not_lose_original_tracks(
    source, console_pair, monkeypatch
):
    layout = IvdcLayout(source.parent)
    layout.seg_dir(source.name).mkdir(parents=True)
    for index in range(3):
        ffmpeg(
            "-ss",
            index,
            "-i",
            source,
            "-t",
            1,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "libx265",
            "-c:a",
            "aac",
            layout.seg(source.name, index),
        )
    before = sorted(nonvideo_packets(source))
    stat = source.stat()
    (layout.seg_dir(source.name) / "resume.json").write_text(
        json.dumps(
            {
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "codec": "hevc",
                "encoder_args": ["-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28"],
                "segments": [[0, 1], [1, 1], [2, 1]],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(Encoder, "encode", lambda *args: pytest.fail("完整旧分片应复用视频"))
    console, output = console_pair
    assert execute_optimize(options(source, 1, monkeypatch), console) == EXIT_OK, output.getvalue()
    assert sorted(nonvideo_packets(source)) == before


@pytest.mark.parametrize("segment_minutes", [0, 1])
def test_audio_before_video_keeps_original_stream_order(
    source, monkeypatch, console_pair, segment_minutes
):
    reordered = source.with_name("reordered.mp4")
    ffmpeg("-i", source, "-map", "0:a", "-map", "0:v", "-map", "0:s", "-c", "copy", reordered)
    signature = stream_signature(reordered)
    packets = sorted(nonvideo_packets(reordered))
    video = probe(reordered, "-select_streams", "v:0", "-show_streams", "-show_packets")
    assert signature[0][0] == "audio"
    console, output = console_pair
    assert execute_optimize(options(reordered, segment_minutes, monkeypatch), console) == EXIT_OK, (
        output.getvalue()
    )
    assert stream_signature(reordered) == signature
    assert sorted(nonvideo_packets(reordered)) == packets
    encoded = probe(reordered, "-select_streams", "v:0", "-show_streams", "-show_packets")
    # FFmpeg 6 的默认帧同步模式曾将 30 帧补为 30720 帧；同时检查播放时间戳。
    for field in ("avg_frame_rate", "nb_frames", "duration"):
        assert encoded["streams"][0][field] == video["streams"][0][field]
    assert sorted(p["pts_time"] for p in encoded["packets"]) == sorted(
        p["pts_time"] for p in video["packets"]
    )


def test_additional_video_fails_before_encoding(tmp_path, monkeypatch, console_pair):
    source = tmp_path / "two-videos.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=10:duration=1",
        "-map",
        "0:v",
        "-map",
        "0:v",
        "-c:v",
        "libx264",
        source,
    )
    original = source.read_bytes()
    console, output = console_pair
    assert execute_optimize(options(source, 0, monkeypatch), console) == EXIT_FAILURE
    assert "额外视频" in output.getvalue()
    assert source.read_bytes() == original
    assert not IvdcLayout(tmp_path).backup(source.name).exists()
