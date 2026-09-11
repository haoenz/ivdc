"""``ivdc clean``：清理状态目录里的残留。

压制过程中断后备份与分片会留在 ``.ivdc/`` 下等着下次续压，跑完之后它们就一直
占着地方——这个命令负责收尾，且**只动 ``.ivdc/``**。

一条安全约束：**源文件不在工作目录时，它的备份不删**。那种状态下备份是唯一的
一份数据，删了就真没了。

删除是逐项进行的，删不掉的项（被占用、权限不足）会被计数并报出来，退出码记为失败：
「已清理」这句话必须与磁盘上的实际结果一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from ivdc.console import STYLE_DONE, STYLE_ERROR, STYLE_INFO, STYLE_WARN, emit
from ivdc.exitcodes import EXIT_ENV, EXIT_FAILURE, EXIT_OK
from ivdc.formatting import format_media_size
from ivdc.fs import IvdcLayout
from ivdc.store import dir_stats, remove_tree

KINDS = ("backups", "segs", "tmp", "logs")

PROTECTED_KIND = "backups"
"""删之前要确认「源文件仍在工作目录」的类别。

备份在而成片不在，说明备份是这份数据的唯一副本（见模块 docstring 里的安全约束）。
"""

__all__ = ["KINDS", "PROTECTED_KIND", "CleanOptions", "run_clean"]


@dataclass(frozen=True)
class CleanOptions:
    """``ivdc clean`` 的选项。"""

    path: Path
    kinds: tuple[str, ...] = ()
    what_if: bool = False


@dataclass
class _Target:
    """一个待清理的类别。"""

    kind: str
    units: list[Path]
    size: int


def run_clean(options: CleanOptions, console: Console) -> int:
    """清理状态目录，返回进程退出码。"""
    root = options.path.expanduser()
    if not root.is_dir():
        emit(console, f"路径不存在或不是目录: {root}", STYLE_ERROR)
        return EXIT_ENV
    kinds = tuple(kind for kind in KINDS if kind in options.kinds)
    if not kinds:
        emit(
            console,
            "请指定要清理的类别（--backups / --segs / --tmp / --logs，或 --all）。",
            STYLE_ERROR,
        )
        return EXIT_ENV

    layout = IvdcLayout(root)
    targets, protected = _collect(layout, kinds)

    if protected:
        emit(
            console,
            f"跳过 {len(protected)} 个备份：对应的成片不在工作目录，"
            "删除会丢掉唯一的一份源文件。",
            STYLE_WARN,
        )

    if not targets:
        emit(console, "没有需要清理的内容。", STYLE_INFO)
        return EXIT_OK

    total_size = sum(target.size for target in targets)
    if options.what_if:
        for target in targets:
            emit(
                console,
                f"  [{target.kind}] {len(target.units)} 项 / "
                f"{format_media_size(target.size)}",
            )
        emit(
            console,
            f"共可释放 {format_media_size(total_size)}（--what-if 未删除任何文件）",
            STYLE_INFO,
        )
        return EXIT_OK

    failures: list[str] = []
    for target in targets:
        failed = [unit for unit in target.units if not _remove(unit)]
        released = target.size - sum(dir_stats(unit)[1] for unit in failed)
        if failed:
            failures.extend(str(unit) for unit in failed)
        emit(
            console,
            f"已清理 [{target.kind}] {len(target.units) - len(failed)} 项，"
            f"释放 {format_media_size(released)}",
            STYLE_WARN if failed else STYLE_DONE,
        )

    if failures:
        emit(
            console,
            f"{len(failures)} 项未能删除（可能正被其它程序占用），仍留在 .ivdc/ 下:",
            STYLE_WARN,
        )
        for unit in failures:
            emit(console, f"  {unit}", STYLE_WARN)
        return EXIT_FAILURE

    emit(console, f"共释放 {format_media_size(total_size)}。", STYLE_DONE)
    return EXIT_OK


def _collect(layout: IvdcLayout, kinds: tuple[str, ...]) -> tuple[list[_Target], list[str]]:
    targets: list[_Target] = []
    protected: list[str] = []
    for kind in kinds:
        # KINDS 的每一项都对应 IvdcLayout 上的一个 <kind>_dir 属性，目录名不必在这里
        # 再抄一遍。
        directory: Path = getattr(layout, f"{kind}_dir")
        if not directory.is_dir():
            continue
        units: list[Path] = []
        for unit in sorted(directory.iterdir()):
            if kind == PROTECTED_KIND and not (layout.root / unit.name).exists():
                protected.append(unit.name)
                continue
            units.append(unit)
        if not units:
            continue
        size = 0
        for unit in units:
            _files, byte_size = dir_stats(unit)
            size += byte_size
        targets.append(_Target(kind=kind, units=units, size=size))
    return targets, protected


def _remove(unit: Path) -> bool:
    """删掉一个文件或目录；成功返回 ``True``（目录交给 :func:`remove_tree` 递归删）。"""
    if unit.is_dir():
        return remove_tree(unit)
    try:
        unit.unlink()
    except OSError:
        return False
    return True
