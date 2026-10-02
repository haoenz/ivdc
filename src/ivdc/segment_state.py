"""分片身份、完成凭据与原子发布；只有协调线程写完成记录。"""

import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ivdc.encode import EncodingConfig
from ivdc.errors import MediaError, StorageError
from ivdc.fs import IvdcLayout
from ivdc.optimization_plan import VideoTask
from ivdc.store import atomic_write_text

logger = logging.getLogger(__name__)


def archive_files(path: Path) -> None:
    """保留旧分片及其凭据；不覆盖任何原有残留。"""
    archive = path.parent.with_name(f"{path.parent.name}.retained-{uuid.uuid4().hex}")
    archive.mkdir(parents=True)
    os.replace(path, archive / path.name)
    receipt = path.with_suffix(".complete.json")
    if receipt.exists():
        os.replace(receipt, archive / receipt.name)
    logger.warning("不能复用的已有分片保留在 %s", archive)


def digest_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


@dataclass(frozen=True)
class SegmentContext:
    identity: str
    legacy_indices: frozenset[int] = frozenset()

    def completed(self, path: Path, index: int) -> bool:
        receipt = path.with_suffix(".complete.json")
        try:
            value = json.loads(receipt.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return False
        except ValueError, UnicodeError:
            return False
        return value == self.receipt(path, index)

    def receipt(self, path: Path, index: int) -> dict[str, object]:
        return {
            "version": 1,
            "identity": self.identity,
            "index": index,
            "size": path.stat().st_size,
            "sha256": digest_file(path),
        }

    def publish(self, candidate: Path, target: Path, index: int) -> None:
        """先发布验证过的文件，再保存凭据；中间中断的文件下次不视为已完成。"""
        try:
            payload = self.receipt(candidate, index)
            if candidate != target:
                with candidate.open("rb+") as handle:
                    os.fsync(handle.fileno())
                os.replace(candidate, target)
            atomic_write_text(
                target.with_suffix(".complete.json"), json.dumps(payload, sort_keys=True) + "\n"
            )
        except OSError as exc:
            raise StorageError(f"无法保存分片完成记录 {target}: {exc}") from exc


def prepare_segments(layout: IvdcLayout, config: EncodingConfig, task: VideoTask) -> SegmentContext:
    directory = layout.seg_dir(task.name)
    metadata_path = directory / "resume.json"
    stat = task.path.stat()
    legacy_metadata = {
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "codec": config.codec_name,
        "encoder_args": list(config.encoder_args),
        "segments": [[part.start, part.duration] for part in task.segments],
    }
    metadata = {**legacy_metadata, "version": 2, "hwaccel_args": list(config.hwaccel_args)}
    legacy = not metadata_path.exists()
    if not legacy:
        try:
            previous = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise MediaError(f"无法读取断点元数据 {metadata_path}: {exc}") from exc
        legacy = previous == legacy_metadata
        if previous != metadata and not legacy:
            archive = directory.with_name(f"{directory.name}.resume-{uuid.uuid4().hex}")
            os.replace(directory, archive)
            logger.warning("编码参数或源文件变化，原分片保留在 %s", archive)
    directory.mkdir(parents=True, exist_ok=True)
    legacy_indices = frozenset(
        part.index
        for part in task.segments
        if legacy and layout.seg(task.name, part.index).is_file()
    )
    for partial in directory.glob("*.partial.mp4"):
        archive_files(partial)
    text = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    atomic_write_text(metadata_path, text + "\n")
    return SegmentContext(hashlib.sha256(text.encode("utf-8")).hexdigest(), legacy_indices)
