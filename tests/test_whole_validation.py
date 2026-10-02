"""整片提交也必须拒绝成功退出但不完整的输出与输入解码错误。"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ivdc.cli import execute_optimize
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions
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
        json.loads(layout.manifest_path.read_text())["entries"][source.name]["status"] == "failed"
    )


@pytest.mark.parametrize("damage", ["short", "undecodable"])
def test_whole_successful_encoder_cannot_commit_invalid_output(
    source, console_pair, monkeypatch, damage
):
    stream = ProcessRunner.stream

    def damaged(self, argv, **kwargs):
        argv = list(argv)
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
