"""文件暂存、提交和恢复；原文件在提交前始终可用，旧备份从不覆盖。"""

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ivdc.fs import IvdcLayout

logger = logging.getLogger(__name__)


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".ivdc-copy-", suffix=".tmp", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        discard_file(temporary)


def discard_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("未能清理 %s: %s", path, exc)


def pending_backups(layout: IvdcLayout) -> dict[str, Path]:
    return {
        path.relative_to(layout.backups_dir).as_posix(): path
        for path in sorted(layout.backups_dir.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and not (path.name.startswith(".ivdc-copy-") and path.name.endswith(".tmp"))
        and not (layout.root / path.relative_to(layout.backups_dir)).exists()
    }


@dataclass
class RecoveryReport:
    restored: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def recover_backups(layout: IvdcLayout) -> RecoveryReport:
    report = RecoveryReport()
    for name, backup in pending_backups(layout).items():
        try:
            atomic_copy(backup, layout.root / name)
        except OSError as exc:
            report.failures.append(f"还原备份 {name} 失败: {exc}")
        else:
            report.restored.append(name)
    return report


@dataclass
class FileTransaction:
    layout: IvdcLayout
    name: str
    keep_backup: bool
    created_backup: bool = False

    @property
    def source(self) -> Path:
        return self.layout.root / self.name

    @property
    def output(self) -> Path:
        return self.layout.tmp(self.name)

    def prepare(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        backup = self.layout.backup(self.name)
        if backup.exists() and not backup.is_file():
            raise IsADirectoryError(f"备份路径不是文件: {backup}")
        if not backup.exists():
            atomic_copy(self.source, backup)
            self.created_backup = True

    def commit(self) -> tuple[int, int]:
        old_bytes = self.source.stat().st_size
        new_bytes = self.output.stat().st_size
        os.replace(self.output, self.source)
        if not self.keep_backup and self.created_backup:
            discard_file(self.layout.backup(self.name))
        return old_bytes, new_bytes

    def rollback(self) -> None:
        # 提交是单次原子替换；失败时源文件仍在原位。
        discard_file(self.output)
