"""命令行入口：``ivdc opt`` / ``ivdc dl`` / ``ivdc clean``。

Typer 只负责解析与帮助文本，实际逻辑都在各自的模块里；退出码统一按
:mod:`ivdc.exitcodes` 的约定返回。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer

from ivdc.__about__ import __version__
from ivdc.cleanup import KINDS, CleanOptions, run_clean
from ivdc.console import get_console
from ivdc.download import DlOptions, OnError, run_download
from ivdc.optimize import (
    DEFAULT_CODEC,
    DEFAULT_CUDA,
    DEFAULT_PATTERN,
    DEFAULT_SEGMENT_MINUTES,
    DEFAULT_THROTTLE,
    OptOptions,
    run_optimize,
)

__all__ = ["app", "main", "Codec", "CudaMode", "OnError"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="基于 yt-dlp 与 ffmpeg 的视频下载 / 压制工具。",
)


class Codec(str, Enum):
    """目标编码器。"""

    x265 = "x265"
    av1 = "av1"
    vp9 = "vp9"


class CudaMode(str, Enum):
    """CUDA 硬件加速模式。"""

    off = "off"
    decode = "decode"
    encode = "encode"


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
    codec: Annotated[Codec, typer.Option("--codec", help="目标编码")] = Codec(DEFAULT_CODEC),
    cuda: Annotated[CudaMode, typer.Option("--cuda", help="CUDA 模式")] = CudaMode(DEFAULT_CUDA),
    crf: Annotated[
        Optional[int],
        typer.Option("--crf", min=0, max=63, help="压缩质量，默认随编码器（28 / 30 / 31）"),
    ] = None,
    throttle: Annotated[
        int, typer.Option("--throttle", min=1, max=64, help="并行任务数")
    ] = DEFAULT_THROTTLE,
    segments: Annotated[
        int, typer.Option("--segments", min=0, max=1440, help="分片时长（分钟），0 表示整片编码")
    ] = DEFAULT_SEGMENT_MINUTES,
    log: Annotated[
        Optional[Path],
        typer.Option("--log", help="日志文件，默认 .ivdc/logs/YYYY-MM-DD.log"),
    ] = None,
    keep_backup: Annotated[
        bool, typer.Option("--keep-backup/--no-keep-backup", help="是否保留源文件备份")
    ] = True,
    mask: Annotated[bool, typer.Option("--mask", help="用随机串代替真实文件名")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="打印底层命令与诊断信息")] = False,
    what_if: Annotated[bool, typer.Option("--what-if", help="只打印将要执行的操作")] = False,
) -> None:
    """批量压制视频文件（x265 / av1 / vp9，可选 CUDA 加速）。"""
    code = run_optimize(
        OptOptions(
            path=path,
            pattern=pattern,
            codec=codec.value,
            cuda=cuda.value,
            crf=crf,
            throttle=throttle,
            segment_minutes=segments,
            log_file=log,
            keep_backup=keep_backup,
            mask=mask,
            debug=debug,
            what_if=what_if,
        ),
        get_console(),
    )
    raise typer.Exit(code)


@app.command()
def dl(
    tasks: Annotated[Path, typer.Option("--tasks", help="任务清单，每行「标题 URL」或「URL 标题」")] = Path(
        "tbd.txt"
    ),
    done: Annotated[Path, typer.Option("--done", help="已完成记录")] = Path("d.txt"),
    maximum: Annotated[
        Optional[int], typer.Option("--max", min=1, help="本次最多处理条数")
    ] = None,
    on_error: Annotated[OnError, typer.Option("--on-error", help="失败后的行为")] = OnError.stop,
    mask: Annotated[bool, typer.Option("--mask", help="用随机串代替真实标题")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="打印底层命令与诊断信息")] = False,
) -> None:
    """按任务清单逐条下载（清单会随完成情况原子写回）。"""
    code = run_download(
        DlOptions(
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
    what_if: Annotated[bool, typer.Option("--what-if", help="只列出将要删除的内容")] = False,
) -> None:
    """清理工作目录下 .ivdc/ 里的残留（只动 .ivdc/）。"""
    flags = {"backups": backups, "segs": segs, "tmp": tmp, "logs": logs}
    # 顺序与成员都以 KINDS 为准；新增类别而忘了加开关时，flags[kind] 会直接报错，
    # 不会静默漏掉。
    selected = list(KINDS) if everything else [kind for kind in KINDS if flags[kind]]
    code = run_clean(CleanOptions(path=path, kinds=tuple(selected), what_if=what_if), get_console())
    raise typer.Exit(code)


def main() -> None:
    """``ivdc`` 控制台脚本入口。"""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
