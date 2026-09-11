"""进度显示：rich 多行进度条 + 任务栏 OSC 序列。"""

from __future__ import annotations

from io import StringIO

from rich.console import Console

from ivdc.progress import (
    TASKBAR_CLEAR,
    TASKBAR_INDETERMINATE,
    TASKBAR_NORMAL,
    ProgressDisplay,
    taskbar_sequence,
    window_title_sequence,
)


def _terminal_console() -> tuple[Console, StringIO]:
    stream = StringIO()
    return Console(file=stream, force_terminal=True, width=120), stream


def test_taskbar_sequence_layout() -> None:
    assert taskbar_sequence(1, 42) == "\x1b]9;4;1;42\x07"
    assert taskbar_sequence(0, 0) == "\x1b]9;4;0;0\x07"


def test_window_title_sequence_layout() -> None:
    assert window_title_sequence("[ivdc]Optimizing(1/2)") == "\x1b]0;[ivdc]Optimizing(1/2)\x07"


def test_taskbar_percent_is_clamped() -> None:
    assert taskbar_sequence(1, 140) == "\x1b]9;4;1;140\x07"
    console, stream = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    display.set_global(250, completed=0, total=1)
    display.close()
    assert taskbar_sequence(TASKBAR_NORMAL, 100) in stream.getvalue()


def test_start_announces_indeterminate_and_close_clears() -> None:
    console, stream = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    display.close()
    text = stream.getvalue()
    assert taskbar_sequence(TASKBAR_INDETERMINATE, 0) in text
    assert taskbar_sequence(TASKBAR_CLEAR, 0) in text


def test_log_goes_to_the_console() -> None:
    console, stream = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    display.log("跳过: a.mp4 (已是 hevc 编码)", "grey62")
    display.close()
    assert "跳过: a.mp4 (已是 hevc 编码)" in stream.getvalue()


def test_terminal_title_tracks_completion() -> None:
    console, stream = _terminal_console()
    display = ProgressDisplay(console, title_prefix="Optimizing", refresh_interval=5.0)
    display.start()
    display.set_global(0, completed=0, total=3)
    display.set_global(33, completed=1, total=3)
    display.close()
    assert window_title_sequence("[ivdc]Optimizing(1/3)") in stream.getvalue()


def test_taskbar_is_deduplicated_between_updates() -> None:
    console, stream = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    display.set_global(10, completed=0, total=2)
    display.set_global(10, completed=0, total=2)
    display.set_global(10, completed=0, total=2)
    display.close()
    assert stream.getvalue().count(taskbar_sequence(TASKBAR_NORMAL, 10)) == 1


def test_task_labels_and_progress_are_rendered() -> None:
    console, stream = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    task_id = display.add_task("样本 一.mp4")
    display.update(task_id, completed=50, status="00:00:02 (50%)")
    display.close()
    text = stream.getvalue()
    assert "样本 一.mp4" in text
    assert "50%" in text


def test_remove_task_stops_rendering_it() -> None:
    console, stream = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    task_id = display.add_task("gone.mp4")
    display.remove_task(task_id)
    display.close()
    assert "gone.mp4" not in stream.getvalue()


def test_non_terminal_console_writes_no_osc_sequences() -> None:
    stream = StringIO()
    console = Console(file=stream)
    display = ProgressDisplay(console)
    assert display.enabled is False
    display.start()
    display.log("hello")
    display.set_global(50)
    display.close()
    text = stream.getvalue()
    assert "hello" in text
    assert "\x1b]9;4" not in text


def test_close_is_idempotent() -> None:
    console, _ = _terminal_console()
    display = ProgressDisplay(console, refresh_interval=5.0)
    display.start()
    display.close()
    display.close()


def test_context_manager_returns_started_display() -> None:
    console, stream = _terminal_console()
    with ProgressDisplay(console, refresh_interval=5.0) as display:
        display.set_global(1, completed=0, total=1)
    assert taskbar_sequence(TASKBAR_CLEAR, 0) in stream.getvalue()


def test_update_without_changes_is_a_noop() -> None:
    console, _ = _terminal_console()
    with ProgressDisplay(console, refresh_interval=5.0) as display:
        task_id = display.add_task("a.mp4")
        display.update(task_id)
