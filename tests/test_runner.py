"""子进程封装：输出收集、尾部保留、退出码、stderr 合并。"""

from __future__ import annotations

import sys

from ivdc.runner import active_count, capture_command, stream_command, terminate_all

PYTHON = sys.executable


def test_capture_command_collects_stdout() -> None:
    result = capture_command([PYTHON, "-c", "print('hi')"])
    assert result.ok
    assert result.stdout.strip() == "hi"


def test_capture_command_reports_nonzero_exit() -> None:
    result = capture_command([PYTHON, "-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.returncode == 3


def test_capture_command_reports_stderr_separately() -> None:
    result = capture_command([PYTHON, "-c", "import sys; print('bad', file=sys.stderr)"])
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == "bad"


def test_capture_command_missing_binary_does_not_raise() -> None:
    result = capture_command(["definitely-not-a-real-binary-ivdc"])
    assert not result.ok
    assert result.returncode == -1


def test_stream_command_collects_all_lines_without_callback() -> None:
    result = stream_command([PYTHON, "-c", "print('a'); print('b')"])
    assert result.ok
    assert result.lines == ("a", "b")


def test_stream_command_merges_stderr_into_the_same_stream() -> None:
    result = stream_command(
        [PYTHON, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
    )
    assert set(result.lines) == {"out", "err"}


def test_stream_command_keeps_only_the_tail_when_callback_is_used() -> None:
    seen: list[str] = []
    result = stream_command(
        [PYTHON, "-c", "print(chr(10).join(str(i) for i in range(50)))"],
        on_output=seen.append,
        keep_last=5,
    )
    assert seen == [str(i) for i in range(50)]
    assert result.lines == ("45", "46", "47", "48", "49")


def test_stream_command_reports_nonzero_exit() -> None:
    result = stream_command([PYTHON, "-c", "import sys; sys.exit(7)"])
    assert result.returncode == 7


def test_stream_command_unregisters_process_after_completion() -> None:
    stream_command([PYTHON, "-c", "print('x')"])
    assert active_count() == 0


def test_terminate_all_without_running_processes_is_zero() -> None:
    assert terminate_all() == 0
