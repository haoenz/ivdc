"""画质配置的启动边界、真实编码、日志与断点身份。"""

import json
import shutil
import subprocess

import pytest

from ivdc.cli import execute_optimize
from ivdc.encoding import Encoder
from ivdc.errors import MediaError, SetupError
from ivdc.exitcodes import EXIT_FAILURE, EXIT_OK
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import OptimizeOptions
from ivdc.optimize import run_optimize
from ivdc.quality import Quality
from ivdc.runner import ProcessRunner

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
requires_media = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="需要真实 ffmpeg 与 ffprobe")


def snapshot(root):
    return {
        path.relative_to(root): (
            path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None,
        )
        for path in [root, *root.rglob("*")]
    }


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "profiles",
    [{"libx265": {"low": 63}}, {"av1_nvenc": {"high": 0}}, {"libsvtav1": {"medium": "35"}}],
)
def test_invalid_configuration_precedes_probe_recovery_cleanup_and_writes(
    tmp_path, monkeypatch, dry_run, profiles
):
    root = tmp_path / "videos"
    layout = IvdcLayout(root)
    layout.backups_dir.mkdir(parents=True)
    layout.backup("missing.mp4").write_bytes(b"original")
    layout.tmp_dir.mkdir()
    layout.tmp("stale.mp4").write_bytes(b"recoverable residue")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"quality_profiles": profiles}))
    monkeypatch.setenv("IVDC_CONFIG", str(path))
    before = snapshot(tmp_path)
    monkeypatch.setattr("ivdc.optimize.find_toolchain", lambda: pytest.fail("配置错误不能启动工具"))
    with pytest.raises(SetupError, match="quality_profiles"):
        run_optimize(OptimizeOptions(root, dry_run=dry_run))
    assert snapshot(tmp_path) == before


@pytest.fixture
def media(tmp_path):
    assert FFMPEG is not None
    root = tmp_path / "videos"
    root.mkdir()
    source = root / "sample.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=1",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    return source, IvdcLayout(root)


@requires_media
@pytest.mark.integration
@pytest.mark.parametrize("segment_minutes", [0, 1])
@pytest.mark.parametrize("quality", list(Quality))
@pytest.mark.parametrize(
    ("codec", "encoder", "values"),
    [
        ("x265", "libx265", (22, 26, 30)),
        ("av1", "libsvtav1", (27, 33, 39)),
        ("vp9", "libvpx-vp9", (24, 30, 36)),
    ],
)
def test_configured_levels_reach_real_whole_and_segment_encoding_and_logs(
    media, tmp_path, monkeypatch, console_pair, codec, encoder, values, quality, segment_minutes
):
    source, layout = media
    original = source.read_bytes()
    expected = values[list(Quality).index(quality)]
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"quality_profiles": {encoder: dict(zip(Quality, values, strict=True))}})
    )
    monkeypatch.setenv("IVDC_CONFIG", str(path))
    stream = ProcessRunner.stream
    commands = []

    def capture_encoding(self, argv, **kwargs):
        commands.append(argv)
        return stream(self, argv, **kwargs)

    monkeypatch.setattr(ProcessRunner, "stream", capture_encoding)
    console, output = console_pair
    options = OptimizeOptions(
        source.parent, codec=codec, cuda="off", quality=quality, segment_minutes=segment_minutes
    )
    assert execute_optimize(options, console) == EXIT_OK, output.getvalue()
    assert len(commands) == 1
    assert commands[0][commands[0].index("-crf") + 1] == str(expected)
    assert layout.backup(source.name).read_bytes() == original
    assert source.read_bytes() != original
    description = f"画质 {quality} | 编码器 {encoder} | CRF {expected}"
    assert description in output.getvalue()
    assert description in next(layout.logs_dir.glob("*.log")).read_text()


@requires_media
@pytest.mark.integration
def test_custom_quality_dry_run_with_recovery_residue_writes_nothing(
    media, tmp_path, monkeypatch, console_pair
):
    source, layout = media
    layout.backups_dir.mkdir(parents=True)
    source.replace(layout.backup(source.name))
    layout.tmp_dir.mkdir()
    layout.tmp(source.name).write_bytes(b"interrupted")
    path = tmp_path / "config.json"
    path.write_text('{"quality_profiles": {"libx265": {"high": 20}}}')
    monkeypatch.setenv("IVDC_CONFIG", str(path))
    before = snapshot(tmp_path)
    console, output = console_pair
    assert (
        execute_optimize(
            OptimizeOptions(source.parent, cuda="off", quality=Quality.high, dry_run=True), console
        )
        == EXIT_OK
    ), output.getvalue()
    assert "CRF 20" in output.getvalue()
    assert source.name in output.getvalue()
    assert snapshot(tmp_path) == before


@requires_media
@pytest.mark.integration
@pytest.mark.parametrize("changed_level", ["medium", "high"])
def test_resume_uses_effective_numeric_quality_not_just_level_name(
    media, tmp_path, monkeypatch, console_pair, changed_level
):
    source, layout = media
    options = OptimizeOptions(source.parent, cuda="off", segment_minutes=1)
    console, output = console_pair

    def fail_concat(*args):
        raise MediaError("模拟拼接中断，保留完成分片")

    with monkeypatch.context() as patch:
        patch.setattr(Encoder, "concatenate", fail_concat)
        assert execute_optimize(options, console) == EXIT_FAILURE
    segment = layout.seg(source.name, 0)
    original_segment = segment.read_bytes()
    assert segment.with_suffix(".complete.json").exists()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "quality_profiles": {
                    "libx265": {changed_level: 29 if changed_level == "medium" else 25}
                }
            }
        )
    )
    monkeypatch.setenv("IVDC_CONFIG", str(path))
    encode = Encoder.encode
    encoded = []

    def capture_encode(self, task, target, part=None):
        encoded.append(target)
        return encode(self, task, target, part)

    monkeypatch.setattr(Encoder, "encode", capture_encode)
    assert execute_optimize(options, console) == EXIT_OK, output.getvalue()
    archives = list(layout.segs_dir.glob("*.resume-*/seg_000.mp4"))
    if changed_level == "medium":
        assert len(encoded) == 1
        assert len(archives) == 1
        assert archives[0].read_bytes() == original_segment
        assert archives[0].with_suffix(".complete.json").exists()
    else:
        assert encoded == []
        assert archives == []
