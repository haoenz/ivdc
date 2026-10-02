"""子进程封装：输出收集、尾部保留、退出码、stderr 合并。"""

from __future__ import annotations

import sys

import pytest

from ivdc.runner import CommandError, ProcessRunner


@pytest.fixture
def runner():
    with ProcessRunner() as runner:
        yield runner


PYTHON = sys.executable


def test_capture_command_collects_stdout(runner) -> None:
    result = runner.capture([PYTHON, "-c", "print('hi')"])
    assert result.ok
    assert result.stdout.strip() == "hi"


def test_capture_command_reports_nonzero_exit(runner) -> None:
    result = runner.capture([PYTHON, "-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.returncode == 3


def test_capture_command_reports_stderr_separately(runner) -> None:
    result = runner.capture([PYTHON, "-c", "import sys; print('bad', file=sys.stderr)"])
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == "bad"


def test_capture_command_missing_binary_preserves_context(runner) -> None:
    with pytest.raises(CommandError, match="definitely-not-a-real-binary-ivdc") as error:
        runner.capture(["definitely-not-a-real-binary-ivdc"])
    assert isinstance(error.value.__cause__, OSError)


def test_stream_command_collects_all_lines_without_callback(runner) -> None:
    result = runner.stream([PYTHON, "-c", "print('a'); print('b')"])
    assert result.ok
    assert result.lines == ("a", "b")


def test_stream_command_merges_stderr_into_the_same_stream(runner) -> None:
    result = runner.stream(
        [PYTHON, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
    )
    assert set(result.lines) == {"out", "err"}


def test_stream_command_keeps_only_the_tail_when_callback_is_used(runner) -> None:
    seen: list[str] = []
    result = runner.stream(
        [PYTHON, "-c", "print(chr(10).join(str(i) for i in range(50)))"],
        on_output=seen.append,
        keep_last=5,
    )
    assert seen == [str(i) for i in range(50)]
    assert result.lines == ("45", "46", "47", "48", "49")


def test_stream_command_reports_nonzero_exit(runner) -> None:
    result = runner.stream([PYTHON, "-c", "import sys; sys.exit(7)"])
    assert result.returncode == 7


def test_stream_command_unregisters_process_after_completion(runner) -> None:
    runner.stream([PYTHON, "-c", "print('x')"])
    assert runner.active_count == 0


def test_terminate_all_without_running_processes_is_zero(runner) -> None:
    assert runner.cancel() == 0


def test_capture_timeout_reaps_process(runner):
    with pytest.raises(CommandError, match="超时") as error:
        runner.capture([PYTHON, "-c", "import time; time.sleep(30)"], timeout=0.05)
    assert error.value.__cause__ is not None
    assert runner.active_count == 0


def test_callback_exception_reaps_stream_process(runner):
    def fail(line):
        raise RuntimeError("broken observer")

    with pytest.raises(RuntimeError, match="broken observer"):
        runner.stream(
            [PYTHON, "-u", "-c", "import time; print('ready'); time.sleep(30)"], on_output=fail
        )
    assert runner.active_count == 0


def test_cancel_owns_capture_stream_and_pending_tasks():
    import time
    from concurrent.futures import ThreadPoolExecutor

    from ivdc.runner import RunCancelled

    first, other = ProcessRunner(), ProcessRunner()
    with first, other, ThreadPoolExecutor(max_workers=3) as pool:
        capture = pool.submit(first.capture, [PYTHON, "-c", "import time; time.sleep(30)"])
        stream = pool.submit(first.stream, [PYTHON, "-c", "import time; time.sleep(30)"])
        independent = pool.submit(
            other.capture, [PYTHON, "-c", "import time; time.sleep(.3); print('ok')"]
        )
        deadline = time.monotonic() + 5
        while first.active_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first.active_count == 2
        assert first.cancel() == 2
        for future in (capture, stream):
            with pytest.raises(RunCancelled):
                future.result(timeout=5)
        with pytest.raises(RunCancelled):
            first.capture([PYTHON, "-c", "raise AssertionError('must not start')"])
        assert independent.result(timeout=5).stdout.strip() == "ok"
        assert first.active_count == 0


def test_worker_exception_cancels_running_and_queued_commands():
    import threading
    import time

    from ivdc.runner import worker_pool

    started = threading.Event()
    runner = ProcessRunner()
    with runner, pytest.raises(RuntimeError, match="worker failed"):
        with worker_pool(runner, 1) as pool:
            active = pool.submit(
                runner.stream,
                [PYTHON, "-u", "-c", "import time; print('ready'); time.sleep(30)"],
                on_output=lambda line: started.set(),
            )
            queued = pool.submit(runner.capture, [PYTHON, "-c", "print('never')"])
            assert started.wait(5)
            before = time.monotonic()
            raise RuntimeError("worker failed")
    assert time.monotonic() - before < 5
    assert active.done()
    assert queued.cancelled()
    assert runner.active_count == 0
