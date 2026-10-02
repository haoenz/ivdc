"""编码执行与可恢复分片校验；不调度、不提交成片、不接触终端。"""

import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ivdc.encode import EncodingConfig, build_ffmpeg_args
from ivdc.errors import MediaError
from ivdc.fs import CONCAT_LIST_NAME, IvdcLayout, concat_lines
from ivdc.optimization_plan import VideoTask
from ivdc.parse import is_progress_line, parse_out_time, parse_progress_line
from ivdc.plan import Segment
from ivdc.probe_runtime import Toolchain, probe_media
from ivdc.recovery import discard_file
from ivdc.runner import CommandError, CommandResult, ProcessRunner
from ivdc.store import atomic_write_text, write_lines

logger = logging.getLogger(__name__)
type ProgressKey = tuple[str, int | None]
type ProgressCallback = Callable[[ProgressKey, float], None]


def failure_reason(result: CommandResult) -> str:
    messages = [
        line.strip()
        for line in (*result.lines, *result.stderr.splitlines())
        if line.strip() and not is_progress_line(line)
    ]
    return " ".join(messages[-2:]) or f"ffmpeg 退出码 {result.returncode}"


@dataclass(frozen=True)
class SegmentResult:
    name: str
    index: int
    path: Path
    reused: bool = False
    error: str = ""


class Encoder:
    def __init__(
        self,
        toolchain: Toolchain,
        config: EncodingConfig,
        runner: ProcessRunner,
        layout: IvdcLayout,
        progress: ProgressCallback,
    ) -> None:
        self.toolchain = toolchain
        self.config = config
        self.runner = runner
        self.layout = layout
        self.progress = progress

    def encode(self, task: VideoTask, target: Path, segment: Segment | None = None) -> None:
        key = (task.name, None if segment is None else segment.index)
        duration = task.duration if segment is None else segment.duration
        argv = [
            self.toolchain.ffmpeg,
            "-hide_banner",
            "-nostdin",
            *build_ffmpeg_args(
                self.config,
                str(task.path),
                str(target),
                start=segment.start if segment else None,
                duration=duration if duration > 0 else None,
            ),
        ]
        if segment:
            logger.debug("编码 %s (seg %d)", task.name, segment.index + 1)

        def on_output(line: str) -> None:
            parsed = parse_progress_line(line)
            if parsed is not None and parsed[0] == "out_time":
                seconds = parse_out_time(parsed[1])
                if seconds is not None:
                    self.progress(key, min(seconds, duration) if duration > 0 else seconds)

        result = self.runner.stream(argv, on_output=on_output)
        if not result.ok:
            raise MediaError(failure_reason(result))
        if not target.is_file() or target.stat().st_size == 0:
            raise MediaError("ffmpeg 未生成目标文件")

    def prepare_segments(self, task: VideoTask) -> None:
        directory = self.layout.seg_dir(task.name)
        metadata_path = directory / "resume.json"
        stat = task.path.stat()
        metadata = {
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "codec": self.config.codec_name,
            "encoder_args": list(self.config.encoder_args),
            "segments": [[part.start, part.duration] for part in task.segments],
        }
        if metadata_path.exists():
            try:
                previous = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            except (ValueError, UnicodeError) as exc:
                raise MediaError(f"无法读取断点元数据 {metadata_path}: {exc}") from exc
            if previous != metadata:
                archive = directory.with_name(f"{directory.name}.resume-{uuid.uuid4().hex}")
                os.replace(directory, archive)
                logger.warning("编码参数或源文件变化，原分片保留在 %s", archive)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False) + "\n")

    def segment(self, task: VideoTask, part: Segment) -> SegmentResult:
        target = self.layout.seg(task.name, part.index)
        self.runner.check_cancelled()
        writing = False
        try:
            reused = False
            if target.is_file():
                try:
                    info = probe_media(self.toolchain, target, self.runner)
                    reused = (
                        info.codec_name == self.config.codec_name
                        and abs(info.duration - part.duration) <= 0.5
                    )
                except MediaError:
                    pass  # 损坏的分片可以重压，启动失败则不能当作损坏数据。
            if not reused:
                if target.exists():
                    archive = target.parent.with_name(
                        f"{target.parent.name}.retained-{uuid.uuid4().hex}"
                    )
                    archive.mkdir(parents=True)
                    os.replace(target, archive / target.name)
                    logger.warning("不能复用的已有分片保留在 %s", archive)
                writing = True
                self.encode(task, target, part)
            self.progress((task.name, part.index), part.duration)
            return SegmentResult(task.name, part.index, target, reused)
        except (MediaError, CommandError, OSError) as exc:
            if writing:
                discard_file(target)
            return SegmentResult(task.name, part.index, target, error=str(exc))

    def concatenate(self, task: VideoTask, results: list[SegmentResult], target: Path) -> None:
        if len(results) != len(task.segments) or any(result.error for result in results):
            errors = "; ".join(result.error for result in results if result.error)
            raise MediaError(f"部分分片缺失或失败: {errors}")
        list_path = self.layout.seg_dir(task.name) / CONCAT_LIST_NAME
        write_lines(list_path, concat_lines(len(task.segments)))
        result = self.runner.capture(
            [
                self.toolchain.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_path,
                "-c",
                "copy",
                "-f",
                "mp4",
                target,
                "-y",
            ]
        )
        if not result.ok or not target.is_file():
            raise MediaError(failure_reason(result))
