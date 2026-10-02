"""编码执行与可恢复分片校验；不调度、不提交成片、不接触终端。"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ivdc.encode import EncodingConfig, build_ffmpeg_args, preserve_stream_args
from ivdc.errors import MediaError
from ivdc.fs import CONCAT_LIST_NAME, IvdcLayout, concat_lines
from ivdc.media_validation import validate_media
from ivdc.optimization_plan import VideoTask
from ivdc.parse import is_progress_line, parse_out_time, parse_progress_line
from ivdc.plan import Segment
from ivdc.probe_runtime import Toolchain, probe_media
from ivdc.recovery import discard_file
from ivdc.runner import CommandError, CommandResult, ProcessRunner
from ivdc.segment_state import SegmentContext, archive_files
from ivdc.store import write_lines

logger = logging.getLogger(__name__)
type ProgressKey = tuple[str, int | None]
type ProgressCallback = Callable[[ProgressKey, float], None]


def failure_reason(result: CommandResult) -> str:
    messages = [
        line.strip()
        for line in (*result.lines, *result.stderr.splitlines())
        if line.strip() and not is_progress_line(line)
    ]
    # stream 已限制诊断缓冲大小；再次只截末尾会丢掉容器不支持某条流的根因。
    return " ".join(messages) or f"ffmpeg 退出码 {result.returncode}"


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
        reference = task.media or probe_media(self.toolchain, task.path, self.runner)
        argv = [
            self.toolchain.ffmpeg,
            "-hide_banner",
            "-nostdin",
            *build_ffmpeg_args(
                self.config,
                str(task.path),
                str(target),
                start=segment.start if segment else None,
                duration=duration if segment is not None else None,
                media=reference,
                video_only=segment is not None,
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
        if segment is None:
            self.validate(task, target, reference.video_duration)

    def validate(
        self,
        task: VideoTask,
        path: Path,
        duration: float,
        *,
        decode: bool = True,
        start: float = 0.0,
        video_only: bool = False,
    ) -> None:
        reference = task.media or probe_media(self.toolchain, task.path, self.runner)
        validate_media(
            self.toolchain,
            self.runner,
            path,
            reference,
            self.config.codec_name,
            duration,
            decode=decode,
            start=start,
            video_only=video_only,
        )

    def segment(self, task: VideoTask, part: Segment, context: SegmentContext) -> SegmentResult:
        target = self.layout.seg(task.name, part.index)
        self.runner.check_cancelled()
        temporary = target.with_name(f"{target.stem}.{uuid.uuid4().hex}.partial.mp4")
        ready = False
        try:
            if target.is_file():
                completed = context.completed(target, part.index)
                if completed or part.index in context.legacy_indices:
                    try:
                        self.validate(
                            task,
                            target,
                            part.duration,
                            decode=not completed,
                            start=part.start,
                            video_only=True,
                        )
                    except MediaError:
                        pass  # 不能确认完整的旧分片先归档；命令启动失败向外传播。
                    else:
                        self.progress((task.name, part.index), part.duration)
                        return SegmentResult(task.name, part.index, target, reused=True)
            if target.exists():
                archive_files(target)
            self.encode(task, temporary, part)
            self.validate(task, temporary, part.duration, start=part.start, video_only=True)
            self.runner.check_cancelled()
            self.progress((task.name, part.index), part.duration)
            ready = True
            # 临时文件由协调线程发布；线程异常或取消不会产生完成凭据。
            return SegmentResult(task.name, part.index, temporary)
        except (MediaError, CommandError, OSError) as exc:
            return SegmentResult(task.name, part.index, target, error=str(exc))
        finally:
            if not ready:
                discard_file(temporary)

    def concatenate(self, task: VideoTask, results: list[SegmentResult], target: Path) -> None:
        if (
            {result.index for result in results} != {part.index for part in task.segments}
            or len(results) != len(task.segments)
            or any(result.error for result in results)
        ):
            errors = "; ".join(result.error for result in results if result.error)
            raise MediaError(f"部分分片缺失或失败: {errors}")
        list_path = self.layout.seg_dir(task.name) / CONCAT_LIST_NAME
        write_lines(list_path, concat_lines(len(task.segments)))
        reference = task.media or probe_media(self.toolchain, task.path, self.runner)
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
                "-i",
                task.path,
                *preserve_stream_args(reference, original_input=1, video_input=0),
                "-f",
                "mp4",
                target,
                "-y",
            ]
        )
        if not result.ok or not target.is_file():
            raise MediaError(failure_reason(result))
        self.validate(task, target, task.duration)
