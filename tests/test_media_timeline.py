"""非零容器和流起点：分片覆盖画面并保留原音画相对时间。"""

import json
import shutil
import subprocess

import pytest

from ivdc.cli import execute_optimize
from ivdc.optimization_plan import OptimizeOptions
from ivdc.plan import plan_segments

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="需要真实 ffmpeg 与 ffprobe"),
]


def ffmpeg(*args):
    subprocess.run([FFMPEG, "-v", "error", *map(str, args)], check=True, capture_output=True)


def probe(path, *args):
    result = subprocess.run(
        [FFPROBE, "-v", "error", *args, "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def packets(path):
    return probe(path, "-show_packets", "-show_data_hash", "sha256")["packets"]


def frames(path):
    return subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "format=gray",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout


@pytest.mark.parametrize("segment_minutes", [0, 1])
@pytest.mark.parametrize(
    "video_offset,audio_offset,global_offset,seconds,subtitle",
    [
        (1, 0, 0, 3, False),
        (0, 1, 0, 3, False),
        (1, 0, 5, 3, False),
        (1, 0, 0, 3, True),
        (1, 0, 0, 61, True),
    ],
)
def test_timeline_preserves_pictures_and_audio(
    tmp_path,
    monkeypatch,
    console_pair,
    segment_minutes,
    video_offset,
    audio_offset,
    global_offset,
    seconds,
    subtitle,
):
    raw = tmp_path / "original.bin"
    source = tmp_path / "source.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=64x64:rate=10:duration={seconds}",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-f",
        "mp4",
        raw,
    )
    subtitle_args = []
    if subtitle:
        path = tmp_path / "subtitle.srt"
        path.write_text(
            "1\n00:00:00,500 --> 00:00:02,500\nAcross first boundary\n\n"
            + (
                "2\n00:00:59,500 --> 00:01:02,000\nAcross minute boundary\n" if seconds > 60 else ""
            ),
            encoding="utf-8",
        )
        subtitle_args = ["-i", path]
    ffmpeg(
        "-itsoffset",
        video_offset,
        "-i",
        raw,
        "-itsoffset",
        audio_offset,
        "-f",
        "lavfi",
        "-i",
        f"sine=duration={seconds + 1}",
        *subtitle_args,
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-c:s",
        "mov_text",
        "-output_ts_offset",
        global_offset,
        source,
    )
    before = probe(source, "-show_streams", "-show_format")
    old_packets = packets(source)
    old_frames = frames(source)
    if seconds < 60:
        monkeypatch.setattr(
            "ivdc.optimization_plan.plan_segments",
            lambda duration, _, **kw: plan_segments(duration, 1, **kw),
        )
    assert (
        execute_optimize(
            OptimizeOptions(tmp_path, cuda="off", segment_minutes=segment_minutes), console_pair[0]
        )
        == 0
    ), console_pair[1].getvalue()
    after = probe(source, "-show_streams", "-show_format")
    streams = after["streams"]
    assert streams[0]["nb_frames"] == str(seconds * 10)
    assert float(streams[0]["duration"]) == pytest.approx(seconds, abs=0.001)
    before_delta = float(before["streams"][0]["start_time"]) - float(
        before["streams"][1]["start_time"]
    )
    after_delta = float(streams[0]["start_time"]) - float(streams[1]["start_time"])
    assert after_delta == pytest.approx(before_delta, abs=0.002)
    new_packets = packets(source)
    old_audio = [p for p in old_packets if p["stream_index"] == 1]
    new_audio = [p for p in new_packets if p["stream_index"] == 1]
    assert [p["data_hash"] for p in new_audio] == [p["data_hash"] for p in old_audio]
    # 原文件整体起点可归零，但音频各包与画面的相对时刻必须不变。
    old_video_start = float(before["streams"][0]["start_time"])
    new_video_start = float(streams[0]["start_time"])
    assert [float(p["pts_time"]) - new_video_start for p in new_audio] == pytest.approx(
        [float(p["pts_time"]) - old_video_start for p in old_audio], abs=0.002
    )
    if subtitle:
        old_subs = [p for p in old_packets if p["stream_index"] == 2]
        new_subs = [p for p in new_packets if p["stream_index"] == 2]
        assert [p["data_hash"] for p in new_subs] == [p["data_hash"] for p in old_subs]
        assert [float(p["pts_time"]) - new_video_start for p in new_subs] == pytest.approx(
            [float(p["pts_time"]) - old_video_start for p in old_subs], abs=0.002
        )
        assert [p.get("duration_time") for p in new_subs] == [
            p.get("duration_time") for p in old_subs
        ]
    new_frames = frames(source)
    count = seconds * 10
    assert len(new_frames) == len(old_frames) == count * 4096
    # 每片首尾画面都应匹配当前帧，而不是错取前后片段。
    indices = [0, 9, 10, 19, 20, 29] if seconds < 60 else [0, 599, 600, count - 1]
    for index in indices:
        output = new_frames[index * 4096 : (index + 1) * 4096]
        distances = {
            j: sum(
                abs(a - b)
                for a, b in zip(output, old_frames[j * 4096 : (j + 1) * 4096], strict=True)
            )
            for j in range(max(0, index - 3), min(count, index + 4))
        }
        assert min(distances, key=distances.__getitem__) == index
