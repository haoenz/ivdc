"""磁盘读写：原子写、清单、日志。

覆盖操作采用「写同目录唯一临时文件再 ``os.replace``」——Windows 上同样是原子替换，
中途崩溃不会留下被截断的原文件。这套流程收敛在 :func:`atomic_write_text` 一处。

删除类操作（:func:`clear_tmp` / :func:`remove_tree`）**不吞异常**：失败时返回计数或
``False``，由调用方决定提示还是忽略——这一层不替调用方决定要不要报错。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import chain
from pathlib import Path

from ivdc.errors import SetupError, StorageError
from ivdc.fs import IvdcLayout, Manifest

TEXT_ENCODING = "utf-8"
READ_ENCODING = "utf-8-sig"

__all__ = [
    "READ_ENCODING",
    "TEXT_ENCODING",
    "TmpClear",
    "atomic_write_text",
    "append_line",
    "clear_tmp",
    "dir_stats",
    "load_manifest",
    "now_iso",
    "read_lines",
    "read_nonempty_lines",
    "remove_tree",
    "resolve_log_path",
    "save_manifest",
    "validate_log_path",
    "write_lines",
]


@dataclass(frozen=True)
class TmpClear:
    """:func:`clear_tmp` 的结果：删掉的条数与删除失败的条数。"""

    removed: int = 0
    failed: int = 0


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    """原子地把文本写入 ``path``（同目录临时文件 + ``os.replace``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding=TEXT_ENCODING, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            logging.getLogger(__name__).warning("未能清理原子写临时文件 %s: %s", tmp_path, exc)
        raise


def read_lines(path: Path) -> list[str]:
    """读取文本行；读取错误向调用方传播。

    ``READ_ENCODING`` 用 ``utf-8-sig``：清单可能被其它编辑器或工具改过，写出的 UTF-8
    常带 BOM，按普通 utf-8 读会在首行留下一个 U+FEFF。
    """
    return path.read_text(encoding=READ_ENCODING).splitlines()


def read_nonempty_lines(path: Path) -> list[str]:
    """读取并丢掉空行（清单允许留空行便于人工编辑）。"""
    return [line for line in read_lines(path) if line.strip()]


def write_lines(path: Path, lines: Sequence[str]) -> None:
    """原子写回多行文本（末尾带换行；空列表则写出空文件）。"""
    text = "".join(f"{line}\n" for line in lines)
    atomic_write_text(path, text)


