"""下载业务与任务队列；外部协议解析在 ytdlp 模块，终端显示在 views 模块。"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from itertools import chain
from pathlib import Path

from ivdc.config import load_config
from ivdc.errors import SetupError, StorageError
from ivdc.naming import new_tag
from ivdc.parse import TaskLine, parse_task_line
from ivdc.store import append_line, read_nonempty_lines, write_lines


class DownloadFailed(RuntimeError):
    """单项下载失败，保留在任务清单中重试。"""


@dataclass(frozen=True)
class DownloadResult:
    title: str
    size_bytes: int


@dataclass(frozen=True)
class DownloadProgress:
    downloaded: int = 0
    total: int = 0
    speed: float | None = None
    eta: float | None = None
    finished: bool = False


type ProgressReporter = Callable[[DownloadProgress], None]
type Fetcher = Callable[[TaskLine, ProgressReporter], DownloadResult]


class OnError(StrEnum):
    stop = "stop"
    skip = "skip"


@dataclass(frozen=True)
class DownloadOptions:
    tasks: Path
    done: Path
    max_count: int | None = None
    on_error: OnError = OnError.stop
    mask: bool = False
    debug: bool = False


@dataclass(frozen=True)
class DownloadEvents:
    planned: Callable[[int], None] = lambda total: None
    started: Callable[[str], None] = lambda label: None
    progress: ProgressReporter = lambda progress: None
    succeeded: Callable[[str, DownloadResult, float], None] = lambda label, result, elapsed: None
    failed: Callable[[str, str], None] = lambda label, error: None


@dataclass(frozen=True)
class DownloadReport:
    completed: int = 0
    failures: tuple[str, ...] = ()
    attempted: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.failures)


class TaskQueue:
    """每次成功持久化后才推进内存队列，失败项按原顺序放到末尾。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pending = deque(read_nonempty_lines(path))
        self.failed: list[str] = []

    def advance(self, *, failed: bool = False) -> None:
        line = self.pending.popleft()
        if failed:
            self.failed.append(line)
        try:
            write_lines(self.path, list(chain(self.pending, self.failed)))
        except OSError as exc:
            self.pending.appendleft(line)
            if failed:
                self.failed.pop()
            raise StorageError(f"无法更新任务清单 {self.path}: {exc}；当前任务仍保留") from exc


def run_download(
    options: DownloadOptions,
    events: DownloadEvents | None = None,
    *,
    fetcher: Fetcher | None = None,
) -> DownloadReport:
    events = events or DownloadEvents()
    if not options.tasks.is_file():
        raise SetupError(f"任务列表文件不存在: {options.tasks}")
    if options.tasks.resolve() == options.done.resolve():
        raise SetupError("任务清单和完成记录不能使用同一路径")
    if options.max_count is not None and options.max_count < 1:
        raise SetupError("max_count 必须大于 0")
    if options.on_error not in (OnError.stop, OnError.skip):
        raise SetupError(f"未知 on_error: {options.on_error}")
    queue = TaskQueue(options.tasks)
    events.planned(len(queue.pending))
    if not queue.pending:
        return DownloadReport()
    fetch = fetcher
    if fetch is None:
        from ivdc.ytdlp import YtdlpFetcher

        fetch = YtdlpFetcher(load_config())
    completed = attempts = 0
    failures: list[str] = []
    while queue.pending and (options.max_count is None or attempts < options.max_count):
        task = parse_task_line(queue.pending[0])
        label = new_tag() if options.mask else task.title or task.url
        attempts += 1
        events.started(label)
        started = time.monotonic()
        try:
            result = fetch(task, events.progress)
        except DownloadFailed as exc:
            failures.append(str(exc))
            events.failed(label, str(exc))
            if options.on_error == OnError.stop:
                break
            queue.advance(failed=True)
            continue
        # 先写完成记录，再移除任务。写入失败宁可保留任务，不能丢失工作。
        try:
            append_line(options.done, f"{task.title or result.title} {task.url}".strip())
        except OSError as exc:
            raise StorageError(f"无法写入完成记录 {options.done}: {exc}；任务仍保留") from exc
        queue.advance()
        completed += 1
        events.succeeded(label, result, time.monotonic() - started)
    return DownloadReport(completed, tuple(failures), attempts)
