"""状态目录清理计划与执行，返回实际删除结果。"""

import logging
from dataclasses import dataclass
from pathlib import Path

from ivdc.errors import SetupError
from ivdc.fs import IvdcLayout
from ivdc.store import dir_stats, remove_tree, validate_state_directory

KINDS = ("backups", "segs", "tmp", "logs")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanOptions:
    path: Path
    kinds: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class CleanupItem:
    kind: str
    path: Path
    size: int


@dataclass(frozen=True)
class CleanupFailure:
    item: CleanupItem
    message: str


@dataclass(frozen=True)
class CleanupResult:
    items: tuple[CleanupItem, ...]
    protected: tuple[str, ...] = ()
    removed: int = 0
    released: int = 0
    failures: tuple[CleanupFailure, ...] = ()

    @property
    def failed(self) -> bool:
        return bool(self.failures)


def run_clean(options: CleanOptions) -> CleanupResult:
    root = options.path.expanduser().resolve()
    if not root.is_dir():
        raise SetupError(f"路径不存在或不是目录: {root}")
    if not options.kinds or set(options.kinds) - set(KINDS):
        raise SetupError("请指定要清理的类别（--backups / --segs / --tmp / --logs，或 --all）。")
    layout = IvdcLayout(root)
    validate_state_directory(layout)
    items: list[CleanupItem] = []
    protected: list[str] = []
    for kind in KINDS:
        if kind not in options.kinds:
            continue
        directory = layout.state_dir / kind
        if not directory.is_dir():
            continue
        units = directory.rglob("*") if kind == "backups" else directory.iterdir()
        for unit in sorted(units):
            if kind == "backups":
                if unit.is_dir():
                    continue
                relative = unit.relative_to(directory)
                if not (root / relative).is_file():
                    protected.append(relative.as_posix())
                    continue
            size = unit.lstat().st_size if unit.is_symlink() else dir_stats(unit)[1]
            items.append(CleanupItem(kind, unit, size))
    if options.dry_run:
        return CleanupResult(tuple(items), tuple(protected))
    removed = released = 0
    failures: list[CleanupFailure] = []
    for item in items:
        try:
            if item.path.is_dir() and not item.path.is_symlink():
                if not remove_tree(item.path):
                    raise OSError("目录删除失败")
            else:
                item.path.unlink()
        except OSError as exc:
            message = f"{item.path}: {exc}"
            logger.warning("未能删除 %s", message)
            failures.append(CleanupFailure(item, message))
            released += max(0, item.size - dir_stats(item.path)[1])
        else:
            removed += 1
            released += item.size
    return CleanupResult(tuple(items), tuple(protected), removed, released, tuple(failures))