def append_line(path: Path, line: str) -> None:
    """追加一行（日志与已完成记录用；顺序写入，不原子）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding=TEXT_ENCODING, newline="\n") as handle:
        handle.write(f"{line}\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_manifest(layout: IvdcLayout) -> Manifest:
    """读取处理清单；仅文件缺失时返回空清单，损坏或读取失败时抛 StorageError。"""
    try:
        text = layout.manifest_path.read_text(encoding=READ_ENCODING)
    except FileNotFoundError:
        return Manifest()
    except (OSError, UnicodeError) as exc:
        raise StorageError(f"无法读取 manifest {layout.manifest_path}: {exc}") from exc
    try:
        return Manifest.from_dict(json.loads(text))
    except (ValueError, TypeError) as exc:
        raise StorageError(f"manifest 无效 {layout.manifest_path}: {exc}") from exc


def save_manifest(layout: IvdcLayout, manifest: Manifest) -> None:
    """写出处理清单（原子写，随时中断都不会损坏上一次的记录）。"""
    text = json.dumps(manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_text(layout.manifest_path, f"{text}\n")


def resolve_log_path(
    layout: IvdcLayout, requested: Path | None, *, day: date | None = None
) -> Path:
    """日志路径：默认 ``.ivdc/logs/YYYY-MM-DD.log``，用户指定时相对当前目录解析。"""
    if requested is None:
        return layout.log(day or date.today())
    return requested if requested.is_absolute() else Path.cwd() / requested


def validate_log_path(layout: IvdcLayout, path: Path, sources: Iterable[Path]) -> Path:
    """只读检查日志与输入、恢复目标及工作状态的冲突，返回实际写入路径。

    单写入者约定下，在所有文件修改前调用；不存在的目标也按保留路径检查。
    已有文件还比较设备与 inode，避免硬链接绕过路径比较。
    """
    try:
        target = path.resolve()
        state, logs = layout.state_dir.resolve(), layout.logs_dir.resolve()
        reserved = (layout.backups_dir, layout.segs_dir, layout.tmp_dir)
        # 检查已有父目录的身份，兼顾目录别名及不区分大小写的文件系统。
        reserved_ids = {
            (info.st_dev, info.st_ino)
            for directory in reserved
            if (info := _stat_if_present(directory)) is not None
        }
        if (
            target == logs
            or state.is_relative_to(target)
            or (target.is_relative_to(state) and not target.is_relative_to(logs))
        ):
            raise SetupError(f"日志路径与工作状态冲突: {path}；.ivdc 内仅允许写入 logs 子目录")
        for parent in target.parents:
            info = _stat_if_present(parent)
            if info is None:
                continue
            if (info.st_dev, info.st_ino) in reserved_ids:
                raise SetupError(f"日志路径位于备份、分片或临时输出目录: {path}")
            if not stat.S_ISDIR(info.st_mode):
                raise SetupError(f"日志父路径不是目录: {parent}")
        target_info = _stat_if_present(target)
        if target_info is not None and not stat.S_ISREG(target_info.st_mode):
            raise SetupError(f"日志路径不是普通文件: {path}")
        protected = chain(
            sources,
            (layout.manifest_path,),
            *(directory.rglob("*") for directory in reserved),
        )
        for source in protected:
            resolved = source.resolve()
            if target.is_relative_to(resolved) or resolved.is_relative_to(target):
                raise SetupError(f"日志路径与输入或状态文件冲突: {path} -> {source}")
            if target_info is not None:
                source_info = _stat_if_present(source)
                if source_info is not None and (target_info.st_dev, target_info.st_ino) == (
                    source_info.st_dev,
                    source_info.st_ino,
                ):
                    raise SetupError(f"日志路径与输入或状态文件指向同一文件: {path} -> {source}")
        return target
    except OSError as exc:
        raise SetupError(f"无法校验日志路径 {path}: {exc}") from exc


def _stat_if_present(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except FileNotFoundError:
        return None


def clear_tmp(layout: IvdcLayout) -> TmpClear:
    """清掉临时输出目录里的残留，返回「删掉几个 / 几个没删掉」。

    只动 ``.ivdc/tmp/``——这个目录从构造上就只属于本工具，所以不会误伤工作目录里
    恰好以 ``.tmp`` 结尾的无关文件（比如下载用的 ``pending_downloads.txt.tmp``）。
    """
    tmp_dir = layout.tmp_dir
    if not tmp_dir.is_dir():
        return TmpClear()
    removed = 0
    failed = 0
    for entry in sorted(tmp_dir.rglob("*")):
        if not entry.is_file():
            continue
        try:
            entry.unlink()
        except OSError as exc:
            logging.getLogger(__name__).warning("无法删除 %s: %s", entry, exc)
            failed += 1
        else:
            removed += 1
    return TmpClear(removed=removed, failed=failed)


def remove_tree(path: Path) -> bool:
    """递归删除一个目录（仅用于本工具自己创建的状态目录）；成功返回 ``True``。

    目标本来就不存在算成功。其余失败（占用、权限）返回 ``False``，由调用方决定
    要不要提示——目录里可能就剩一个占着句柄的文件，递归删到一半。
    """
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        logging.getLogger(__name__).warning("无法删除目录 %s: %s", path, exc)
        return False
    return True


def dir_stats(path: Path) -> tuple[int, int]:
    """返回 ``(文件数, 字节数)``；路径不存在时为 ``(0, 0)``。"""
    if not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    count = 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            total += entry.stat().st_size
            count += 1
    return count, total


def validate_state_directory(layout: IvdcLayout) -> None:
    """状态目录中的链接不能将恢复、编码或清理引向外部文件。"""
    paths = chain((layout.state_dir,), layout.state_dir.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise SetupError(f"状态路径不能是符号链接: {path}")
