"""逃逸异常在日志生命周期内保留原始上下文，写日志失败不掩盖原异常。"""

import logging
import shutil
import subprocess

import pytest

from ivdc.cli import execute_optimize
from ivdc.encoding import Encoder
from ivdc.errors import StorageError
from ivdc.fs import IvdcLayout, Manifest
from ivdc.optimization_plan import OptimizeOptions
from ivdc.records import ResultRecorder, StrictFileHandler
from ivdc.runner import CommandError, ProcessRunner


@pytest.mark.integration
@pytest.mark.parametrize("fault", ["worker", "manifest", "concat"])
def test_running_failure_reaches_log_before_handler_closes(
    tmp_path, monkeypatch, console_pair, fault
):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("需要真实 ffmpeg 与 ffprobe")
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=10:duration=1",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    secret = "PRIVATE_ORIGINAL_FAILURE"

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    if fault == "worker":
        monkeypatch.setattr(Encoder, "encode", fail)
    elif fault == "manifest":

        def denied(*args):
            raise PermissionError(secret)

        monkeypatch.setattr("ivdc.records.save_manifest", denied)
    else:
        capture = ProcessRunner.capture

        def no_concat(self, argv, **kwargs):
            if "concat" in argv:
                raise CommandError(secret)
            return capture(self, argv, **kwargs)

        monkeypatch.setattr(ProcessRunner, "capture", no_concat)
    logger = logging.getLogger("ivdc")
    state = logger.level, logger.propagate, tuple(logger.handlers)
    assert (
        execute_optimize(
            OptimizeOptions(
                tmp_path,
                cuda="off",
                mask=True,
                debug=True,
                segment_minutes=1 if fault == "concat" else 0,
            ),
            console_pair[0],
        )
        == 1
    )
    assert secret not in console_pair[1].getvalue()
    text = next(IvdcLayout(tmp_path).logs_dir.glob("*.log")).read_text()
    assert secret in text
    if fault in ("worker", "manifest"):
        assert "Traceback" in text
    assert (logger.level, logger.propagate, tuple(logger.handlers)) == state


def test_failure_while_recording_does_not_hide_original_or_leak_handler(tmp_path, monkeypatch):
    logger = logging.getLogger("ivdc")
    state = logger.level, tuple(logger.handlers)
    original = RuntimeError("original failure")

    def denied(*args):
        raise StorageError("log disk full")

    with pytest.raises(RuntimeError, match="original failure") as raised:
        with ResultRecorder(IvdcLayout(tmp_path), Manifest(), tmp_path / "run.log", True):
            monkeypatch.setattr(StrictFileHandler, "emit", denied)
            raise original
    assert raised.value is original
    assert "log disk full" in " ".join(original.__notes__)
    assert (logger.level, tuple(logger.handlers)) == state
