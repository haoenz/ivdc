"""能力探测运行时层：用假的命令结果驱动，不依赖真的 ffmpeg。"""

from __future__ import annotations

import json

import pytest

from ivdc import probe_runtime
from ivdc.probe_runtime import (
    Environment,
    MissingToolsError,
    Toolchain,
    detect_gpu,
    find_toolchain,
    is_media_file,
    probe_environment,
    probe_media,
    probe_many,
)
from ivdc.runner import CommandResult

TOOLCHAIN = Toolchain(ffmpeg="ffmpeg", ffprobe="ffprobe")


def _result(argv: object, returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(tuple(str(item) for item in argv), returncode, stdout=stdout, stderr=stderr)


def test_find_toolchain_reports_every_missing_command(monkeypatch) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: None)
    with pytest.raises(MissingToolsError) as info:
        find_toolchain()
    assert "ffmpeg" in str(info.value)
    assert "ffprobe" in str(info.value)


def test_find_toolchain_resolves_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        probe_runtime.shutil, "which", lambda name: f"C:/bin/{name}.exe"
    )
    toolchain = find_toolchain()
    assert toolchain.ffmpeg == "C:/bin/ffmpeg.exe"
    assert toolchain.ffprobe == "C:/bin/ffprobe.exe"


def test_probe_media_parses_json_payload(monkeypatch) -> None:
    payload = {
        "format": {"duration": "12.5"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264"},
        ],
    }
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, stdout=json.dumps(payload))
    )
    info = probe_media(TOOLCHAIN, "a.mp4")
    assert info.duration == pytest.approx(12.5)
    assert info.codec_name == "h264"


def test_probe_media_returns_zero_when_command_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, returncode=1)
    )
    info = probe_media(TOOLCHAIN, "a.mp4")
    assert info.duration == 0.0
    assert info.codec_name == ""


def test_probe_media_returns_zero_on_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, stdout="not json")
    )
    assert probe_media(TOOLCHAIN, "a.mp4").duration == 0.0


def test_probe_media_sends_one_ffprobe_call(monkeypatch) -> None:
    """时长与编码一次取回，不额外多调一次 ffprobe。"""
    calls: list[list[str]] = []

    def fake(argv, **kw):
        calls.append([str(item) for item in argv])
        return _result(argv, stdout="{}")

    monkeypatch.setattr(probe_runtime, "capture_command", fake)
    probe_media(TOOLCHAIN, "a.mp4")
    assert len(calls) == 1
    assert "-show_format" in calls[0]
    assert "-show_streams" in calls[0]


def test_is_media_file_uses_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, returncode=1)
    )
    assert is_media_file(TOOLCHAIN, "seg.mp4") is False
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, returncode=0)
    )
    assert is_media_file(TOOLCHAIN, "seg.mp4") is True


def test_probe_environment_separates_encoders_and_hwaccels(monkeypatch) -> None:
    def fake(argv, **kw):
        joined = " ".join(str(item) for item in argv)
        if "-encoders" in joined:
            return _result(argv, stdout=" V..... libx265\n V....D hevc_nvenc")
        if "-hwaccels" in joined:
            return _result(argv, stdout="Hardware acceleration methods:\ncuda\ndxva2")
        return _result(argv, returncode=1)

    monkeypatch.setattr(probe_runtime, "capture_command", fake)
    monkeypatch.setattr(probe_runtime, "detect_gpu", lambda: True)

    environment = probe_environment(TOOLCHAIN)
    assert isinstance(environment, Environment)
    assert "libx265" in environment.encoders
    assert "libx265" not in environment.hwaccels
    assert environment.gpu_available is True


def test_probe_many_deduplicates_paths(monkeypatch, tmp_path) -> None:
    seen: list[str] = []

    def fake(toolchain, path):
        seen.append(str(path))
        from ivdc.probe import MediaInfo

        return MediaInfo(duration=1.0, codec_name="h264")

    monkeypatch.setattr(probe_runtime, "probe_media", fake)
    first = tmp_path / "a.mp4"
    second = tmp_path / "b.mp4"
    result = probe_many(TOOLCHAIN, [first, second, first], workers=2)
    assert sorted(seen) == sorted([str(first), str(second)])
    assert set(result) == {first, second}


def test_probe_many_without_paths(monkeypatch) -> None:
    assert probe_many(TOOLCHAIN, [], workers=2) == {}


def test_detect_gpu_uses_nvidia_smi_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: "C:/nvidia-smi.exe")
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, returncode=1)
    )
    monkeypatch.setattr(probe_runtime.os, "name", "posix")
    assert detect_gpu() is False


def test_detect_gpu_accepts_successful_nvidia_smi(monkeypatch) -> None:
    monkeypatch.setattr(probe_runtime.shutil, "which", lambda name: "C:/nvidia-smi.exe")
    monkeypatch.setattr(
        probe_runtime, "capture_command", lambda argv, **kw: _result(argv, stdout="GPU 0: X")
    )
    assert detect_gpu() is True
