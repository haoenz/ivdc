"""磁盘布局与处理清单（纯逻辑部分，不碰 I/O）。

所有中间产物集中在视频目录下的一个状态目录里，工作目录只见成片：

    <视频目录>/.ivdc/
        backups/<文件名>            源文件备份
        segs/<文件名>/seg_000.mp4   分片
        tmp/<文件名>                临时输出
        logs/YYYY-MM-DD.log         日志（按天分文件）
        manifest.json               处理记录

这样 ``*.mp4`` 的 glob 永远不会误伤中间产物，清理也只需要删掉一个目录。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

STATE_DIR_NAME = ".ivdc"
CONCAT_LIST_NAME = "concat_list.txt"

STATUS_OK = "ok"
STATUS_FAILED = "failed"

MANIFEST_VERSION = 1

__all__ = [
    "CONCAT_LIST_NAME",
    "MANIFEST_VERSION",
    "STATE_DIR_NAME",
    "STATUS_FAILED",
    "STATUS_OK",
    "FileRecord",
    "IvdcLayout",
    "Manifest",
    "concat_lines",
    "seg_filename",
]


# 命名约定：落盘的东西叫 ``seg``（``segs/`` 目录、``seg_000.mp4``），时间轴上的分片叫
# ``segment``（:class:`~ivdc.plan.Segment`）。同一件事的两个面——标识符用 ``segment``，
# 只有磁盘约定（目录名、文件名）保留 ``seg``。
def seg_filename(index: int) -> str:
    """分片文件名 ``seg_NNN.mp4``；前缀与三位序号是磁盘约定，改动会让旧分片不再被复用。"""
    return f"seg_{index:03d}.mp4"


def concat_lines(count: int) -> list[str]:
    """拼接清单的每一行（相对分片目录）。"""
    return [f"file '{seg_filename(index)}'" for index in range(count)]


@dataclass(frozen=True)
class IvdcLayout:
    """一个视频目录下的状态目录布局。只算路径，不创建任何东西。"""

    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIR_NAME

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def segs_dir(self) -> Path:
        return self.state_dir / "segs"

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / "manifest.json"

    def backup(self, name: str) -> Path:
        """源文件备份的位置。"""
        return self.backups_dir / name

    def tmp(self, name: str) -> Path:
        """临时输出文件的位置。"""
        return self.tmp_dir / name

    def seg_dir(self, name: str) -> Path:
        """某个文件的分片目录。"""
        return self.segs_dir / name

    def seg(self, name: str, index: int) -> Path:
        """某个文件某个分片的完整路径。"""
        return self.seg_dir(name) / seg_filename(index)

    def log(self, day: date) -> Path:
        """某一天的日志文件。"""
        return self.logs_dir / f"{day:%Y-%m-%d}.log"


@dataclass(frozen=True)
class FileRecord:
    """一个文件的处理结果。"""

    name: str
    status: str
    codec: str = ""
    old_bytes: int = 0
    new_bytes: int = 0
    seconds: float = 0.0
    finished_at: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "codec": self.codec,
            "old_bytes": self.old_bytes,
            "new_bytes": self.new_bytes,
            "seconds": round(self.seconds, 3),
            "finished_at": self.finished_at,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, payload: object) -> FileRecord:
        if not isinstance(payload, dict):
            raise ValueError("记录必须是对象")
        name = payload.get("name")
        status = payload.get("status")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).is_absolute()
            or ".." in Path(name).parts
        ):
            raise ValueError("记录名称必须是安全的相对路径")
        if status not in (STATUS_OK, STATUS_FAILED):
            raise ValueError(f"记录状态无效: {status!r}")
        values = {key: payload.get(key, "") for key in ("codec", "finished_at", "message")}
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("记录文本字段必须是字符串")
        old_bytes, new_bytes = payload.get("old_bytes", 0), payload.get("new_bytes", 0)
        if any(type(value) is not int or value < 0 for value in (old_bytes, new_bytes)):
            raise ValueError("记录大小必须是非负整数")
        seconds = payload.get("seconds", 0.0)
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("记录耗时必须是有限非负数")
        return cls(
            name=name,
            status=status,
            old_bytes=old_bytes,
            new_bytes=new_bytes,
            seconds=float(seconds),
            **values,
        )


@dataclass
class Manifest:
    """``manifest.json`` 的内存形态。

    记录结果与体积耗时；可恢复分片另行探测校验。
    """

    entries: dict[str, FileRecord] = field(default_factory=dict)

    def record(self, record: FileRecord) -> None:
        self.entries[record.name] = record

    def get(self, name: str) -> FileRecord | None:
        return self.entries.get(name)

    def is_done(self, name: str) -> bool:
        record = self.entries.get(name)
        return record is not None and record.status == STATUS_OK

    def as_dict(self) -> dict[str, object]:
        return {
            "version": MANIFEST_VERSION,
            "entries": {name: record.as_dict() for name, record in self.entries.items()},
        }

    @classmethod
    def from_dict(cls, payload: object) -> Manifest:
        if not isinstance(payload, dict):
            raise ValueError("manifest 必须是对象")
        version = payload.get("version", MANIFEST_VERSION)
        if type(version) is not int or version != MANIFEST_VERSION:
            raise ValueError("不支持的 manifest 版本")
        raw_entries = payload.get("entries", {})
        if not isinstance(raw_entries, dict):
            raise ValueError("entries 必须是对象")
        entries: dict[str, FileRecord] = {}
        for name, raw_record in raw_entries.items():
            record = FileRecord.from_dict(raw_record)
            if name != record.name:
                raise ValueError(f"记录键与名称不一致: {name!r}")
            entries[name] = record
        return cls(entries=entries)
