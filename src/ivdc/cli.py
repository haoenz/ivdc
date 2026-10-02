"""命令行入口：``ivdc opt`` / ``ivdc dl`` / ``ivdc clean``。

Typer 只负责解析与帮助文本，实际逻辑都在各自的模块里；退出码统一按
:mod:`ivdc.exitcodes` 的约定返回。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

import typer
from rich.console import Console

from ivdc.__about__ import __version__
from ivdc.cleanup import KINDS, CleanOptions, run_clean
from ivdc.console import STYLE_ERROR, emit, get_console
from ivdc.download import DownloadOptions, Fetcher, OnError, run_download
from ivdc.errors import SetupError, StorageError
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.optimization_plan import (
    DEFAULT_PATTERN,
    DEFAULT_SEGMENT_MINUTES,
    DEFAULT_WORKERS,
    OptimizeOptions,
)
from ivdc.optimize import run_optimize
from ivdc.progress import ProgressDisplay
from ivdc.quality import Quality
from ivdc.runner import CommandError, RunCancelled
from ivdc.views import DownloadDisplay, OptimizationDisplay, diagnostics, show_cleanup

__all__ = ["app", "main", "Codec", "CudaMode", "OnError"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="基于 yt-dlp 与 ffmpeg 的视频下载 / 压制工具。",
)


class Codec(StrEnum):
    """目标编码器。"""

    x265 = "x265"
    av1 = "av1"
    vp9 = "vp9"


class CudaMode(StrEnum):
    """CUDA 硬件加速模式。"""

    off = "off"
    decode = "decode"
    encode_video = "encode"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ivdc {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="显示版本号"),
    ] = False,
) -> None:
    """基于 yt-dlp 与 ffmpeg 的视频下载 / 压制工具。"""


@app.command()
def opt(
    path: Annotated[Path, typer.Option("--path", "-p", help="待处理的视频目录")] = Path("."),
    pattern: Annotated[str, typer.Option("--filter", help="文件匹配规则")] = DEFAULT_PATTERN,
    codec: Annotated[Codec, typer.Option("--codec", help="目标编码")] = Codec.x265,
    cuda: Annotated[CudaMode, typer.Option("--cuda", help="CUDA 模式")] = CudaMode.decode,
    quality: Annotated[
        Quality,
        typer.Option("--quality", help="画质：high 高 / medium 中 / low 低；高画质通常文件更大"),
    ] = Quality.medium,
    workers: Annotated[
        int, typer.Option("--workers", min=1, max=64, help="并行任务数")
    ] = DEFAULT_WORKERS,
    segments: Annotated[
        int, typer.Option("--segments", min=0, max=1440, help="分片时长（分钟），0 表示整片编码")
    ] = DEFAULT_SEGMENT_MINUTES,
    log: Annotated[
        Path | None,
        typer.Option(
            "--log", help="日志文件，默认 .ivdc/logs/YYYY-MM-DD.log；不可与输入或状态文件冲突"
        ),
    ] = None,
    keep_backup: Annotated[
        bool,
        typer.Option(
            "--keep-backup/--no-keep-backup", help="成功后保留本次新建备份；已有备份始终保留"
        ),
    ] = True,
    mask: Annotated[bool, typer.Option("--mask", help="用随机串代替真实文件名")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="打印底层命令与诊断信息")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只读计划，不恢复、清理或写入任何文件")
    ] = False,
) -> None:
    """批量压制视频文件（x265 / av1 / vp9，可选 CUDA 加速）。"""
    code = execute_optimize(
        OptimizeOptions(
            path=path,
            pattern=pattern,
            codec=codec.value,
            cuda=cuda.value,
            quality=quality,
            workers=workers,
            segment_minutes=segments,
            log_file=log,
            keep_backup=keep_backup,
            mask=mask,
            debug=debug,
            dry_run=dry_run,
        ),
        get_console(),
    )
    raise typer.Exit(code)


@app.command()
def dl(
    tasks: Annotated[
        Path, typer.Option("--tasks", help="任务清单，每行「标题 URL」或「URL 标题」")
    ] = Path("tbd.txt"),
    done: Annotated[Path, typer.Option("--done", help="已完成记录")] = Path("d.txt"),
    maximum: Annotated[int | None, typer.Option("--max", min=1, help="本次最多处理条数")] = None,
    on_error: Annotated[OnError, typer.Option("--on-error", help="失败后的行为")] = OnError.stop,
    mask: Annotated[bool, typer.Option("--mask", help="用随机串代替真实标题")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="打印底层命令与诊断信息")] = False,
) -> None:
    """按任务清单逐条下载（清单会随完成情况原子写回）。"""
    code = execute_download(
        DownloadOptions(
            tasks=tasks,
            done=done,
            max_count=maximum,
            on_error=on_error,
            mask=mask,
            debug=debug,
        ),
        get_console(),
    )
    raise typer.Exit(code)


@app.command()
def clean(
    path: Annotated[Path, typer.Option("--path", "-p", help="目标目录")] = Path("."),
    backups: Annotated[bool, typer.Option("--backups", help="清理源文件备份")] = False,
    segs: Annotated[bool, typer.Option("--segs", help="清理分片目录")] = False,
    tmp: Annotated[bool, typer.Option("--tmp", help="清理临时输出")] = False,
    logs: Annotated[bool, typer.Option("--logs", help="清理日志")] = False,
    everything: Annotated[bool, typer.Option("--all", help="等价于全选")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只列出将要删除的内容")] = False,
) -> None:
    """清理工作目录下 .ivdc/ 里的残留（只动 .ivdc/）。"""
    flags = {"backups": backups, "segs": segs, "tmp": tmp, "logs": logs}
    # 顺序与成员都以 KINDS 为准；新增类别而忘了加开关时，flags[kind] 会直接报错，
    # 不会静默漏掉。
    selected = list(KINDS) if everything else [kind for kind in KINDS if flags[kind]]
    code = execute_clean(
        CleanOptions(path=path, kinds=tuple(selected), dry_run=dry_run), get_console()
    )
    raise typer.Exit(code)


class Outcome(Protocol):
    @property
    def failed(self) -> bool: ...


def execute(operation: Callable[[], Outcome], console: Console) -> int:
    """唯一的业务异常到退出码边界；保留异常日志与具体错误上下文。"""
    try:
        return EXIT_FAILURE if operation().failed else EXIT_OK
    except SetupError as exc:
        emit(console, str(exc), STYLE_ERROR)
        return EXIT_ENV
    except KeyboardInterrupt, RunCancelled:
        emit(console, "已中断。子进程已回收；未完成的任务与分片保留，下次运行可继续。", STYLE_ERROR)
        return EXIT_FAILURE
    except (StorageError, CommandError, OSError, UnicodeError) as exc:
        emit(console, str(exc), STYLE_ERROR)
        logging.getLogger(__name__).debug("运行失败", exc_info=True)
        return EXIT_FAILURE
    except Exception:
        # CLI 顶层兜底：程序错误属于整轮失败，不冒充可恢复的单项结果。
        logging.getLogger(__name__).exception("未预期的运行错误")
        return EXIT_FAILURE


def execute_optimize(options: OptimizeOptions, console: Console) -> int:
    with (
        diagnostics(console, options.debug),
        ProgressDisplay(console, title_prefix="Optimizing") as progress,
    ):
        view = OptimizationDisplay(options, console, progress)

        def operation() -> Outcome:
            result = run_optimize(options, view.events)
            view.summary(result)
            return result

        return execute(operation, console)


def execute_download(
    options: DownloadOptions, console: Console, *, fetcher: Fetcher | None = None
) -> int:
    with (
        diagnostics(console, options.debug),
        ProgressDisplay(console, title_prefix="Downloading") as progress,
    ):
        view = DownloadDisplay(progress)

        def operation() -> Outcome:
            result = run_download(options, view.events, fetcher=fetcher)
            view.summary(result, stop=options.on_error == OnError.stop)
            return result

        return execute(operation, console)


def execute_clean(options: CleanOptions, console: Console) -> int:
    with diagnostics(console):

        def operation() -> Outcome:
            result = run_clean(options)
            show_cleanup(options, result, console)
            return result

        return execute(operation, console)


def main() -> None:
    """``ivdc`` 控制台脚本入口。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
