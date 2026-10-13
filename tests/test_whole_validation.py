"""整片提交也必须拒绝成功退出但不完整的输出与输入解码错误。"""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ivdc.cli import execute_optimize
from ivdc.fs import IvdcLayout
from ivdc.media_validation import validate_media
from ivdc.optimization_plan import OptimizeOptions
from ivdc.probe_runtime import find_toolchain, probe_media, probe_video_timeline
from ivdc.runner import ProcessRunner

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="需要真实 ffmpeg 与 ffprobe"),
]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=3",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-bf",
            "0",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def assert_protected(source, console_pair):
    original = source.read_bytes()
    layout = IvdcLayout(source.parent)
    assert (
        execute_optimize(
            OptimizeOptions(source.parent, cuda="off", keep_backup=False), console_pair[0]
        )
        == 1
    ), console_pair[1].getvalue()
    assert source.read_bytes() == original
    assert layout.backup(source.name).read_bytes() == original
    assert not layout.tmp(source.name).exists()
    assert (
        json.loads(layout.manifest_path.read_text(encoding="utf-8"))["entries"][source.name][
            "status"
        ]
        == "failed"
    )


@pytest.mark.parametrize("damage", ["short", "undecodable"])
def test_whole_successful_encoder_cannot_commit_invalid_output(
    source, console_pair, monkeypatch, damage
):
    stream = ProcessRunner.stream

    def damaged(self, argv, **kwargs):
        argv = list(argv)
        if "-progress" not in argv:
            return stream(self, argv, **kwargs)
        if damage == "short":
            argv[-4:-4] = ["-t", "1"]
        result = stream(self, argv, **kwargs)
        assert result.ok  # 故障边界是“外部程序返回成功”。
        if damage == "undecodable":
            target = Path(argv[-2])
            data = bytearray(target.read_bytes())
            position = data.index(b"mdat")
            size = int.from_bytes(data[position - 4 : position], "big")
            data[position + 4 : position - 4 + size] = b"\0" * (size - 8)
            target.write_bytes(data)
        return result

    monkeypatch.setattr(ProcessRunner, "stream", damaged)
    assert_protected(source, console_pair)


def test_input_decode_errors_cannot_be_silently_committed(source, console_pair):
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_packets", "-of", "json", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    data = bytearray(source.read_bytes())
    for packet in json.loads(result.stdout)["packets"][10:20]:
        start, size = int(packet["pos"]), int(packet["size"])
        data[start : start + size] = b"\0" * size
    source.write_bytes(data)
    assert_protected(source, console_pair)


@pytest.mark.parametrize("damage", [None, "missing", "duplicate"])
def test_real_frames_resolve_inaccurate_metadata_but_reject_damage(source, damage):
    target = source.with_name("encoded.mp4")
    extra = []
    if damage == "missing":
        extra = ["-frames:v", "29"]
    elif damage == "duplicate":
        # 帧数与总时长不变，单独改变一个中间帧的时间戳。
        extra = ["-vf", "setpts=if(eq(N\\,15)\\,PTS-0.1/TB\\,PTS)"]
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(source),
            *extra,
            "-c:v",
            "libx265",
            "-enc_time_base:v",
            "demux",
            "-fps_mode:v",
            "passthrough",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    with ProcessRunner() as runner:
        toolchain = find_toolchain()
        reference = probe_media(toolchain, source, runner)
        # 模拟下载文件的标注时长比实际显示时间轴短 30ms。
        reference = replace(
            reference, video_duration=reference.video_duration - 0.03, frame_rate=10.01
        )

        def validate():
            validate_media(
                toolchain,
                runner,
                target,
                reference,
                "hevc",
                reference.video_duration,
                source=source,
            )

        if damage is None:
            validate()
        else:
            from ivdc.errors import MediaError

            with pytest.raises(MediaError):
                validate()


def test_irregular_close_tail_frames_pass_full_decode(tmp_path, console_pair):
    source = tmp_path / "irregular.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=60:duration=1",
            "-vf",
            "settb=1/90000,setpts=if(eq(N\\,59)\\,58.2/(60*TB)\\,PTS)",
            "-c:v",
            "libx264",
            "-enc_time_base:v",
            "1/90000",
            "-fps_mode:v",
            "passthrough",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    assert execute_optimize(OptimizeOptions(tmp_path, cuda="off"), console_pair[0]) == 0, (
        console_pair[1].getvalue()
    )
    with ProcessRunner() as runner:
        info = probe_media(find_toolchain(), source, runner)
    assert info.codec_name == "hevc"
    assert info.frame_count == 60


def test_submillisecond_video_offset_survives_mp4_muxing(source, console_pair):
    shifted = source.with_name("shifted.mp4")
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-itsoffset",
            "0.079989",
            "-i",
            str(source),
            "-f",
            "lavfi",
            "-i",
            "sine=duration=3.2",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movie_timescale",
            "1000000",
            str(shifted),
        ],
        check=True,
        capture_output=True,
    )
    source.unlink()
    with ProcessRunner() as runner:
        toolchain = find_toolchain()
        before = probe_media(toolchain, shifted, runner)
        old_frames = probe_video_timeline(toolchain, shifted, runner)
    assert execute_optimize(OptimizeOptions(shifted.parent, cuda="off"), console_pair[0]) == 0, (
        console_pair[1].getvalue()
    )
    with ProcessRunner() as runner:
        after = probe_media(toolchain, shifted, runner)
        new_frames = probe_video_timeline(toolchain, shifted, runner)
    assert after.video_offset == pytest.approx(before.video_offset, abs=2e-6)
    assert new_frames.timestamps == pytest.approx(old_frames.timestamps, abs=2e-6)


@pytest.mark.parametrize("tail_tolerance", [None, 0.0, 0.1, 0.2])
def test_missing_source_tail_respects_default_and_overrides_through_whole_pipeline(
    source, console_pair, monkeypatch, tail_tolerance
):
    from ivdc.optimization_plan import probe_many

    before = source.read_bytes()

    def inaccurate_metadata(*args):
        infos = probe_many(*args)
        infos[source] = replace(infos[source], video_duration=2.88)
        return infos

    def missing_tail(toolchain, path, runner):
        timeline = probe_video_timeline(toolchain, path, runner)
        return replace(timeline, last_duration=None) if path == source else timeline

    monkeypatch.setattr("ivdc.optimization_plan.probe_many", inaccurate_metadata)
    monkeypatch.setattr("ivdc.media_validation.probe_video_timeline", missing_tail)
    options = OptimizeOptions(source.parent, cuda="off")
    if tail_tolerance is not None:
        options = replace(options, tail_tolerance=tail_tolerance)
    code = execute_optimize(options, console_pair[0])
    accepted = tail_tolerance in (None, 0.2)
    assert code == (0 if accepted else 1), console_pair[1].getvalue()
    assert IvdcLayout(source.parent).backup(source.name).read_bytes() == before
    if accepted:
        with ProcessRunner() as runner:
            info = probe_media(find_toolchain(), source, runner)
        assert info.codec_name == "hevc"
        assert info.frame_count == 30
    else:
        assert source.read_bytes() == before
