"""Rich 终端展示；将有类型的业务事件转换为进度与用户提示。"""

import logging
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import copy

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import TaskID

from ivdc.cleanup import KINDS, CleanOptions, CleanupResult
from ivdc.console import STYLE_DONE, STYLE_FAIL, STYLE_INFO, STYLE_SKIP, STYLE_WARN, emit
from ivdc.download import DownloadEvents, DownloadProgress, DownloadReport, DownloadResult
from ivdc.encoding import ProgressKey
from ivdc.formatting import (
    format_done_line,
    format_download_line,
    format_download_status,
    format_media_size,
    progress_percent,
    shorten_label,
)
from ivdc.fs import STATUS_OK, FileRecord
from ivdc.optimization_plan import OptimizationPlan, OptimizeOptions
from ivdc.optimize import OptimizationEvents, OptimizationResult
from ivdc.progress import ProgressDisplay


def error_message(message: str, *, mask: bool, summary: str = "操作失败") -> str:
    """外部错误可能含任意路径或 URL；匿名时只显示由展示层提供的固定说明。"""
    return f"{summary}；详细信息已隐藏（--mask）。" if mask else message


class MaskedDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        # 仅替换当前终端 handler 的副本；文件 handler 仍收到完整上下文。
        masked = copy(record)
        masked.msg = "诊断详情已隐藏（--mask）；关闭 --mask 可查看。"
        masked.args = ()
        masked.exc_info = None
        masked.exc_text = None
        masked.stack_info = None
        return masked


@contextmanager
def diagnostics(console: Console, debug: bool = False, *, mask: bool = False) -> Iterator[None]:
    logger = logging.getLogger("ivdc")
    previous = logger.level
    previous_propagate = logger.propagate
    handler = RichHandler(console=console, show_time=False, show_path=False, markup=False)
    handler.setLevel(logging.DEBUG if debug and not mask else logging.WARNING)
    if mask:
        handler.addFilter(MaskedDiagnostics())
        # 根 logger 的控制台 handler 不能绕过匿名展示；ivdc 的文件 handler 不受影响。
        logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous)
        logger.propagate = previous_propagate


class OptimizationDisplay:
    def __init__(
        self, options: OptimizeOptions, console: Console, display: ProgressDisplay
    ) -> None:
        self.options, self.console, self.display = options, console, display
        self.lock = threading.Lock()
        self.tasks: dict[ProgressKey, TaskID] = {}
        self.labels: dict[str, str] = {}
        self.durations: dict[ProgressKey, float] = {}
        self.seconds: dict[ProgressKey, float] = {}
        self.completed = 0
        self.total = 0

    @property
    def events(self) -> OptimizationEvents:
        return OptimizationEvents(
            self.planned,
            self.progress,
            self.finished,
            lambda message: emit(self.console, message, STYLE_INFO),
        )

    def planned(self, plan: OptimizationPlan, codec: str) -> None:
        for label in plan.skipped:
            emit(self.console, f"跳过: {label} (已是 {codec} 编码)", STYLE_SKIP)
        if not plan.tasks:
            message = "未找到匹配的视频文件。"
            if plan.skipped:
                message = "所有视频已是目标编码，无需处理。"
            elif not self.options.mask:
                message = f"未找到匹配的文件: {self.options.pattern}"
            emit(self.console, message, STYLE_INFO)
        self.total = len(plan.tasks)
        if self.options.dry_run:
            for name in plan.recoveries:
                emit(self.console, f"将恢复备份: {name if not self.options.mask else '已匿名'}")
            emit(self.console, f"将清理临时文件 {len(plan.temporary_files)} 个；保留可恢复分片。")
            emit(self.console, f"将要处理 {len(plan.tasks)} 个文件（编码 {codec}）:")
            for task in plan.tasks:
                details = f"，{len(task.segments)} 片" if task.segments else ""
                error = (
                    ", "
                    + error_message(task.error, mask=self.options.mask, summary="无法处理此文件")
                    if task.error
                    else ""
                )
                emit(self.console, f"  {task.label} {task.duration:.1f}s{details}{error}")
            emit(self.console, "（--dry-run 不做任何修改）", STYLE_SKIP)
            return
        for task in plan.tasks:
            self.labels[task.name] = task.label
            parts = (
                [
                    (
                        (task.name, part.index),
                        part.duration,
                        f"{shorten_label(task.label)} [seg {part.index + 1}/{len(task.segments)}]",
                    )
                    for part in task.segments
                ]
                if task.segments
                else [((task.name, None), task.duration, task.label)]
            )
            for key, duration, label in parts:
                self.durations[key] = duration
                self.tasks[key] = self.display.add_task(
                    shorten_label(label, 36), total=100 if duration else None
                )
        self.display.set_global(0, completed=0, total=self.total)

    def progress(self, key: ProgressKey, seconds: float) -> None:
        with self.lock:
            self.seconds[key] = max(self.seconds.get(key, 0), seconds)
            percent = progress_percent(seconds, self.durations[key])
            self.display.update(
                self.tasks[key], completed=percent, status=f"{seconds:.1f}s ({percent}%)"
            )
            self.display.set_global(
                progress_percent(
                    math.fsum(self.seconds.values()), math.fsum(self.durations.values())
                ),
                completed=self.completed,
                total=self.total,
            )

    def finished(self, record: FileRecord) -> None:
        with self.lock:
            for key in tuple(self.tasks):
                if key[0] == record.name:
                    self.seconds[key] = self.durations[key]
                    self.display.remove_task(self.tasks.pop(key))
            self.completed += 1
            self.display.set_global(
                progress_percent(
                    math.fsum(self.seconds.values()), math.fsum(self.durations.values())
                ),
                completed=self.completed,
                total=self.total,
            )
        label = self.labels[record.name]
        if record.status == STATUS_OK:
            self.display.log(
                format_done_line(label, record.old_bytes, record.new_bytes, record.seconds / 60),
                STYLE_DONE,
            )
        else:
            message = error_message(record.message, mask=self.options.mask, summary="详见处理日志")
            self.display.log(f"压制失败 ({label}): {message}", STYLE_FAIL)

    def summary(self, result: OptimizationResult) -> None:
        if self.options.dry_run:
            return
        succeeded = sum(record.status == STATUS_OK for record in result.records)
        for failure in result.failures:
            emit(
                self.console,
                error_message(failure, mask=self.options.mask, summary="恢复或清理失败"),
                STYLE_WARN,
            )
        emit(
            self.console,
            f"完成: 成功 {succeeded} 个，失败 {len(result.records) - succeeded} 个。",
            STYLE_WARN if result.failed else STYLE_DONE,
        )


