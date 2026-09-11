"""进度渲染：rich 多行进度条 + 任务栏进度（OSC 9;4）。

多行渲染、CJK 宽度对齐、非终端自动降级都由 rich 的 ``Progress`` 负责；这一层只补
它不提供的两件事——任务栏进度与窗口标题。

刷新由一条后台线程按固定频率驱动（``auto_refresh=False``），这样 10Hz 的刷新与
OSC 序列写在同一个锁内，不会互相插队把画面撕开。
"""

from __future__ import annotations

import os
import threading
from contextlib import suppress
from typing import IO

from rich.console import Console
from rich.progress import BarColumn, Progress, TaskID, TaskProgressColumn, TextColumn
from rich.table import Column

from ivdc.console import STYLE_PLAIN, emit

TASKBAR_CLEAR = 0
TASKBAR_NORMAL = 1
TASKBAR_INDETERMINATE = 3

DEFAULT_REFRESH_INTERVAL = 0.1

__all__ = [
    "DEFAULT_REFRESH_INTERVAL",
    "TASKBAR_CLEAR",
    "TASKBAR_INDETERMINATE",
    "TASKBAR_NORMAL",
    "ProgressDisplay",
    "taskbar_sequence",
    "window_title_sequence",
]

_LABEL_COLUMN = 24
_BAR_WIDTH = 20


def taskbar_sequence(state: int, percent: int = 0) -> str:
    """Windows Terminal 的任务栏进度控制序列（``OSC 9;4``）。"""
    return f"\x1b]9;4;{state};{percent}\x07"


def window_title_sequence(title: str) -> str:
    """设置窗口标题的 OSC 序列。"""
    return f"\x1b]0;{title}\x07"


def _read_window_title() -> str | None:
    """读取当前控制台窗口标题（仅 Windows 可用，失败返回 None）。"""
    if os.name != "nt":
        return None
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        length = ctypes.windll.kernel32.GetConsoleTitleW(buffer, 1024)  # type: ignore[attr-defined]
        return buffer.value if length else ""
    except Exception:  # pragma: no cover - 非控制台宿主
        return None


class ProgressDisplay:
    """一屏多行的进度显示，外加任务栏百分比与窗口标题。"""

    def __init__(
        self,
        console: Console,
        *,
        title_prefix: str = "ivdc",
        label_width: int = _LABEL_COLUMN,
        bar_width: int = _BAR_WIDTH,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    ) -> None:
        self._console = console
        self._title_prefix = title_prefix
        self._refresh_interval = refresh_interval
        self._lock = threading.RLock()
        self._enabled = bool(console.is_terminal)
        self._progress = Progress(
            TextColumn(
                "{task.description}",
                table_column=Column(width=label_width, no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(
                bar_width=bar_width,
                style="grey35",
                complete_style="green",
                finished_style="green",
                pulse_style="green",
            ),
            TaskProgressColumn(),
            TextColumn(
                "{task.fields[status]}",
                style="dim",
                table_column=Column(no_wrap=True, overflow="ellipsis"),
            ),
            console=console,
            auto_refresh=False,
            disable=not self._enabled,
        )
        self._started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._percent = 0
        self._completed = 0
        self._total = 0
        self._last_taskbar: tuple[int, int] | None = None
        self._last_title: str | None = None
        self._saved_title: str | None = None

    # ---- 生命周期 ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """是否为交互式终端（非终端下只保留纯文本输出）。"""
        return self._enabled

    def start(self) -> ProgressDisplay:
        if self._started:
            return self
        self._started = True
        if not self._enabled:
            return self
        self._saved_title = _read_window_title()
        self._progress.start()
        self._push_taskbar(TASKBAR_INDETERMINATE, 0)
        self._thread = threading.Thread(target=self._tick, name="ivdc-progress", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if not self._started:
            return
        self._started = False
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        if self._enabled:
            with self._lock:
                with suppress(Exception):
                    self._progress.refresh()
                with suppress(Exception):
                    self._progress.stop()
            self._push_taskbar(TASKBAR_CLEAR, 0)
            self._push_title(self._saved_title if self._saved_title is not None else "")
        self._console.print()

    def __enter__(self) -> ProgressDisplay:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ---- 进度条 -----------------------------------------------------------

    def add_task(self, label: str, *, total: int | None = 100, status: str = "") -> TaskID:
        """新增一条进度条；``total=None`` 表示未知总量（脉冲式）。"""
        with self._lock:
            return self._progress.add_task(label, total=total, status=status)

    def update(
        self,
        task_id: TaskID,
        *,
        completed: float | None = None,
        total: float | None = None,
        status: str | None = None,
    ) -> None:
        fields: dict[str, object] = {}
        if completed is not None:
            fields["completed"] = completed
        if total is not None:
            fields["total"] = total
        if status is not None:
            fields["status"] = status
        if not fields:
            return
        with self._lock:
            with suppress(Exception):
                self._progress.update(task_id, **fields)

    def remove_task(self, task_id: TaskID) -> None:
        with self._lock:
            with suppress(Exception):
                self._progress.remove_task(task_id)

    def set_global(
        self, percent: int, *, completed: int | None = None, total: int | None = None
    ) -> None:
        """更新整体进度（任务栏 + 窗口标题用）。

        顺手把两个 OSC 序列推出去：任务栏百分比与窗口标题都按值去重，所以只有
        真正变化时才会写，不必等下一次 tick。
        """
        with self._lock:
            self._percent = max(0, min(100, int(percent)))
            if completed is not None:
                self._completed = completed
            if total is not None:
                self._total = total
            percent_value = self._percent
            completed_value = self._completed
            total_value = self._total
        self._push_taskbar(TASKBAR_NORMAL, percent_value)
        self._push_title(f"[ivdc]{self._title_prefix}({completed_value}/{total_value})")

    # ---- 纯文本输出 -------------------------------------------------------

    def log(self, message: str, style: str | None = None) -> None:
        """在进度条上方打印一行（rich 会把活动区域挪到下面重绘）。"""
        with self._lock:
            emit(self._console, message, style or STYLE_PLAIN)

    # ---- 内部 -------------------------------------------------------------

    def _tick(self) -> None:
        while not self._stop.wait(self._refresh_interval):
            with self._lock:
                with suppress(Exception):
                    self._progress.refresh()

    def _write(self, text: str) -> None:
        stream: IO[str] = self._console.file
        try:
            stream.write(text)
            stream.flush()
        except (OSError, ValueError):  # pragma: no cover - 流已关闭
            pass

    def _push_taskbar(self, state: int, percent: int) -> None:
        if not self._enabled:
            return
        key = (state, max(0, min(100, int(percent))))
        if key == self._last_taskbar:
            return
        self._last_taskbar = key
        self._write(taskbar_sequence(*key))

    def _push_title(self, title: str) -> None:
        if not self._enabled:
            return
        if title == self._last_title:
            return
        self._last_title = title
        self._write(window_title_sequence(title))
