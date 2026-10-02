"""实际显示矩阵的整片、分片及断点恢复验证。"""

import json
import shutil
import subprocess

import pytest

from ivdc.cli import execute_optimize
from ivdc.encoding import Encoder
from ivdc.errors import MediaError
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions
from ivdc.plan import plan_segments

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="需要真实 ffmpeg 与 ffprobe"),
]


def ffmpeg(*args):
    subprocess.run([FFMPEG, "-v", "error", *map(str, args)], check=True, capture_output=True)


def displayed_frame(path, last=False):
    return subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-vf",
            "select=eq(n\\,19),format=gray" if last else "format=gray",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("mode", ["whole", "segments", "resume"])
def test_display_rotation_keeps_orientation_and_full_picture(
    tmp_path, monkeypatch, console_pair, angle, mode
):
    raw, source = tmp_path / "raw.bin", tmp_path / "source.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=96x64:rate=10:duration=2",
        "-c:v",
        "libx264",
        "-f",
        "mp4",
        raw,
    )
    ffmpeg("-display_rotation:v:0", angle, "-i", raw, "-c", "copy", source)
    original = source.read_bytes()
    before = [displayed_frame(source, last) for last in (False, True)]
    monkeypatch.setattr(
        "ivdc.optimization_plan.plan_segments",
        lambda duration, _, **kw: plan_segments(duration, 1, **kw),
    )
    options = OptimizeOptions(tmp_path, cuda="off", segment_minutes=0 if mode == "whole" else 1)
    if mode == "resume":

        def interrupted(*args):
            raise MediaError("interrupted concatenation")

        with monkeypatch.context() as patch:
            patch.setattr(Encoder, "concatenate", interrupted)
            assert execute_optimize(options, console_pair[0]) == 1
        assert source.read_bytes() == original
        assert len(list(IvdcLayout(tmp_path).seg_dir(source.name).glob("*.complete.json"))) == 2
        monkeypatch.setattr(Encoder, "encode", lambda *args: pytest.fail("必须复用已验证片"))
    assert execute_optimize(options, console_pair[0]) == 0, console_pair[1].getvalue()
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    info = json.loads(result.stdout)["streams"][0]
    assert (info["width"], info["height"]) == ((64, 96) if angle % 180 else (96, 64))
    assert info["nb_frames"] == "20"
    assert not any(d.get("rotation", 0) for d in info.get("side_data_list", []))
    for original_frame, last in zip(before, (False, True), strict=True):
        after = displayed_frame(source, last)
        assert len(after) == len(original_frame) == 96 * 64
        # 比较按显示方向解码后的首尾像素，错误旋转/镜像不能仅凭宽高蒙混过关。
        assert sum(abs(a - b) for a, b in zip(original_frame, after, strict=True)) / len(after) < 8
    assert IvdcLayout(tmp_path).backup(source.name).read_bytes() == original