class DownloadDisplay:
    def __init__(self, display: ProgressDisplay, *, mask: bool = False) -> None:
        self.display = display
        self.mask = mask
        self.task_id: TaskID | None = None
        self.completed = 0
        self.total = 0

    @property
    def events(self) -> DownloadEvents:
        return DownloadEvents(
            self.planned, self.started, self.progress, self.succeeded, self.failed
        )

    def planned(self, total: int) -> None:
        self.total = total
        self.display.set_global(0, completed=0, total=total)

    def started(self, label: str) -> None:
        self.task_id = self.display.add_task(shorten_label(label))

    def progress(self, progress: DownloadProgress) -> None:
        if self.task_id is not None:
            self.display.update(
                self.task_id,
                completed=100
                if progress.finished
                else progress_percent(progress.downloaded, progress.total),
                status="下载完成，处理中..."
                if progress.finished
                else format_download_status(
                    progress.downloaded, progress.total, progress.speed, progress.eta
                ),
            )

    def _remove(self) -> None:
        if self.task_id is not None:
            self.display.remove_task(self.task_id)
            self.task_id = None

    def succeeded(self, label: str, result: DownloadResult, elapsed: float) -> None:
        self._remove()
        self.completed += 1
        self.display.set_global(
            progress_percent(self.completed, self.total), completed=self.completed, total=self.total
        )
        self.display.log(
            format_download_line(label, format_media_size(result.size_bytes), elapsed / 60),
            STYLE_DONE,
        )

    def failed(self, label: str, error: str) -> None:
        self._remove()
        message = error_message(error, mask=self.mask, summary="关闭 --mask 可查看错误详情")
        self.display.log(f"✗ {label} 下载失败: {message}", STYLE_FAIL)

    def summary(self, result: DownloadReport, *, stop: bool) -> None:
        if result.attempted == 0:
            self.display.log("任务列表为空，没有需要下载的内容。", STYLE_INFO)
        elif stop and result.failed:
            self.display.log("中止后续任务。", STYLE_FAIL)
        self.display.log(
            f"完成: 成功 {result.completed} 个，失败 {len(result.failures)} 个。",
            STYLE_WARN if result.failed else STYLE_DONE,
        )


def show_cleanup(options: CleanOptions, result: CleanupResult, console: Console) -> None:
    if result.protected:
        emit(
            console,
            f"跳过 {len(result.protected)} 个备份：对应的成片不在工作目录，"
            "删除会丢掉唯一的一份源文件。",
            STYLE_WARN,
        )
    if not result.items:
        emit(console, "没有需要清理的内容。", STYLE_INFO)
        return
    if options.dry_run:
        for item in result.items:
            emit(console, f"[{item.kind}] {item.path} / {format_media_size(item.size)}")
        emit(
            console,
            f"共可释放 {format_media_size(sum(item.size for item in result.items))}"
            "（--dry-run 未删除任何文件）",
        )
    else:
        emit(console, f"已清理 {result.removed} 项，共释放 {format_media_size(result.released)}。")
        for kind in KINDS:
            selected = [item for item in result.items if item.kind == kind]
            if selected:
                failed = sum(failure.item.kind == kind for failure in result.failures)
                emit(console, f"已清理 [{kind}] {len(selected) - failed} 项")
        for error in result.failures:
            emit(console, f"未能删除: {error.message}", STYLE_WARN)
