"""协调线程中的结果持久化与运行日志资源。"""

import logging
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Self

from ivdc.errors import StorageError
from ivdc.formatting import format_fail_line, format_log_line
from ivdc.fs import STATUS_OK, FileRecord, IvdcLayout, Manifest
from ivdc.store import save_manifest


class StrictFileHandler(logging.FileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        raise StorageError(f"日志写入失败: {self.baseFilename}")


class ResultRecorder:
    def __init__(self, layout: IvdcLayout, manifest: Manifest, log_path: Path, debug: bool) -> None:
        self.layout, self.manifest, self.log_path = layout, manifest, log_path
        self.logger = logging.getLogger("ivdc")
        self.result_logger = logging.Logger("ivdc.results", logging.INFO)
        self.debug = debug
        self.handler: StrictFileHandler | None = None
        self.old_level = self.logger.level

    def __enter__(self) -> Self:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = StrictFileHandler(self.log_path, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)
        self.result_logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc is not None and exc_type is not None:
                try:
                    # 在释放文件 handler 之前记录原异常；终端摘要仍由 CLI 决定。
                    self.result_logger.error("运行异常退出", exc_info=(exc_type, exc, traceback))
                except Exception as log_error:
                    # 日志自身故障不能替换已经发生的业务异常。
                    exc.add_note(f"原异常未能写入处理日志: {log_error}")
        finally:
            try:
                if self.handler is not None:
                    self.logger.removeHandler(self.handler)
                    self.result_logger.removeHandler(self.handler)
                    try:
                        self.handler.close()
                    except Exception as close_error:
                        if exc is None:
                            raise
                        exc.add_note(f"关闭处理日志失败: {close_error}")
            finally:
                self.logger.setLevel(self.old_level)

    def record(self, record: FileRecord) -> None:
        self.manifest.record(record)
        try:
            save_manifest(self.layout, self.manifest)
        except OSError as exc:
            raise StorageError(f"无法保存 {self.layout.manifest_path}: {exc}") from exc
        if record.status == STATUS_OK:
            self.result_logger.info(
                format_log_line(
                    date.today(),
                    record.name,
                    record.old_bytes,
                    record.new_bytes,
                    record.seconds / 60,
                )
            )
        else:
            self.result_logger.warning(format_fail_line(date.today(), record.name, record.message))
