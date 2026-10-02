"""真实媒体验证分片完成协议与提交边界；所有文件均在临时目录。"""

import json
import shutil
import subprocess
from dataclasses import replace

import pytest

from ivdc import segment_state
from ivdc.cli import execute_optimize
from ivdc.encoding import Encoder
from ivdc.exitcodes import EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions
from ivdc.runner import ProcessRunner, RunCancelled

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="需要真实 ffmpeg 与 ffprobe"),
]


def ffmpeg(*args):
    assert FFMPEG is not None
    subprocess.run([FFMPEG, "-v", "error", *map(str, args)], check=True, capture_output=True)


def video_duration(path):
    assert FFPROBE is not None
    output = subprocess.run(
        [
            FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(json.loads(output.stdout)["streams"][0]["duration"])


@pytest.fixture
def media(tmp_path):
    source = tmp_path / "source.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10:duration=1", "-c:v", "libx264", source)
    return (
        source,
        IvdcLayout(tmp_path),
        OptimizeOptions(
            tmp_path,
            cuda="off",
            segment_minutes=1,
            keep_backup=False,
            workers=2,
        ),
    )


def make_segment(source, target, duration):
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(
        "-i", source, "-t", str(duration), "-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28", target
    )


def corrupt_payload(target):
    payload = bytearray(target.read_bytes())
    position = payload.index(b"mdat")
    size = int.from_bytes(payload[position - 4 : position], "big")
    payload[position + 4 : position - 4 + size] = b"\0" * (size - 8)
    target.write_bytes(payload)


@pytest.mark.parametrize("old_metadata", [False, True])
def test_short_legacy_segment_is_archived_and_reencoded(media, console_pair, old_metadata):
    source, layout, options = media
    segment = layout.seg(source.name, 0)
    make_segment(source, segment, 0.6)
    incomplete = segment.read_bytes()
    if old_metadata:
        stat = source.stat()
        (segment.parent / "resume.json").write_text(
            json.dumps(
                {
                    "source_size": stat.st_size,
                    "source_mtime_ns": stat.st_mtime_ns,
                    "codec": "hevc",
                    "encoder_args": ["-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28"],
                    "segments": [[0, 1]],
                }
            )
        )
    console, output = console_pair
    assert execute_optimize(options, console) == EXIT_OK, output.getvalue()
    assert video_duration(source) == pytest.approx(1, abs=0.001)
    archives = list(layout.segs_dir.glob("*.retained-*/seg_000.mp4"))
    assert len(archives) == 1 and archives[0].read_bytes() == incomplete
    assert not layout.backup(source.name).exists()


def test_encoder_success_with_short_output_cannot_publish(media, console_pair, monkeypatch):
    source, layout, options = media
    original_bytes = source.read_bytes()
    encode = Encoder.encode

    def short_output(self, task, target, segment=None):
        assert segment is not None
        encode(self, task, target, replace(segment, duration=0.6))

    monkeypatch.setattr(Encoder, "encode", short_output)
    console, output = console_pair
    assert execute_optimize(options, console) == EXIT_FAILURE
    assert "不完整" in output.getvalue()
    assert source.read_bytes() == original_bytes
    assert layout.backup(source.name).read_bytes() == original_bytes
    assert not layout.seg(source.name, 0).exists()
    assert not list(layout.seg_dir(source.name).glob("*.complete.json"))
    assert not list(layout.seg_dir(source.name).glob("*.partial.mp4"))


def test_cancelled_encoder_never_creates_completion_receipt(media, console_pair, monkeypatch):
    source, layout, options = media
    original_bytes = source.read_bytes()
    encode = Encoder.encode
    runners = []

    def cancelled(self, task, target, segment=None):
        encode(self, task, target, segment)
        runners.append(self.runner)
        self.runner.cancel()
        raise RunCancelled("cancel after encoding")

    with monkeypatch.context() as patch:
        patch.setattr(Encoder, "encode", cancelled)
        assert execute_optimize(options, console_pair[0]) == EXIT_FAILURE
    assert runners and all(runner.active_count == 0 for runner in runners)
    assert source.read_bytes() == original_bytes
    assert layout.backup(source.name).read_bytes() == original_bytes
    assert not layout.seg(source.name, 0).exists()
    assert not list(layout.seg_dir(source.name).glob("*.complete.json"))
    assert execute_optimize(options, console_pair[0]) == EXIT_OK
    assert video_duration(source) == pytest.approx(1, abs=0.001)


@pytest.mark.parametrize("interruption", [PermissionError, KeyboardInterrupt])
def test_interruption_between_publish_and_receipt_forces_safe_retry(
    media,
    console_pair,
    monkeypatch,
    interruption,
):
    source, layout, options = media
    original_bytes = source.read_bytes()
    write = segment_state.atomic_write_text

    def interrupt(path, text):
        if path.name.endswith(".complete.json"):
            raise interruption("receipt interrupted")
        write(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(segment_state, "atomic_write_text", interrupt)
        assert execute_optimize(options, console_pair[0]) == EXIT_FAILURE
    candidate = layout.seg(source.name, 0).read_bytes()
    assert candidate
    assert not list(layout.seg_dir(source.name).glob("*.complete.json"))
    assert source.read_bytes() == original_bytes
    assert layout.backup(source.name).read_bytes() == original_bytes
    encode = Encoder.encode
    encoded = []

    def tracked(self, task, target, segment=None):
        encoded.append(segment.index)
        return encode(self, task, target, segment)

    monkeypatch.setattr(Encoder, "encode", tracked)
    assert execute_optimize(options, console_pair[0]) == EXIT_OK
    assert encoded == [0]
    archives = list(layout.segs_dir.glob("*.retained-*/seg_000.mp4"))
    assert len(archives) == 1 and archives[0].read_bytes() == candidate
    assert video_duration(source) == pytest.approx(1, abs=0.001)


@pytest.mark.parametrize("damage", ["short", "undecodable"])
def test_incomplete_concat_preserves_original_backup_and_completed_segment(
    media,
    console_pair,
    monkeypatch,
    tmp_path,
    damage,
):
    source, layout, options = media
    original_bytes = source.read_bytes()
    shortened = tmp_path / "short-output.bin"
    ffmpeg("-i", source, "-t", "0.6", "-c:v", "libx265", "-f", "mp4", shortened)
    incomplete = shortened.read_bytes()
    capture = ProcessRunner.capture

    def truncate_concat(self, argv, **kwargs):
        result = capture(self, argv, **kwargs)
        if "concat" in argv:
            if damage == "short":
                layout.tmp(source.name).write_bytes(incomplete)
            else:
                corrupt_payload(layout.tmp(source.name))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(ProcessRunner, "capture", truncate_concat)
        assert execute_optimize(options, console_pair[0]) == EXIT_FAILURE
    assert source.read_bytes() == original_bytes
    assert layout.backup(source.name).read_bytes() == original_bytes
    assert not layout.tmp(source.name).exists()
    assert layout.seg(source.name, 0).is_file()
    assert layout.seg(source.name, 0).with_suffix(".complete.json").is_file()

    def must_reuse(*args, **kwargs):
        pytest.fail("validated and recorded segment must be reused")

    monkeypatch.setattr(Encoder, "encode", must_reuse)
    assert execute_optimize(options, console_pair[0]) == EXIT_OK
    assert video_duration(source) == pytest.approx(1, abs=0.001)


def test_metadata_alone_cannot_hide_undecodable_segment(media, console_pair, monkeypatch):
    source, layout, options = media
    original_bytes = source.read_bytes()
    encode = Encoder.encode

    def damaged_output(self, task, target, segment=None):
        encode(self, task, target, segment)
        corrupt_payload(target)

    monkeypatch.setattr(Encoder, "encode", damaged_output)
    console, output = console_pair
    assert execute_optimize(options, console) == EXIT_FAILURE
    assert source.read_bytes() == original_bytes
    assert layout.backup(source.name).read_bytes() == original_bytes
    assert not layout.seg(source.name, 0).exists()
    assert not list(layout.seg_dir(source.name).glob("*.complete.json"))
    assert "校验失败" in output.getvalue() or "无法探测" in output.getvalue()


def test_orphan_partial_file_is_preserved_but_never_reused(media, console_pair):
    source, layout, options = media
    partial = layout.seg_dir(source.name) / "seg_000.orphan.partial.mp4"
    make_segment(source, partial, 0.6)
    contents = partial.read_bytes()
    assert execute_optimize(options, console_pair[0]) == EXIT_OK
    assert video_duration(source) == pytest.approx(1, abs=0.001)
    archives = list(layout.segs_dir.glob("*.retained-*/*.partial.mp4"))
    assert len(archives) == 1 and archives[0].read_bytes() == contents


def test_fractional_frame_rate_with_audio_and_subframe_tail(tmp_path, console_pair, monkeypatch):
    from ivdc.plan import plan_segments

    source = tmp_path / "fractional.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=30000/1001:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        source,
    )
    monkeypatch.setattr(
        "ivdc.optimization_plan.plan_segments",
        lambda duration, _, **kw: plan_segments(duration, 1, **kw),
    )
    console, output = console_pair
    options = OptimizeOptions(tmp_path, cuda="off", segment_minutes=1, workers=2)
    assert execute_optimize(options, console) == EXIT_OK, output.getvalue()
    assert video_duration(source) == pytest.approx(3.003, abs=0.017)
    assert FFPROBE is not None
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    streams = json.loads(result.stdout)["streams"]
    assert [stream["codec_type"] for stream in streams] == ["video", "audio"]
    assert streams[0]["nb_frames"] == "90"


@pytest.mark.parametrize("codec", ["av1", "vp9"])
def test_segment_validation_supports_other_software_codecs(media, console_pair, codec):
    source, _, options = media
    console, output = console_pair
    assert execute_optimize(replace(options, codec=codec), console) == EXIT_OK, output.getvalue()
    assert video_duration(source) == pytest.approx(1, abs=0.001)


def test_fractional_frame_rate_at_actual_minute_boundary(tmp_path, console_pair):
    source = tmp_path / "minute.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=30000/1001:duration=61",
        "-c:v",
        "libx264",
        source,
    )
    duration = video_duration(source)
    console, output = console_pair
    assert (
        execute_optimize(
            OptimizeOptions(tmp_path, cuda="off", segment_minutes=1, workers=2),
            console,
        )
        == EXIT_OK
    ), output.getvalue()
    assert video_duration(source) == pytest.approx(duration, abs=0.001)


