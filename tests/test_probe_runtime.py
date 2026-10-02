"""能力探测运行时层：用假的命令结果驱动，不依赖真的 ffmpeg。"""

from __future__ import annotations

import json

import pytest

from ivdc import probe_runtime
from ivdc.errors import MediaError
from ivdc.probe_runtime import (
    Environment,
    MissingToolsError,
    Toolchain,
    detect_gpu,
    find_toolchain,
    is_media_file,
    probe_environment,
    probe_many,
    probe_media,
)
from ivdc.runner import CommandResult, ProcessRunner


@pytest.fixture
def runner():
    with ProcessRunner() as runner:
        yield runner


TOOLCHAIN = Toolchain(ffmpeg="ffmpeg", ffprobe="ffprobe")


def _result(argv: object, returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(
        tuple(str(item) for item in argv), returncode, stdout=stdout, stderr=stderr
    )


def test_find_toolchain_reports_every_missing_command(monkeypatch, runner) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: None)
    with pytest.raises(MissingToolsError) as info:
        find_toolchain()
    assert "ffmpeg" in str(info.value)
    assert "ffprobe" in str(info.value)


def test_find_toolchain_resolves_paths(monkeypatch, runner) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    toolchain = find_toolchain()
    assert toolchain.ffmpeg == "C:/bin/ffmpeg.exe"
    assert toolchain.ffprobe == "C:/bin/ffprobe.exe"


def test_probe_media_parses_json_payload(monkeypatch, runner) -> None:
    payload = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264"},
        ],
    }
    monkeypatch.setattr(
        runner, "capture", lambda argv, **kw: _result(argv, stdout=json.dumps(payload))
    )
    info = probe_media(TOOLCHAIN, "a.mp4", runner)
    assert info.duration == pytest.approx(12.5)
    assert info.codec_name == "h264"


def test_probe_media_raises_when_command_fails(monkeypatch, runner) -> None:
    monkeypatch.setattr(runner, "capture", lambda argv, **kw: _result(argv, returncode=1))
    with pytest.raises(MediaError, match="无法探测"):
        probe_media(TOOLCHAIN, "a.mp4", runner)


def test_probe_media_raises_on_invalid_json(monkeypatch, runner) -> None:
    monkeypatch.setattr(runner, "capture", lambda argv, **kw: _result(argv, stdout="not json"))
    with pytest.raises(MediaError, match="无效的媒体信息"):
        probe_media(TOOLCHAIN, "a.mp4", runner)


def test_probe_media_sends_one_ffprobe_call(monkeypatch, runner) -> None:
    """时长与编码一次取回，不额外多调一次 ffprobe。"""
    calls: list[list[str]] = []

    def fake(argv, **kw):
        calls.append([str(item) for item in argv])
        return _result(argv, stdout='{"streams":[{"codec_type":"video","codec_name":"h264"}]}')

    monkeypatch.setattr(runner, "capture", fake)
    probe_media(TOOLCHAIN, "a.mp4", runner)
    assert len(calls) == 1
    assert "-show_format" in calls[0]
    assert "-show_streams" in calls[0]


def test_is_media_file_uses_exit_code(monkeypatch, runner) -> None:
    monkeypatch.setattr(runner, "capture", lambda argv, **kw: _result(argv, returncode=1))
    assert is_media_file(TOOLCHAIN, "seg.mp4", runner) is False
    monkeypatch.setattr(
        runner,
        "capture",
        lambda argv, **kw: _result(
            argv,
            stdout='{"format":{"duration":"1"},"streams":[{"codec_type":"video","codec_name":"h264"}]}',
        ),
    )
    assert is_media_file(TOOLCHAIN, "seg.mp4", runner) is True


def test_probe_environment_separates_encoders_and_hwaccels(monkeypatch, runner) -> None:
    def fake(argv, **kw):
        joined = " ".join(str(item) for item in argv)
        if "-encoders" in joined:
            return _result(argv, stdout=" V..... libx265\n V....D hevc_nvenc")
        if "-hwaccels" in joined:
            return _result(argv, stdout="Hardware acceleration methods:\ncuda\ndxva2")
        return _result(argv, returncode=1)

    monkeypatch.setattr(runner, "capture", fake)
    monkeypatch.setattr(probe_runtime, "detect_gpu", lambda runner: True)

    environment = probe_environment(TOOLCHAIN, runner)
    assert isinstance(environment, Environment)
    assert "libx265" in environment.encoders
    assert "libx265" not in environment.hwaccels
    assert environment.gpu_available is True


def test_probe_many_deduplicates_paths(monkeypatch, tmp_path, runner) -> None:
    seen: list[str] = []

    def fake(toolchain, path, runner):
        seen.append(str(path))
        from ivdc.probe import MediaInfo

        return MediaInfo(duration=1.0, codec_name="h264")

    monkeypatch.setattr(probe_runtime, "probe_media", fake)
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    result = probe_many(TOOLCHAIN, [first, second, first], workers=2, runner=runner)
    assert sorted(seen) == sorted([str(first), str(second)])
    assert set(result) == {first, second}


def test_probe_many_without_paths(monkeypatch, runner) -> None:
    assert probe_many(TOOLCHAIN, [], workers=2, runner=runner) == {}


def test_detect_gpu_uses_nvidia_smi_exit_code(monkeypatch, runner) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: "C:/nvidia-smi.exe")
    monkeypatch.setattr(runner, "capture", lambda argv, **kw: _result(argv, returncode=1))
    monkeypatch.setattr(probe_runtime.os, "name", "posix")
    assert detect_gpu(runner) is False


def test_detect_gpu_accepts_successful_nvidia_smi(monkeypatch, runner) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: "C:/nvidia-smi.exe")
    monkeypatch.setattr(runner, "capture", lambda argv, **kw: _result(argv, stdout="GPU 0: X"))
    assert detect_gpu(runner) is True