@pytest.mark.parametrize("identity", ["missing", "missing_with_receipt", "legacy_with_receipt"])
def test_untrusted_legacy_picture_cannot_replace_current_source(media, console_pair, identity):
    from ivdc.segment_state import SegmentContext

    source, layout, options = media
    source.unlink()
    ffmpeg(
        "-f", "lavfi", "-i", "color=red:size=64x64:rate=10:duration=1", "-c:v", "libx264", source
    )
    target = layout.seg(source.name, 0)
    target.parent.mkdir(parents=True)
    ffmpeg(
        "-f", "lavfi", "-i", "color=blue:size=64x64:rate=10:duration=1", "-c:v", "libx265", target
    )
    old = target.read_bytes()
    if identity != "missing":
        SegmentContext("different-source").publish(target, target, 0)
    if identity == "legacy_with_receipt":
        stat = source.stat()
        (target.parent / "resume.json").write_text(
            json.dumps(
                {
                    "source_size": stat.st_size,
                    "source_mtime_ns": stat.st_mtime_ns,
                    "codec": "hevc",
                    "encoder_args": ["-c:v", "libx265", "-tag:v", "hvc1", "-crf", "28"],
                    "segments": [[0, 1]],
                }
            ),
            encoding="utf-8",
        )
    assert execute_optimize(options, console_pair[0]) == EXIT_OK, console_pair[1].getvalue()
    assert any(path.read_bytes() == old for path in layout.segs_dir.glob("*/seg_000.mp4"))
    frame = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert frame[0] > 200 and frame[2] < 10  # 必须仍是当前源的红色，而非旧片的蓝色。
    assert not layout.backup(source.name).exists()
